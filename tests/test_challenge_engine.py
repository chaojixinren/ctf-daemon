"""
Tests for challenge_engine.py — flag extraction, challenge analysis, file categorization.
"""

import sys
import os
import tempfile
import json
from pathlib import Path

# Add parent dir to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from challenge_engine import (
    extract_flag,
    extract_flags,
    is_low_confidence_flag,
    ChallengeAnalyzer,
    build_solving_prompt,
    categorize_file,
    basic_file_analysis,
)


# ── Flag Extraction ────────────────────────────────────────────────

def test_extract_flag_dutctf():
    assert extract_flag("here is dutctf{test_flag_123} in text") == "dutctf{test_flag_123}"


def test_extract_flag_standard():
    assert extract_flag("flag{hello_world}") == "flag{hello_world}"


def test_extract_flag_gzctf():
    assert extract_flag("GZCTF{capture_the_flag_2026}") == "GZCTF{capture_the_flag_2026}"


def test_extract_flag_ctf():
    assert extract_flag("ctf{something}") == "ctf{something}"


def test_extract_flag_none():
    assert extract_flag("no flag here") is None


def test_extract_flag_empty():
    assert extract_flag("") is None


def test_extract_flags_multiple():
    text = "flag{first} and dutctf{second}"
    flags = extract_flags(text)
    assert "flag{first}" in flags
    assert "dutctf{second}" in flags


def test_extract_flags_dedup():
    text = "flag{dup} flag{dup} flag{dup}"
    flags = extract_flags(text)
    assert flags == ["flag{dup}"]


def test_extract_flags_filters_whitespace_in_braces():
    assert extract_flags("flag{hello world}") == []


def test_extract_flags_filters_quotes_in_braces():
    assert extract_flags('flag{hello"world}') == []


def test_extract_flags_filters_chinese_punctuation():
    assert extract_flags("flag{hello，world}") == []


# ── Low Confidence Detection ───────────────────────────────────────

def test_low_confidence_empty():
    assert is_low_confidence_flag("") is True


def test_low_confidence_empty_braces():
    assert is_low_confidence_flag("flag{}") is True


def test_low_confidence_placeholder():
    assert is_low_confidence_flag("flag{xxxx}") is True


def test_low_confidence_example():
    assert is_low_confidence_flag("flag{example_flag}") is True


def test_low_confidence_your_flag_here():
    assert is_low_confidence_flag("flag{your_flag_here}") is True


def test_low_confidence_repeated_char():
    assert is_low_confidence_flag("flag{aaaaaa}") is True


def test_low_confidence_real_flag():
    assert is_low_confidence_flag("flag{aB3_xY9}") is False


def test_low_confidence_dutctf_real():
    assert is_low_confidence_flag("dutctf{th1s_1s_r3al}") is False


# ── Challenge Analyzer ─────────────────────────────────────────────

def test_analyze_challenge_web_sqli():
    ch = {
        "id": 1, "title": "SQL Basics", "type": "DynamicContainer",
        "content": "Try some SQL injection to bypass the login",
        "hints": ["sqli detected"],
        "_category": "Web", "score": 100, "context": {},
    }
    analysis = ChallengeAnalyzer.analyze_challenge(ch)
    assert analysis["strategy"] in ("web_sqli_container", "web_sqli")
    assert analysis["category"] == "Web"
    assert analysis["has_container"] is True
    assert "sqlmap_scan" in analysis["recommended_tools"]


def test_analyze_challenge_web_ssti():
    ch = {
        "id": 2, "title": "Template Fun", "type": "StaticAttachment",
        "content": "SSTI - Server-Side Template Injection with Jinja2",
        "hints": [], "_category": "Web", "score": 200, "context": {"url": "/files/template.zip"},
    }
    analysis = ChallengeAnalyzer.analyze_challenge(ch)
    assert "ssti" in analysis["strategy"]
    assert analysis["has_attachment"] is True
    assert analysis["attachment_url"] == "/files/template.zip"


def test_analyze_challenge_pwn():
    ch = {
        "id": 3, "title": "Heap Master", "type": "StaticAttachment",
        "content": "Can you exploit this heap vulnerability?",
        "hints": ["malloc", "free", "use after free"],
        "_category": "Pwn", "score": 500, "context": {},
    }
    analysis = ChallengeAnalyzer.analyze_challenge(ch)
    assert analysis["strategy"] == "pwn_heap"
    assert "pwnpasi_auto_pwn" in analysis["recommended_tools"]


def test_analyze_challenge_crypto_rsa():
    ch = {
        "id": 4, "title": "RSA 101", "type": "StaticAttachment",
        "content": "We intercepted an RSA encrypted message. Can you factor the modulus?",
        "_category": "Crypto", "score": 150, "context": {},
    }
    analysis = ChallengeAnalyzer.analyze_challenge(ch)
    assert analysis["strategy"] == "crypto_rsa"


def test_analyze_challenge_misc_default():
    ch = {
        "id": 5, "title": "Weird Stuff", "type": "StaticAttachment",
        "content": "Something strange...", "_category": "Misc",
        "score": 50, "context": {},
    }
    analysis = ChallengeAnalyzer.analyze_challenge(ch)
    assert analysis["strategy"] == "general"


def test_analyze_challenge_flag_in_content():
    ch = {
        "id": 6, "title": "Free Flag", "type": "StaticAttachment",
        "content": "The flag is dutctf{free_points_here} enjoy!",
        "_category": "Misc", "score": 10, "context": {},
    }
    analysis = ChallengeAnalyzer.analyze_challenge(ch)
    assert "dutctf{free_points_here}" in analysis["flags_found"]


def test_analyze_challenge_hints_normalization():
    """Hints can be strings, dicts with content, or dicts with text."""
    ch = {
        "id": 7, "title": "Hint Test", "type": "StaticAttachment",
        "content": "Challenge with hints", "hints": [
            "plain hint",
            {"content": "dict with content"},
            {"text": "dict with text"},
            None,
        ],
        "_category": "Web", "score": 100, "context": {},
    }
    analysis = ChallengeAnalyzer.analyze_challenge(ch)
    assert "plain hint" in analysis["hints"]
    assert "dict with content" in analysis["hints"]
    assert "dict with text" in analysis["hints"]
    assert len(analysis["hints"]) == 3  # None filtered out


def test_determine_strategy_web_xxe():
    result = ChallengeAnalyzer._determine_strategy("Web", "StaticAttachment",
                                                    "XML External Entity injection", [])
    assert "xxe" in result


def test_determine_strategy_web_deserialization():
    result = ChallengeAnalyzer._determine_strategy("Web", "DynamicContainer",
                                                    "unserialize this pickle object injection!",
                                                    [])
    assert "deser" in result


def test_determine_strategy_forensics_memory():
    result = ChallengeAnalyzer._determine_strategy("Forensics", "StaticAttachment",
                                                    "Analyze this memory dump file",
                                                    ["volatility"])
    assert result == "forensics_memory"


def test_determine_strategy_reverse_android():
    result = ChallengeAnalyzer._determine_strategy("Reverse", "StaticAttachment",
                                                    "Android APK reverse engineering",
                                                    [])
    assert result == "reverse_mobile"


# ── Solving Prompt ─────────────────────────────────────────────────

def test_build_solving_prompt():
    analysis = {
        "id": 1, "title": "Test Challenge", "category": "Web",
        "type": "StaticAttachment", "score": 100, "strategy": "web_sqli",
        "content_preview": "Find the flag via SQLi",
        "hints": ["try UNION SELECT"],
        "has_container": False,
    }
    prompt = build_solving_prompt(analysis, ["/tmp/test.zip"])
    assert "Test Challenge" in prompt
    assert "web_sqli" in prompt
    assert "UNION SELECT" in prompt
    assert "/tmp/test.zip" in prompt


# ── File Categorization ────────────────────────────────────────────

def test_categorize_file_elf():
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        f.write(b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 100)
        path = f.name
    try:
        result = categorize_file(path)
        assert result == "binary_elf"
    finally:
        os.unlink(path)


def test_categorize_file_text():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("Hello, world! This is a test.\n")
        path = f.name
    try:
        result = categorize_file(path)
        assert result in ("text", "unknown")  # 'file' may vary
    finally:
        os.unlink(path)


def test_categorize_file_python_bytecode():
    """Extension-based check: .pyc files return 'python_bytecode' regardless of 'file' output."""
    # Minimal data — 'file' will say 'data', but .pyc extension should still win
    with tempfile.NamedTemporaryFile(suffix=".pyc", delete=False) as f:
        f.write(b"\x00" * 16)
        path = f.name
    try:
        result = categorize_file(path)
        assert result == "python_bytecode"
    finally:
        os.unlink(path)


# ── Basic File Analysis ────────────────────────────────────────────

def test_basic_file_analysis_small_file():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("hello world\nflag{test_in_strings}\nmore text\n")
        path = f.name
    try:
        result = basic_file_analysis(path)
        assert result["type"] in ("text", "unknown")
        assert result["size"] > 0
        assert "flag{test_in_strings}" in result.get("flags_in_strings", [])
    finally:
        os.unlink(path)


def test_basic_file_analysis_no_flag():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("just some normal text, nothing to see here\n")
        path = f.name
    try:
        result = basic_file_analysis(path)
        assert "flags_in_strings" not in result
    finally:
        os.unlink(path)


if __name__ == "__main__":
    # Simple manual runner
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
