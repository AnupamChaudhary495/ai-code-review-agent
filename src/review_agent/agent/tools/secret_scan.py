"""Deterministic hardcoded-secret scanner — a non-LLM pre-filter.

High-confidence secrets (recognizable provider key shapes, private-key headers)
must never depend on a model noticing them, so this runs alongside the LLM
security pass and its hits are always included in the result.

Design bias: recall on real credential shapes, but low false positives — the
generic "secret-named assignment" heuristic is gated on a secret-suggesting
identifier AND a high-entropy value that is not a UUID, plain hash, or
placeholder, because a false `critical` erodes trust faster than a miss.
"""

import math
import re
from collections.abc import Iterator

from ...diffing.models import FileChange
from ...schemas.finding import Finding

# Distinctive provider secret shapes — the prefix/format alone is enough to be
# confident, so these are always flagged (a UUID or hash cannot match them).
_JWT = r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"
_PEM = r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"

# (label, compiled pattern)
_PROVIDER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("AWS access key ID", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("GitHub fine-grained PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}\b")),
    ("Stripe secret key", re.compile(r"\bsk_(?:live|test)_[0-9A-Za-z]{16,}\b")),
    ("Google OAuth access token", re.compile(r"\bya29\.[0-9A-Za-z_\-]{20,}\b")),
    ("JSON Web Token", re.compile(_JWT)),
    ("private key block", re.compile(_PEM)),
    ("Slack webhook URL", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_\-]{20,}")),
]

# Generic "NAME = 'value'" / "NAME: 'value'" / '"NAME": "value"' where the
# identifier suggests a secret. Value captured as group 'val'.
_SECRET_NAME = (
    r"(?:secret|token|passwd|password|pwd|api[_\-]?key|access[_\-]?key|"
    r"private[_\-]?key|client[_\-]?secret|auth[_\-]?token|credentials?)"
)
_ASSIGNMENT = re.compile(
    r"""['"]?[A-Za-z0-9_\-]*""" + _SECRET_NAME + r"""[A-Za-z0-9_\-]*['"]?"""
    r"""\s*[:=]\s*['"](?P<val>[^'"]{12,})['"]""",
    re.IGNORECASE,
)

# Values that look like a secret shape but are common false positives.
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_PLAIN_HASH = re.compile(r"^[0-9a-fA-F]{32}$|^[0-9a-fA-F]{40}$|^[0-9a-fA-F]{64}$")
_PLACEHOLDER = re.compile(
    r"(?i)(your[_\-]?|example|changeme|change_me|placeholder|dummy|redacted|"
    r"xxx+|\.\.\.|<[^>]+>|\$\{[^}]+\}|os\.environ|getenv|process\.env|null|none)"
)
_MIN_ENTROPY_BITS_PER_CHAR = 3.0


def _shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(value)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _iter_added_lines(change: FileChange) -> Iterator[tuple[int, str]]:
    """Yield (new-file line number, added-line text) for each added ('+') line."""
    for hunk in change.hunks:
        new_line = hunk.new_start
        for raw in hunk.lines:
            if raw.startswith("+"):
                yield new_line, raw[1:]
                new_line += 1
            elif raw.startswith(" ") or raw == "":
                new_line += 1
            # removed lines and "\ No newline" markers: no new-file line


def _looks_like_real_secret(value: str) -> bool:
    if _UUID.match(value) or _PLAIN_HASH.match(value):
        return False
    if _PLACEHOLDER.search(value):
        return False
    return _shannon_entropy(value) >= _MIN_ENTROPY_BITS_PER_CHAR


def scan_file(change: FileChange) -> list[Finding]:
    """Return one critical security Finding per hardcoded secret in added lines."""
    findings: list[Finding] = []
    for line_no, text in _iter_added_lines(change):
        matched_spans: list[tuple[int, int]] = []

        for label, pattern in _PROVIDER_PATTERNS:
            for m in pattern.finditer(text):
                matched_spans.append(m.span())
                findings.append(
                    Finding(
                        file=change.path,
                        line=line_no,
                        category="security",
                        severity="critical",
                        message=(
                            f"Hardcoded {label} detected in source. Anyone with "
                            "repository read access obtains a live credential; treat it "
                            "as compromised."
                        ),
                        suggestion=(
                            "Remove the secret from source, load it from an environment "
                            "variable or secret manager, and rotate the exposed value."
                        ),
                        cwe="CWE-798",
                    )
                )

        for m in _ASSIGNMENT.finditer(text):
            # Skip if this assignment's value was already caught by a provider
            # pattern on the same line (avoid duplicate finding for one secret).
            if any(s <= m.start("val") < e for s, e in matched_spans):
                continue
            value = m.group("val")
            if _looks_like_real_secret(value):
                findings.append(
                    Finding(
                        file=change.path,
                        line=line_no,
                        category="security",
                        severity="critical",
                        message=(
                            "A secret appears to be hardcoded in a credential-named "
                            "assignment. Committed secrets are exposed to anyone with "
                            "repository read access."
                        ),
                        suggestion=(
                            "Remove the secret from source, load it from an environment "
                            "variable or secret manager, and rotate the exposed value."
                        ),
                        cwe="CWE-798",
                    )
                )
    return findings
