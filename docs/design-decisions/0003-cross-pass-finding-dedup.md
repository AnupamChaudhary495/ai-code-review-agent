# ADR-0003: Cross-pass finding dedup and deterministic report synthesis

- **Status:** Accepted
- **Date:** 2026-07-24
- **Phase:** 8 (report generation & finding aggregation)

## Context

After Phase 7 the graph returns a flat `list[FileReviewResult]` — up to three
results per file (bug, security, performance), each carrying its own findings,
with no ordering and no relationship between them. Three independent passes
looking at the same diff **will** report the same issue more than once: a raw
SQL string built by concatenation is a bug to the bug pass, an injection to the
security pass, and — if it sits in a loop — a performance problem too. Phase 6
explicitly deferred dedup to here.

Posting that raw list would show a reviewer the same line three times in three
different voices. The job of this phase is to decide, deterministically, when
two findings are *the same issue* and when they are two issues that happen to
share a line.

## Decision

### 1. Identity anchor: exact `(file, line)`

Findings are bucketed by exact file path and exact line number. Nothing merges
across files, and nothing merges across lines — not even adjacent ones.
File-level findings (`line is None`) form their own bucket per file.

Line-fuzzy matching (± a few lines) was rejected: two passes reporting the
*same* issue overwhelmingly agree on the line, because they are both reading
the same diff with line numbers in it. Fuzzing the anchor mostly buys the
ability to swallow a genuinely different nearby finding.

### 2. Sameness within a bucket: message-token overlap, not category

Two findings on the same `(file, line)` are the same issue when either:

- their normalised messages are identical, or
- they agree on negation (below) **and** their content-word sets (lowercased,
  stopwords removed) share **≥ 3 tokens** at an **overlap coefficient ≥ 0.6**.

**Category is deliberately not part of the identity test.** Keying on category
would preserve exactly the duplicate this phase exists to remove — the bug pass
and the security pass describing one SQL injection under two category labels
is the *central* case, not an edge case. Distinctness therefore comes from the
message text instead.

#### Overlap coefficient, not Jaccard

The similarity metric is `|A∩B| / min(|A|,|B|)` — "is the shorter message
essentially contained in the longer one" — not Jaccard's `|A∩B| / |A∪B|`.

Jaccard was implemented first and rejected on measurement. It penalises two
passes for describing one issue at different lengths, which is exactly what
they do: the security pass writes longer, CWE-flavoured messages than the bug
pass. One real SQL injection reported by both scored **0.55 Jaccard** — below
any threshold that would still keep distinct findings apart — while scoring
**0.75 overlap**. A metric that punishes a pass for being more thorough is
measuring the wrong thing. The regression is pinned by
`test_verbose_and_terse_wording_of_one_issue_still_merges`.

#### Guards against over-merging

- **Negation parity.** Opposite claims share almost every content word: "the
  path is sanitised" and "the path is *not* sanitised" score 1.0 on any overlap
  metric, so word counting cannot separate them. Negation is therefore checked
  separately, and two messages merge only if both are negated or neither is.
  Parity of *presence*, not equality of markers — so "no timeout is set" and
  "the call runs without a timeout" (both negated, different words) can still
  merge, while a claim and its negation cannot.
- **A 3-shared-token floor.** The overlap coefficient is generous to short
  messages — a two-word finding contained in a long one scores 1.0. The floor
  stops "Unused import." and "Unused variable." from merging on function words.

0.6 is loose enough to merge two passes describing one injection in different
words, tight enough that a null-dereference and an authorization gap on the
same line stay separate. Both directions are pinned by tests in
`tests/test_report_synthesis.py`; the threshold is not a free parameter to be
tuned without a test failing.

### 3. Merge outcome: worst severity wins, nothing is destroyed

Within a cluster the survivor is chosen by **severity first**, then category
precedence (`security > bug > performance > quality`), then source name, then
normalised message. Consequences:

- **Severity is never downgraded** by a duplicate that a less confident pass
  rated lower. Two passes disagreeing between `high` and `medium` yields
  `high`.
- **Security framing wins ties** — it carries the CWE and the more actionable
  wording.
- A missing `suggestion` or `cwe` on the winner is **backfilled** from the
  losers. Merging must not lose information the reviewer could act on.
- The merged finding records `sources` (every pass that reported it) and
  `duplicates_merged` (how many collapsed), and the Markdown renderer surfaces
  both as "flagged by bug + security". The dedup is auditable in the output
  rather than a silent deletion.

Clustering runs over a pre-sorted bucket, so the same input set produces the
same clusters regardless of which node finished first — parallel fan-out must
not make the report non-reproducible.

## Also decided here

- **Synthesis is a plain function, not a graph node.** `reporting/synthesis.py`
  is called after `graph.review_files()` returns. There is no fan-out and
  nothing to parallelise — one call, one output — so a node would buy state
  plumbing and a reducer entry in exchange for nothing.
- **No LLM anywhere in the report path.** The summary, the verdict, the
  severity tally and the per-file "what changed" line are arithmetic and string
  assembly over finding counts and the Phase 3 diff metadata. A report is free,
  instant, and byte-for-byte reproducible — which is also what makes Phase 10's
  idempotency checks meaningful.
- **"What changed" is a restatement, never an inference.** `describe_change`
  reads `change_type`, `additions`, `deletions`, `language`, `is_binary`,
  `patch_omitted` off the `FileChange` and formats them. With no `FileChange`
  available it degrades to "Change details unavailable." rather than inventing
  a description of code it has not seen.
- **`ReportFinding` subclasses `Finding`.** Anything already accepting
  `list[Finding]` — notably `github.delivery.post_review` — accepts these
  unchanged, so Phase 9 wires a report into delivery instead of migrating a
  schema.

## Known limitation

The rule is biased toward **under-merging**, and the bias is deliberate: a
duplicate costs a reviewer ten seconds, a dropped finding costs them the bug.
The visible consequence is that two passes describing one defect in nearly
disjoint vocabulary stay separate — "`or "superuser"` is always truthy" and
"the role check grants privileged access to every user" are the same defect but
share only three content words. No token-counting metric can close that gap;
doing so needs semantics, which means embeddings or a model call, which costs
the determinism the rest of the report is built on. Recorded here rather than
hidden, because the next person to look at a report with two near-identical
findings should know it is a chosen trade-off and not an oversight.

## Alternatives considered

- **Key on `(file, line, category)`.** Trivial and wrong: it preserves the
  cross-pass duplicate that motivated the phase.
- **Merge everything on the same line, unconditionally.** Cheapest possible
  rule; rejected because a null-deref and an authz gap can legitimately land on
  one line, and losing a real finding is far worse than showing two.
- **Jaccard similarity.** The obvious first choice; measured and replaced (see
  above). Kept in this ADR because "we tried the obvious metric and it failed
  on the central case" is the useful part of the record.
- **Embedding or LLM similarity.** Better semantic recall, but non-deterministic
  and it spends a model call to decide whether two model calls agreed. It also
  breaks the reproducibility property the rest of the report depends on.
- **Stemming the tokens.** Would let `concatenated`/`concatenating` unify and
  rescue Jaccard part-way. Rejected as the wrong fix for the observed failure —
  the problem was message *length* asymmetry, not morphology, and a hand-rolled
  suffix stripper is a second heuristic to maintain and mis-tune.
- **Report the duplicates and let the human dedup.** Honest, but it is the
  status quo this phase exists to replace.

## What we are NOT doing

- No delivery, no webhook wiring, no `ingest.py` changes — Phase 9.
- No persistence of reports — Phase 10.
- `github/delivery.py` still renders single-file findings from Phase 4 and
  keeps its own severity-badge table. The two agree deliberately; collapsing
  them belongs to the phase that changes delivery's input type.
