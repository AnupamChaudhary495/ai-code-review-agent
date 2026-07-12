"""LLM review of one changed file: a typed FileChange in, validated Findings out.

The prompt lives in prompts/bug_review_v1.md (versioned, not inline). The diff
is passed as untrusted data in the user message; the system prompt instructs
the model to ignore any instructions embedded in it. Malformed model output
gets exactly one repair retry (re-prompted with the parse error) before the
review fails loudly.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import anthropic
import pydantic
from anthropic.types import Message, MessageParam

from .config import get_settings
from .diffing.models import FileChange
from .schemas.finding import Finding, ReviewFindings

logger = logging.getLogger(__name__)

PROMPT_VERSION = "bug_review_v1"
# Adaptive thinking counts toward max_tokens; leave headroom so the JSON
# output never gets truncated by a long thinking pass.
_MAX_TOKENS = 16000


class ReviewOutputError(Exception):
    """The model's output could not be parsed into findings, even after repair."""


class ReviewRefusedError(Exception):
    """The model declined to produce a review."""


@dataclass
class ReviewResult:
    findings: list[Finding]
    model: str
    prompt_version: str
    input_tokens: int
    output_tokens: int
    repair_used: bool


def load_prompt(version: str = PROMPT_VERSION) -> str:
    return (Path(__file__).parent / "prompts" / f"{version}.md").read_text(encoding="utf-8")


def render_change(change: FileChange) -> str:
    """Render hunks with new-file line numbers on added/context lines.

    The numbers give the model an unambiguous source for Finding.line; removed
    lines and no-newline markers exist only in the old file and get no number.
    """
    out = [
        f"File: {change.path}",
        f"Change type: {change.change_type}",
        f"Language: {change.language or 'unknown'}",
        "",
    ]
    for hunk in change.hunks:
        header = f"@@ -{hunk.old_start},{hunk.old_count} +{hunk.new_start},{hunk.new_count} @@"
        out.append(f"{header} {hunk.section}".rstrip())
        new_line = hunk.new_start
        for raw in hunk.lines:
            if raw.startswith("+") or raw.startswith(" ") or raw == "":
                out.append(f"{new_line:>6} {raw}")
                new_line += 1
            else:  # removed lines and "\ No newline" markers
                out.append(f"{'':>6} {raw}")
    return "\n".join(out)


def _extract_json(text: str) -> str:
    """Cut the first {...last} span so stray prose or code fences don't kill parsing."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in model output")
    return text[start : end + 1]


def _parse_findings(text: str, change: FileChange) -> list[Finding]:
    parsed = ReviewFindings.model_validate(json.loads(_extract_json(text)))
    for finding in parsed.findings:
        # The path is ours, not the model's, to decide.
        finding.file = change.path
    return parsed.findings


def _call_model(
    client: anthropic.Anthropic, system_prompt: str, messages: list[MessageParam]
) -> tuple[Message, str]:
    settings = get_settings()
    response = client.messages.create(
        model=settings.llm_model,
        max_tokens=_MAX_TOKENS,
        thinking={"type": "adaptive"},
        system=system_prompt,
        messages=messages,
    )
    if response.stop_reason == "refusal":
        raise ReviewRefusedError("model refused to review this diff")
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    logger.info(
        "llm call completed",
        extra={
            "model": response.model,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "stop_reason": response.stop_reason,
        },
    )
    return response, text


def review_file(change: FileChange) -> ReviewResult:
    """Run the bug-review prompt over one FileChange."""
    if not change.hunks:
        raise ValueError(f"{change.path} has no reviewable hunks (binary or omitted patch)")

    settings = get_settings()
    system_prompt = load_prompt()
    user_content = f"<diff>\n{render_change(change)}\n</diff>"
    client = anthropic.Anthropic(
        api_key=settings.anthropic_api_key.get_secret_value() or None,
    )

    messages: list[MessageParam] = [{"role": "user", "content": user_content}]
    response, text = _call_model(client, system_prompt, messages)
    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens

    repair_used = False
    try:
        findings = _parse_findings(text, change)
    except (json.JSONDecodeError, pydantic.ValidationError, ValueError) as exc:
        repair_used = True
        logger.warning(
            "review output unparseable; attempting one repair",
            extra={"file": change.path, "parse_error": str(exc)[:300]},
        )
        messages.append({"role": "assistant", "content": text})
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Your previous response could not be parsed: {exc}. "
                    "Respond again with ONLY the valid JSON object matching the schema "
                    "from your instructions — no code fences, no prose."
                ),
            }
        )
        response, text = _call_model(client, system_prompt, messages)
        input_tokens += response.usage.input_tokens
        output_tokens += response.usage.output_tokens
        try:
            findings = _parse_findings(text, change)
        except (json.JSONDecodeError, pydantic.ValidationError, ValueError) as exc2:
            raise ReviewOutputError(
                f"model output for {change.path} unparseable after repair retry: {exc2}"
            ) from exc2

    logger.info(
        "file review completed",
        extra={
            "file": change.path,
            "findings": len(findings),
            "repair_used": repair_used,
            "prompt_version": PROMPT_VERSION,
        },
    )
    return ReviewResult(
        findings=findings,
        model=response.model,
        prompt_version=PROMPT_VERSION,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        repair_used=repair_used,
    )
