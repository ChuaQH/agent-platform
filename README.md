# Agent Platform — Assistant Agent

A personal research project that builds a general-purpose chatbot agent:
a Next.js chat UI talking to a FastAPI backend that runs a bounded
tool-calling loop against the DeepSeek API (OpenAI-compatible), with
persistent conversation history, semantic memory, and per-user skill/tool
toggles.

The repository also contains an early coding-agent CLI prototype
(`src/aca/`); the Assistant Agent is the working application and the focus
of this documentation.

## What problem is this exploring?

The interesting engineering problems here are the ones that appear when an
LLM is wrapped in a usable system:

- how tools should be described, exposed, and **authorized per user**;
- how a model-driven loop is kept **bounded and observable** (turn budget,
  timeouts, explicit terminal states);
- how conversation context is built and **compacted** when it outgrows the
  model's window;
- how in-memory execution state relates to **durable** state;
- how retries and concurrent requests stay **idempotent** at the database;
- how a schema with real constraints is **migrated and verified** instead
  of ad-hoc created.

The goal is not to build the most complex architecture possible, but to
test which patterns actually hold up for a single-user assistant used in
practice.

## Main capabilities

- **Chat with streaming** — SSE (`reasoning`, `tool_call`, `tool_result`,
  `text`, `usage`, `done`) with explicit `error`/`limit_reached` terminal
  semantics.
- **Tool calling** — a ReAct loop over 19 tools (calculator, web search,
  web fetch, file I/O, currency/weather/unit conversion, personal memory,
  skill content, and more), bounded by `max_agent_turns`.
- **Skills** — markdown guidance injected on demand via the
  `get_skill_content` tool; per-user enable/disable.
- **Tool toggles** — per-user enable/disable enforced at tool *execution*,
  not just schema visibility.
- **Persistent threads** — messages, reasoning, tool calls, and LLM call
  logs stored in PostgreSQL.
- **Context management** — static-prefix assembly for prompt caching and
  two-pass compaction (tool-output pruning → full summarization).
- **Personal memory** — facts with pgvector embeddings (bge-m3), queried
  only through tools.
- **Auth** — email/password (pbkdf2) and optional Google OAuth, opaque
  session tokens.
- **Reliability plumbing** — request idempotency (client message ID +
  database uniqueness), `/health` liveness and `/ready` readiness probes,
  migrations as the sole schema mechanism, CI.

## Architecture

The backend is split into three layers plus a shared core:

```mermaid
flowchart TB
    UI["Next.js / assistant-ui"] -->|"POST /api/chat"| ROUTES["FastAPI server.py<br/>auth · setup · SSE"]
    ROUTES --> CTX["ContextBuilder<br/>static prefix · compaction"]
    ROUTES --> STREAM["chat_stream.py<br/>persistence + SSE protocol"]
    STREAM --> RUNNER["AgentRunner<br/>ReAct loop"]
    RUNNER --> LLM["LLMClient"] --> API["DeepSeek API"]
    RUNNER --> CONV["Conversation<br/>in-memory model-visible state"]
    RUNNER --> GATEWAY["RegistryExecutionGateway<br/>schema · policy · budget · approval"]
    GATEWAY --> REG["request-local ToolRegistry<br/>(filtered per user)"]
    REG --> TOOLS["19 tools"]
    GATEWAY --> RUNS["durable agent runs<br/>actions · approvals · events"]
    RUNS --> INSPECTOR["Owner-scoped Run Inspector"]
    STREAM --> PG[("PostgreSQL + pgvector<br/>Alembic head 007")]
    CTX --> PG
    TOOLS --> PG
    RUNNER -->|"events"| STREAM
    ROUTES -->|"SSE"| UI
```

**Layer rules** (enforced by `scripts/check_imports.py`):

```text
src/core/      ← shared library; imports nothing internal
src/assistant/ ← FastAPI backend; imports core only
src/aca/       ← coding-agent prototype; imports core only
```

Ownership boundaries: `server.py` owns HTTP/auth/request setup;
`AgentRunner` owns the model loop and in-memory `Conversation` mutation;
`chat_stream.py` owns SSE wire format and progressive persistence
coordination; `ContextBuilder` owns model-facing context; stores own all
SQLAlchemy persistence.

## How a chat request flows

1. `POST /api/chat` with `messages`, optional `thread_id`, optional
   `client_message_id` → Bearer-token auth → thread resolved or created
   (id returned in `X-Thread-Id`).
2. **Idempotency gate** — an already-accepted `(thread_id,
   client_message_id)` returns `409` before any LLM work; the database
   unique index is the concurrency guard.
3. The user message is persisted and committed immediately.
4. Enabled tools/skills are loaded; the full tool registry is built and
   filtered per request (non-mutating `filtered()`).
5. `ContextBuilder` compacts the thread if it exceeds the budget, then
   assembles the static prefix (system prompt → tool policy → response
   prefs → checkpoint summary → enabled skills).
6. `AgentRunner` runs the loop: LLM call → reasoning/text/tool events →
   tool execution (bounded by `tool_timeout`) → results appended to
   `Conversation` → next turn, up to `max_agent_turns`.
7. Each event is persisted progressively and emitted as SSE; the stream
   ends in exactly one terminal state (`done`, `limit_reached → usage →
   done`, or `error`).

Key distinction: **`Conversation` is the model-visible in-memory execution
state; PostgreSQL is the durable state.** Persistence is progressive —
there is no single transaction wrapping a whole generation.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for diagrams and the
numbered flow, and [`docs/API.md`](docs/API.md) for the endpoint/SSE
reference.

## Technologies and why

| Area | Choice | Why |
|---|---|---|
| Backend | FastAPI + Uvicorn (async) | Native async streaming (SSE), typed request models, simple dependency injection |
| LLM | DeepSeek via OpenAI-compatible SDK | One well-known client interface; reasoning + function calling |
| Streaming | Server-Sent Events | One-directional response streaming over plain HTTP (ADR-001) |
| Durable state | PostgreSQL + pgvector | Relational data + constraints + transactions + in-DB vector search (ADR-002) |
| ORM / migrations | async SQLAlchemy + Alembic | Explicit, versioned, verifiable schema (ADR-007) |
| Embeddings | bge-m3 (sentence-transformers) | 1024-dim multilingual embeddings for memory recall |
| Frontend | Next.js 16 + assistant-ui | Rapid chat-UI development; `useExternalStoreRuntime` fits a custom SSE backend |
| Auth | stdlib pbkdf2 + opaque tokens | No auth dependency for a personal project; Google OAuth optional |

## Running locally

Prerequisites: Python 3.11+, Node.js 20+, Docker (for PostgreSQL), a
DeepSeek API key.

```bash
# 1. PostgreSQL with pgvector
docker compose up -d

# 2. Backend
python -m venv .venv && .venv\Scripts\activate    # Windows; or source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env                              # then fill in DEEPSEEK_API_KEY etc.

# 3. Database schema (Alembic is the only schema mechanism)
alembic upgrade head

# 4. Backend server (http://localhost:8000)
python -m uvicorn assistant.server:app --host 0.0.0.0 --port 8000

# 5. Frontend (http://localhost:3000) — in another terminal
cd apps/assistant-web
npm ci
npm run dev
```

For development there is also `python dev.py`, which starts both the
backend (port 8000) and the frontend (port 3000) and stops both process
trees on Ctrl+C.

> The server **refuses to start** against a missing or unmigrated database
> (explicit error telling you to run `alembic upgrade head`). It never
> creates schema at runtime and never runs migrations itself.

## Database and migrations

- Alembic owns the schema; the full history is `001` → `010` (head).
- New schema changes: edit the SQLAlchemy models, then
  `alembic revision --autogenerate -m "..."`, review the generated file,
  and verify with `alembic check` (zero unexpected drift) — the same check
  runs in CI.
- `scripts/verify_migrations.py` proves the whole lifecycle on a scratch
  database (create → `upgrade head` → `alembic check` → downgrade the
  newest migration → re-upgrade → drop) and never touches your development
  database.

## Tests

| Category | Command | Requirements |
|---|---|---|
| Fast unit tests | `python -m pytest -m "not integration"` | None (deterministic fakes; no network/DB/LLM) |
| PostgreSQL integration | `python -m pytest -m integration` | Isolated pgvector test service (`docker compose -f docker-compose.test.yml up -d`) |
| Everything | `python -m pytest` | Integration tests self-skip when PostgreSQL is unreachable |
| Migration lifecycle | `python scripts/verify_migrations.py` | Running PostgreSQL |
| Architecture boundaries | `python scripts/check_imports.py` | None |

The integration suite creates a dedicated `agent_platform_test` database
from the real migration chain and drops it afterwards; override with
`ACA_TEST_DATABASE_URL` only when it still targets that exact test-only name.
It refuses destructive setup for any other name. It covers message ordering/isolation, toggle
uniqueness and user isolation, LLM-call-log round-trips, transaction
recovery after failed writes, database constraints (FK, NOT NULL, CHECK,
uniqueness, HNSW index preservation), idempotency races, and health/
readiness (including schema-mismatch detection).

See [approval workflow testing](docs/APPROVAL_WORKFLOW_TESTING.md) for the
isolated database commands and deterministic chat â†’ approval â†’ resume demo.

Frontend checks: `npx tsc --noEmit` and `npm run build` (run in
`apps/assistant-web`).

For offline control conformance and optional live-model behaviour evaluation,
see [governed agent evaluations](docs/AGENT_CONTROL_EVALUATIONS.md). The
default scripted suite uses only temporary/in-memory evaluation tools.

For exact-action approval and durable resume behaviour, see
[approval workflow testing](docs/APPROVAL_WORKFLOW_TESTING.md). The
[governed run inspector](docs/GOVERNED_RUN_INSPECTOR.md) documents the
owner-scoped, redacted activity view. Strategy contracts and trade-offs are
documented in [governed agent evaluations](docs/AGENT_CONTROL_EVALUATIONS.md).

## Governed-execution evidence

Phase 5D ran 80 live v1.2 scenario executions across React and plan-react:
both completed 35/40 tasks, with zero observed prohibited executions,
approval bypasses, or duplicate dispatches. Plan-react incurred additional
model calls, tokens, and latency. Phase 5E scripted v1.3 bounded-recovery
conformance passed 9/9 scenarios for each strategy. Those scripted results
are mechanism evidence, not live-model evidence; live recovery utility is not
established.

> Run Inspector screenshot placeholder: no authenticated local run screenshot
> is committed because it could contain user-derived action metadata. The
> inspector layout is documented in `docs/GOVERNED_RUN_INSPECTOR.md`.

## Configuration

Configuration resolution order (highest first): environment variables →
`.env` file → `.agent-config.yml` → built-in defaults (see
`src/core/config.py`).

| Variable | Default | Purpose |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | Required for LLM calls |
| `DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@localhost:5432/agent_platform` | Durable store |
| `BRAVE_API_KEY` | — | Web search tool |
| `ACA_MAX_AGENT_TURNS` | `10` | Max LLM calls per chat request |
| `ACA_TOOL_TIMEOUT` | `30` | Per-tool-execution timeout (s) |
| `ACA_LLM_TIMEOUT` | `120.0` | Per-LLM-call timeout (s) |
| `ACA_LLM_MODEL` / `ACA_LLM_BASE_URL` | `deepseek-chat` / `api.deepseek.com` | Model/provider |
| `ACA_TEST_DATABASE_URL` | `.../agent_platform_test` | Integration-test database |
| `OAUTH_GOOGLE_CLIENT_ID` / `_SECRET` | — | Optional Google OAuth |
| `FRONTEND_URL` | `http://localhost:3000` | OAuth redirect target |

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs on push to `main` and pull
requests. Backend job: Python 3.11 + `pgvector/pgvector:pg17` service →
install → import-boundary check → compileall → `alembic upgrade head` →
`alembic check` → `verify_migrations.py` → unit tests → integration tests.
Frontend job: `npm ci` (lockfile-pinned) → `npx tsc --noEmit` →
`npm run build`. No real credentials are used.

## Known limitations

Honest, current limitations (details and ADRs in `docs/`):

- **First-request idempotency gap** — if a request dies before the client
  receives the newly created thread ID, a retry cannot be identified as a
  duplicate (uniqueness is thread-scoped) and creates a new thread.
- **No resume** — a duplicate request is rejected (409); it does not resume
  or replay an interrupted generation.
- **Client message IDs are page-session scoped** — the frontend counter
  resets on reload; uniqueness is guaranteed within a page session, not
  globally.
- **Blocking-sync tools** get best-effort (not hard) cancellation under
  `tool_timeout`; only async tools cancel promptly.
- **Readiness ≠ model-provider health** — `/ready` checks PostgreSQL and
  schema only; DeepSeek downtime will not flip readiness to 503 (a
  deliberate availability choice).
- **Single-node assumptions** — one backend process, one database, no
  distributed execution, no background job system.
- **Frontend dependency audit** — `npm audit` reports 4 high-severity
  advisories in the lockfile; the lockfile pins today's resolution but
  `package.json` uses `latest` ranges and has not been audited/upgraded.
- **Personal memory scale** — embedding storage lives in PostgreSQL via
  pgvector; suitable for a personal corpus, not a large production
  retrieval index.
- The coding agent (`src/aca/`) remains an early prototype and is not part
  of the chat application.

## Scaling and production considerations

This is a single-service personal/research assistant — not a distributed
platform. The following separates what the code actually does today from
how it could plausibly evolve. None of the evolution items are implemented.

### Current implementation

- One FastAPI process holding an async connection pool to one PostgreSQL
  instance (the single source of truth).
- All request-specific mutable state is local to the request (request-local
  tool registries, per-request `AgentRunner`/`Conversation`, request-scoped
  DB session); there is no shared in-process mutable state that would
  prevent horizontal replication.
- SSE streams are generated synchronously with model/tool execution; a
  long-running generation occupies one worker connection.
- LLM/tool-call telemetry is written into transactional PostgreSQL and
  surfaced through thread usage summaries.

### How it could evolve

- **Stateless API replicas** — FastAPI processes can be replicated behind a
  load balancer today, because mutable state is request-local and durable
  state lives in PostgreSQL. (No orchestration config exists yet.)
- **PostgreSQL** — at larger scale: connection pooling/proxying (e.g.,
  PgBouncer), read replicas for history reads, and partition/archive
  strategies for very large `conversation_messages`/`llm_call_logs` tables.
- **Redis** — only for ephemeral concerns if they arise: rate limiting,
  short-lived caches, distributed coordination where justified. It is not
  currently required and is not part of the transactional source of truth.
- **Kafka / event streaming** — only for decoupled concerns: telemetry,
  audit events, analytics, offline evaluation, long-running asynchronous
  workflows. It is deliberately absent from the synchronous chat path,
  because the user's response immediately depends on model/tool execution.
- **OLAP store** — at much larger telemetry volume, LLM/tool-call events
  could be exported to ClickHouse or similar rather than querying
  transactional PostgreSQL for analytics.
- **Streaming layer** — if long-lived SSE connections become a constraint,
  execution workers and connection/stream gateways could be separated; a
  job/resume protocol would need to exist before that becomes meaningful.

## Documentation index

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — component map and chat
  request flow (diagrams).
- [`docs/API.md`](docs/API.md) — endpoint reference and SSE protocol.
- [`docs/decisions/`](docs/decisions/) — ADR-001…ADR-008.
- [`docs/PROJECT_STATE.md`](docs/PROJECT_STATE.md) — current technical
  snapshot.
- `tests/` — deterministic unit tests and the PostgreSQL integration suite.
- `scripts/` — `check_imports.py`, `verify_migrations.py`.
