"""
GZCTF API Client — Autonomous CTF Solver.
API patterns inspired by Misuzu's GZCTF plugin (github.com/TechnickOcean/Misuzu).
Handles authentication, challenge fetching, flag submission.
"""

import os
import time
import json
import re
import logging
from pathlib import Path
from urllib.parse import urljoin
from typing import Optional, Dict, List, Any

import requests

logger = logging.getLogger("gzctf")

class GZCTFClient:
    """Client for GZCTF competition platform API."""

    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "CTF-AutoSolver/1.0",
            "Accept": "application/json",
        })
        self.game_id: Optional[int] = None
        self.game_info: Dict = {}
        self.team_token: Optional[str] = None
        self._logged_in = False

    # ── Authentication ────────────────────────────────────────────

    def login(self) -> bool:
        """Log in to GZCTF and store session cookie."""
        try:
            resp = self.session.post(
                urljoin(self.base_url, "/api/account/login"),
                json={"userName": self.username, "password": self.password},
                timeout=15,
            )
            if resp.status_code == 200:
                self._logged_in = True
                logger.info("Login successful")
                return True
            else:
                logger.error(f"Login failed: {resp.status_code} {resp.text[:200]}")
                return False
        except Exception as e:
            logger.error(f"Login error: {e}")
            return False

    # ── Game Operations ───────────────────────────────────────────

    def list_games(self) -> List[Dict]:
        """Get list of available games."""
        resp = self.session.get(
            urljoin(self.base_url, "/api/game"),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        # GZCTF wraps responses in {data: [...], length, total}
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data if isinstance(data, list) else []

    def get_game_detail(self, game_id: int) -> Dict:
        """Get detailed game info including challenges and team token."""
        # Basic game info
        resp = self.session.get(
            urljoin(self.base_url, f"/api/game/{game_id}"),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        
        # Also fetch details for team token and challenges
        try:
            details_resp = self.session.get(
                urljoin(self.base_url, f"/api/game/{game_id}/details"),
                timeout=15,
            )
            details_resp.raise_for_status()
            details = details_resp.json()
            # Merge team token and challenges from details
            data["teamToken"] = details.get("teamToken", "")
            data["challenges"] = details.get("challenges", {})
            data["challengeCount"] = details.get("challengeCount", 0)
        except Exception:
            pass
        
        self.game_id = game_id
        self.game_info = data
        self.team_token = data.get("teamToken", "")
        return data

    def join_game(self, game_id: int, team_name: str = None) -> bool:
        """Join a game, optionally creating/joining a team."""
        payload = {}
        if team_name:
            payload["teamName"] = team_name
        try:
            resp = self.session.post(
                urljoin(self.base_url, f"/api/game/{game_id}"),
                json=payload if payload else None,
                timeout=15,
            )
            if resp.status_code in (200, 201):
                logger.info(f"Joined game {game_id} successfully")
                return True
            data = resp.json() if resp.text else {}
            logger.warning(f"Join game {game_id}: {resp.status_code} {data}")
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Join game error: {e}")
            return False

    # ── Challenge Operations ──────────────────────────────────────

    def get_challenges(self) -> Dict[str, List[Dict]]:
        """Get all challenges grouped by category via /details endpoint.
        Returns: {"Web": [...], "Pwn": [...], ...}
        """
        if not self.game_id:
            return {}
        resp = self.session.get(
            urljoin(self.base_url, f"/api/game/{self.game_id}/details"),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("challenges", {})

    def get_challenge_detail(self, challenge_id: int) -> Dict:
        """Get detailed info for a single challenge (description, hints, attachments).
        Falls back to basic info from /details if the challenge endpoint returns HTML."""
        resp = self.session.get(
            urljoin(self.base_url, f"/api/game/{self.game_id}/challenges/{challenge_id}"),
            timeout=15,
        )
        # GZCTF may return HTML for this endpoint — fall back
        try:
            if resp.status_code == 200 and resp.text.strip().startswith('{'):
                return resp.json()
        except Exception:
            pass
        # Fallback: get from details endpoint
        details = self.get_challenges()
        for cat, ch_list in details.items():
            for ch in ch_list:
                if ch.get("id") == challenge_id:
                    ch["_category"] = cat
                    return ch
        return {"id": challenge_id, "_error": "not found"}

    def get_all_challenge_details(self) -> List[Dict]:
        """Get details for ALL challenges in the game."""
        challenges = self.get_challenges()
        all_details = []
        for category, ch_list in challenges.items():
            for ch in ch_list:
                try:
                    detail = self.get_challenge_detail(ch["id"])
                    detail["_category"] = category
                    all_details.append(detail)
                except Exception as e:
                    logger.error(f"Failed to get detail for challenge {ch['id']}: {e}")
                    ch["_category"] = category
                    all_details.append(ch)
        return all_details

    # ── Flag Submission ───────────────────────────────────────────

    # Statuses from GZCTF (observed across different versions)
    ACCEPTED_STATUSES = {"Correct", "Accepted", "Solved", "Success"}
    FINISHED_STATUSES = {
        "Correct", "Accepted", "Solved", "Success",
        "WrongAnswer", "AlreadySolved", "TooManyAttempts", "Forbidden",
    }

    def submit_flag(self, challenge_id: int, flag: str) -> int:
        """Submit a flag. Returns submit ID on success, -1 on failure."""
        resp = self.session.post(
            urljoin(self.base_url, f"/api/game/{self.game_id}/challenges/{challenge_id}"),
            json={"flag": flag},
            timeout=15,
        )
        text = resp.text.strip()
        # Handle JSON-quoted responses like '"1"' or '"Accepted"'
        if text.startswith('"') and text.endswith('"'):
            text = text[1:-1]
        if resp.status_code == 200:
            # Response can be either a submission ID (int) or a status string
            try:
                submit_id = int(text)
            except (ValueError, TypeError):
                submit_id = -1
                if text in self.ACCEPTED_STATUSES:
                    logger.info(f"Flag directly accepted: {text}")
                    return 0  # special: direct accept, no submitId
            logger.info(f"Flag submitted for challenge {challenge_id}, submitId={submit_id}")
            return submit_id
        data = {}
        try:
            data = resp.json() if resp.text else {}
        except Exception:
            pass
        logger.warning(f"Flag submit failed for {challenge_id}: {resp.status_code} {text[:200]}")
        return -1

    def check_flag_status(self, challenge_id: int, submit_id: int, 
                           poll: bool = True, max_polls: int = 8, poll_delay: float = 0.7) -> str:
        """Check flag submission status. If poll=True, retries until a final status.
        Returns: 'Correct', 'Accepted', 'Solved', 'Success', 'WrongAnswer',
                 'AlreadySolved', 'TooManyAttempts', 'Forbidden', 'Pending', 'Unknown'
        """
        for attempt in range(max_polls if poll else 1):
            resp = self.session.get(
                urljoin(self.base_url, 
                       f"/api/game/{self.game_id}/challenges/{challenge_id}/status/{submit_id}"),
                timeout=15,
            )
            if resp.status_code == 200:
                status = resp.text.strip()
                # Some GZCTF versions return JSON-quoted strings like '"WrongAnswer"'
                if status.startswith('"') and status.endswith('"'):
                    status = status[1:-1]
                if status in self.FINISHED_STATUSES:
                    return status
                if attempt + 1 < max_polls and poll:
                    time.sleep(poll_delay)
                    continue
                return status if status else "Pending"

            # Non-200 might mean the status isn't ready yet
            if attempt + 1 < max_polls and poll:
                time.sleep(poll_delay)
            else:
                return "Unknown"
        
        return "Pending"

    # ── Attachments ───────────────────────────────────────────────

    def download_attachment(self, url: str, save_dir: str) -> Optional[str]:
        """Download an attachment. Handles both relative paths and external URLs.
        Returns local file path or None.
        """
        # Handle both relative paths and absolute URLs
        if url.startswith("http://") or url.startswith("https://"):
            full_url = url
        else:
            full_url = urljoin(self.base_url, url)
        
        # Extract a reasonable filename
        parts = url.rstrip("/").split("/")
        filename = parts[-1] if parts[-1] else "attachment"
        if "?" in filename:
            filename = filename.split("?")[0]
        
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.join(save_dir, filename)

        try:
            resp = self.session.get(full_url, timeout=60, stream=True)
            resp.raise_for_status()
            with open(save_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            logger.info(f"Downloaded: {filename} ({os.path.getsize(save_path)} bytes)")
            return save_path
        except Exception as e:
            logger.error(f"Download failed for {url}: {e}")
            return None

    def download_challenge_attachments(self, challenge_detail: Dict, save_dir: str) -> List[str]:
        """Download all attachments for a challenge. Returns list of local paths."""
        files = []
        context = challenge_detail.get("context", {}) or {}
        if context.get("url"):
            path = self.download_attachment(context["url"], save_dir)
            if path:
                files.append(path)
        return files

    # ── Notices (Platform Events) ─────────────────────────────────

    def get_notices(self, last_id: int = 0) -> tuple:
        """Poll game notices since last_id. Returns (next_cursor, [updates]).
        Each update: {id, time, type, message}
        """
        resp = self.session.get(
            urljoin(self.base_url, f"/api/game/{self.game_id}/notices"),
            timeout=15,
        )
        resp.raise_for_status()
        notices = resp.json() if isinstance(resp.json(), list) else []
        
        sorted_notices = sorted(notices, key=lambda n: n.get("id", 0))
        updates = []
        for notice in sorted_notices:
            if notice.get("id", 0) > last_id:
                values = notice.get("values", [])
                updates.append({
                    "id": notice["id"],
                    "time": notice.get("time", 0),
                    "type": notice.get("type", ""),
                    "message": " ".join(values) if values else "",
                })
        
        max_id = sorted_notices[-1]["id"] if sorted_notices else last_id
        return max_id, updates

    # ── Containers (Dynamic Challenges) ───────────────────────────

    def create_container(self, challenge_id: int) -> Optional[Dict]:
        """Create a container for a dynamic challenge."""
        resp = self.session.post(
            urljoin(self.base_url, f"/api/game/{self.game_id}/container/{challenge_id}"),
            timeout=30,
        )
        if resp.status_code in (200, 201):
            data = resp.json() if resp.text else {}
            entry = data.get("entry", "")
            logger.info(f"Container created for challenge {challenge_id}: {entry or 'no entry'}")
            return data
        logger.warning(f"Container create failed: {resp.status_code}")
        return None

    def wait_for_container_ready(self, challenge_id: int, timeout: int = 60) -> bool:
        """Poll until container is ready and entry point is available.
        Returns True if ready, False on timeout.
        """
        import socket
        deadline = time.time() + timeout
        while time.time() < deadline:
            # Re-fetch challenge detail to check container state
            detail = self.get_challenge_detail(challenge_id)
            context = detail.get("context", {}) or {}
            entry = context.get("instanceEntry")
            if entry:
                # Probe the port
                parts = entry.rsplit(":", 1)
                host = parts[0]
                port = int(parts[1]) if len(parts) > 1 else 80
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(3)
                    result = sock.connect_ex((host, port))
                    sock.close()
                    if result == 0:
                        logger.info(f"Container ready: {entry}")
                        return True
                except Exception:
                    pass
            time.sleep(3)
        logger.warning(f"Container not ready after {timeout}s for challenge {challenge_id}")
        return False

    def delete_container(self, challenge_id: int) -> bool:
        """Delete a container."""
        resp = self.session.delete(
            urljoin(self.base_url, f"/api/game/{self.game_id}/container/{challenge_id}"),
            timeout=15,
        )
        return resp.status_code in (200, 204)

    def extend_container(self, challenge_id: int) -> bool:
        """Extend container lifetime."""
        resp = self.session.post(
            urljoin(self.base_url, f"/api/game/{self.game_id}/container/{challenge_id}/extend"),
            timeout=15,
        )
        return resp.status_code == 200

    # ── Scoreboard & Submissions ──────────────────────────────────

    def get_scoreboard(self) -> List[Dict]:
        """Get current scoreboard."""
        resp = self.session.get(
            urljoin(self.base_url, f"/api/game/{self.game_id}/scoreboard"),
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def get_submissions(self) -> List[Dict]:
        """Get own team's submissions."""
        resp = self.session.get(
            urljoin(self.base_url, f"/api/game/{self.game_id}/submissions"),
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    # ── Health Check ──────────────────────────────────────────────

    def is_logged_in(self) -> bool:
        """Check if session is still valid."""
        try:
            resp = self.session.get(
                urljoin(self.base_url, "/api/account/profile"),
                timeout=10,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def health_check(self) -> Dict:
        """Full health check: login status, game access."""
        return {
            "logged_in": self.is_logged_in(),
            "game_id": self.game_id,
            "team_token": bool(self.team_token),
            "base_url": self.base_url,
        }


def load_config(config_path: str = None) -> Dict:
    """Load configuration from .env file."""
    config = {}
    if config_path and os.path.exists(config_path):
        with open(config_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    config[key.strip()] = val.strip()

    # Override with env vars
    for key in ["GZCTF_BASE_URL", "GZCTF_USERNAME", "GZCTF_PASSWORD",
                "GZCTF_GAME_ID", "GZCTF_TEAM_NAME", "POLL_INTERVAL",
                "MAX_ATTEMPTS", "CHALLENGE_TIMEOUT", "ATTACHMENT_DIR"]:
        if key in os.environ:
            config[key] = os.environ[key]

    return config
