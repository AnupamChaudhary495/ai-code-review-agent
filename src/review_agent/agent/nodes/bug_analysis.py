"""Bug-analysis node: a thin wrapper over reviewer.review_file for one file.

This node does NOT reimplement any LLM logic — it calls the Phase 4
review_file() and adapts its outcome into a FileReviewResult. Its job is
resilience: a bounded, explicit retry loop around transient failures (LangGraph
does not retry exceptions for us), and turning any failure into a recorded
"analysis unavailable" result rather than an exception that aborts the whole
graph run.
"""

import logging
import time

import anthropic

from ... import reviewer
from ...reviewer import ReviewOutputError, ReviewRefusedError
from ..state import BugAnalysisInput, FileReviewResult
from .router import select_model

logger = logging.getLogger(__name__)

# Hard iteration ceiling on the retry loop. A stuck file fails loudly after
# this many attempts and finishes; it never spins.
MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_CAP_SECONDS = 8.0

# Transient failures worth retrying (network / rate limit / 5xx). Persistent
# failures (unparseable output after the model's own repair, refusals) are NOT
# retried — retrying them only burns the ceiling and spins.
_TRANSIENT = (
    anthropic.APITimeoutError,
    anthropic.RateLimitError,
    anthropic.APIConnectionError,
    anthropic.InternalServerError,
)


def _backoff_seconds(attempt: int) -> float:
    return min(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), _BACKOFF_CAP_SECONDS)


def analyze_file(state: BugAnalysisInput) -> dict[str, list[FileReviewResult]]:
    """Analyse one file; always return exactly one FileReviewResult."""
    file = state["file"]
    model = select_model(file)  # seam: today one model, Phase 6/7 routes by tier
    transient_failures = 0
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            outcome = reviewer.review_file(file)
        except _TRANSIENT as exc:
            transient_failures += 1
            last_error = exc
            logger.warning(
                "transient failure analysing file; will retry",
                extra={
                    "file": file.path,
                    "attempt": attempt,
                    "max_attempts": MAX_ATTEMPTS,
                    "error": type(exc).__name__,
                },
            )
            if attempt < MAX_ATTEMPTS:
                time.sleep(_backoff_seconds(attempt))
            continue
        except (ReviewOutputError, ReviewRefusedError) as exc:
            # Persistent: reviewer already did its own repair/refusal handling.
            # Don't spin — record unavailable now.
            logger.warning(
                "file analysis unavailable (non-retryable)",
                extra={"file": file.path, "error": type(exc).__name__},
            )
            return {
                "results": [
                    FileReviewResult(
                        path=file.path,
                        status="unavailable",
                        reason=f"{type(exc).__name__}: {exc}",
                        error_count=transient_failures,
                        model=model,
                    )
                ]
            }
        else:
            return {
                "results": [
                    FileReviewResult(
                        path=file.path,
                        status="reviewed",
                        findings=outcome.findings,
                        error_count=transient_failures,
                        model=outcome.model,
                        input_tokens=outcome.input_tokens,
                        output_tokens=outcome.output_tokens,
                    )
                ]
            }

    # Retry ceiling exhausted on transient failures.
    logger.error(
        "file analysis unavailable after exhausting retries",
        extra={"file": file.path, "attempts": MAX_ATTEMPTS, "error": type(last_error).__name__},
    )
    return {
        "results": [
            FileReviewResult(
                path=file.path,
                status="unavailable",
                reason=f"exhausted {MAX_ATTEMPTS} attempts; last error "
                f"{type(last_error).__name__}: {last_error}",
                error_count=transient_failures,
                model=model,
            )
        ]
    }
