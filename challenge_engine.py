"""
Challenge Engine - Analyze and solve CTF challenges autonomously.
Integrates with Kali MCP tools for actual exploitation.
"""

import os
import re
import json
import subprocess
import logging
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Tuple

logger = logging.getLogger("challenge_engine")

# ── Flag Rescue Cache ─────────────────────────────────────────────

from state import RESCUE_FLAGS_PATH

_RESCUE_FLAGS_LOCK = threading.Lock()
_RESCUED_FLAGS: set[tuple[int, str]] = set()  # (challenge_id, flag)


def load_rescue_flags() -> list[tuple[int, str]]:
    """Load previously cached flags that need retrying."""
    flags = []
    try:
        if RESCUE_FLAGS_PATH.exists():
            for line in RESCUE_FLAGS_PATH.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split("\t", 2)
                if len(parts) >= 2:
                    try:
                        ch_id = int(parts[0])
                        flag = parts[1]
                        flags.append((ch_id, flag))
                    except ValueError:
                        pass
    except Exception:
        pass
    return flags


def save_rescue_flag(challenge_id: int, flag: str, reason: str = "") -> str:
    """Save a flag that couldn't be submitted for later retry."""
    key = (challenge_id, flag.strip())
    with _RESCUE_FLAGS_LOCK:
        if key in _RESCUED_FLAGS:
            return str(RESCUE_FLAGS_PATH)
        _RESCUED_FLAGS.add(key)
        try:
            RESCUE_FLAGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().astimezone().isoformat(timespec="seconds")
            with RESCUE_FLAGS_PATH.open("a") as f:
                f.write(f"{challenge_id}\t{flag}\t{reason}\t{ts}\n")
            logger.info(f"[Rescue] Flag cached for retry: challenge={challenge_id}")
        except Exception as e:
            logger.warning(f"[Rescue] Failed to cache flag: {e}")
    return str(RESCUE_FLAGS_PATH)


def remove_rescue_flag(challenge_id: int, flag: str):
    """Remove a flag from rescue cache after successful submission."""
    key = (challenge_id, flag.strip())
    with _RESCUE_FLAGS_LOCK:
        _RESCUED_FLAGS.discard(key)


# ── Flag Patterns ─────────────────────────────────────────────────

FLAG_PATTERNS = [
    r"dutctf\{[^}]+\}",
    r"DUTCTF\{[^}]+\}",
    r"GZCTF\{[^}]+\}",
    r"gzctf\{[^}]+\}",
    r"flag\{[^}]+\}",
    r"FLAG\{[^}]+\}",
    r"Flag\{[^}]+\}",
    r"CTF\{[^}]+\}",
    r"ctf\{[^}]+\}",
    r"flag\{.*?\}",
    r"[A-Za-z0-9_\-]{20,}\{[^}]+\}",  # Generic xxx{...}
]

# Low-confidence flag substrings (placeholder/example patterns)
_LOW_CONFIDENCE_SUBSTRINGS = (
    "xxxx", "yyyy", "zzzz", "xxx", "yyy",
    "keya_keyb_keyc", "keya:", "keyb:", "keyc:",
    "placeholder", "example", "sample",
    "your_flag_here", "put_your_flag",
)

def extract_flag(text: str) -> Optional[str]:
    """Extract flag from text using regex patterns."""
    for pattern in FLAG_PATTERNS:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            return matches[0]
    return None

def extract_flags(text: str) -> List[str]:
    """Extract ALL flags from text, filtering low-confidence placeholders."""
    found = set()
    for pattern in FLAG_PATTERNS:
        for m in re.findall(pattern, text, re.IGNORECASE):
            candidate = m.strip()
            # Skip flags with whitespace, quotes, or non-ASCII inside braces
            inner = candidate[candidate.find("{") + 1:-1]
            if any(ch.isspace() for ch in inner):
                continue
            if any(ch in inner for ch in ['"', "'", "，", "。", "：", "；"]):
                continue
            if is_low_confidence_flag(candidate):
                continue
            found.add(candidate)
    return list(found)


def is_low_confidence_flag(flag: str) -> bool:
    """Check if a flag looks like a placeholder/example rather than a real flag."""
    normalized = (flag or "").strip()
    if not normalized:
        return True
    lower = normalized.lower()
    # Must start with known prefix and end with }
    if not (lower.startswith(("flag{", "ctf{", "dutctf{", "gzctf{")) and lower.endswith("}")):
        return False  # let generic pattern hits through
    body = lower[lower.find("{") + 1:-1]
    if not body:
        return True
    # Check for placeholder substrings
    if any(token in body for token in _LOW_CONFIDENCE_SUBSTRINGS):
        return True
    # Single char repeated 6+ times
    if len(body) >= 6 and len(set(body)) == 1:
        return True
    return False


class ChallengeAnalyzer:
    """Analyzes challenge details and determines solving strategy."""

    # Category → Tools mapping
    CATEGORY_TOOLS = {
        "Web": [
            "nuclei_scan", "sqlmap_scan", "ffuf_scan", "gobuster_scan",
            "nikto_scan", "whatweb_scan", "wpscan_scan", "joomscan_scan",
            "dirb_scan", "burpsuite_scan", "xsstrike_scan",
            "commix_scan", "lfi_scan", "ssrf_scan",
        ],
        "Pwn": [
            "quick_pwn_check", "pwnpasi_auto_pwn", "checksec_analyze",
            "radare2_analyze_binary", "ghidra_analyze_binary",
            "rop_gadget_finder", "one_gadget_finder",
        ],
        "Crypto": [
            # Mostly manual analysis with Python
        ],
        "Reverse": [
            "auto_reverse_analyze", "radare2_analyze_binary",
            "ghidra_analyze_binary", "strings_extract",
        ],
        "Forensics": [
            "binwalk_analyze", "foremost_extract", "volatility_analyze",
            "exiftool_analyze", "stego_toolkit",
        ],
        "Misc": [
            "comprehensive_recon", "ai_analyze_intent",
        ],
    }

    @classmethod
    def _normalize_hints(cls, hints) -> List[str]:
        """Normalize hints — GZCTF may return strings or {content, text} objects."""
        if not hints:
            return []
        result = []
        for h in hints:
            if h is None:
                continue
            if isinstance(h, str):
                result.append(h)
            elif isinstance(h, dict):
                result.append(h.get("content") or h.get("text") or "")
            else:
                result.append(str(h))
        return [h for h in result if h]

    @classmethod
    def analyze_challenge(cls, detail: Dict) -> Dict:
        """Analyze a challenge and return recommended actions."""
        category = detail.get("_category", detail.get("category", "Misc"))
        ch_type = detail.get("type", "StaticAttachment")
        title = detail.get("title", "Unknown")
        content = detail.get("content", "")
        hints = cls._normalize_hints(detail.get("hints", []))
        context = detail.get("context", {}) or {}
        score = detail.get("score", 0)

        analysis = {
            "id": detail.get("id"),
            "title": title,
            "category": category,
            "type": ch_type,
            "score": score,
            "has_attachment": bool(context.get("url")),
            "attachment_url": context.get("url"),
            "has_container": ch_type in ("StaticContainer", "DynamicContainer"),
            "container_entry": context.get("instanceEntry"),
            "hints": hints,
            "content_preview": content[:500] if content else "",
            "recommended_tools": cls.CATEGORY_TOOLS.get(category, []),
            "strategy": cls._determine_strategy(category, ch_type, content, hints),
            "flags_found": [],
        }

        # Check if flag is already in content (Misc challenges sometimes embed it)
        if content:
            found = extract_flags(content)
            if found:
                analysis["flags_found"] = found

        return analysis

    @classmethod
    def _determine_strategy(cls, category: str, ch_type: str, 
                            content: str, hints: List[str]) -> str:
        """Determine the solving strategy based on challenge characteristics."""
        
        content_lower = (content + " " + " ".join(hints)).lower()
        
        # Container challenges have additional container management
        # but the attack vector is still determined by content
        is_container = ch_type in ("DynamicContainer", "StaticContainer")
        
        if category == "Web":
            if any(kw in content_lower for kw in ["ssti", "template injection", "jinja", "twig", "flask template"]):
                return "web_ssti_container" if is_container else "web_ssti"
            elif any(kw in content_lower for kw in ["xxe", "xml external entity"]):
                return "web_xxe_container" if is_container else "web_xxe"
            elif any(kw in content_lower for kw in ["sql injection", "sqli"]):
                return "web_sqli_container" if is_container else "web_sqli"
            elif any(kw in content_lower for kw in ["xss", "cross-site"]):
                return "web_xss_container" if is_container else "web_xss"
            elif any(kw in content_lower for kw in ["command injection", "rce", "code exec", "os command"]):
                return "web_rce_container" if is_container else "web_rce"
            elif any(kw in content_lower for kw in ["file inclusion", "lfi", "path traversal", "local file"]):
                return "web_lfi_container" if is_container else "web_lfi"
            elif any(kw in content_lower for kw in ["ssrf", "server-side request"]):
                return "web_ssrf_container" if is_container else "web_ssrf"
            elif any(kw in content_lower for kw in ["file upload"]):
                return "web_upload_container" if is_container else "web_upload"
            elif any(kw in content_lower for kw in ["deserializ", "unserialize", "pickle", "object injection"]):
                return "web_deser_container" if is_container else "web_deser"
            else:
                return "web_container" if is_container else "web_general"

        elif category == "Pwn":
            if any(kw in content_lower for kw in ["format", "printf"]):
                return "pwn_format_string"
            elif any(kw in content_lower for kw in ["heap", "malloc", "free"]):
                return "pwn_heap"
            elif any(kw in content_lower for kw in ["rop", "gadget"]):
                return "pwn_rop"
            elif any(kw in content_lower for kw in ["shellcode", "nop"]):
                return "pwn_shellcode"
            else:
                return "pwn_general"

        elif category == "Crypto":
            if any(kw in content_lower for kw in ["rsa", "factor", "modulus"]):
                return "crypto_rsa"
            elif any(kw in content_lower for kw in ["aes", "cbc", "ecb", "ctr", "block"]):
                return "crypto_symmetric"
            elif any(kw in content_lower for kw in ["ecc", "elliptic", "ecdsa"]):
                return "crypto_ecc"
            elif any(kw in content_lower for kw in ["hash", "md5", "sha", "collision"]):
                return "crypto_hash"
            elif any(kw in content_lower for kw in ["classical", "caesar", "vigenere", "base"]):
                return "crypto_classical"
            else:
                return "crypto_general"

        elif category == "Reverse":
            if any(kw in content_lower for kw in ["android", "apk", "mobile"]):
                return "reverse_mobile"
            elif any(kw in content_lower for kw in [".net", "c#", "il"]):
                return "reverse_dotnet"
            elif any(kw in content_lower for kw in ["python", "pyc", "py"]):
                return "reverse_python"
            else:
                return "reverse_binary"

        elif category == "Forensics":
            if any(kw in content_lower for kw in ["memory dump", "volatility", "mem dump", "ram dump"]):
                return "forensics_memory"
            elif any(kw in content_lower for kw in ["disk image", "dd image", "raw image", "img file"]):
                return "forensics_disk"
            elif any(kw in content_lower for kw in ["pcap", "traffic", "network capture", "wireshark"]):
                return "forensics_network"
            elif any(kw in content_lower for kw in ["stego", "steg", "lsb", "hidden data", "watermark"]):
                return "forensics_stego"
            else:
                return "forensics_general"

        else:
            return "general"


def build_solving_prompt(analysis: Dict, attachment_paths: List[str] = None) -> str:
    """Build a detailed solving prompt for the AI agent."""
    lines = [
        f"## CTF Challenge: {analysis['title']}",
        f"**Category**: {analysis['category']}",
        f"**Type**: {analysis['type']}",
        f"**Score**: {analysis['score']}",
        f"**Strategy**: {analysis['strategy']}",
        f"**Challenge ID**: {analysis['id']}",
        "",
    ]

    if analysis["content_preview"]:
        lines.append("### Description")
        lines.append(analysis["content_preview"])
        lines.append("")

    if analysis["hints"]:
        lines.append("### Hints")
        for h in analysis["hints"]:
            lines.append(f"- {h}")
        lines.append("")

    if attachment_paths:
        lines.append("### Attachments Downloaded")
        for p in attachment_paths:
            size = os.path.getsize(p) if os.path.exists(p) else 0
            lines.append(f"- `{p}` ({size} bytes)")
        lines.append("")

    if analysis["has_container"] and analysis["container_entry"]:
        lines.append(f"### Container: {analysis['container_entry']}")
        lines.append("")

    lines.append("### Task")
    lines.append("Solve this challenge and extract the flag. The flag format is `dutctf{...}` or `flag{...}`.")
    lines.append("Use all available analysis tools. Be thorough. Think step by step.")
    lines.append("")
    lines.append("Output ONLY the flag when found, in format: `FLAG: dutctf{...}`")

    return "\n".join(lines)


def categorize_file(filepath: str) -> str:
    """Categorize a file by its type (using file command + extension)."""
    try:
        result = subprocess.run(
            ["file", "-b", filepath],
            capture_output=True, text=True, timeout=10
        )
        output = result.stdout.lower()

        # Extension-based checks (independent of file output)
        if filepath.endswith(".pyc"):
            return "python_bytecode"

        # Content-based checks
        if "elf" in output:
            return "binary_elf"
        elif "pe32" in output or "pe64" in output:
            return "binary_pe"
        elif "zip" in output or "archive" in output:
            return "archive"
        elif "python" in output:
            return "python_bytecode"
        elif "pcap" in output:
            return "pcap"
        elif "image" in output or "png" in output or "jpeg" in output:
            return "image"
        elif "text" in output:
            return "text"
        elif "json" in output:
            return "json"
        elif "pdf" in output:
            return "pdf"
        else:
            return "unknown"
    except Exception:
        return "unknown"


def basic_file_analysis(filepath: str) -> Dict:
    """Perform basic automated analysis on a file."""
    results = {
        "path": filepath,
        "size": os.path.getsize(filepath),
        "type": categorize_file(filepath),
        "strings_summary": "",
        "hex_preview": "",
    }

    # Extract strings
    try:
        strings_out = subprocess.run(
            ["strings", filepath],
            capture_output=True, text=True, timeout=30
        ).stdout
        # Look for flags in strings
        flags = extract_flags(strings_out)
        if flags:
            results["flags_in_strings"] = flags
        # Show first 2000 chars
        results["strings_summary"] = strings_out[:2000]
    except Exception:
        pass

    # Hex preview for small files
    if results["size"] < 1024 * 100:
        try:
            hex_out = subprocess.run(
                ["xxd", filepath, "-l", "256"],
                capture_output=True, text=True, timeout=10
            ).stdout
            results["hex_preview"] = hex_out
        except Exception:
            pass

    return results
