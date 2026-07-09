# AI Code Review Agent

An AI-powered agent that reviews GitHub pull requests. This is the **Phase 1
vertical slice** of the [roadmap](AI-Code-Review-Agent-Roadmap.md): the
smallest possible end-to-end loop, built with the foundations
(idempotency, structured logging, webhook security) in place from day one.

## What it does

```
GitHub PR webhook (opened / synchronize / reopened)
  └─> verify HMAC signature (X-Hub-Signature-256)
      └─> idempotency claim in PostgreSQL (repo + PR + head SHA)
          └─> fetch the diff of ONE changed file (GitHub REST API)
              └─> ONE LLM call (Claude, via the anthropic SDK)
                  └─> persist + return the review comment in the response
```

Duplicate webhook deliveries (GitHub retries, repeated `synchronize` events
for the same commit) return the stored review instead of triggering a second
LLM call. Failed reviews are re-claimable, so GitHub's "Redeliver" button acts
as a retry.

## Quickstart (Docker)

```bash
cp .env.example .env   # fill in GITHUB_WEBHOOK_SECRET, ANTHROPIC_API_KEY, GITHUB_TOKEN
docker compose up --build
curl http://localhost:8000/health
```

## Quickstart (local)

Requires Python 3.12+ and a PostgreSQL reachable at `DATABASE_URL`
(`docker compose up db` works for just the database).

```bash
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
cp .env.example .env          # fill in secrets
uvicorn review_agent.main:app --port 8000
```

## Pointing GitHub at it

1. Expose port 8000 publicly (e.g. `smee.io` or `ngrok http 8000`).
2. Repo → Settings → Webhooks → Add webhook:
   - Payload URL: `https://<public-url>/webhook/github`
   - Content type: `application/json`
   - Secret: the same value as `GITHUB_WEBHOOK_SECRET`
   - Events: "Pull requests"
3. Open a PR. The webhook response (visible under "Recent Deliveries")
   contains the review comment. Posting the comment back to GitHub is a
   later phase by design.

Simulating a delivery without GitHub:

```bash
BODY='{"action":"opened","repository":{"full_name":"owner/repo"},"pull_request":{"number":1,"head":{"sha":"HEAD_SHA"}}}'
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$GITHUB_WEBHOOK_SECRET" | cut -d' ' -f2)"
curl -s http://localhost:8000/webhook/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: pull_request" \
  -H "X-GitHub-Delivery: manual-test-1" \
  -H "X-Hub-Signature-256: $SIG" \
  -d "$BODY"
```

## Phase 2: webhook ingestion & GitHub App auth

`POST /webhooks/github` is the ingestion endpoint: it verifies the HMAC
signature, dedupes by GitHub's `X-GitHub-Delivery` GUID (unique constraint on
`webhook_events.delivery_id` — a replayed delivery is exactly one row, always),
persists the raw payload as JSONB, and returns 200 immediately. No processing
is attached yet; that arrives in Phase 3+.

`review_agent/github/auth.py` + `client.py` implement GitHub App
authentication: a short-lived RS256 app JWT is exchanged for an installation
access token, which is cached per installation and refreshed before expiry.

### Registering the GitHub App (one-time, manual)

GitHub → Settings → Developer settings → GitHub Apps → **New GitHub App**:

1. Webhook URL: `https://<public-url>/webhooks/github`; webhook secret: the
   value of `GITHUB_WEBHOOK_SECRET`.
2. Repository permissions — least privilege, nothing speculative:
   - **Contents: Read-only**
   - **Pull requests: Read and write**
   - **Metadata: Read-only** (mandatory default)
3. Subscribe to events: **Pull request**.
4. After creation: copy the App ID into `GITHUB_APP_ID`, generate a private
   key and put the PEM into `GITHUB_APP_PRIVATE_KEY` (never commit it), then
   install the App on the repos you want reviewed.

## Configuration

| Env var | Required | Description |
| --- | --- | --- |
| `GITHUB_WEBHOOK_SECRET` | yes | Shared secret for webhook signature verification. Endpoint returns 503 until set. |
| `ANTHROPIC_API_KEY` | yes* | API key for the review LLM call. (*Optional if the SDK can resolve credentials another way.) |
| `GITHUB_TOKEN` | for private repos | PAT with repo read access (Phase 1 slice). Public repos work unauthenticated at low rate limits. |
| `GITHUB_APP_ID` | for App auth | GitHub App ID (Phase 2). |
| `GITHUB_APP_PRIVATE_KEY` | for App auth | GitHub App private key PEM; `\n` escapes allowed (Phase 2). |
| `LLM_MODEL` | no | Defaults to `claude-opus-4-8`. Must support adaptive thinking (Claude 4.6+). |
| `DATABASE_URL` | no | Defaults to local PostgreSQL; overridden in docker-compose. |
| `LOG_LEVEL` | no | Defaults to `INFO`. |

## Tests

```bash
pytest                        # unit tests, no external services needed
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/review_agent pytest
                              # additionally runs the SQL idempotency tests
```

## Observability

All logs are single-line JSON on stdout. Every request gets a
`correlation_id` — GitHub's `X-GitHub-Delivery` GUID when present — which is
also echoed back as the `X-Correlation-ID` response header. LLM calls log
token usage (`input_tokens` / `output_tokens`). Failures are stored on the
review row (`status = 'failed'`, `error`) and logged with stack traces;
HTTP error responses carry a generic message only.

## Deliberately not built yet (per roadmap)

- Multi-file / whole-PR review, diff parsing, repository analysis (Phases 4+)
- Structured findings, report generation, comment posting (Phases 5, 8, 9)
- LangGraph or any orchestration framework (Phase 11, conditional)
- Async job queue / 202-accepted webhook handling (Phase 9) — the review runs
  synchronously inside the webhook request, which can exceed GitHub's 10s
  webhook timeout; GitHub marks the delivery as timed out but the review
  still completes and duplicates are absorbed by idempotency
- Rate-limit retries, richer GitHub client (later phases)
- Wiring ingested `webhook_events` to the review pipeline (Phase 3+) — the
  Phase 1 slice endpoint (`/webhook/github`) and the Phase 2 ingestion
  endpoint (`/webhooks/github`) coexist until then
- Redis, agent memory, multi-agent anything
