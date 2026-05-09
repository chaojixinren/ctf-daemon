#!/usr/bin/env python3
"""
CTF Daemon v2.1 — Non-blocking orchestrator with abort signaling and attempt history.
Inspired by LingXi's concurrent task model.

Key features:
  1. TASK_TIMEOUT:  Agent stuck → auto-abort → next challenge.
  2. ABORT SIGNAL:  Writes /tmp/ctf_abort so agent knows to stop immediately.
  3. RETRY_COOLDOWN: Escalating backoff per challenge (60s→120s→240s→480s→1800s).
  4. MAX_RETRIES:   After N failures, permanently skip.
  5. INFRA_FAILURE: Network/LLM errors don't consume retry count.
  6. ATTEMPT_HISTORY: Tracks what was tried, injected into task on retry.
  7. MACHINE-READABLE OUTPUT: TASK: / PENDING: / ABORT: / DONE for agent loop.
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

TASK_FILE   = Path("/tmp/ctf_task.json")
FLAG_FILE   = Path("/tmp/ctf_flag.txt")
ANSWER_FILE = Path("/tmp/ctf_answer.json")
DONE_FILE   = Path("/tmp/ctf_done")
ABORT_FILE  = Path("/tmp/ctf_abort")       # v2.1: signals agent to stop

# ── Configurable knobs ─────────────────────────────────────────────

def _cfg_seconds(env_var: str, default: int) -> int:
    val = os.environ.get(env_var, "").strip()
    try:
        return max(1, int(val)) if val else default
    except ValueError:
        return default

TASK_TIMEOUT  = _cfg_seconds("CTF_TASK_TIMEOUT_SECONDS", 600)
MAX_RETRIES   = _cfg_seconds("CTF_MAX_RETRIES_PER_CHALLENGE", 4)
BASE_COOLDOWN = _cfg_seconds("CTF_BASE_RETRY_COOLDOWN", 60)
MAX_COOLDOWN  = _cfg_seconds("CTF_MAX_RETRY_COOLDOWN", 1800)

# ── Infra failure keywords ─────────────────────────────────────────

INFRA_FAILURE_KEYWORDS = (
    "connection reset", "connection refused", "connection aborted",
    "timeout reading", "network error", "bad gateway", "gateway timeout",
    "http 502", "http 503", "http 504", "http 429", "rate limit",
    "temporarily unavailable", "server disconnected",
    "token limit", "context length", "max tokens",
    "llm 调用失败", "api 连接失败", "mcp 工具调用失败",
    "jsonrpc message", "failed to parse jsonrpc",
)

def _is_infra_failure(text: str) -> bool:
    text = str(text or "").lower()
    return any(kw in text for kw in INFRA_FAILURE_KEYWORDS) if text else False

def _compute_cooldown(retry_count: int) -> int:
    return min(MAX_COOLDOWN, BASE_COOLDOWN * (2 ** max(0, retry_count - 1)))

# ── Abort signaling ────────────────────────────────────────────────

def signal_abort(challenge_id: int, reason: str = "timeout"):
    """Write /tmp/ctf_abort so the agent loop knows to stop."""
    ABORT_FILE.write_text(json.dumps({
        "challenge_id": challenge_id,
        "reason": reason,
        "at": time.time(),
    }))
    logger.info(f"ABORT signal sent for challenge {challenge_id}: {reason}")

def clear_abort():
    if ABORT_FILE.exists():
        ABORT_FILE.unlink()

def check_abort() -> dict | None:
    """Check if abort was signaled. Returns None if no abort pending."""
    if ABORT_FILE.exists():
        try:
            return json.loads(ABORT_FILE.read_text())
        except Exception:
            pass
    return None

# ── Attempt history ────────────────────────────────────────────────

def record_attempt(state: dict, challenge_id: int, summary: str = "",
                   tools_used: list = None, error: str = ""):
    """Save what was tried for this challenge attempt."""
    key = str(challenge_id)
    history = state.setdefault("attempt_history", {}).setdefault(key, [])
    entry = {
        "attempt": len(history) + 1,
        "at": time.time(),
        "summary": summary[:500] if summary else "",
        "tools_used": (tools_used or [])[:10],
        "error": error[:300] if error else "",
    }
    history.append(entry)
    # Keep only last 10 attempts
    if len(history) > 10:
        state["attempt_history"][key] = history[-10:]

def get_attempt_history(state: dict, challenge_id: int) -> list:
    """Get previous attempt history for retry context."""
    key = str(challenge_id)
    return state.get("attempt_history", {}).get(key, [])

def format_history_for_agent(history: list) -> str:
    """Format attempt history as human-readable hints for the agent."""
    if not history:
        return ""
    lines = ["", "## Previous Attempts (learn from these)", ""]
    for entry in history:
        lines.append(
            f"- Attempt {entry['attempt']}: {entry['summary']}"
        )
        if entry.get("tools_used"):
            lines.append(f"  Tools tried: {', '.join(entry['tools_used'][:5])}")
        if entry.get("error"):
            lines.append(f"  Error: {entry['error'][:120]}")
    lines.append("")
    return "\n".join(lines)

# ── Task I/O ───────────────────────────────────────────────────────

def write_task(challenge: dict, analysis: dict, attachments: list,
               state: dict = None):
    """Write task file with retry context and attempt history."""
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
        # v2.1: retry context
        "retry_number": state.get("retry_counts", {}).get(str(challenge.get("id")), 0) if state else 0,
        "attempt_history": format_history_for_agent(
            get_attempt_history(state, challenge.get("id", 0))
        ) if state else "",
    }
    TASK_FILE.write_text(json.dumps(task, indent=2, ensure_ascii=False))
    logger.info(f"Task written: [{challenge.get('_category')}] {challenge.get('title')} "
                f"(ID:{challenge.get('id')}, retry #{task['retry_number']})")

def read_flag() -> str | None:
    if FLAG_FILE.exists():
        flag = FLAG_FILE.read_text().strip()
        if flag:
            FLAG_FILE.unlink()
            return flag
    return None

def read_answer() -> dict | None:
    if ANSWER_FILE.exists():
        try:
            data = json.loads(ANSWER_FILE.read_text())
            ANSWER_FILE.unlink()
            return data
        except Exception:
            pass
    return None

# ── Failure management ─────────────────────────────────────────────

def _mark_challenge_failed(state: dict, ch_id: int, reason: str, is_infra: bool):
    key = str(ch_id)
    retries = state.setdefault("retry_counts", {}).get(key, 0)

    if not is_infra:
        retries += 1
        state["retry_counts"][key] = retries
        logger.info(f"Challenge {ch_id} failed ({retries}/{MAX_RETRIES}): {reason}")
    else:
        logger.info(f"Challenge {ch_id} infra failure (retry unchanged): {reason}")

    if retries >= MAX_RETRIES and not is_infra:
        state.setdefault("permanently_failed", {})[key] = {
            "reason": reason, "retries": retries, "at": time.time(),
        }
        logger.warning(f"Challenge {ch_id} PERMANENTLY FAILED after {retries} retries")

    cooldown = _compute_cooldown(retries)
    state.setdefault("cooldown_until", {})[key] = time.time() + cooldown


def _mark_solved(state: dict, ch_id: int, flag: str):
    """Clean up all failure state for a solved challenge."""
    key = str(ch_id)
    state["solved"][key] = flag
    state.get("retry_counts", {}).pop(key, None)
    state.get("cooldown_until", {}).pop(key, None)
    state.get("permanently_failed", {}).pop(key, None)
    state.get("attempt_history", {}).pop(key, None)
    state["current"] = None
    state.pop("task_assigned_at", None)

# ── Main loop ──────────────────────────────────────────────────────

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
    perm_failed = set(state.get("permanently_failed", {}).keys())

    # ── Step 1: Process completed/aborted tasks ─────────────────────

    flag = read_flag()
    answer = read_answer()
    current_ch_id = state.get("current")

    if current_ch_id:
        task_assigned_at = state.get("task_assigned_at", 0)
        elapsed = time.time() - task_assigned_at if task_assigned_at else 0

        if flag:
            logger.info(f"Flag received: challenge {current_ch_id}: {flag}")
            submit_and_record(client, current_ch_id, flag, state)
            _mark_solved(state, current_ch_id, flag)
            clear_abort()
            if TASK_FILE.exists():
                TASK_FILE.unlink()
            save_state(state)

        elif answer:
            logger.info(f"Answer received: {json.dumps(answer, ensure_ascii=False)[:200]}")
            if answer.get("success"):
                for f in answer.get("flags", []):
                    submit_and_record(client, current_ch_id, f, state)
            _mark_solved(state, current_ch_id, answer.get("flags", [""])[0])
            clear_abort()
            if TASK_FILE.exists():
                TASK_FILE.unlink()
            save_state(state)

        elif elapsed > TASK_TIMEOUT:
            # ── TIMEOUT: abort signal + fail + move on ─────────────
            logger.warning(f"TIMEOUT: challenge {current_ch_id} after {elapsed:.0f}s "
                           f"(limit: {TASK_TIMEOUT}s)")

            # Check if task file hints at infra failure
            is_infra = False
            if TASK_FILE.exists():
                try:
                    task = json.loads(TASK_FILE.read_text())
                    is_infra = _is_infra_failure(task.get("description", ""))
                except Exception:
                    pass

            # Record attempt with timeout info
            record_attempt(state, current_ch_id,
                          summary=f"Timeout after {elapsed:.0f}s",
                          error=f"Exceeded TASK_TIMEOUT ({TASK_TIMEOUT}s)")

            _mark_challenge_failed(state, current_ch_id,
                                   f"timeout after {elapsed:.0f}s", is_infra)

            # Send abort signal so agent stops working on this
            signal_abort(current_ch_id, f"timeout ({elapsed:.0f}s)")

            state["current"] = None
            state.pop("task_assigned_at", None)
            save_state(state)
            if TASK_FILE.exists():
                TASK_FILE.unlink()
            print(f"ABORT:{current_ch_id}:timeout:{elapsed:.0f}s")
            logger.info(f"Challenge {current_ch_id} → cooldown, rotating to next")

        elif TASK_FILE.exists():
            # ── IN PROGRESS: let agent continue ────────────────────
            try:
                task = json.loads(TASK_FILE.read_text())
                logger.info(f"In progress ({elapsed:.0f}s/{TASK_TIMEOUT}s): "
                            f"[{task.get('category')}] {task.get('title')}")
            except Exception:
                logger.info(f"In progress ({elapsed:.0f}s/{TASK_TIMEOUT}s): "
                            f"challenge {current_ch_id}")
            print(f"PENDING:{current_ch_id}:{elapsed:.0f}s")
            return

    else:
        # No current challenge — clean up any stale files
        clear_abort()
        if TASK_FILE.exists():
            TASK_FILE.unlink()
            logger.info("Cleaned up stale task file (no current challenge)")

    # ── Step 2: Pick next challenge ─────────────────────────────────

    all_challenges = client.get_all_challenge_details()
    now_ts = time.time()

    eligible = []
    skipped_cooldown = 0
    for ch in all_challenges:
        ch_id = str(ch.get("id"))
        if ch_id in solved_ids:
            continue
        if ch_id in perm_failed:
            continue
        cooldown_until = state.get("cooldown_until", {}).get(ch_id, 0)
        if cooldown_until > now_ts:
            skipped_cooldown += 1
            continue
        eligible.append(ch)

    if not eligible:
        status_msg = (f"ALL DONE! Solved: {len(solved_ids)}/{len(all_challenges)}"
                      f" | PermFailed: {len(perm_failed)}"
                      f" | Cooldown: {skipped_cooldown}")
        DONE_FILE.write_text(status_msg)
        DONE_FILE.chmod(0o644)
        logger.info(status_msg)
        print("DONE")
        return

    # Sort: fewer retries first, lower score first
    eligible.sort(key=lambda ch: (
        state.get("retry_counts", {}).get(str(ch.get("id")), 0),
        ch.get("score", 0),
    ))

    challenge = eligible[0]
    ch_id = challenge["id"]
    ch_key = str(ch_id)

    retries = state.get("retry_counts", {}).get(ch_key, 0)
    if retries > 0:
        logger.info(f"Retry #{retries + 1}/{MAX_RETRIES} for challenge {ch_id}: "
                    f"{challenge.get('title')}")
        # Show cooldown wait time
        wait_time = max(0, int(now_ts - state.get("cooldown_until", {}).get(ch_key, now_ts)))
        if wait_time > 0:
            logger.info(f"  (waited {wait_time}s cooldown)")

    # ── Step 3: Prepare ─────────────────────────────────────────────

    attachments = client.download_challenge_attachments(challenge, attachment_dir)

    if challenge.get("type") == "DynamicContainer":
        logger.info(f"Creating container for challenge {ch_id}...")
        client.create_container(ch_id)
        if client.wait_for_container_ready(ch_id, timeout=60):
            logger.info(f"Container ready: challenge {ch_id}")
            challenge = client.get_challenge_detail(ch_id)
        else:
            logger.warning(f"Container not ready: challenge {ch_id}, proceeding anyway")

    analysis = ChallengeAnalyzer.analyze_challenge(challenge)

    # Quick flag checks
    if analysis["flags_found"]:
        for f in analysis["flags_found"]:
            logger.info(f"Flag in description, auto-submit: {f}")
            submit_and_record(client, ch_id, f, state)
            _mark_solved(state, ch_id, f)
            save_state(state)
            return

    for path in attachments:
        from challenge_engine import basic_file_analysis
        fa = basic_file_analysis(path)
        if fa.get("flags_in_strings"):
            for f in fa["flags_in_strings"]:
                logger.info(f"Flag in attachment {path}: {f}")
                submit_and_record(client, ch_id, f, state)
                _mark_solved(state, ch_id, f)
                save_state(state)
                return

    # ── Step 4: Dispatch ────────────────────────────────────────────

    state["current"] = ch_id
    state["task_assigned_at"] = time.time()
    # Record that we're starting a new attempt
    record_attempt(state, ch_id, summary=f"Starting attempt")
    save_state(state)

    write_task(challenge, analysis, attachments, state=state)

    # Machine-readable output for agent loop
    print(f"TASK:{ch_id}:{challenge.get('_category')}:{challenge.get('title')}")


if __name__ == "__main__":
    main()
