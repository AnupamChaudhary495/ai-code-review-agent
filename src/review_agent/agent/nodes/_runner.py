"""Shared per-file analysis runner: bounded retry, backoff, error_count.

Both the bug-analysis and security-analysis nodes route their LLM pass through
this one function so resilience logic exists exactly once. LangGraph does not
retry node exceptions for us — this loop is explicit, has a hard iteration
ceiling, and turns any failure into a recorded result rather than an exception
that aborts the graph run.
"""

import logging
import time
from collections.abc import Callable, Sequence

import anthropic

from ...diffing.models import FileChange
from ...reviewer import ReviewOutputError, ReviewRefusedError, ReviewResult
from ...schemas.finding import Finding
from ..state import FileReviewResult
from .router import select_model

logger = logging.getLogger(__name__)

# Hard iteration ceiling. A stuck file fails loudly after this many attempts
# and finishes; it never spins.
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


def run_with_retry(
    file: FileChange,
    review_fn: Callable[[FileChange], ReviewResult],
    source: str,
    seed_findings: Sequence[Finding] = (),
) -> FileReviewResult:
    """Run one LLM analysis pass over `file` with bounded retry.

    `seed_findings` are deterministic findings (e.g. secret-scan hits) that are
    always included in the result — even when the LLM pass is unavailable — so
    high-confidence findings never depend on the model succeeding.
    """
    model = select_model(file)  # seam: today one model, Phase 7 routes by tier
    seed = list(seed_findings)
    transient_failures = 0
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            outcome = review_fn(file)
        except _TRANSIENT as exc:
            transient_failures += 1
            last_error = exc
            logger.warning(
                "transient failure during analysis; will retry",
                extra={
                    "file": file.path,
                    "source": source,
                    "attempt": attempt,
                    "max_attempts": MAX_ATTEMPTS,
                    "error": type(exc).__name__,
                },
            )
            if attempt < MAX_ATTEMPTS:
                time.sleep(_backoff_seconds(attempt))
            continue
        except (ReviewOutputError, ReviewRefusedError) as exc:
            # Persistent: reviewer already handled its own repair/refusal.
            # Don't spin — record now, keeping any deterministic seed findings.
            logger.warning(
                "analysis unavailable (non-retryable)",
                extra={"file": file.path, "source": source, "error": type(exc).__name__},
            )
            return FileReviewResult(
                path=file.path,
                status="unavailable",
                source=source,
                findings=seed,
                reason=f"{type(exc).__name__}: {exc}",
                error_count=transient_failures,
                model=model,
            )
        else:
            return FileReviewResult(
                path=file.path,
                status="reviewed",
                source=source,
                findings=seed + outcome.findings,
                error_count=transient_failures,
                model=outcome.model,
                input_tokens=outcome.input_tokens,
                output_tokens=outcome.output_tokens,
            )

    logger.error(
        "analysis unavailable after exhausting retries",
        extra={
            "file": file.path,
            "source": source,
            "attempts": MAX_ATTEMPTS,
            "error": type(last_error).__name__,
        },
    )
    return FileReviewResult(
        path=file.path,
        status="unavailable",
        source=source,
        findings=seed,
        reason=f"exhausted {MAX_ATTEMPTS} attempts; last error "
        f"{type(last_error).__name__}: {last_error}",
        error_count=transient_failures,
        model=model,
    )
