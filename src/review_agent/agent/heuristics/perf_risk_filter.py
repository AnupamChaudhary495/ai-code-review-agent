"""Decide whether a file is worth the (expensive) performance LLM pass.

A deterministic cost/coverage gate — see
docs/design-decisions/0002-performance-risk-prefilter.md. Biased toward recall:
any performance surface (a loop, a DB/ORM call, or a large change) sends the
file to the LLM, which then judges whether there is a real issue. Detection is
NOT done here — this only decides worthiness.
"""

import re
from collections.abc import Iterator

from ...diffing.models import FileChange, SizeTier

# Loop constructs (word-boundaried so "before"/"whilst" don't match). Also
# catches comprehensions, which contain `for`.
_LOOP = re.compile(r"\b(for|while)\b")

# DB/ORM / raw-SQL call shapes — the substrate of N+1 and query-in-a-loop.
_DB_ORM = re.compile(
    r"""
    \.execute\(        | \.executemany\(  | \.query\(        | \.filter\(       |
    \.filter_by\(      | \.get_or_create\(| \.fetchone\(     | \.fetchall\(     |
    \.fetchmany\(      | \.all\(\)        | \.first\(\)      | \.save\(         |
    \.commit\(         | \.aggregate\(    | \.annotate\(     | \.bulk_create\(  |
    \.objects\.        | session\.        | cursor           |
    \b(SELECT|INSERT|UPDATE|DELETE)\s
    """,
    re.IGNORECASE | re.VERBOSE,
)

# At/above this tier, scale alone makes a performance look worthwhile.
_LARGE_TIERS = frozenset({SizeTier.LARGE, SizeTier.HUGE})


def _iter_added_text(change: FileChange) -> Iterator[str]:
    for hunk in change.hunks:
        for raw in hunk.lines:
            if raw.startswith("+"):
                yield raw[1:]


def is_perf_worthwhile(change: FileChange) -> bool:
    """True if the file's changed lines show a performance surface."""
    if change.size_tier in _LARGE_TIERS:
        return True
    added = "\n".join(_iter_added_text(change))
    return bool(_LOOP.search(added) or _DB_ORM.search(added))


def perf_skip_reason(change: FileChange) -> str:
    return (
        "no loops, DB/ORM calls, or large change — not worth a performance pass (risk pre-filter)"
    )
