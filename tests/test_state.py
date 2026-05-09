"""
Tests for state.py — persistent state load/save.
"""

import sys
import json
import tempfile
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from state import load_state, save_state, STATE_FILE


class TestStateRoundtrip:
    """Save state, load it back, verify all fields preserved."""

    def setup_method(self):
        self._saved_path = str(STATE_FILE)
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        self.tmp_path = self.tmp.name
        self.tmp.close()

    def teardown_method(self):
        if os.path.exists(self.tmp_path):
            os.unlink(self.tmp_path)

    def _run(self, fn):
        # Patch STATE_FILE for this test
        import state
        original = state.STATE_FILE
        state.STATE_FILE = Path(self.tmp_path)
        try:
            fn()
        finally:
            state.STATE_FILE = original

    def test_fresh_state_has_all_fields(self):
        def body():
            s = load_state()
            assert "solved" in s
            assert "retry_counts" in s
            assert "cooldown_until" in s
            assert "permanently_failed" in s
            assert "attempt_history" in s
            assert "slots" in s
            assert "current" in s
            assert "start_time" in s
            assert s["solved"] == {}
            assert s["slots"] == {}
            assert s["current"] is None
        self._run(body)

    def test_save_and_load_preserves_data(self):
        def body():
            s = load_state()
            s["solved"]["1"] = "flag{test}"
            s["retry_counts"]["1"] = 2
            s["cooldown_until"]["2"] = 9999999999
            s["slots"]["0"] = {"challenge_id": 1, "assigned_at": 1234567890}
            save_state(s)

            s2 = load_state()
            assert s2["solved"]["1"] == "flag{test}"
            assert s2["retry_counts"]["1"] == 2
            assert s2["cooldown_until"]["2"] == 9999999999
            assert s2["slots"]["0"]["challenge_id"] == 1
        self._run(body)

    def test_corrupted_state_file_returns_default(self):
        def body():
            with open(self.tmp_path, "w") as f:
                f.write("this is not json {{{")
            s = load_state()
            assert s["solved"] == {}
            assert s["slots"] == {}
        self._run(body)

    def test_missing_state_file_returns_default(self):
        def body():
            if os.path.exists(self.tmp_path):
                os.unlink(self.tmp_path)
            s = load_state()
            assert s["solved"] == {}
        self._run(body)

    def test_backfill_old_state(self):
        """State file missing v3 fields should get them backfilled."""
        def body():
            old_state = {
                "solved": {"1": "flag{old}"},
                # Missing: retry_counts, cooldown_until, slots, attempt_history, etc.
            }
            with open(self.tmp_path, "w") as f:
                json.dump(old_state, f)
            s = load_state()
            assert s["solved"]["1"] == "flag{old}"
            assert s["retry_counts"] == {}
            assert s["slots"] == {}
            assert s["attempt_history"] == {}
        self._run(body)

    def test_env_var_overrides_path(self):
        """CTF_STATE_FILE env var should override default path."""
        import state as _st
        original_env = os.environ.get("CTF_STATE_FILE")
        original_state_file = _st.STATE_FILE
        try:
            os.environ["CTF_STATE_FILE"] = "/tmp/custom_ctf_state.json"
            import importlib
            importlib.reload(_st)
            assert str(_st.STATE_FILE) == "/tmp/custom_ctf_state.json"
        finally:
            if original_env:
                os.environ["CTF_STATE_FILE"] = original_env
            else:
                os.environ.pop("CTF_STATE_FILE", None)
            # Restore original
            _st.STATE_FILE = original_state_file


if __name__ == "__main__":
    import state as _st
    passed = 0
    failed = 0
    test_suite = TestStateRoundtrip()
    for name in dir(test_suite):
        if name.startswith("test_"):
            try:
                test_suite.setup_method()
                getattr(test_suite, name)()
                test_suite.teardown_method()
                passed += 1
                print(f"  PASS {name}")
            except Exception as e:
                test_suite.teardown_method()
                failed += 1
                print(f"  FAIL {name}: {e}")
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
