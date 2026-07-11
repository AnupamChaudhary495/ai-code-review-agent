"""Typed representation of a pull request's changes."""

from dataclasses import dataclass, field
from enum import StrEnum


class ChangeType(StrEnum):
    """Mirrors the `status` values GitHub's pulls/files API emits."""

    ADDED = "added"
    MODIFIED = "modified"
    REMOVED = "removed"
    RENAMED = "renamed"
    COPIED = "copied"
    CHANGED = "changed"
    UNCHANGED = "unchanged"


class SizeTier(StrEnum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    HUGE = "huge"


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    section: str  # trailing context after the second @@, often the enclosing function
    lines: list[str]  # raw lines including their +/-/space prefix
    added: int
    removed: int


@dataclass
class FileChange:
    path: str
    old_path: str | None  # set for renames/copies
    change_type: ChangeType
    additions: int
    deletions: int
    is_binary: bool
    # True when GitHub withheld the patch for an oversized text diff;
    # additions/deletions are still reported, hunks are unavailable.
    patch_omitted: bool
    language: str | None
    size_tier: SizeTier
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def changed_lines(self) -> int:
        return self.additions + self.deletions
