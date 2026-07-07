"""Single LLM call: one file diff in, one review comment out."""

import logging

import anthropic

from .config import get_settings

logger = logging.getLogger(__name__)

# Cost guard for the Phase 1 slice; chunking strategies are a later phase.
MAX_PATCH_CHARS = 40_000

SYSTEM_PROMPT = (
    "You are a senior software engineer reviewing a pull request. "
    "You will receive the unified diff of a single changed file. "
    "Write a concise review comment in GitHub-flavored Markdown: briefly explain "
    "what the change does, then point out any bugs, security issues, or clear "
    "quality problems, referencing the relevant lines. "
    "If the change looks good, say so briefly. Do not invent issues."
)


class ReviewRefusedError(Exception):
    """The model declined to produce a review."""


def review_file_diff(filename: str, patch: str) -> str:
    settings = get_settings()
    if len(patch) > MAX_PATCH_CHARS:
        patch = patch[:MAX_PATCH_CHARS] + "\n... [diff truncated for review]"

    client = anthropic.Anthropic(
        api_key=settings.anthropic_api_key.get_secret_value() or None,
    )
    response = client.messages.create(
        model=settings.llm_model,
        max_tokens=4096,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"File: `{filename}`\n\nUnified diff:\n```diff\n{patch}\n```",
            }
        ],
    )
    if response.stop_reason == "refusal":
        raise ReviewRefusedError("model refused to review this diff")

    text = "".join(block.text for block in response.content if block.type == "text").strip()
    if not text:
        raise RuntimeError(f"empty review from model (stop_reason={response.stop_reason})")

    logger.info(
        "llm review completed",
        extra={
            "model": response.model,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "stop_reason": response.stop_reason,
        },
    )
    return text
