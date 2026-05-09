"""
Tests for gzctf_client.py — submit_flag response parsing, flag status polling, config loading.
Uses unittest.mock to avoid real network calls.
"""

import sys
import json
import tempfile
import os
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from gzctf_client import GZCTFClient, load_config


# ── Submit Flag Response Parsing ───────────────────────────────────

def _make_client():
    """Create a GZCTFClient with a mocked session."""
    client = GZCTFClient("http://localhost:8080", "test", "password")
    client.session = MagicMock()
    client.game_id = 2
    return client


def test_submit_flag_returns_submit_id():
    """Server returns '\"5\"' — a JSON-quoted submit ID."""
    client = _make_client()
    resp = MagicMock()
    resp.status_code = 200
    resp.text = '"5"'
    client.session.post.return_value = resp

    result = client.submit_flag(1, "flag{test}")
    assert result == 5


def test_submit_flag_plain_int():
    """Server returns '5' — a plain int submit ID (no JSON quoting)."""
    client = _make_client()
    resp = MagicMock()
    resp.status_code = 200
    resp.text = "5"
    client.session.post.return_value = resp

    result = client.submit_flag(1, "flag{test}")
    assert result == 5


def test_submit_flag_direct_accept():
    """Server returns '\"Accepted\"' — direct status, no submit ID."""
    client = _make_client()
    resp = MagicMock()
    resp.status_code = 200
    resp.text = '"Accepted"'
    client.session.post.return_value = resp

    result = client.submit_flag(1, "flag{test}")
    assert result == 0  # Special: direct accept


def test_submit_flag_direct_solved():
    """Server returns '\"Solved\"' — another direct accept variant."""
    client = _make_client()
    resp = MagicMock()
    resp.status_code = 200
    resp.text = '"Solved"'
    client.session.post.return_value = resp

    result = client.submit_flag(1, "flag{test}")
    assert result == 0


def test_submit_flag_http_error():
    """Server returns 400 — submission failed."""
    client = _make_client()
    resp = MagicMock()
    resp.status_code = 400
    resp.text = "Bad Request"
    client.session.post.return_value = resp

    result = client.submit_flag(1, "flag{test}")
    assert result == -1


# ── Flag Status Polling ────────────────────────────────────────────

def test_check_flag_status_accepted():
    client = _make_client()
    resp = MagicMock()
    resp.status_code = 200
    resp.text = "Accepted"
    client.session.get.return_value = resp

    status = client.check_flag_status(1, 5, poll=False)
    assert status == "Accepted"


def test_check_flag_status_wrong_answer():
    client = _make_client()
    resp = MagicMock()
    resp.status_code = 200
    resp.text = '"WrongAnswer"'  # JSON-quoted
    client.session.get.return_value = resp

    status = client.check_flag_status(1, 5, poll=False)
    assert status == "WrongAnswer"


def test_check_flag_status_json_quoted():
    """Status is JSON-quoted, should be stripped."""
    client = _make_client()
    resp = MagicMock()
    resp.status_code = 200
    resp.text = '"Correct"'
    client.session.get.return_value = resp

    status = client.check_flag_status(1, 5, poll=False)
    assert status == "Correct"


def test_check_flag_status_pending():
    """Non-final status without polling returns the raw status."""
    client = _make_client()
    resp = MagicMock()
    resp.status_code = 200
    resp.text = "Pending"
    client.session.get.return_value = resp

    status = client.check_flag_status(1, 5, poll=False)
    assert status == "Pending"


def test_check_flag_status_unknown_on_non_200():
    """Non-200 without polling returns 'Unknown'."""
    client = _make_client()
    resp = MagicMock()
    resp.status_code = 404
    resp.text = "Not Found"
    client.session.get.return_value = resp

    status = client.check_flag_status(1, 5, poll=False)
    assert status == "Unknown"


# ── Config Loading ─────────────────────────────────────────────────

def test_load_config_basic():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write("""
GZCTF_BASE_URL=http://ctf.example.com:8080
GZCTF_USERNAME=player1
GZCTF_PASSWORD=secret
GZCTF_GAME_ID=5
# This is a comment
POLL_INTERVAL=15
""")
        path = f.name
    try:
        config = load_config(path)
        assert config["GZCTF_BASE_URL"] == "http://ctf.example.com:8080"
        assert config["GZCTF_USERNAME"] == "player1"
        assert config["GZCTF_PASSWORD"] == "secret"
        assert config["GZCTF_GAME_ID"] == "5"
        assert config["POLL_INTERVAL"] == "15"
    finally:
        os.unlink(path)


def test_load_config_missing_file():
    config = load_config("/nonexistent/config.env")
    assert config == {}


def test_load_config_env_var_override():
    os.environ["GZCTF_BASE_URL"] = "http://override:9999"
    os.environ["GZCTF_GAME_ID"] = "99"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write("GZCTF_BASE_URL=http://file:8080\nGZCTF_USERNAME=fileuser\n")
        path = f.name
    try:
        config = load_config(path)
        assert config["GZCTF_BASE_URL"] == "http://override:9999"  # env overrides
        assert config["GZCTF_GAME_ID"] == "99"
        assert config["GZCTF_USERNAME"] == "fileuser"  # not overridden
    finally:
        os.unlink(path)
        os.environ.pop("GZCTF_BASE_URL", None)
        os.environ.pop("GZCTF_GAME_ID", None)


# ── ACCEPTED_STATUSES / FINISHED_STATUSES ──────────────────────────

def test_accepted_statuses_includes_common():
    client = _make_client()
    assert "Correct" in client.ACCEPTED_STATUSES
    assert "Accepted" in client.ACCEPTED_STATUSES
    assert "Solved" in client.ACCEPTED_STATUSES


def test_finished_statuses_superset():
    client = _make_client()
    assert "WrongAnswer" in client.FINISHED_STATUSES
    assert "AlreadySolved" in client.FINISHED_STATUSES
    assert "TooManyAttempts" in client.FINISHED_STATUSES


if __name__ == "__main__":
    passed = 0
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                passed += 1
                print(f"  PASS {name}")
            except Exception as e:
                failed += 1
                print(f"  FAIL {name}: {e}")
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
