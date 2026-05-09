#!/usr/bin/env python3
"""
CTF Daemon v3.4 — Multi-slot concurrent orchestrator with LLM-driven selection.
             精灵学会了分身术 + 搬家术，每道题拥有独立工作目录。

Per-challenge isolated workdirs:
  /tmp/ctf_{sanitized_title}/
    └── attachments, analysis files, exploit scripts — all scoped to one challenge

Slot files (unchanged):
  /tmp/ctf_tasks/
    slot_0.json   → challenge for slot 0 (includes "workdir" field)
    slot_1.json   → challenge for slot 1
    slot_2.json   → challenge for slot 2
    flag_0.txt    → flag found in slot 0
    flag_1.txt    → flag found in slot 1
    flag_2.txt    → flag found in slot 2
    abort_0       → abort signal for slot 0
    abort_1       → abort signal for slot 1
    abort_2       → abort signal for slot 2

Backward compatible: CTF_CONCURRENT_SLOTS=1 behaves identically to v2.1.
"""

import os, sys, json, time, logging
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from gzctf_client import GZCTFClient, load_config
from challenge_engine import ChallengeAnalyzer, extract_flag, build_solving_prompt, basic_file_analysis
from solver import submit_and_record
from state import load_state, save_state, TASKS_DIR, WORKDIR_BASE

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s [daemon] %(message)s")
logger = logging.getLogger("ctf_daemon")

# ── Slots ──────────────────────────────────────────────────────────

DONE_FILE  = Path("/tmp/ctf_done")
LEGACY_TASK = Path("/tmp/ctf_task.json")   # v2 compatibility
LEGACY_FLAG = Path("/tmp/ctf_flag.txt")

def _cfg_int(env_var: str, default: int) -> int:
    val = os.environ.get(env_var, "").strip()
    try: return max(1, int(val)) if val else default
    except ValueError: return default

MAX_SLOTS     = _cfg_int("CTF_CONCURRENT_SLOTS", 3)
TASK_TIMEOUT  = _cfg_int("CTF_TASK_TIMEOUT_SECONDS", 600)
MAX_RETRIES   = _cfg_int("CTF_MAX_RETRIES_PER_CHALLENGE", 4)
BASE_COOLDOWN = _cfg_int("CTF_BASE_RETRY_COOLDOWN", 60)
MAX_COOLDOWN  = _cfg_int("CTF_MAX_RETRY_COOLDOWN", 1800)

def slot_dir() -> Path:
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    return TASKS_DIR

def task_file(slot: int) -> Path:      return slot_dir() / f"slot_{slot}.json"
def flag_file(slot: int) -> Path:      return slot_dir() / f"flag_{slot}.txt"
def abort_file(slot: int) -> Path:     return slot_dir() / f"abort_{slot}"

# ── Infra failure detection ────────────────────────────────────────

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

# ── Workdir isolation ──────────────────────────────────────────────

def sanitize_title(title: str) -> str:
    """Convert challenge title to safe directory name: 'Ez_DSA' → 'ez_dsa'"""
    import re
    safe = title.lower().strip()
    safe = re.sub(r'[^a-z0-9_-]', '_', safe)
    safe = re.sub(r'_+', '_', safe)
    safe = safe.strip('_')
    return safe or "challenge"

def challenge_workdir(title: str) -> Path:
    """Get/create per-challenge isolated workdir: /tmp/ctf_{sanitized_title}/"""
    base = Path(os.environ.get("CTF_WORKDIR_BASE", str(WORKDIR_BASE)))
    wd = base / f"ctf_{sanitize_title(title)}"
    wd.mkdir(parents=True, exist_ok=True)
    return wd

# ── Abort signaling ────────────────────────────────────────────────

def signal_abort(slot: int, challenge_id: int, reason: str = "timeout"):
    abort_file(slot).write_text(json.dumps({
        "slot": slot, "challenge_id": challenge_id,
        "reason": reason, "at": time.time(),
    }))
    logger.info(f"[Slot {slot}] ABORT signal for challenge {challenge_id}: {reason}")

def clear_abort(slot: int):
    f = abort_file(slot)
    if f.exists(): f.unlink()

def check_abort(slot: int) -> dict | None:
    f = abort_file(slot)
    if f.exists():
        try: return json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning(f"[Slot {slot}] Corrupted abort file, ignoring")
    return None

# ── Attempt history ────────────────────────────────────────────────

def record_attempt(state: dict, challenge_id: int, summary: str = "",
                   tools_used: list = None, error: str = ""):
    key = str(challenge_id)
    history = state.setdefault("attempt_history", {}).setdefault(key, [])
    entry = {
        "attempt": len(history) + 1, "at": time.time(),
        "summary": summary[:500] if summary else "",
        "tools_used": (tools_used or [])[:10],
        "error": error[:300] if error else "",
    }
    history.append(entry)
    if len(history) > 10:
        state["attempt_history"][key] = history[-10:]

def get_attempt_history(state: dict, challenge_id: int) -> list:
    return state.get("attempt_history", {}).get(str(challenge_id), [])

def format_history_for_agent(history: list) -> str:
    if not history: return ""
    lines = ["", "## Previous Attempts (learn from these)", ""]
    for entry in history:
        lines.append(f"- Attempt {entry['attempt']}: {entry['summary']}")
        if entry.get("tools_used"):
            lines.append(f"  Tools tried: {', '.join(entry['tools_used'][:5])}")
        if entry.get("error"):
            lines.append(f"  Error: {entry['error'][:120]}")
    lines.append("")
    return "\n".join(lines)

# ── Task I/O ───────────────────────────────────────────────────────

def write_task(challenge: dict, analysis: dict, attachments: list,
               workdir: Path = None, state: dict = None, slot: int = 0):
    """Write task to a specific slot."""
    task = {
        "slot": slot,
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
        "workdir": str(workdir) if workdir else None,
        "context_file": str(analysis.get("attachment_url") or ""),
        "assigned_at": time.time(),
        "retry_number": state.get("retry_counts", {}).get(str(challenge.get("id")), 0) if state else 0,
        "attempt_history": format_history_for_agent(
            get_attempt_history(state, challenge.get("id", 0))
        ) if state else "",
    }
    task_file(slot).write_text(json.dumps(task, indent=2, ensure_ascii=False))
    logger.info(f"[Slot {slot}] Task: [{challenge.get('_category')}] "
                f"{challenge.get('title')} (ID:{challenge.get('id')})")

def read_flag(slot: int) -> str | None:
    f = flag_file(slot)
    if f.exists():
        flag = f.read_text().strip()
        if flag:
            f.unlink()
            return flag
    return None

# ── State / slot management ────────────────────────────────────────

def slots_state(state: dict) -> dict:
    """Get or initialize the slots state dict."""
    s = state.setdefault("slots", {})
    # Ensure all slots 0..MAX_SLOTS-1 exist
    for i in range(MAX_SLOTS):
        if str(i) not in s:
            s[str(i)] = None  # None = empty
    return s

def occupied_slots(state: dict) -> dict[int, dict]:
    """Return {slot_num: slot_data} for occupied slots."""
    result = {}
    for k, v in slots_state(state).items():
        if v is not None:
            result[int(k)] = v
    return result

def empty_slots(state: dict) -> list[int]:
    """Return list of empty slot numbers."""
    return [int(k) for k, v in slots_state(state).items() if v is None]

def assigned_challenge_ids(state: dict) -> set[str]:
    """All challenge IDs currently assigned to slots."""
    return {str(v["challenge_id"]) for v in slots_state(state).values() if v}

# ── Failure management ─────────────────────────────────────────────

def _mark_failed(state: dict, ch_id: int, reason: str, is_infra: bool):
    """Mark a challenge as failed. NEVER permanently fail — just escalate cooldown."""
    key = str(ch_id)
    retries = state.setdefault("retry_counts", {}).get(key, 0)
    if not is_infra:
        retries += 1
        state["retry_counts"][key] = retries
        logger.info(f"Challenge {ch_id} failed ({retries} retries): {reason}")
    else:
        logger.info(f"Challenge {ch_id} infra failure (retry unchanged): {reason}")

    cooldown = _compute_cooldown(retries)
    state.setdefault("cooldown_until", {})[key] = time.time() + cooldown
    logger.info(f"Challenge {ch_id} cooldown: {cooldown}s")

def _verify_platform_solved(client: GZCTFClient) -> dict[str, str]:
    """Query platform to find actually-solved challenges. Returns {ch_id: flag_placeholder}."""
    solved = {}
    try:
        resp = client.session.get(
            f"{client.base_url}/api/game/{client.game_id}/details",
            timeout=15,
        )
        if resp.status_code == 200:
            challenges = resp.json().get("challenges", {})
            for cat, ch_list in challenges.items():
                for ch in ch_list:
                    if ch.get("status") == "Accepted" or ch.get("solved"):
                        solved[str(ch["id"])] = "verified_on_platform"
        if solved:
            logger.info(f"Platform verified solved: {list(solved.keys())}")
    except Exception as e:
        logger.warning(f"Failed to verify platform solved: {e}")
    return solved

def _sync_solved_from_platform(state: dict, client: GZCTFClient):
    """Cross-reference internal state with platform. Returns number newly discovered."""
    platform_solved = _verify_platform_solved(client)
    newly_discovered = 0
    internal = state.get("solved", {})
    for ch_id in platform_solved:
        if ch_id not in internal:
            internal[ch_id] = "synced_from_platform"
            newly_discovered += 1
            # Clean up retry/cooldown state
            for cleanup in ("retry_counts", "cooldown_until", "attempt_history"):
                state.get(cleanup, {}).pop(ch_id, None)
    if newly_discovered:
        state["solved"] = internal
        logger.info(f"Synced {newly_discovered} challenges from platform")
    return newly_discovered

def _mark_solved(state: dict, ch_id: int, flag: str):
    key = str(ch_id)
    state["solved"][key] = flag
    for cleanup in ("retry_counts", "cooldown_until", "permanently_failed", "attempt_history"):
        state.get(cleanup, {}).pop(key, None)

def _free_slot(state: dict, slot: int):
    """Release a slot back to the pool."""
    slots_state(state)[str(slot)] = None
    clear_abort(slot)
    f = task_file(slot)
    if f.exists(): f.unlink()

# ── Challenge preparation ──────────────────────────────────────────

def prepare_challenge(client: GZCTFClient, challenge: dict,
                      workdir: Path, state: dict = None) -> tuple:
    """Prepare a challenge: create workdir, download attachments, container, analysis.
    Returns (challenge, analysis, attachments, workdir)."""
    ch_id = challenge["id"]
    title = challenge.get("title", f"challenge_{ch_id}")
    wd = workdir if workdir else challenge_workdir(title)
    attachments = client.download_challenge_attachments(challenge, str(wd))

    if challenge.get("type") == "DynamicContainer":
        logger.info(f"Creating container for challenge {ch_id}...")
        try:
            client.create_container(ch_id)
            if client.wait_for_container_ready(ch_id, timeout=60):
                challenge = client.get_challenge_detail(ch_id)
                logger.info(f"Container ready: challenge {ch_id}")
            else:
                logger.warning(f"Container not ready: challenge {ch_id}")
        except Exception as e:
            logger.warning(f"Container creation failed for {ch_id}: {e}")

    analysis = ChallengeAnalyzer.analyze_challenge(challenge)
    return challenge, analysis, attachments, wd

# ── Main ───────────────────────────────────────────────────────────

def main():
    config = load_config(str(SCRIPT_DIR / "config.env"))
    base_url    = config.get("GZCTF_BASE_URL", "")
    username    = config.get("GZCTF_USERNAME", "")
    password    = config.get("GZCTF_PASSWORD", "")
    game_id     = int(config.get("GZCTF_GAME_ID", "0"))
    # workdir_base supports CTF_WORKDIR_BASE from both config.env and env var
    _wb = config.get("CTF_WORKDIR_BASE", "").strip()
    if _wb:
        os.environ.setdefault("CTF_WORKDIR_BASE", _wb)

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
    slots = slots_state(state)  # ensure initialized
    
    # ── Sync with platform truth ──────────────────────────────────
    _sync_solved_from_platform(state, client)
    solved_ids = set(state["solved"].keys())
    # v3.1: Never permanently fail a challenge — always allow retries.
    # perm_failed is kept for backward compat only, not used for filtering.

    # ── Legacy migration: if old task files exist, move to slot 0 ──
    if LEGACY_TASK.exists() and slots.get("0") is None:
        try:
            legacy_task = json.loads(LEGACY_TASK.read_text())
            ch_id = legacy_task.get("challenge_id")
            if ch_id:
                slots["0"] = {
                    "challenge_id": ch_id,
                    "assigned_at": legacy_task.get("assigned_at", time.time()),
                }
                # Copy to slot task file
                task_file(0).write_text(LEGACY_TASK.read_text())
                LEGACY_TASK.unlink()
                save_state(state)
                logger.info(f"Migrated legacy task to slot 0: challenge {ch_id}")
        except (json.JSONDecodeError, OSError, KeyError) as e:
            logger.warning(f"Legacy migration failed: {e}")
            if LEGACY_TASK.exists(): LEGACY_TASK.unlink()

    if LEGACY_FLAG.exists():
        flag = LEGACY_FLAG.read_text().strip()
        if flag:
            flag_file(0).write_text(flag)
            LEGACY_FLAG.unlink()
            logger.info("Migrated legacy flag to slot 0")

    # ── Step 1: Process completed/aborted slots ─────────────────────

    for slot_num, slot_data in occupied_slots(state).items():
        ch_id = slot_data["challenge_id"]
        assigned_at = slot_data.get("assigned_at", 0)
        elapsed = time.time() - assigned_at if assigned_at else 0

        flag = read_flag(slot_num)
        if flag:
            logger.info(f"[Slot {slot_num}] Flag: challenge {ch_id}: {flag}")
            submit_and_record(client, ch_id, flag, state)
            # Verify on platform before trusting
            _sync_solved_from_platform(state, client)
            if str(ch_id) in state.get("solved", {}):
                logger.info(f"[Slot {slot_num}] ✅ Verified solved: challenge {ch_id}")
                _free_slot(state, slot_num)
                save_state(state)
                continue
            else:
                # Flag rejected — retry later
                logger.warning(f"[Slot {slot_num}] ❌ Flag REJECTED: challenge {ch_id}")
                _mark_failed(state, ch_id, "flag_rejected", is_infra=False)
                _free_slot(state, slot_num)
                save_state(state)
                continue

        if elapsed > TASK_TIMEOUT:
            logger.warning(f"[Slot {slot_num}] TIMEOUT: challenge {ch_id} "
                           f"after {elapsed:.0f}s")
            is_infra = False
            tf = task_file(slot_num)
            if tf.exists():
                try:
                    task = json.loads(tf.read_text())
                    is_infra = _is_infra_failure(task.get("description", ""))
                except: pass

            record_attempt(state, ch_id,
                          summary=f"Timeout after {elapsed:.0f}s",
                          error=f"Exceeded TASK_TIMEOUT ({TASK_TIMEOUT}s)")
            _mark_failed(state, ch_id, f"timeout ({elapsed:.0f}s)", is_infra)
            signal_abort(slot_num, ch_id, f"timeout ({elapsed:.0f}s)")
            _free_slot(state, slot_num)
            save_state(state)
            logger.info(f"[Slot {slot_num}] Freed — challenge {ch_id} → cooldown")
            continue

        # Task in progress — keep it
        logger.info(f"[Slot {slot_num}] Running: challenge {ch_id} "
                    f"({elapsed:.0f}s/{TASK_TIMEOUT}s)")

    # ── Step 2: Fill empty slots ───────────────────────────────────

    free_slots = empty_slots(state)
    if not free_slots:
        # All slots busy
        occ = occupied_slots(state)
        status_parts = [f"{s}:ch{occ[s]['challenge_id']}" for s in sorted(occ)]
        print(f"BUSY:{len(occ)}/{MAX_SLOTS}:{','.join(status_parts)}")
        return

    # Fetch all challenges
    all_challenges = client.get_all_challenge_details()
    now_ts = time.time()
    in_flight = assigned_challenge_ids(state)

    eligible = []
    for ch in all_challenges:
        ch_id = str(ch.get("id"))
        if ch_id in solved_ids:     continue
        if ch_id in in_flight:      continue  # already in another slot
        cooldown = state.get("cooldown_until", {}).get(ch_id, 0)
        if cooldown > now_ts:       continue
        eligible.append(ch)

    if not eligible and not occupied_slots(state):
        # Nothing eligible right now. Check if truly done or just waiting for cooldown.
        total_challenges = len(all_challenges)
        if len(solved_ids) >= total_challenges:
            status_msg = f"ALL DONE! Solved: {len(solved_ids)}/{total_challenges}"
            DONE_FILE.write_text(status_msg)
            DONE_FILE.chmod(0o644)
            logger.info(status_msg)
            print("DONE")
            return
        else:
            # Still waiting — cooldowns, retries, or unsolved
            remaining = total_challenges - len(solved_ids)
            cooldown_count = sum(1 for v in state.get("cooldown_until", {}).values() if v > now_ts)
            print(f"WAITING:{remaining} remaining, {cooldown_count} in cooldown")
            return

    # ── LLM-driven selection ─────────────────────────────────────
    # Instead of hardcoded sorting, output a menu for the LLM to decide.
    # The LLM (Hermes) reads this, analyzes challenges, and writes back
    # its priority list to /tmp/ctf_selection.json

    # Write menu for LLM
    menu_lines = []
    for ch in eligible:
        menu_lines.append(json.dumps({
            "id": ch.get("id"),
            "title": ch.get("title"),
            "category": ch.get("_category", ch.get("category", "?")),
            "type": ch.get("type", ""),
            "score": ch.get("score", 0),
            "needs_container": "Container" in (ch.get("type") or ""),
            "retries": state.get("retry_counts", {}).get(str(ch.get("id")), 0),
            "content_preview": (ch.get("content") or "")[:300],
        }, ensure_ascii=False))

    MENU_FILE = Path("/tmp/ctf_menu.jsonl")
    MENU_FILE.write_text("\n".join(menu_lines))

    # Check if LLM already made a selection
    SEL_FILE = Path("/tmp/ctf_selection.json")
    if SEL_FILE.exists():
        try:
            sel = json.loads(SEL_FILE.read_text())
            ordered_ids = sel.get("challenge_ids", [])
            # Reorder eligible to match LLM's priority
            id_to_ch = {str(ch.get("id")): ch for ch in eligible}
            eligible = [id_to_ch[cid] for cid in ordered_ids if cid in id_to_ch]
            logger.info(f"Using LLM selection: {ordered_ids}")
            SEL_FILE.unlink()  # consume the selection
        except Exception as e:
            logger.warning(f"Failed to parse LLM selection: {e}")
    else:
        # No LLM selection yet — output menu and stop
        menu_preview = "\n".join(
            f"  [{ch.get('_category','?')}] {ch.get('title')} "
            f"(ID:{ch.get('id')} score:{ch.get('score')} "
            f"type:{ch.get('type','?')[:20]} retries:{state.get('retry_counts',{}).get(str(ch.get('id')),0)})"
            for ch in eligible[:10]
        )
        print(f"MENU:{len(eligible)}:{menu_preview}")
        logger.info(f"Menu written to {MENU_FILE} — waiting for LLM selection")
        return

    # Fill each empty slot with the best eligible challenge
    filled = 0
    containers_this_run = 0
    MAX_CONTAINERS_PER_RUN = 2  # Don't spin up more than 2 containers per daemon tick
    for slot_num in free_slots:
        if not eligible:
            break
        challenge = eligible.pop(0)
        ch_id = challenge["id"]

        retries = state.get("retry_counts", {}).get(str(ch_id), 0)
        if retries > 0:
            logger.info(f"[Slot {slot_num}] Retry #{retries + 1}/{MAX_RETRIES}: "
                        f"{challenge.get('title')}")

        # Container rate-limit: skip container challenges if we've hit the cap
        is_container = "Container" in (challenge.get("type") or "")
        if is_container and containers_this_run >= MAX_CONTAINERS_PER_RUN:
            logger.info(f"[Slot {slot_num}] Skipping {challenge.get('title')} — "
                        f"container limit ({MAX_CONTAINERS_PER_RUN}) reached")
            continue

        # Quick flag check before dispatching
        wd = challenge_workdir(challenge.get("title") or f"challenge_{ch_id}")
        challenge, analysis, attachments, wd = prepare_challenge(
            client, challenge, wd, state
        )

        if is_container:
            containers_this_run += 1

        if analysis.get("flags_found"):
            for f in analysis["flags_found"]:
                logger.info(f"Flag in description, auto-submit: {f}")
                submit_and_record(client, ch_id, f, state)
                _mark_solved(state, ch_id, f)
                save_state(state)
                continue  # skip this slot, will be filled next tick

        # Check attachments for flags
        found_in_attach = False
        for path in attachments:
            fa = basic_file_analysis(path)
            if fa.get("flags_in_strings"):
                for f in fa["flags_in_strings"]:
                    logger.info(f"Flag in attachment {path}: {f}")
                    submit_and_record(client, ch_id, f, state)
                    _mark_solved(state, ch_id, f)
                    save_state(state)
                    found_in_attach = True
                    break
        if found_in_attach:
            continue  # skip this slot

        # Assign to slot
        record_attempt(state, ch_id, summary=f"Starting attempt (slot {slot_num})")
        slots[str(slot_num)] = {
            "challenge_id": ch_id,
            "assigned_at": time.time(),
        }
        clear_abort(slot_num)
        write_task(challenge, analysis, attachments, workdir=wd, state=state, slot=slot_num)
        filled += 1

    save_state(state)

    # ── Output status ──────────────────────────────────────────────
    occ = occupied_slots(state)
    if filled > 0 or not occ:
        # Fresh dispatch or re-check
        if not occ and not empty_slots(state):
            print("DONE")
        elif occ:
            parts = []
            for s in sorted(occ):
                d = occ[s]
                ch_id = d["challenge_id"]
                # Try to read title from task file
                title = f"ch{ch_id}"
                tf = task_file(s)
                if tf.exists():
                    try:
                        t = json.loads(tf.read_text())
                        title = t.get("title", title)[:20]
                    except: pass
                parts.append(f"s{s}:{title}")
            print(f"DISPATCH:{len(occ)}/{MAX_SLOTS}:{','.join(parts)}")
        else:
            print("IDLE:waiting_for_cooldown")


if __name__ == "__main__":
    main()
