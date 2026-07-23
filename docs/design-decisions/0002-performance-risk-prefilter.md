# ADR-0002: A risk pre-filter for the performance LLM pass

- **Status:** Accepted
- **Date:** 2026-07-12
- **Phase:** 7 (performance analysis)

## Context

Phase 6 added a second per-file LLM pass (security) alongside the Phase 5 bug
pass. Both run **unconditionally** on every eligible file. Phase 7 adds a third
pass — performance analysis (N+1 queries, needless loops, algorithmic
blowups). Running a third unconditional LLM pass on every file would grow
per-PR review cost by ~50% (two passes → three) for a class of issue that
simply cannot occur in most changed files: a config edit, a constant, a
docstring, a small pure function have no performance surface at all.

Unlike security — where a hardcoded secret can appear in any one-line change,
so every file warrants a look — performance risk is concentrated in files that
actually do repeated or expensive work. That asymmetry is what justifies
filtering the performance pass but not the others.

## Decision

Gate the performance LLM pass behind a deterministic pre-filter,
`agent/heuristics/perf_risk_filter.py`. A file is sent to the performance node
only if its changed lines show a performance surface:

1. **Loop constructs** — `for` / `while` / comprehensions in the added code.
2. **DB/ORM calls** — query/execute/filter/session/cursor/`.objects.`/raw SQL
   patterns (the substrate of N+1 and query-in-a-loop problems).
3. **Size** — the change is `large`/`huge` by the Phase 3 size tier
   (> 300 changed lines), where scale alone makes a perf look worthwhile.

Files that fail the filter are **not** silently dropped: the router records a
`FileReviewResult(source="performance", status="skipped", reason=...)` for
each, so a PR with no performance commentary is provably a filter decision,
not a gap.

## Trade-off (the point of this ADR)

This is an explicit **cost vs. coverage** decision:

- **Cost saved:** measured ≥40% fewer performance-node invocations than
  sending every eligible file, on the current fixture corpus. That is a direct
  reduction in LLM spend and latency for the performance concern.
- **Coverage risk:** a filter that excludes a file with a real performance bug
  is worse than no filter. The mitigation is that the filter's positive
  signals (loops, DB calls, size) are exactly the necessary conditions for the
  problems the performance pass targets — an N+1 requires a loop and a query;
  an O(n²) requires a loop. A regression test asserts the golden-set N+1 and
  O(n²) files route **through** the filter to the node, so a future tightening
  that would exclude them fails CI.

The filter is deliberately biased toward recall (over-including) rather than
precision: a heuristic keyword hit sends the file to the LLM, which then
decides whether there is a real issue. False positives cost one extra LLM call;
false negatives lose a real finding.

## What we are NOT doing

- No attempt to *detect* the performance issue in the filter itself — it only
  decides worthiness. Detection is the LLM's job (`performance_review_v1.md`).
- No new schema — findings reuse `Finding` with `category="performance"`.
- No third resilience implementation — the node routes through the shared
  `run_with_retry` used by bug and security.

## Alternatives considered

- **Run performance unconditionally like bug/security.** Simplest; rejected on
  cost — a third pass on files that cannot have performance issues is pure
  waste, and the roadmap calls for an explicit cost/coverage trade-off here.
- **An LLM-based "is this worth reviewing" gate.** Defeats the purpose — it
  spends a model call to decide whether to spend a model call. A deterministic
  keyword/size filter is free.
