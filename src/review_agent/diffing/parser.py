"""Unified-diff parsing: GitHub pulls/files entries -> FileChange/Hunk objects.

GitHub's per-file `patch` is a unified diff without the ---/+++ file header:
a sequence of @@-delimited hunks. The parser is strict about structure (a
malformed hunk raises DiffParseError) but tolerant about metadata mismatches,
which are logged rather than fatal so one odd file can't sink a whole PR.
"""

import logging
import re
from typing import Any

from . import classify
from .models import ChangeType, FileChange, Hunk

logger = logging.getLogger(__name__)

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: (.*))?$")


class DiffParseError(Exception):
    """The patch text does not conform to unified-diff structure."""


def parse_hunks(patch: str) -> list[Hunk]:
    """Parse a GitHub-style patch (hunks only, no file header) into Hunk objects."""
    hunks: list[Hunk] = []
    current: Hunk | None = None
    expected_old = expected_new = 0

    for line_number, line in enumerate(patch.splitlines(), start=1):
        header = _HUNK_HEADER.match(line)
        if header:
            _check_hunk_complete(current, expected_old, expected_new)
            old_start = int(header.group(1))
            old_count = int(header.group(2)) if header.group(2) is not None else 1
            new_start = int(header.group(3))
            new_count = int(header.group(4)) if header.group(4) is not None else 1
            current = Hunk(
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                section=header.group(5) or "",
                lines=[],
                added=0,
                removed=0,
            )
            expected_old = old_count
            expected_new = new_count
            hunks.append(current)
            continue

        if current is None:
            raise DiffParseError(f"line {line_number}: content before first hunk header")

        if line.startswith("+"):
            current.added += 1
            expected_new -= 1
        elif line.startswith("-"):
            current.removed += 1
            expected_old -= 1
        elif line.startswith(" ") or line == "":
            # context line; a fully empty line is an empty context line
            expected_old -= 1
            expected_new -= 1
        elif line.startswith("\\"):
            # "\ No newline at end of file" — annotation, counts toward neither side
            current.lines.append(line)
            continue
        else:
            raise DiffParseError(f"line {line_number}: unexpected prefix {line[:1]!r}")

        if expected_old < 0 or expected_new < 0:
            raise DiffParseError(f"line {line_number}: hunk longer than its header declares")
        current.lines.append(line)

    _check_hunk_complete(current, expected_old, expected_new)
    return hunks


def _check_hunk_complete(hunk: Hunk | None, expected_old: int, expected_new: int) -> None:
    if hunk is not None and (expected_old != 0 or expected_new != 0):
        raise DiffParseError(
            f"hunk @@ -{hunk.old_start} +{hunk.new_start} @@ is shorter than its header declares"
        )


def parse_file(api_file: dict[str, Any]) -> FileChange:
    """Build a FileChange from one entry of GitHub's pulls/files response."""
    path = api_file["filename"]
    status = api_file.get("status", "modified")
    try:
        change_type = ChangeType(status)
    except ValueError:
        logger.warning("unknown file status from GitHub", extra={"status": status, "file": path})
        change_type = ChangeType.MODIFIED

    additions = int(api_file.get("additions", 0))
    deletions = int(api_file.get("deletions", 0))
    changes = int(api_file.get("changes", additions + deletions))
    patch = api_file.get("patch")

    hunks: list[Hunk] = []
    is_binary = False
    patch_omitted = False
    if patch:
        hunks = parse_hunks(patch)
        parsed_added = sum(h.added for h in hunks)
        parsed_removed = sum(h.removed for h in hunks)
        if parsed_added != additions or parsed_removed != deletions:
            logger.warning(
                "parsed line counts disagree with GitHub metadata",
                extra={
                    "file": path,
                    "parsed_added": parsed_added,
                    "parsed_removed": parsed_removed,
                    "api_additions": additions,
                    "api_deletions": deletions,
                },
            )
    elif changes == 0:
        # No patch and no counted line changes: binary content (or a pure
        # rename, which GitHub also reports patchless with zero changes).
        is_binary = change_type not in (ChangeType.RENAMED, ChangeType.COPIED)
    else:
        # No patch but nonzero changes: GitHub withheld an oversized text diff.
        patch_omitted = True

    return FileChange(
        path=path,
        old_path=api_file.get("previous_filename"),
        change_type=change_type,
        additions=additions,
        deletions=deletions,
        is_binary=is_binary,
        patch_omitted=patch_omitted,
        language=classify.detect_language(path),
        size_tier=classify.size_tier(additions + deletions),
        hunks=hunks,
    )


def parse_files(api_files: list[dict[str, Any]]) -> list[FileChange]:
    return [parse_file(f) for f in api_files]
