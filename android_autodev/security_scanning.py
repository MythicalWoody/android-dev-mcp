"""Fail-closed secret detection for added lines in review diffs.

Findings never contain the suspected credential.  The scanner reports only a
rule, location, and one-way fingerprint so calling clients cannot accidentally
copy a secret into chat, logs, or review state.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_SECRET_PATTERNS = (
    ("private-key", re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("github-token", re.compile(r"\b(?:gh[opusr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{50,255})\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,200}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
)
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?key|auth[_-]?token|client[_-]?secret|password|passwd|secret|token)\b"
    r"\s*(?::|=)\s*[\"']([^\"']{8,})[\"']"
)
_UNQUOTED_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?key|auth[_-]?token|client[_-]?secret|password|passwd|secret|token)\b"
    r"\s*(?::|=)\s*([A-Za-z0-9_+/=-]{8,})\s*(?:#.*)?$"
)
_QUOTED_VALUE_RE = re.compile(r"[\"']([A-Za-z0-9_+/=-]{32,})[\"']")
_PLACEHOLDER_MARKERS = (
    "example",
    "sample",
    "dummy",
    "test",
    "fake",
    "placeholder",
    "changeme",
    "redacted",
    "your_",
    "your-",
    "xxxx",
)


def _entropy(value: str) -> float:
    """Calculate Shannon entropy to identify unlabelled credential-like values."""
    counts = Counter(value)
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _is_placeholder(value: str) -> bool:
    """Exclude obvious documentation placeholders without weakening real-token rules."""
    lowered = value.lower()
    return (
        any(marker in lowered for marker in _PLACEHOLDER_MARKERS)
        or "${" in value
        or value.startswith("<")
        or len(set(value)) <= 4
    )


def _fingerprint(value: str) -> str:
    """Create a stable identifier that cannot reveal the matched credential."""
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:12]


def _line_findings(content: str) -> list[tuple[str, str]]:
    """Return unique rule/value pairs found on one added source line."""
    matches: list[tuple[str, str]] = []
    for rule, pattern in _SECRET_PATTERNS:
        for match in pattern.finditer(content):
            matches.append((rule, match.group(0)))

    for match in _ASSIGNMENT_RE.finditer(content):
        value = match.group(1)
        if not _is_placeholder(value):
            matches.append(("credential-assignment", value))
    for match in _UNQUOTED_ASSIGNMENT_RE.finditer(content):
        value = match.group(1)
        if not _is_placeholder(value):
            matches.append(("credential-assignment", value))

    # Entropy catches opaque vendor tokens that have no recognizable prefix.
    # Requiring mixed character classes and a high threshold limits noisy hits.
    for match in _QUOTED_VALUE_RE.finditer(content):
        value = match.group(1)
        classes = sum(
            bool(pattern.search(value))
            for pattern in (re.compile(r"[a-z]"), re.compile(r"[A-Z]"), re.compile(r"\d"), re.compile(r"[_+/=-]"))
        )
        if not _is_placeholder(value) and classes >= 3 and _entropy(value) >= 4.2:
            matches.append(("high-entropy-string", value))

    # A value may match a vendor rule and the entropy heuristic; one finding is
    # enough to block the change and avoids overwhelming the reviewer.
    unique: dict[str, tuple[str, str]] = {}
    for rule, value in matches:
        unique.setdefault(_fingerprint(value), (rule, value))
    return list(unique.values())


def scan_diff(proposed_diff: str) -> list[dict[str, object]]:
    """Scan only added patch lines and return redacted, source-located findings."""
    findings: list[dict[str, object]] = []
    current_path = "unknown"
    new_line_number = 0
    in_hunk = False

    for line in proposed_diff.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            current_path = parts[3][2:] if len(parts) == 4 and parts[3].startswith("b/") else "unknown"
            in_hunk = False
            continue
        hunk = _HUNK_RE.match(line)
        if hunk:
            new_line_number = int(hunk.group(1))
            in_hunk = True
            continue
        if not in_hunk or line.startswith("\\ No newline at end of file"):
            continue
        if line.startswith("+") and not line.startswith("+++"):
            for rule, value in _line_findings(line[1:]):
                findings.append(
                    {
                        "rule": rule,
                        "path": current_path,
                        "line": new_line_number,
                        "fingerprint": _fingerprint(value),
                    }
                )
            new_line_number += 1
        elif not line.startswith("-"):
            new_line_number += 1
    return findings


def require_clean_diff(proposed_diff: str) -> list[dict[str, object]]:
    """Raise a redacted error when a review diff contains suspected secrets."""
    findings = scan_diff(proposed_diff)
    if findings:
        locations = ", ".join(
            f"{item['path']}:{item['line']} ({item['rule']}, {item['fingerprint']})"
            for item in findings[:10]
        )
        suffix = "" if len(findings) <= 10 else f" and {len(findings) - 10} more"
        raise ValueError(
            "Secret scan blocked this diff. Remove the suspected credential(s) and use "
            f"environment or secret storage instead: {locations}{suffix}."
        )
    return findings
