"""The aggregated, deduplicated report for one pull-request review.

`ReviewReport` is the single object every consumer sees after the graph has
run: the Markdown renderer, the JSON renderer, and (Phase 9) delivery. The
per-node `FileReviewResult` list is an *internal* orchestration artifact —
three loosely related results per file, no dedup, no ordering. This is the
external contract.

Two properties are deliberate:

1. **Nothing here needs an LLM.** The summary, the verdict and the per-file
   "what changed" line are all derived arithmetically from finding counts and
   diff metadata. A report is free and reproducible.
2. **`ReportFinding` subclasses `Finding`.** Anything that already accepts a
   `list[Finding]` — notably `github.delivery.post_review` — accepts these
   unchanged, so wiring the report into delivery is a wiring exercise rather
   than a schema migration.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .finding import Finding, Severity

SCHEMA_VERSION = "1.0"

# Worst-first. Used for ordering everywhere: findings within a file, files
# within the report, and the summary breakdown.
SEVERITY_ORDER: tuple[Severity, ...] = ("critical", "high", "medium", "low")
SEVERITY_RANK: dict[str, int] = {s: i for i, s in enumerate(SEVERITY_ORDER)}

# Shared with the Markdown renderer. github/delivery.py has its own copy from
# Phase 4; they intentionally agree, and Phase 9 can collapse them when
# delivery starts consuming a ReviewReport.
SEVERITY_BADGES: dict[str, str] = {
    "critical": "🟥",
    "high": "🟧",
    "medium": "🟨",
    "low": "🟩",
}

# Canonical order of analysis passes, for stable rendering.
PASS_ORDER: tuple[str, ...] = ("bug", "security", "performance")

# What a human is expected to do about the report. Derived from the highest
# severity present — never from a model's opinion.
Verdict = Literal["clean", "advisory", "attention", "blocking"]


class ReportFinding(Finding):
    """A `Finding` after cross-pass aggregation.

    Adds provenance: `sources` names every analysis pass that produced this
    finding (more than one means the passes agreed and were merged), and
    `duplicates_merged` counts how many raw findings collapsed into this one.
    Keeping both makes the dedup auditable in the rendered output instead of
    silently discarding evidence.
    """

    sources: list[str] = Field(
        default_factory=list,
        description="Analysis passes that produced this finding, sorted; >1 means merged",
    )
    duplicates_merged: int = Field(
        default=0, description="How many additional raw findings collapsed into this one"
    )


class PassOutcome(BaseModel):
    """What one analysis pass did to one file — including deciding not to run."""

    source: str  # "bug" | "security" | "performance" | "skipped"
    status: str  # "reviewed" | "skipped" | "unavailable"
    reason: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


class SeverityCounts(BaseModel):
    critical: int = 0
    high: int = 0
    medium: int = 0
    low: int = 0

    @property
    def total(self) -> int:
        return self.critical + self.high + self.medium + self.low

    def items(self) -> list[tuple[str, int]]:
        """(severity, count) pairs, worst first — including zeroes."""
        return [(s, int(getattr(self, s))) for s in SEVERITY_ORDER]

    def nonzero(self) -> list[tuple[str, int]]:
        return [(s, n) for s, n in self.items() if n]


class FileReport(BaseModel):
    """Every finding for one file, plus what happened to that file."""

    path: str
    # Plain-language, derived strictly from diff metadata (change type,
    # additions/deletions, language). Never model-generated.
    what_changed: str
    change_type: str | None = None
    additions: int = 0
    deletions: int = 0
    language: str | None = None
    # True when at least one analysis pass actually reviewed this file.
    reviewed: bool = False
    findings: list[ReportFinding] = Field(default_factory=list)
    passes: list[PassOutcome] = Field(default_factory=list)

    @property
    def severity_counts(self) -> SeverityCounts:
        counts = SeverityCounts()
        for f in self.findings:
            setattr(counts, f.severity, int(getattr(counts, f.severity)) + 1)
        return counts

    @property
    def worst_severity(self) -> str | None:
        if not self.findings:
            return None
        return min((f.severity for f in self.findings), key=lambda s: SEVERITY_RANK[s])

    @property
    def skip_reason(self) -> str | None:
        """Why a wholly unreviewed file was not reviewed, if that is why."""
        for outcome in self.passes:
            if outcome.source == "skipped":
                return outcome.reason
        return None


class ReviewStats(BaseModel):
    """Counting only — every field is arithmetic over the raw node results."""

    files_total: int = 0
    files_reviewed: int = 0
    files_not_reviewed: int = 0
    files_with_findings: int = 0
    passes_total: int = 0
    passes_reviewed: int = 0
    passes_skipped: int = 0
    passes_unavailable: int = 0
    findings_total: int = 0
    findings_before_dedup: int = 0
    duplicates_merged: int = 0
    severity_counts: SeverityCounts = Field(default_factory=SeverityCounts)
    input_tokens: int = 0
    output_tokens: int = 0


class ReviewReport(BaseModel):
    """One coherent report for one pull request."""

    schema_version: str = SCHEMA_VERSION
    repo: str | None = None
    pr_number: int | None = None
    head_sha: str | None = None
    # Left None by default so rendering is byte-for-byte reproducible; callers
    # that want a timestamp pass one explicitly.
    generated_at: datetime | None = None
    summary: str = ""
    verdict: Verdict = "clean"
    stats: ReviewStats = Field(default_factory=ReviewStats)
    # Worst-severity first, then most findings, then path.
    files: list[FileReport] = Field(default_factory=list)

    def all_findings(self) -> list[ReportFinding]:
        """Every finding in the report, in the report's own display order."""
        return [f for file_report in self.files for f in file_report.findings]

    def files_with_findings(self) -> list[FileReport]:
        return [f for f in self.files if f.findings]

    def files_clean(self) -> list[FileReport]:
        return [f for f in self.files if f.reviewed and not f.findings]

    def files_unreviewed(self) -> list[FileReport]:
        return [f for f in self.files if not f.reviewed]
