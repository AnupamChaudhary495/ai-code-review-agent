"""Security golden-set eval: run the security node over the planted-vuln corpus.

Runs the DETERMINISTIC secret scanner (always) plus the REAL LLM security pass
(needs ANTHROPIC_API_KEY; never in CI) over the recorded eval PR and reports:

- deterministic secret detection (must be 100% of planted secrets)
- overall detection across the golden set (target >= 80%)
- false criticals on a clean baseline (must be 0)

Usage:
    python scripts/eval_security_review.py
"""

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tests"))

from helpers import load_fixture  # noqa: E402
from review_agent.agent.nodes.security_analysis import analyze_security  # noqa: E402
from review_agent.agent.tools.secret_scan import scan_file  # noqa: E402
from review_agent.diffing import parser  # noqa: E402


@dataclass
class Vuln:
    file: str
    label: str
    line_range: tuple[int, int]
    is_secret: bool


GOLDEN = [
    Vuln("eval/api_keys.py", "hardcoded secret", (1, 4), is_secret=True),
    Vuln("eval/sql_query.py", "SQL injection", (6, 8), is_secret=False),
    Vuln("eval/file_download.py", "path traversal", (6, 10), is_secret=False),
    Vuln("eval/role_check.py", "auth logic (always-true)", (1, 4), is_secret=False),
    Vuln("eval/data_export.py", "unsafe eval of input", (3, 5), is_secret=False),
]


def _detected(findings, v: Vuln) -> bool:
    lo, hi = v.line_range
    return any(
        f.category == "security" and (f.line is None or lo <= f.line <= hi) for f in findings
    )


def main() -> int:
    entries = {e["filename"]: e for e in load_fixture("pr_eval_files.json")}

    detected = 0
    secrets_planted = secrets_caught = 0
    for v in GOLDEN:
        change = parser.parse_file(entries[v.file])
        if v.is_secret:
            secrets_planted += 1
            if scan_file(change):
                secrets_caught += 1
        (result,) = analyze_security({"file": change})["results"]
        hit = _detected(result.findings, v)
        detected += hit
        print(
            f"[{'OK  ' if hit else 'MISS'}] {v.file} ({v.label}): "
            f"{len(result.findings)} finding(s), status={result.status}"
        )

    rate = detected / len(GOLDEN)
    print("\n=== SUMMARY ===")
    print(f"deterministic secrets: {secrets_caught}/{secrets_planted} (must be 100%)")
    print(f"golden-set detection: {detected}/{len(GOLDEN)} ({rate:.0%}; target >= 80%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
