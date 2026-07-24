"""Serialize a ReviewReport for API and storage consumers.

Deliberately thin: the report schema *is* the JSON contract, so this module
does not reshape anything. Reshaping here is how the two renderers would drift
apart — the Markdown and the JSON must never disagree about what was found
(tests/test_report_render.py asserts exactly that).
"""

from typing import Any

from ..schemas.review_report import ReviewReport


def render_json(report: ReviewReport, indent: int | None = 2) -> str:
    """Serialize the report to JSON text."""
    return report.model_dump_json(indent=indent)


def render_dict(report: ReviewReport) -> dict[str, Any]:
    """Serialize to a JSON-compatible dict (for embedding in an API response)."""
    return report.model_dump(mode="json")


def parse_json(text: str) -> ReviewReport:
    """Parse JSON text back into a validated ReviewReport (round-trip)."""
    return ReviewReport.model_validate_json(text)
