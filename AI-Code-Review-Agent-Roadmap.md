# AI Code Review & Pull Request Automation Agent — Engineering Roadmap

**Project type:** Portfolio-grade, production-oriented AI engineering system
**Owner:** Daddy
**Status:** Planning
**Last updated:** 2026-07-08

## Purpose

An AI-powered agent that automatically reviews GitHub Pull Requests: reads diffs and commits, explains changes in plain language, detects bugs, flags security vulnerabilities, evaluates code quality and performance, and produces structured review reports (with GitHub comment posting as a later phase).

This is not a tutorial project. Every phase should be built to demonstrate real engineering judgment — trade-off reasoning, cost control, idempotency, observability, and security — not just a working demo.

## Governing Principles

- **End-to-end first.** Get a single file through the full loop (fetch → diff → LLM review → output) before generalizing to full PRs or repos.
- **Avoid premature complexity.** Agent memory and advanced LangGraph features (multi-agent graphs, cyclic reasoning, persistence layers) are not introduced until a concrete need forces them.
- **Justified tooling.** LangGraph is used only where its state-machine/branching model earns its cost over a plain function pipeline. Each phase that touches orchestration should explicitly answer "why LangGraph here."
- **Foundations are first-class.** Idempotency, structured logging, and security are designed in from Phase 1, not bolted on before deployment.

## Suggested Architecture (modules)

GitHub Integration · Diff Parser · Repository Analyzer · AI Review Engine · Security Analysis · Performance Analysis · Report Generator · FastAPI Backend · Configuration Management · Logging & Monitoring

## Tech Stack

- **Backend:** Python, FastAPI
- **AI:** LangGraph (conditional), LangChain (only where it removes real boilerplate), configurable LLM providers (Claude / OpenAI / Gemini)
- **GitHub:** REST API, Webhooks, GitHub App auth
- **Data:** PostgreSQL (review history, idempotency keys), Redis (queueing/caching, optional)
- **Deployment:** Docker, GitHub Actions

---

## Phase 1 — Foundations & Environment Setup

**Goal:** A clean, reproducible project skeleton before any AI logic exists.

- Repo scaffolding: `src/` layout, `pyproject.toml`, pre-commit (ruff/black/mypy), pytest config
- Configuration management module (pydantic-settings): env-driven, no hardcoded secrets
- Structured logging set up from day one (JSON logs, correlation IDs)
- Base FastAPI app with a `/health` endpoint
- Dockerfile + docker-compose for local dev (app + Postgres)

**Exit criteria:** `docker compose up` boots a working, empty FastAPI service with health checks and structured logs.

## Phase 2 — GitHub Integration Basics

**Goal:** Authenticate and pull real data from GitHub.

- Decide: Personal Access Token (fast start) vs. GitHub App (production-correct, needed for org installs and posting comments later) — document the trade-off and pick PAT for MVP, GitHub App for Phase 9+
- Client wrapper around GitHub REST API: fetch PR metadata, commit list, changed files, file diffs
- Rate-limit handling and retries (exponential backoff)
- Unit tests using recorded/mocked API responses (no live calls in CI)

**Exit criteria:** Given a PR URL, the service can pull and print the full diff and file list.

## Phase 3 — End-to-End MVP Loop (single file)

**Goal:** Prove the full pipeline on the smallest possible slice before any architecture generalization. This is the highest-priority milestone in the roadmap.

- Take one changed file's diff → send to an LLM with a minimal review prompt → return plain-text findings
- No database, no queue, no multi-file handling yet — deliberately thin
- Manual trigger only (CLI script or a single POST endpoint)

**Exit criteria:** Running one command against a real PR produces a coherent, useful review comment for one file. This is the demo that proves the concept works before investing further.

## Phase 4 — Diff Parser & Repository Analyzer

**Goal:** Generalize from "one file" to "the whole PR" reliably.

- Robust unified-diff parser (added/removed/context lines, hunks, renames, binary files)
- Repository Analyzer: language detection, file categorization (test vs. source vs. config), optional surrounding-context retrieval for better review accuracy
- Handle edge cases: huge diffs (truncation/chunking strategy), deleted files, generated files (lockfiles, vendored code) — explicitly excluded by config

**Exit criteria:** Parser + analyzer produce a clean, structured representation of an entire PR, tested against real-world messy diffs.

## Phase 5 — AI Review Engine

**Goal:** Turn raw diffs into structured, reliable model output — not free-text prose.

- Prompt design per concern (correctness/bugs, explanation) with structured output (Pydantic schema + function calling / tool-use, not regex-parsed text)
- Configurable LLM provider layer (Claude default, pluggable others) via a thin abstraction — not a hard dependency on one vendor SDK
- Chunking strategy for large PRs (per-file vs. batched) with cost/token accounting
- Golden-set test cases: known-buggy diffs with expected findings, used as regression tests for prompt changes

**Exit criteria:** The engine returns structured, schema-validated findings for a multi-file PR, with token cost logged per review.

## Phase 6 — Security Analysis Module

**Goal:** Dedicated, higher-precision pass for security-relevant patterns.

- Rule-based pre-filter (e.g. known-bad patterns: hardcoded secrets, SQL string concatenation, unsafe deserialization, missing auth checks) combined with LLM judgment — don't rely on the LLM alone for security-critical findings
- Optional integration point for existing SAST tools (e.g. Semgrep) as a complementary signal, not a replacement
- Severity classification (critical/high/medium/low) with justification text

**Exit criteria:** Module correctly flags a curated set of intentionally vulnerable sample diffs with low false-negative rate on the golden set.

## Phase 7 — Performance & Code Quality Analysis

**Goal:** Round out review coverage beyond correctness/security.

- Performance heuristics (N+1 query patterns, unbounded loops, obvious algorithmic red flags) + LLM reasoning for subtler cases
- Code smell / maintainability checks (duplication, function length/complexity, naming) — can lean on existing linters where a linter already solves it; don't reinvent linting with an LLM
- Clear separation: what's a linter's job vs. what's the AI's job (cost and reliability trade-off, documented explicitly)

**Exit criteria:** Reports distinguish linter-sourced findings from AI-sourced findings, each attributed correctly.

## Phase 8 — Report Generator

**Goal:** Turn structured findings into a report a human actually wants to read.

- Structured report schema: summary, per-file findings grouped by severity/category, plain-language "what changed" section
- Output formats: Markdown (for GitHub comments) and JSON (for API consumers / storage)
- Deterministic, testable rendering (template-based, not another LLM call)

**Exit criteria:** A full PR review renders as a clean Markdown report suitable for posting as-is.

## Phase 9 — FastAPI Backend & Webhook Processing

**Goal:** Move from manual trigger to a real automated service.

- GitHub webhook endpoint (PR opened/synchronize events), signature verification
- Async job handling so webhook responses return fast (202 Accepted) while review runs in background (in-process background task first; queue in Phase 10 if load demands it)
- Switch GitHub auth to GitHub App (needed to post comments as the bot, not a personal account)
- API endpoints: trigger review, fetch review status/result, list review history

**Exit criteria:** Opening/updating a real PR on a test repo triggers an automatic review end-to-end, no manual steps.

## Phase 10 — Persistence, Idempotency & Observability

**Goal:** Make the system trustworthy under real, repeated, concurrent traffic.

- PostgreSQL schema: reviews, findings, PR metadata, review status, token/cost tracking
- Idempotency: webhook retries and duplicate `synchronize` events must not trigger duplicate reviews or duplicate comments (idempotency key = PR + commit SHA)
- Observability: structured logs with correlation IDs, basic metrics (review latency, token cost, failure rate), error handling that fails loud and traceable, not silent
- Redis introduced here only if background-task approach hits real concurrency limits — justify before adding

**Exit criteria:** Replaying the same webhook event twice produces exactly one review; a failed review is visible and diagnosable from logs/metrics alone.

## Phase 11 — LangGraph Orchestration (conditional)

**Goal:** Introduce graph-based orchestration only where a linear pipeline genuinely breaks down.

- Explicit decision point: does the review flow need branching/looping (e.g., re-review after clarifying question, multi-pass escalation for ambiguous findings)? If the answer is no, skip this phase and keep the Phase 5–8 pipeline as plain function composition.
- If justified: model the review as a LangGraph graph (nodes per analysis type, conditional edges for severity escalation), with the trade-off vs. the simple pipeline documented in an ADR
- Guard against scope creep: no agent memory, no autonomous multi-agent negotiation unless a concrete use case demands it

**Exit criteria:** Either a documented decision to not use LangGraph, or a working graph with a clear ADR explaining what it buys over the linear version.

## Phase 12 — Deployment, CI/CD, Documentation & Portfolio Polish

**Goal:** Ship it like a real product, not a script that ran once on a laptop.

- Docker image finalized, docker-compose for full local stack (app, Postgres, optional Redis)
- GitHub Actions: lint, type-check, test, build image, (optionally) deploy on merge to main
- Documentation set: README (setup + usage), architecture diagram, API docs (FastAPI auto-docs + written overview), design-decisions doc (the ADRs from Phases 2/11 etc.), deployment guide, "future improvements" section
- Final security pass: secrets management review, dependency audit, webhook signature verification double-checked
- Portfolio framing: a short write-up of the hardest engineering decisions (idempotency, LangGraph go/no-go, cost control) — this is what differentiates it from a tutorial clone

**Exit criteria:** A stranger can clone the repo, follow the README, and get a working local instance reviewing a real PR within 15 minutes.

---

## Open Decisions to Revisit

- PAT vs. GitHub App timing (currently: PAT for MVP, App by Phase 9)
- Whether Redis is ever actually needed, or in-process background tasks suffice through Phase 12
- LLM provider default and fallback strategy under rate limits/outages
- How much SAST-tool integration (Phase 6) is worth the added infra vs. LLM-only security review
