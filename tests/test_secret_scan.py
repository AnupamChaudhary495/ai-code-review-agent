"""Deterministic secret-scanner tests: real detection, real false-positive guards.

No LLM, no API key needed — this is pure regex/entropy, so these run everywhere.
Secret-shaped strings are assembled from fragments at runtime so no full secret
token ever appears verbatim in this committed file (avoids commit-scanner
rejection) and so the values are obviously synthetic.
"""

import pytest

from review_agent.agent.tools.secret_scan import scan_file
from review_agent.diffing.models import ChangeType, FileChange, Hunk, SizeTier


def make_change(lines: list[str], path: str = "x.py") -> FileChange:
    """Build a FileChange whose hunk is all added lines (new-file numbers from 1)."""
    added = ["+" + ln for ln in lines]
    hunk = Hunk(
        old_start=0,
        old_count=0,
        new_start=1,
        new_count=len(added),
        section="",
        lines=added,
        added=len(added),
        removed=0,
    )
    return FileChange(
        path=path,
        old_path=None,
        change_type=ChangeType.ADDED,
        additions=len(added),
        deletions=0,
        is_binary=False,
        patch_omitted=False,
        language="python",
        size_tier=SizeTier.SMALL,
        hunks=[hunk],
    )


def _cat(*parts: str) -> str:
    return "".join(parts)


# Provider secret SHAPES, assembled from fragments (never a full token in source).
AWS = _cat("AKIA", "IOSFODNN7", "EXAMPLE")  # AWS's documented example id
GITHUB = _cat("gh", "p_", "0123456789abcdefghij0123456789abcdef")  # ghp_ + 36
GOOGLE = _cat("AI", "za", "Sy", "B" * 33)  # AIza + 35
SLACK = _cat("xo", "xb", "-", "2222222222", "-", "abcdefghijklmnopqrst")
STRIPE = _cat("sk", "_", "live_", "4eC39HqLyjWDarjtT1zdp7dc")
JWT = _cat("ey", "Jhbtestheaderpart", ".", "payloadpartxxxxxxxx", ".", "signaturepartyyyyyy")
PEM_HEADER = "-----BEGIN RSA PRIVATE KEY-----"


@pytest.mark.parametrize(
    ("label", "line"),
    [
        ("aws", f'AWS_KEY = "{AWS}"'),
        ("github", f'token = "{GITHUB}"'),
        ("google", f'API_KEY = "{GOOGLE}"'),
        ("slack", f'SLACK = "{SLACK}"'),
        ("stripe", f'stripe.api_key = "{STRIPE}"'),
        ("jwt", f'auth = "{JWT}"'),
        ("pem", PEM_HEADER),
    ],
)
def test_provider_secret_shapes_are_detected(label, line):
    findings = scan_file(make_change([line]))
    assert len(findings) >= 1, f"{label} not detected"
    assert findings[0].category == "security"
    assert findings[0].severity == "critical"
    assert findings[0].cwe == "CWE-798"


def test_generic_secret_named_assignment_is_detected():
    line = 'PAYMENT_API_SECRET = "live-secret-9f8e7d6c5b4a32100123456789abcdef"'
    findings = scan_file(make_change([line]))
    assert len(findings) == 1
    assert findings[0].severity == "critical"


@pytest.mark.parametrize(
    "line",
    [
        # UUID assigned to a non-secret name.
        'request_id = "550e8400-e29b-41d4-a716-446655440000"',
        # UUID even under a secret-suggesting name must be excluded.
        'token = "550e8400-e29b-41d4-a716-446655440000"',
        # 40-char git SHA / hash.
        'commit_sha = "356a192b7913b04c54574d18c28d46e6395428ab"',
        # 64-char hex hash under a secret-ish name.
        'password_hash = "' + "a" * 64 + '"',
        # Placeholder values.
        'api_key = "your-api-key-here"',
        'SECRET = "changeme"',
        # Env-var indirection (no inline literal secret).
        'api_key = os.environ["MY_KEY"]',
        'secret = os.getenv("APP_SECRET")',
        # Ordinary non-secret code.
        'greeting = "hello, world, this is a long friendly message"',
        'url = "https://example.com/docs/getting-started/index.html"',
    ],
)
def test_false_positives_are_not_flagged(line):
    assert scan_file(make_change([line])) == []


def test_line_numbers_point_at_the_secret():
    change = make_change(
        [
            "import os",
            "def connect():",
            f'    KEY = "{GITHUB}"',  # this is new-file line 3
            "    return KEY",
        ]
    )
    findings = scan_file(change)
    assert len(findings) == 1
    assert findings[0].line == 3
    assert findings[0].file == "x.py"


def test_one_secret_reported_once_not_twice():
    # A provider token that also sits in a secret-named assignment must not be
    # double-counted (provider hit and generic hit on the same span).
    findings = scan_file(make_change([f'GITHUB_TOKEN = "{GITHUB}"']))
    assert len(findings) == 1


def test_clean_file_yields_nothing():
    clean = [
        "def add(a, b):",
        "    return a + b",
        "",
        "class Greeter:",
        "    def hello(self, name):",
        "        return f'hello {name}'",
    ]
    assert scan_file(make_change(clean)) == []
