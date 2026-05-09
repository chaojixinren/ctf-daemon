"""
Persistent state management for CTF Daemon v3.1.

Manages the JSON state file (~/.hermes/ctf_state.json) that tracks:
- solved flags
- retry counts and cooldowns
- slot assignments
- attempt history

Both ctf_daemon.py and solver.py share this module — no more circular imports.
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("ctf_state")

# Overridable via CTF_STATE_FILE env var
STATE_FILE = Path(
    os.environ.get("CTF_STATE_FILE", str(Path.home() / ".hermes" / "ctf_state.json"))
)

# Overridable via CTF_TASKS_DIR env var
TASKS_DIR = Path(
    os.environ.get("CTF_TASKS_DIR", "/tmp/ctf_tasks")
)

# Overridable via CTF_RESCUE_FLAGS env var
RESCUE_FLAGS_PATH = Path(
    os.environ.get("CTF_RESCUE_FLAGS", "/tmp/ctf_flags_rescue.txt")
)


def load_state() -> dict:
    """Load solver state from disk. Returns fresh default if file missing."""
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
            # Backfill new fields for compatibility
            state.setdefault("retry_counts", {})
            state.setdefault("cooldown_until", {})
            state.setdefault("permanently_failed", {})
            state.setdefault("task_assigned_at", None)
            state.setdefault("attempted", {})
            state.setdefault("failed", [])
            state.setdefault("attempt_history", {})
            state.setdefault("slots", {})            # v3
            state.setdefault("solved", {})
            state.setdefault("current", None)
            state.setdefault("start_time", None)
            return state
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Corrupted state file, starting fresh: {e}")
    return {
        "solved": {},              # {challenge_id: flag}
        "attempted": {},           # {challenge_id: count}
        "failed": [],              # [challenge_id, ...]
        "current": None,           # current challenge being worked on
        "start_time": None,
        "retry_counts": {},        # {challenge_id: count}
        "cooldown_until": {},      # {challenge_id: timestamp}
        "permanently_failed": {},  # {challenge_id: {reason, retries, at}}
        "task_assigned_at": None,
        "attempt_history": {},
        "slots": {},                # v3: {"0": {"challenge_id":..., "assigned_at":...}, ...}
    }


def save_state(state: dict) -> None:
    """Save solver state to disk atomically."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))
