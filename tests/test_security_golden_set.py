"""Golden-set security detection over real recorded PR files.

The corpus (pr_eval_files.json) has planted vulnerabilities of three kinds:
- hardcoded secret         (eval/api_keys.py)   -> caught deterministically
- SQL injection            (eval/sql_query.py)  -> LLM security pass
- path traversal           (eval/file_download.py) -> LLM security pass

Two things are asserted here, and they differ in what they prove:

* The DETERMINISTIC secret scanner catches 100% of planted *secrets* and
  raises zero critical findings on a clean baseline. This is real and needs no
  API key — the non-negotiable guarantee.
* The security NODE, given a competent (mocked) LLM, surfaces every planted
  vuln class tagged as security. This proves the wiring/scoring harness; the
  live model detection rate is measured by scripts/eval_security_review.py and
  needs ANTHROPIC_API_KEY.
"""

import pytest

from helpers import load_fixture
from review_agent import reviewer
from review_agent.agent.nodes.security_analysis import analyze_security
from review_agent.agent.tools.secret_scan import scan_file
from review_agent.diffing import parser
from review_agent.diffing.models import ChangeType, FileChange, Hunk, SizeTier
from review_agent.reviewer import ReviewResult
from review_agent.schemas.finding import Finding

# file -> (vuln label, expected new-file line, detected_by)
GOLDEN = {
    "eval/api_keys.py": ("hardcoded secret", 3, "scanner"),
    "eval/sql_query.py": ("SQL injection", 7, "llm"),
    "eval/file_download.py": ("path traversal", 10, "llm"),
}


@pytest.fixture
def golden_changes():
    entries = {e["filename"]: e for e in load_fixture("pr_eval_files.json")}
    return {path: parser.parse_file(entries[path]) for path in GOLDEN}


def test_deterministic_scanner_catches_all_planted_secrets(golden_changes):
    planted_secret_files = [f for f, (_, _, by) in GOLDEN.items() if by == "scanner"]
    caught = 0
    for path in planted_secret_files:
        findings = scan_file(golden_changes[path])
        assert findings, f"deterministic scanner MISSED the secret in {path}"
        assert all(f.cwe == "CWE-798" and f.severity == "critical" for f in findings)
        caught += 1
    # Non-negotiable: 100% of planted secrets, no LLM involved.
    assert caught == len(planted_secret_files)


def test_scanner_no_false_critical_on_non_secret_vulns(golden_changes):
    # The SQL-injection and path-traversal files are NOT secrets; the
    # deterministic scanner must not manufacture secret findings for them.
    for path, (_, _, by) in GOLDEN.items():
        if by == "llm":
            assert scan_file(golden_changes[path]) == []


def _competent_llm(change: FileChange) -> ReviewResult:
    """A stand-in for a capable security model: returns the correct finding for
    each planted vuln, empty otherwise. Proves the harness, not the model."""
    label, line, by = GOLDEN.get(change.path, (None, None, None))
    findings: list[Finding] = []
    if by == "llm":
        cwe = "CWE-89" if "injection" in label else "CWE-22"
        findings.append(
            Finding(
                file=change.path,
                line=line,
                category="security",
                severity="critical",
                message=f"{label} detected",
                cwe=cwe,
            )
        )
    return ReviewResult(
        findings=findings,
        model="claude-test",
        prompt_version="security_review_v1",
        input_tokens=100,
        output_tokens=20,
        repair_used=False,
    )


def test_security_node_surfaces_every_planted_vuln(golden_changes, monkeypatch):
    monkeypatch.setattr(reviewer, "review_security", _competent_llm)

    detected = 0
    for path, (_label, line, _by) in GOLDEN.items():
        (result,) = analyze_security({"file": golden_changes[path]})["results"]
        assert result.source == "security"
        hit = any(f.line == line and f.category == "security" for f in result.findings)
        if hit:
            detected += 1
    rate = detected / len(GOLDEN)
    assert rate >= 0.80, f"golden-set detection {rate:.0%} < 80% target"
    assert detected == len(GOLDEN)  # harness: competent model finds all three


def test_clean_baseline_has_zero_critical_findings(monkeypatch):
    clean_lines = [
        "def normalize(name):",
        "    return name.strip().lower()",
        "",
        "def total(items):",
        "    return sum(i.price for i in items)",
    ]
    added = ["+" + ln for ln in clean_lines]
    clean = FileChange(
        path="clean.py",
        old_path=None,
        change_type=ChangeType.ADDED,
        additions=len(added),
        deletions=0,
        is_binary=False,
        patch_omitted=False,
        language="python",
        size_tier=SizeTier.SMALL,
        hunks=[Hunk(0, 0, 1, len(added), "", added, len(added), 0)],
    )
    # Deterministic scanner: real, no key.
    assert scan_file(clean) == []
    # Security node with an LLM that (correctly) finds nothing: no critical flags.
    monkeypatch.setattr(
        reviewer,
        "review_security",
        lambda c: ReviewResult([], "claude-test", "security_review_v1", 10, 2, False),
    )
    (result,) = analyze_security({"file": clean})["results"]
    criticals = [f for f in result.findings if f.severity == "critical"]
    assert criticals == []
