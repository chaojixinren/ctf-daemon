#!/usr/bin/env python3
"""
CTF Daemon v2 — Background orchestrator with timeout/retry/skip mechanisms.
Inspired by LingXi's concurrent task model.
No single task should block all others.

Key improvements over v1:
  1. TASK_TIMEOUT: If agent doesn't solve within N seconds, abandon and move on.
  2. RETRY_COOLDOWN: Failed challenges wait before retry (escalating backoff).
  3. MAX_RETRIES: Challenges that fail too many times are permanently skipped.
  4. INFRA_FAILURE_DETECTION: Network/model issues don't consume retry count.
  5. Progress tracking: Even a partial-timeout can save progress.
"""

import os, sys, json, time, logging
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from gzctf_client import GZCTFClient, load_config
from challenge_engine import ChallengeAnalyzer, extract_flag, build_solving_prompt
from solver import submit_and_record, load_state, save_state

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s [daemon] %(message)s")
logger = logging.getLogger("ctf_daemon")

TASK_FILE = Path("/tmp/ctf_task.json")
FLAG_FILE = Path("/tmp/ctf_flag.txt")
ANSWER_FILE = Path("/tmp/ctf_answer.json")
DONE_FILE = Path("/tmp/ctf_done")

# ── Configurable knobs (from config.env or defaults) ───────────────

def _cfg_seconds(env_var: str, default: int) -> int:
    val = os.environ.get(env_var, "").strip()
    if val:
        try:
            return max(1, int(val))
        except ValueError:
            pass
    return default

TASK_TIMEOUT = _cfg_seconds("CTF_TASK_TIMEOUT_SECONDS", 600)        # 10 min per task
MAX_RETRIES = _cfg_seconds("CTF_MAX_RETRIES_PER_CHALLENGE", 4)       # 4 attempts max
BASE_COOLDOWN = _cfg_seconds("CTF_BASE_RETRY_COOLDOWN", 60)          # 1 min base cooldown
MAX_COOLDOWN = _cfg_seconds("CTF_MAX_RETRY_COOLDOWN", 1800)          # 30 min max cooldown

# ── Infra failure keywords (don't consume retry count) ─────────────

INFRA_FAILURE_KEYWORDS = (
    "connection reset", "connection refused", "connection aborted",
    "timeout reading", "network error", "bad gateway", "gateway timeout",
    "http 502", "http 503", "http 504", "http 429", "rate limit",
    "temporarily unavailable", "server disconnected",
    "token limit", "context length", "max tokens",
    "llm 调用失败", "api 连接失败", "mcp 工具调用失败",
    "jsonrpc message", "failed to parse jsonrpc",
)


def _is_infra_failure(error_text: str) -> bool:
    """Check if error looks like infrastructure failure."""
    text = str(error_text or "").lower()
    if not text:
        return False
    return any(kw in text for kw in INFRA_FAILURE_KEYWORDS)


def _compute_cooldown(retry_count: int) -> int:
    """Escalating backoff: BASE * 2^retries, capped at MAX."""
    return min(MAX_COOLDOWN, BASE_COOLDOWN * (2 ** max(0, retry_count - 1)))


def write_task(challenge: dict, analysis: dict, attachments: list):
    """Write the current challenge for the agent to solve."""
    task = {
        "challenge_id": challenge.get("id"),
        "title": challenge.get("title"),
        "category": challenge.get("_category"),
        "type": analysis["type"],
        "strategy": analysis["strategy"],
        "score": analysis["score"],
        "description": challenge.get("content", ""),
        "hints": analysis["hints"],
        "tools": analysis["recommended_tools"][:5],
        "has_container": analysis["has_container"],
        "container_entry": analysis.get("container_entry"),
        "attachments": attachments,
        "context_file": str(analysis.get("attachment_url") or ""),
        "assigned_at": time.time(),
    }
    TASK_FILE.write_text(json.dumps(task, indent=2, ensure_ascii=False))
    logger.info(f"Task written: [{challenge.get('_category')}] {challenge.get('title')} (ID:{challenge.get('id')})")


def read_flag() -> str | None:
    """Check if agent wrote a flag."""
    if FLAG_FILE.exists():
        flag = FLAG_FILE.read_text().strip()
        if flag:
            FLAG_FILE.unlink()
            return flag
    return None


def read_answer() -> dict | None:
    """Check if agent wrote a structured answer (multi-flag)."""
    if ANSWER_FILE.exists():
        try:
            data = json.loads(ANSWER_FILE.read_text())
            ANSWER_FILE.unlink()
            return data
        except Exception:
            pass
    return None


def _mark_challenge_failed(state: dict, ch_id: int, reason: str, is_infra: bool):
    """Mark a challenge as failed with cooldown."""
    key = str(ch_id)
    retries = state.setdefault("retry_counts", {}).get(key, 0)

    if not is_infra:
        retries += 1
        state["retry_counts"][key] = retries
        logger.info(f"Challenge {ch_id} failed ({retries}/{MAX_RETRIES}): {reason}")
    else:
        logger.info(f"Challenge {ch_id} infra failure (retry count unchanged): {reason}")

    if retries >= MAX_RETRIES and not is_infra:
        state.setdefault("permanently_failed", {})[key] = {
            "reason": reason,
            "retries": retries,
            "at": time.time(),
        }
        logger.warning(f"Challenge {ch_id} PERMANENTLY FAILED after {retries} retries")

    cooldown = _compute_cooldown(retries)
    state.setdefault("cooldown_until", {})[key] = time.time() + cooldown


def main():
    config = load_config(str(SCRIPT_DIR / "config.env"))
    base_url = config.get("GZCTF_BASE_URL", "")
    username = config.get("GZCTF_USERNAME", "")
    password = config.get("GZCTF_PASSWORD", "")
    game_id = int(config.get("GZCTF_GAME_ID", "0"))
    attachment_dir = config.get("ATTACHMENT_DIR", "/tmp/ctf_attachments")

    if not base_url or not username or not password:
        logger.error("Missing credentials in config.env")
        sys.exit(1)

    client = GZCTFClient(base_url, username, password)
    if not client.login():
        logger.error("Login failed")
        sys.exit(1)

    if game_id == 0:
        games = client.list_games()
        if not games:
            logger.error("No games available")
            sys.exit(1)
        game_id = games[0]["id"]
        logger.info(f"Auto-selected game: {games[0].get('title')} (ID:{game_id})")

    client.get_game_detail(game_id)

    state = load_state()
    solved_ids = set(state["solved"].keys())
    permanently_failed = set(state.get("permanently_failed", {}).keys())

    # ── Step 1: Check if agent finished last task ──────────────────

    flag = read_flag()
    answer = read_answer()
    current_ch_id = state.get("current")

    if current_ch_id:
        key = str(current_ch_id)

        # Check for timeout
        task_assigned_at = state.get("task_assigned_at", 0)
        elapsed = time.time() - task_assigned_at if task_assigned_at else 0

        if flag:
            logger.info(f"Agent submitted flag for challenge {current_ch_id}: {flag}")
            submit_and_record(client, current_ch_id, flag, state)
            solved_ids.add(str(current_ch_id))
            state["current"] = None
            state.pop("task_assigned_at", None)
            save_state(state)
            if TASK_FILE.exists():
                TASK_FILE.unlink()

        elif answer:
            logger.info(f"Agent submitted answer for challenge {current_ch_id}: {json.dumps(answer, ensure_ascii=False)[:200]}")
            if answer.get("success"):
                for f in answer.get("flags", []):
                    submit_and_record(client, current_ch_id, f, state)
            solved_ids.add(str(current_ch_id))
            state["current"] = None
            state.pop("task_assigned_at", None)
            save_state(state)
            if TASK_FILE.exists():
                TASK_FILE.unlink()

        elif elapsed > TASK_TIMEOUT:
            logger.warning(f"Challenge {current_ch_id} TIMEOUT after {elapsed:.0f}s (limit: {TASK_TIMEOUT}s)")
            # Check for infra failure from task file
            is_infra = False
            if TASK_FILE.exists():
                try:
                    task = json.loads(TASK_FILE.read_text())
                    # Check if the description has infra failure indicators
                    is_infra = _is_infra_failure(task.get("description", ""))
                except Exception:
                    pass
            _mark_challenge_failed(state, current_ch_id, f"timeout after {elapsed:.0f}s", is_infra)
            state["current"] = None
            state.pop("task_assigned_at", None)
            save_state(state)
            if TASK_FILE.exists():
                TASK_FILE.unlink()
            logger.info(f"Challenge {current_ch_id} moved to cooldown, will try next")

        elif TASK_FILE.exists():
            # Task still pending within timeout — OK, let it continue
            try:
                task = json.loads(TASK_FILE.read_text())
                logger.info(f"Task in progress ({elapsed:.0f}s / {TASK_TIMEOUT}s): [{task.get('category')}] {task.get('title')}")
            except Exception:
                logger.info(f"Task in progress ({elapsed:.0f}s / {TASK_TIMEOUT}s): challenge {current_ch_id}")
            print(f"PENDING:{current_ch_id}")
            return

    # ── Step 2: Find next challenge to work on ─────────────────────

    all_challenges = client.get_all_challenge_details()
    now_ts = time.time()

    # Build list of eligible challenges
    eligible = []
    for ch in all_challenges:
        ch_id = str(ch.get("id"))
        if ch_id in solved_ids:
            continue
        if ch_id in permanently_failed:
            continue
        # Cooldown check
        cooldown_until = state.get("cooldown_until", {}).get(ch_id, 0)
        if cooldown_until > now_ts:
            remaining = cooldown_until - now_ts
            logger.debug(f"Challenge {ch_id} in cooldown ({remaining:.0f}s remaining)")
            continue
        eligible.append(ch)

    if not eligible:
        if not solved_ids:
            logger.warning("No eligible challenges found (all in cooldown or permanently failed?)")
        DONE_FILE.write_text(
            f"ALL DONE! Solved: {len(solved_ids)}/{len(all_challenges)} "
            f"| PermanentlyFailed: {len(permanently_failed)}"
        )
        DONE_FILE.chmod(0o644)
        logger.info("ALL CHALLENGES PROCESSED!")
        print("DONE")
        return

    # Sort: lower score first, fewer retries first
    eligible.sort(key=lambda ch: (
        state.get("retry_counts", {}).get(str(ch.get("id")), 0),  # fewer retries first
        ch.get("score", 0),  # lower score first
    ))

    challenge = eligible[0]
    ch_id = challenge["id"]

    # Retry count for logging
    retries = state.get("retry_counts", {}).get(str(ch_id), 0)
    if retries > 0:
        logger.info(f"Retrying challenge {ch_id} (attempt {retries + 1}/{MAX_RETRIES})")

    # ── Step 3: Prepare the challenge ──────────────────────────────

    attachments = client.download_challenge_attachments(challenge, attachment_dir)

    if challenge.get("type") == "DynamicContainer":
        logger.info(f"Creating container for challenge {ch_id}...")
        client.create_container(ch_id)
        ready = client.wait_for_container_ready(ch_id, timeout=60)
        if ready:
            logger.info(f"Container ready for challenge {ch_id}")
            challenge = client.get_challenge_detail(ch_id)
        else:
            logger.warning(f"Container not ready for challenge {ch_id}, proceeding anyway")

    analysis = ChallengeAnalyzer.analyze_challenge(challenge)

    # Check for easy flags in description
    if analysis["flags_found"]:
        for f in analysis["flags_found"]:
            logger.info(f"Flag in description, auto-submitting: {f}")
            submit_and_record(client, ch_id, f, state)
            solved_ids.add(str(ch_id))
            save_state(state)
            return

    for path in attachments:
        from challenge_engine import basic_file_analysis
        fa = basic_file_analysis(path)
        if fa.get("flags_in_strings"):
            for f in fa["flags_in_strings"]:
                logger.info(f"Flag in attachment {path}, auto-submitting: {f}")
                submit_and_record(client, ch_id, f, state)
                solved_ids.add(str(ch_id))
                save_state(state)
                return

    # ── Step 4: Dispatch ───────────────────────────────────────────

    state["current"] = ch_id
    state["task_assigned_at"] = time.time()
    save_state(state)
    write_task(challenge, analysis, attachments)

    print(f"TASK:{ch_id}:{challenge.get('_category')}:{challenge.get('title')}")


if __name__ == "__main__":
    main()
