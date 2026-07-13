# ADR-0001: LangGraph for multi-file review orchestration

- **Status:** Accepted
- **Date:** 2026-07-12
- **Phase:** 5 (multi-file fan-out)

## Context

Phase 4 produces a review for **one** file: `reviewer.review_file(FileChange)`
makes one LLM call and returns validated `Finding`s. A real PR has many files.
We need to run that per-file logic across every eligible file in a
`PullRequestDiff` (Phase 3's `diff_fetcher` output) in a single invocation,
in parallel, with one file's failure not taking the others down.

The roadmap is deliberately conservative about LangGraph. Its Governing
Principles say LangGraph "is used only where its state-machine/branching model
earns its cost over a plain function pipeline," and it places graph
orchestration in a later, explicitly *conditional* phase whose exit criteria
is "either a documented decision to not use LangGraph, or a working graph with
a clear ADR explaining what it buys over the linear version." This ADR is that
document, and adopting the graph here **pulls that decision forward** from its
original later-phase slot — a deliberate reordering, recorded honestly rather
than presented as if the roadmap already mandated it.

## Decision

Adopt LangGraph now for the review-orchestration layer, with the current
graph being a fan-out/gather over per-file bug analysis.

## Why a graph, honestly

**Fan-out alone does not justify LangGraph.** Running N independent
`review_file` calls concurrently is `asyncio.gather` or a `ThreadPoolExecutor`
— a few lines, no new dependency. If parallelism were the *only* requirement,
the roadmap's minimalism principle would say: don't add LangGraph. We take
that objection seriously; it is the strongest argument against this decision.

**What actually earns the graph is where the pipeline is about to go.** The
next two phases turn a flat "review each file" loop into a genuine branching,
multi-node, per-file pipeline:

- **Phase 6 (security):** a second analysis pass per file, with a rule-based
  pre-filter feeding an LLM pass — a distinct node with its own retry and
  failure semantics, whose results merge with bug findings.
- **Phase 7 (performance/quality):** a third per-file pass, partly
  linter-sourced and partly LLM-sourced, attributed separately.
- **Size-tier routing (this phase's stub):** small/medium/large/huge files
  should route to different handling (single prompt vs. chunked vs.
  summary-only) and different model tiers for cost control. That is
  conditional edges keyed on file classification — the thing a graph
  expresses natively and a gather does not.

Building the `StateGraph` seam now, with bug-analysis as the first node type,
means Phases 6–7 add nodes and conditional edges to an existing graph instead
of retrofitting one onto a pile of `asyncio.gather` calls. The graph concepts
that pay for themselves are:

1. **Typed accumulation across parallel branches** — a state reducer collects
   per-file results from concurrently-executing nodes into one list, without
   manual future-joining and result-ordering glue.
2. **Per-node retry/backoff and partial-failure isolation as first-class
   concerns** — each file's analysis is a node that can exhaust its own
   bounded retries and return an "analysis unavailable" result while its nine
   siblings finish intact.
3. **Conditional routing** — dispatch decides per file which analysis path and
   model tier apply; more branches arrive in Phases 6–7.

## What we are explicitly NOT doing here (scope guard)

- **No checkpointer / `PostgresSaver` / persistence.** State lives in memory
  for one run. Durable state is a Phase 9/10 concern; borrowing it now is
  premature.
- **No cross-file aggregation into one review, and no delivery.** The graph
  produces a *list of per-file results with a log trail* — nothing more.
  Aggregation is Phase 8, delivery already exists (Phase 4) but is not wired
  in here.
- **No reliance on framework retry.** LangGraph does **not** retry node
  exceptions automatically. Retry/backoff is explicit in our node with a hard
  iteration ceiling and an `error_count` recorded in the result. Treating the
  framework as if it retries is exactly how this silently breaks in
  production.
- **No agent memory, no cyclic reasoning, no multi-agent negotiation.**

## Consequences

- New dependency (`langgraph`) and its transitive deps enter the project.
- Contributors must understand `StateGraph`, `Send`-based fan-out, and
  reducers to modify orchestration.
- In exchange, Phases 6–7 extend a graph rather than rebuild orchestration,
  and per-file failure isolation / retry / routing are structural rather than
  bolted on.

## Alternatives considered

- **`asyncio.gather` / `ThreadPoolExecutor` fan-out.** Simplest for *today's*
  requirement; rejected because it would be replaced by a graph within two
  phases once conditional multi-node routing lands, and the retrofit cost
  (moving retry, routing, and result-merging into a graph later) exceeds the
  cost of the seam now. If Phases 6–7 were cancelled, this alternative would
  be the correct choice and this ADR should be revisited.
- **Keep the linear per-file loop and skip the graph entirely.** Valid per the
  roadmap's "skip this phase if a linear pipeline suffices" clause; rejected
  for the same forward-looking reason.
