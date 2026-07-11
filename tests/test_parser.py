"""Table-driven parser tests over recorded real PR payloads (tests/fixtures/)."""

import pytest

from helpers import load_fixture
from review_agent.diffing import parser
from review_agent.diffing.models import ChangeType, SizeTier
from review_agent.diffing.parser import DiffParseError

A = ChangeType.ADDED
M = ChangeType.MODIFIED
R = ChangeType.RENAMED
D = ChangeType.REMOVED

# fixture file, filename, change_type, old_path, hunk count,
# is_binary, patch_omitted, language, size tier
CASES = [
    ("pr_small_files.json", "greet.py", A, None, 1, False, False, "python", SizeTier.SMALL),
    ("pr_edge_files.json", "data.bin", M, None, 0, True, False, None, SizeTier.SMALL),
    ("pr_edge_files.json", "long_module.py", M, None, 2, False, False, "python", SizeTier.SMALL),
    ("pr_edge_files.json", "new_file.py", A, None, 1, False, False, "python", SizeTier.SMALL),
    (
        "pr_edge_files.json",
        "src/renamed.py",
        R,
        "to_rename.py",
        0,
        False,
        False,
        "python",
        SizeTier.SMALL,
    ),
    (
        "pr_edge_files.json",
        "src/renamed_edited.py",
        R,
        "to_rename_edit.py",
        1,
        False,
        False,
        "python",
        SizeTier.SMALL,
    ),
    ("pr_edge_files.json", "to_delete.txt", D, None, 1, False, False, "text", SizeTier.SMALL),
    ("pr_binary_files.json", "image.png", A, None, 0, True, False, None, SizeTier.SMALL),
    (
        "pr_public_requests_files.json",
        "src/requests/auth.py",
        M,
        None,
        3,
        False,
        False,
        "python",
        SizeTier.SMALL,
    ),
    (
        "pr_public_requests_7566_files.json",
        "src/requests/_types.py",
        M,
        None,
        1,
        False,
        False,
        "python",
        SizeTier.SMALL,
    ),
    (
        "pr_public_requests_7565_files.json",
        "src/requests/adapters.py",
        M,
        None,
        2,
        False,
        False,
        "python",
        SizeTier.SMALL,
    ),
    (
        "pr_truncated_files.json",
        "bulk/file_0000.py",
        A,
        None,
        1,
        False,
        False,
        "python",
        SizeTier.SMALL,
    ),
    (
        "pr_truncated_files.json",
        "bulk/file_2999.py",
        A,
        None,
        1,
        False,
        False,
        "python",
        SizeTier.SMALL,
    ),
    ("pr_variety_files.json", "Dockerfile", A, None, 1, False, False, "dockerfile", SizeTier.SMALL),
    ("pr_variety_files.json", "big_module.py", A, None, 1, False, False, "python", SizeTier.LARGE),
    (
        "pr_variety_files.json",
        "config/settings.yaml",
        A,
        None,
        1,
        False,
        False,
        "yaml",
        SizeTier.SMALL,
    ),
    ("pr_variety_files.json", "data/sample.json", A, None, 1, False, False, "json", SizeTier.SMALL),
    (
        "pr_variety_files.json",
        "docs/guide.md",
        A,
        None,
        1,
        False,
        False,
        "markdown",
        SizeTier.SMALL,
    ),
    ("pr_variety_files.json", "giant_module.py", A, None, 0, False, True, "python", SizeTier.HUGE),
    ("pr_variety_files.json", "no_newline.txt", A, None, 1, False, False, "text", SizeTier.SMALL),
]

ALL_FILE_FIXTURES = sorted({case[0] for case in CASES})


def entry_for(fixture: str, filename: str) -> dict:
    return next(f for f in load_fixture(fixture) if f["filename"] == filename)


@pytest.mark.parametrize(
    (
        "fixture",
        "filename",
        "change_type",
        "old_path",
        "hunk_count",
        "is_binary",
        "patch_omitted",
        "language",
        "tier",
    ),
    CASES,
    ids=[f"{c[0].removesuffix('_files.json')}:{c[1]}" for c in CASES],
)
def test_parse_file_against_recorded_payloads(
    fixture,
    filename,
    change_type,
    old_path,
    hunk_count,
    is_binary,
    patch_omitted,
    language,
    tier,
):
    entry = entry_for(fixture, filename)
    change = parser.parse_file(entry)

    assert change.path == filename
    assert change.change_type == change_type
    assert change.old_path == old_path
    assert len(change.hunks) == hunk_count
    assert change.is_binary == is_binary
    assert change.patch_omitted == patch_omitted
    assert change.language == language
    assert change.size_tier == tier
    # Round-trip integrity: parsed line counts equal GitHub's own metadata.
    if change.hunks:
        assert sum(h.added for h in change.hunks) == entry["additions"]
        assert sum(h.removed for h in change.hunks) == entry["deletions"]


def test_every_recorded_file_round_trips_without_error():
    """Every file entry in every recorded fixture parses cleanly (3000+ files)."""
    total = 0
    for fixture in ALL_FILE_FIXTURES:
        for entry in load_fixture(fixture):
            change = parser.parse_file(entry)
            if entry.get("patch"):
                assert sum(h.added for h in change.hunks) == entry["additions"], entry["filename"]
                assert sum(h.removed for h in change.hunks) == entry["deletions"], entry["filename"]
            total += 1
    assert total > 3000  # the truncated PR alone contributes 3000 real entries


def test_no_newline_marker_is_kept_but_not_counted():
    entry = entry_for("pr_variety_files.json", "no_newline.txt")
    change = parser.parse_file(entry)
    (hunk,) = change.hunks
    assert any(line.startswith("\\ No newline") for line in hunk.lines)
    assert hunk.added == entry["additions"]


def test_multi_hunk_boundaries_are_parsed():
    entry = entry_for("pr_edge_files.json", "long_module.py")
    first, second = parser.parse_file(entry).hunks
    assert first.new_start < second.new_start
    assert first.section == ""  # first hunk starts at the top of the file
    assert (first.added, first.removed) == (2, 2)
    assert (second.added, second.removed) == (2, 2)


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ("+orphan line", "content before first hunk header"),
        ("@@ -1,2 +1,2 @@\n context\n-gone\n+here\n+extra", "hunk longer than declared"),
        ("@@ -1,3 +1,3 @@\n context", "hunk shorter than declared"),
        ("@@ -1 +1 @@\n?weird prefix", "unknown line prefix"),
    ],
)
def test_malformed_patches_raise(patch, reason):
    with pytest.raises(DiffParseError):
        parser.parse_hunks(patch)
