# ADR-0004: In-process background jobs, not a durable queue

- **Status:** Accepted
- **Date:** 2026-07-26
- **Phase:** 9 (webhook-triggered automation & async processing)

## Context

Phase 2 built a webhook endpoint that verifies, dedups and stores deliveries.
Nothing consumed them. Phases 3–8 built the pipeline those events should drive
— diff fetch, multi-node graph review, report synthesis — but the only way to
run it was `scripts/review_pr.py` by hand.

Phase 9 connects the two. The constraint that shapes everything here: GitHub
expects a webhook response in **10 seconds**, and a real review is a diff
fetch plus three LLM passes per file. It is nowhere near 10 seconds. So the
response and the work have to come apart.

## Decision

### 1. FastAPI `BackgroundTasks`, not Celery/RQ/arq

The handler stores the event, claims a review row, schedules
`pipeline.run_review` as a background task, and returns **202 Accepted**
immediately. The job runs in the same process.

`run_review` is a plain **synchronous** function. Every step it takes is
blocking I/O against a sync client — psycopg, httpx, LangGraph's `invoke` — so
`async def` would buy nothing but the obligation to keep it off the event loop
by hand. Starlette runs a sync background callable in its threadpool, so the
event loop stays free while the review runs.

### 2. The accepted gap, stated plainly

**An in-process background task dies with the process.** If the service is
restarted, redeployed, OOM-killed or crashes while a review is running, that
review is lost: its row stays at `running` forever, no comment is ever posted,
and nothing retries it. There is no durable queue, no visibility timeout, no
redelivery.

This is a real gap and it is **accepted for this phase**, not overlooked:

- The blast radius is one PR review, and the recovery is a human calling
  `POST /reviews` with `force=true` — which is exactly why the manual trigger
  endpoint exists rather than being decoration.
- Reviews are minutes long and idempotent-ish in practice: re-running one
  produces another comment, not corruption.
- Phase 10 (Persistence, Idempotency & Observability) is where this closes
  properly — that is where a stuck `running` row becomes detectable (status
  transitions with timestamps and enforcement), where redelivery becomes safe
  (partial unique index on repo + head SHA), and where "a failed review is
  diagnosable from logs/metrics alone" is a stated exit criterion.

Reaching for Celery now would mean adding a broker, a worker process, a
serialization boundary and a deployment topology to a system whose actual
traffic is one developer's test PRs. That is the premature complexity this
roadmap has avoided at every prior decision point (ADR-0002's deterministic
pre-filter over an LLM gate; ADR-0003's plain function over a graph node). The
trigger to revisit is concrete: **when losing a review on restart stops being
acceptable, or when reviews need to survive a deploy, introduce a durable
queue.** Not before.

### 3. A minimal `reviews` table, and a guard that is not idempotency

Answering "fetch review status/result" and "list history" needs persistence, so
this phase adds `reviews`: id, repo, pr_number, head_sha, status, report
(JSONB, null until synthesized), error, review_url, timestamps.

Status is a plain TEXT column. There is **no transition enforcement**, no
partial unique constraint, no token/cost tracking — all Phase 10's job. The
table is sized to this phase's questions and no further.

The duplicate guard is a `WHERE NOT EXISTS` inside the INSERT: a new review is
not created when a `pending`, `running` or `completed` one already exists for
the same (repo, head_sha). `failed` is deliberately absent from that list — a
failed review is precisely what should be re-runnable.

**This is a stopgap, not a race-condition-proof constraint.** It is one
statement, but under READ COMMITTED two concurrent transactions can both see no
existing row and both insert. It handles the case this phase actually has —
GitHub redelivering an event, or `synchronize` firing twice for one push,
seconds apart rather than simultaneously. It does not handle genuine
concurrency. Phase 10 replaces it with a partial unique index, which is the
only thing that actually makes it true.

### 4. Delivery posts one review, not one per file

`delivery.post_review` (Phase 4) takes a single `FileChange` — it was built when
the unit of delivery was a file. Driving the Phase 8 report through it
unchanged would post *N* separate GitHub reviews for an *N*-file PR, which
throws away the single coherent report Phase 8 exists to produce.

So Phase 9 adds `delivery.post_report`: one review for the whole PR, whose body
is the Phase 8 Markdown and whose inline comments are the findings that anchor
to the diff. Both entry points share the anchoring and 422-fallback logic;
`post_review` is untouched and still tested. A finding that anchors appears
both inline and in the body on purpose — the body is the complete record, the
inline comment is the actionable pointer.

### 5. The report is stored before delivery is attempted

`store_report` and `complete_review` are separate calls. The analysis is the
expensive, unrepeatable part; posting to GitHub is the part most likely to fail
(network, permissions, a 422). Persisting first means a delivery failure costs
the comment, not the review — the report is still fetchable from
`GET /reviews/{id}`.

## Consequences

- Webhook responses stay well inside GitHub's timeout regardless of PR size.
- A restart mid-review loses that review, recoverable only by hand. Documented
  above; closed in Phase 10.
- `run_review` catches `Exception` broadly and never propagates. A background
  task has nowhere to raise *to* — an escaped exception would vanish into the
  threadpool and strand the row at `running`. Every failure is recorded on the
  row and logged.
- Two processes serving the same webhook would both accept and both run a
  review for one event. Single-process deployment is an unstated assumption
  until Phase 10's constraint makes it safe.

## What we are NOT doing

- No queue, broker, worker process, or retry-with-backoff at the job level.
- No status state machine or transition validation — Phase 10.
- No `webhook_events`-table polling or replay. The trigger is in-request;
  stored events remain an audit trail, not a work queue.
- No change to duplicate-delivery or non-triggering-event behavior. Those still
  answer exactly what Phase 2 made them answer.
