"""Typed graph state for the multi-file review run.

Minimal by design: the only field that needs a reducer is `results`, because
per-file branches execute in parallel and each contributes one result that
must accumulate into a single list. Everything else is read-only input.
"""

import operator
from dataclasses import dataclass, field
from typing import Annotated, TypedDict

from ..diffing.models import FileChange
from ..schemas.finding import Finding


@dataclass
class FileReviewResult:
    """The outcome of one analysis pass over one file — always produced.

    A file can yield several results: one per analysis node (bug, security),
    plus a single "skipped" result if it was ineligible. `source` tags which
    pass produced it so downstream (Phase 8) can group them; Phase 6 does no
    aggregation or dedup between passes yet.
    """

    path: str
    status: str  # "reviewed" | "skipped" | "unavailable"
    source: str = "bug"  # "bug" | "security" | "skipped"
    findings: list[Finding] = field(default_factory=list)
    reason: str | None = None  # why skipped, or why unavailable
    error_count: int = 0  # transient failures seen before this result was produced
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


class AnalysisInput(TypedDict):
    """Per-file payload delivered to a fanned-out analysis node via Send."""

    file: FileChange


class ReviewState(TypedDict):
    """Graph-level state.

    `diff_files` is the read-only input (the eligible + ineligible file set).
    `results` accumulates across parallel branches via the add reducer — this
    is the one place accumulation is genuinely needed.
    """

    diff_files: list[FileChange]
    results: Annotated[list[FileReviewResult], operator.add]
