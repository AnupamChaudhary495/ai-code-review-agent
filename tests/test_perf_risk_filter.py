"""Performance risk pre-filter: real, deterministic, no LLM.

Asserts the filter routes the golden-set N+1 and O(n^2) files to the node
(a filter that excludes what it should catch is worse than no filter), leaves
non-perf files out, and achieves the roadmap's >=40% call-count reduction over
the combined fixture corpus.
"""

import pytest

from helpers import load_fixture
from review_agent.agent.heuristics.perf_risk_filter import is_perf_worthwhile, perf_skip_reason
from review_agent.diffing import parser
from review_agent.diffing.models import ChangeType, FileChange, Hunk, SizeTier


def make_change(lines: list[str], tier: SizeTier = SizeTier.SMALL) -> FileChange:
    added = ["+" + ln for ln in lines]
    return FileChange(
        path="x.py",
        old_path=None,
        change_type=ChangeType.ADDED,
        additions=len(added),
        deletions=0,
        is_binary=False,
        patch_omitted=False,
        language="python",
        size_tier=tier,
        hunks=[Hunk(0, 0, 1, len(added), "", added, len(added), 0)],
    )


def perf_change(name):
    entries = {e["filename"]: e for e in load_fixture("pr_perf_files.json")}
    return parser.parse_file(entries[name])


def test_n_plus_one_routes_to_the_node():
    # The whole point of the filter: it must NOT exclude a real N+1.
    assert is_perf_worthwhile(perf_change("perf/n_plus_one.py"))


def test_quadratic_routes_to_the_node():
    assert is_perf_worthwhile(perf_change("perf/quadratic.py"))


def test_non_perf_file_is_filtered_out():
    assert not is_perf_worthwhile(perf_change("perf/formatting.py"))


@pytest.mark.parametrize(
    ("lines", "worthy"),
    [
        (["for x in items:", "    total += x"], True),  # loop
        (["while more:", "    pull()"], True),  # while loop
        (["rows = db.session.query(User).all()"], True),  # DB/ORM
        (["cur.execute('SELECT 1')"], True),  # raw SQL / execute
        (["result = [g(x) for x in xs]"], True),  # comprehension
        (["return a + b"], False),  # pure
        (['name = "before-and-after"'], False),  # 'for'/'while' as substrings only
        (["x = whilst_var + fortune"], False),  # not real keywords
    ],
)
def test_worthiness_signals(lines, worthy):
    assert is_perf_worthwhile(make_change(lines)) is worthy


def test_large_change_is_worthwhile_even_without_loops_or_db():
    assert is_perf_worthwhile(make_change(["x = 1", "y = 2"], tier=SizeTier.LARGE))
    assert is_perf_worthwhile(make_change(["x = 1"], tier=SizeTier.HUGE))
    assert not is_perf_worthwhile(make_change(["x = 1"], tier=SizeTier.MEDIUM))


def test_skip_reason_is_descriptive():
    assert "risk pre-filter" in perf_skip_reason(make_change(["x = 1"]))


def test_call_count_reduction_is_at_least_40_percent():
    """Over the combined corpus, the filter must cut performance-node calls
    by >=40% vs. sending every eligible file (the roadmap's number)."""
    corpus = []
    for fixture in ("pr_eval_files.json", "pr_perf_files.json"):
        corpus.extend(parser.parse_file(e) for e in load_fixture(fixture))

    eligible = [f for f in corpus if f.hunks and not f.is_binary and not f.patch_omitted]
    worthy = [f for f in eligible if is_perf_worthwhile(f)]

    without_filter = len(eligible)
    with_filter = len(worthy)
    reduction = 1 - (with_filter / without_filter)

    # Sanity: the two planted perf files are among the worthy set.
    worthy_paths = {f.path for f in worthy}
    assert "perf/n_plus_one.py" in worthy_paths
    assert "perf/quadratic.py" in worthy_paths

    assert reduction >= 0.40, (
        f"risk filter reduced performance calls by only {reduction:.0%} "
        f"({with_filter}/{without_filter}); target >= 40%"
    )
