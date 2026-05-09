#!/usr/bin/env python3
"""
Autonomous CTF Solver - Main Entry Point
=========================================
Designed to be invoked by Hermes Agent as a one-shot execution.
Fetches challenges from GZCTF, solves them using AI + Kali MCP tools,
and submits flags automatically.

This is the script that runs inside the "one prompt" flow.
"""

import os
import sys
import time
import json
import logging
from datetime import datetime
from pathlib import Path

# Add script directory to path
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from gzctf_client import GZCTFClient, load_config
from challenge_engine import (
    ChallengeAnalyzer, extract_flag, extract_flags,
    build_solving_prompt, basic_file_analysis, categorize_file,
    save_rescue_flag, load_rescue_flags, remove_rescue_flag,
)

# ── Logging ───────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stderr),
        logging.FileHandler(Path.home() / ".hermes" / "logs" / "ctf_solver.log"),
    ],
)
logger = logging.getLogger("ctf_solver")

# ── State Management ──────────────────────────────────────────────

STATE_FILE = Path.home() / ".hermes" / "ctf_state.json"

def load_state() -> dict:
    """Load solver state from disk."""
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
            # Backfill new fields for v2 compatibility
            state.setdefault("retry_counts", {})
            state.setdefault("cooldown_until", {})
            state.setdefault("permanently_failed", {})
            state.setdefault("task_assigned_at", None)
            state.setdefault("attempted", {})
            state.setdefault("failed", [])
            return state
        except Exception:
            pass
    return {
        "solved": {},              # {challenge_id: flag}
        "attempted": {},           # {challenge_id: count}
        "failed": [],              # [challenge_id, ...]
        "current": None,           # current challenge being worked on
        "start_time": None,
        "retry_counts": {},        # {challenge_id: count} — v2: per-challenge retries
        "cooldown_until": {},      # {challenge_id: timestamp} — v2: cooldown deadline
        "permanently_failed": {},  # {challenge_id: {reason, retries, at}} — v2: give-up list
        "task_assigned_at": None,  # v2: unix timestamp when current task was dispatched
    }

def save_state(state: dict):
    """Save solver state to disk."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ── Main Solver ────────────────────────────────────────────────────

def main():
    """Main autonomous CTF solving loop. Called by Hermes Agent."""
    
    # Load config
    config_path = os.environ.get("CTF_CONFIG", str(SCRIPT_DIR / "config.env"))
    config = load_config(config_path)
    
    base_url = config.get("GZCTF_BASE_URL", "")
    username = config.get("GZCTF_USERNAME", "")
    password = config.get("GZCTF_PASSWORD", "")
    game_id = int(config.get("GZCTF_GAME_ID", "0"))
    team_name = config.get("GZCTF_TEAM_NAME", "AI_Solver")
    attachment_dir = config.get("ATTACHMENT_DIR", "/tmp/ctf_attachments")
    
    # Validate
    if not base_url or not username or not password:
        print("ERROR: Missing credentials. Set GZCTF_BASE_URL, GZCTF_USERNAME, GZCTF_PASSWORD")
        print("Either in config.env or as environment variables.")
        sys.exit(1)
    
    # Load state
    state = load_state()
    if not state["start_time"]:
        state["start_time"] = datetime.now().isoformat()
        save_state(state)
    
    # ── Phase 0: Rescue Retry ─────────────────────────────────────
    
    print("[*] Checking for rescued flags to retry...")
    rescued = load_rescue_flags()
    if rescued:
        print(f"[*] Found {len(rescued)} rescued flag(s) to retry")
        for ch_id, flag in rescued:
            print(f"    Retrying challenge {ch_id}: {flag}")
            if submit_and_record(client, ch_id, flag, state, rescue_on_fail=False):
                print(f"    [+] Rescued flag accepted!")
    
    # ── Phase 1: Connect ──────────────────────────────────────────
    
    print(f"[*] Connecting to {base_url}...")
    client = GZCTFClient(base_url, username, password)
    
    if not client.login():
        print("ERROR: Login failed! Check credentials.")
        sys.exit(1)
    
    print(f"[+] Logged in as {username}")
    
    # ── Phase 2: Find/Join Game ───────────────────────────────────
    
    if game_id == 0:
        print("[*] Auto-discovering game...")
        games = client.list_games()
        if not games:
            print("ERROR: No games available!")
            sys.exit(1)
        game = games[0]  # Pick first available
        game_id = game.get("id", 0)
        print(f"[+] Found game: {game.get('title', 'Unknown')} (ID: {game_id})")
    
    # Get game detail
    try:
        game_detail = client.get_game_detail(game_id)
        print(f"[+] Game: {game_detail.get('title', 'Unknown')}")
        print(f"[+] Challenges: {game_detail.get('challengeCount', 0)}")
    except Exception as e:
        print(f"ERROR: Failed to get game {game_id}: {e}")
        sys.exit(1)
    
    # Join if needed
    if not client.team_token:
        print(f"[*] Joining game as team '{team_name}'...")
        client.join_game(game_id, team_name)
        game_detail = client.get_game_detail(game_id)
    
    # ── Phase 3: Get All Challenges ───────────────────────────────
    
    print("[*] Fetching challenge details...")
    all_challenges = client.get_all_challenge_details()
    print(f"[+] Retrieved {len(all_challenges)} challenges")
    
    # Build solved set from state
    solved_ids = set(state["solved"].keys())
    
    # Find unsolved challenges
    unsolved = []
    for ch in all_challenges:
        ch_id = ch.get("id")
        if ch_id not in solved_ids:
            unsolved.append(ch)
    
    print(f"[*] Unsolved: {len(unsolved)}, Solved: {len(solved_ids)}")
    
    if not unsolved:
        print("[+] All challenges solved! \\o/")
        return print_final_results(state, all_challenges)
    
    # ── Phase 4: Select Next Challenge ────────────────────────────
    
    # Prioritize:
    # 1. Challenges with flags already visible
    # 2. Lower score first (easier challenges)
    # 3. Not yet attempted
    
    unsolved.sort(key=lambda ch: (
        ch.get("id") in state.get("failed", []),  # failed last
        ch.get("score", 0),  # lower score first
    ))
    
    # Check if there's a current challenge we're working on
    if state["current"]:
        current_id = state["current"]
        # Find it in unsolved
        current_ch = next((ch for ch in unsolved if ch.get("id") == current_id), None)
        if current_ch:
            challenge = current_ch
            print(f"[*] Resuming challenge: {challenge.get('title')} (ID: {current_id})")
        else:
            challenge = unsolved[0]
            state["current"] = challenge.get("id")
            save_state(state)
    else:
        challenge = unsolved[0]
        state["current"] = challenge.get("id")
        save_state(state)
    
    ch_id = challenge.get("id")
    ch_title = challenge.get("title", "Unknown")
    ch_category = challenge.get("_category", "Misc")
    
    print(f"\n{'='*60}")
    print(f"[*] Solving: {ch_title} [{ch_category}] (ID: {ch_id})")
    print(f"{'='*60}")
    
    # Track attempts
    state["attempted"][str(ch_id)] = state["attempted"].get(str(ch_id), 0) + 1
    save_state(state)
    
    # ── Phase 5: Download Attachments ─────────────────────────────
    
    attachments = client.download_challenge_attachments(challenge, attachment_dir)
    if attachments:
        print(f"[+] Downloaded {len(attachments)} attachment(s)")
        for path in attachments:
            analysis = basic_file_analysis(path)
            print(f"    {path}: {analysis['type']} ({analysis['size']} bytes)")
            if analysis.get("flags_in_strings"):
                print(f"    >>> FLAGS FOUND IN STRINGS: {analysis['flags_in_strings']}")
    
    # ── Phase 6: Analyze Challenge ────────────────────────────────
    
    analysis = ChallengeAnalyzer.analyze_challenge(challenge)
    
    # Check if flag is already visible
    if analysis["flags_found"]:
        for flag in analysis["flags_found"]:
            print(f"[!!!] Flag already found in description: {flag}")
            # Submit immediately
            submit_and_record(client, ch_id, flag, state)
            state["current"] = None
            save_state(state)
            # Print next prompt
            print_next_prompt(state, all_challenges)
            return
    
    # If hints/description suggest a flag pattern
    if attachments:
        for path in attachments:
            file_analysis = basic_file_analysis(path)
            if file_analysis.get("flags_in_strings"):
                for flag in file_analysis["flags_in_strings"]:
                    print(f"[!!!] Flag found in attachment: {flag}")
                    submit_and_record(client, ch_id, flag, state)
                    state["current"] = None
                    save_state(state)
                    print_next_prompt(state, all_challenges)
                    return
    
    # ── Phase 7: Build Solving Prompt ─────────────────────────────
    
    solving_prompt = build_solving_prompt(analysis, attachments)
    
    # Save challenge context for the AI agent
    context_file = Path(attachment_dir) / f"challenge_{ch_id}.md"
    context_file.parent.mkdir(parents=True, exist_ok=True)
    context_file.write_text(solving_prompt)
    
    # ── Phase 8: Output Instructions ──────────────────────────────
    
    print(f"\n[CHALLENGE_CONTEXT_FILE] {context_file}")
    print(f"[ATTACHMENT_DIR] {attachment_dir}")
    print(f"[CHALLENGE_ID] {ch_id}")
    print(f"[CATEGORY] {ch_category}")
    print(f"[STRATEGY] {analysis['strategy']}")
    print(f"[RECOMMENDED_TOOLS] {', '.join(analysis['recommended_tools'][:5])}")
    print(f"[CONTAINER] {analysis['has_container']}")
    if analysis.get("container_entry"):
        print(f"[CONTAINER_ENTRY] {analysis['container_entry']}")
    
    print(f"""
[ACTION_REQUIRED]
Use the Kali MCP tools to solve this challenge:
  Category: {ch_category}
  Strategy: {analysis['strategy']}
  Context file: {context_file}
  Attachments: {attachments}

Steps:
1. Read the context file for full challenge details
2. Analyze any attachments thoroughly
3. Use recommended tools to exploit/discover the flag
4. When you find the flag, call execute_code with:
   from gzctf_client import GZCTFClient, load_config
   # ... submit the flag

OR use the submit_flag.py script:
   python3 submit_flag.py {ch_id} "flag{{...}}"
""")


def submit_and_record(client: GZCTFClient, ch_id: int, flag: str, state: dict,
                       rescue_on_fail: bool = True) -> bool:
    """Submit a flag, poll for status, and record the result.
    If rescue_on_fail=True and submission fails, cache to rescue file for retry.
    """
    print(f"[*] Submitting flag for challenge {ch_id}: {flag}")
    submit_id = client.submit_flag(ch_id, flag)
    
    if submit_id == 0:
        # Direct accept (status string returned instead of submit ID)
        state["solved"][str(ch_id)] = flag
        print(f"[+] FLAG ACCEPTED (direct)! {flag}")
        remove_rescue_flag(ch_id, flag)
        save_state(state)
        return True
    elif submit_id > 0:
        status = client.check_flag_status(ch_id, submit_id, poll=True)
        print(f"[*] Flag status after polling: {status}")
        
        if status in client.ACCEPTED_STATUSES:
            state["solved"][str(ch_id)] = flag
            print(f"[+] FLAG ACCEPTED! {flag}")
            remove_rescue_flag(ch_id, flag)
            save_state(state)
            return True
        elif status == "AlreadySolved":
            print(f"[*] Flag already solved by team: {flag}")
            state["solved"][str(ch_id)] = flag
            remove_rescue_flag(ch_id, flag)
            save_state(state)
            return True
        elif status in ("WrongAnswer", "TooManyAttempts", "Forbidden"):
            print(f"[-] Flag rejected ({status}): {flag}")
            if rescue_on_fail:
                save_rescue_flag(ch_id, flag, f"rejected:{status}")
            return False
        else:
            print(f"[?] Flag status unclear ({status}): {flag}")
            if rescue_on_fail:
                save_rescue_flag(ch_id, flag, f"unclear:{status}")
            return False
    else:
        print(f"[-] Submission failed (transport error)")
        if rescue_on_fail:
            save_rescue_flag(ch_id, flag, "transport_error")
        return False


def print_next_prompt(state: dict, all_challenges: list):
    """Print instructions for the next iteration."""
    solved = len(state["solved"])
    total = len(all_challenges)
    print(f"\n[+] Progress: {solved}/{total} challenges solved")
    
    if solved >= total:
        print("\n[!!!] ALL CHALLENGES SOLVED! GG! \\o/")
    else:
        unsolved = [ch for ch in all_challenges if str(ch.get("id")) not in state["solved"]]
        print(f"\n[*] Next: {len(unsolved)} remaining. Re-run solver for next challenge.")


def print_final_results(state: dict, all_challenges: list):
    """Print final competition results."""
    print(f"\n{'='*60}")
    print("FINAL RESULTS")
    print(f"{'='*60}")
    print(f"Total challenges: {len(all_challenges)}")
    print(f"Solved: {len(state['solved'])}")
    print(f"Start time: {state['start_time']}")
    print(f"End time: {datetime.now().isoformat()}")
    print(f"\nSolved Flags:")
    for ch_id, flag in state["solved"].items():
        ch = next((c for c in all_challenges if str(c.get("id")) == ch_id), {})
        print(f"  [{ch.get('_category', '?')}] {ch.get('title', ch_id)}: {flag}")


# ── CLI for manual flag submission ────────────────────────────────

if __name__ == "__main__":
    # If called with args: submit a flag manually
    if len(sys.argv) >= 3 and sys.argv[1] == "submit":
        config = load_config()
        client = GZCTFClient(
            config.get("GZCTF_BASE_URL", ""),
            config.get("GZCTF_USERNAME", ""),
            config.get("GZCTF_PASSWORD", ""),
        )
        challenge_id = int(sys.argv[2])
        flag = sys.argv[3]
        
        if client.login():
            state = load_state()
            submit_and_record(client, challenge_id, flag, state)
        else:
            print("ERROR: Login failed")
            sys.exit(1)
    
    elif len(sys.argv) >= 2 and sys.argv[1] == "status":
        state = load_state()
        print(json.dumps(state, indent=2))
    
    elif len(sys.argv) >= 2 and sys.argv[1] == "list":
        config = load_config()
        client = GZCTFClient(
            config.get("GZCTF_BASE_URL", ""),
            config.get("GZCTF_USERNAME", ""),
            config.get("GZCTF_PASSWORD", ""),
        )
        if client.login():
            challenges = client.get_all_challenge_details()
            state = load_state()
            solved = set(state["solved"].keys())
            for ch in challenges:
                ch_id = str(ch.get("id"))
                status = "SOLVED" if ch_id in solved else "UNSOLVED"
                print(f"[{status}] [{ch.get('_category', '?')}] {ch.get('title')} (score: {ch.get('score', 0)})")
    
    else:
        main()
