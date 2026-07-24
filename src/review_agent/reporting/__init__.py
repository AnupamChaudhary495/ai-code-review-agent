"""Aggregation and rendering of a completed review run.

`synthesis.build_report` turns the graph's flat `list[FileReviewResult]` into
one `ReviewReport`; `markdown` and `json_report` render that report. All three
are deterministic and LLM-free — see
docs/design-decisions/0003-cross-pass-finding-dedup.md.
"""

from .json_report import parse_json, render_dict, render_json
from .markdown import render_markdown
from .synthesis import build_report, report_for_pull_request

__all__ = [
    "build_report",
    "parse_json",
    "render_dict",
    "render_json",
    "render_markdown",
    "report_for_pull_request",
]
