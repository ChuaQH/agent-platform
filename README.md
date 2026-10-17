# Agent Platform — governed assistant agent

Agent Platform is a reproducible research/portfolio project for a bounded
tool-calling assistant. A Next.js chat UI talks to a FastAPI backend that
treats model tool calls as untrusted proposals, then independently validates
schema, effective policy, budgets, and exact-action approval before dispatch.
It also records the durable action lifecycle for owner-scoped inspection.

The repository also contains an early coding-agent CLI prototype
(`src/aca/`); the Assistant Agent is the working application and the focus
of this documentation.

## What problem is this exploring?

Governed agents are difficult because a plausible model response is not an
authorization decision. The interesting engineering problems here are:

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

- **Chat with streaming** — SSE (`run_started`, `reasoning`, `tool_call`,
  `tool_result`, `text`, `usage`, `done`) with explicit `error`/`limit_reached` terminal
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

## Architecture and control path

The backend is split into three layers plus a shared core:

```mermaid
flowchart TB
    UI["Authenticated browser"] -->|"POST /api/chat"| ROUTES["FastAPI server.py<br/>auth · immutable effective policy · SSE"]
    ROUTES --> CTX["ContextBuilder<br/>static prefix · compaction"]
    ROUTES --> STREAM["chat_stream.py<br/>persistence + SSE protocol"]
    STREAM --> RUNNER["Strategy / AgentRunner<br/>model loop"]
    RUNNER --> LLM["LLMClient"] --> API["DeepSeek API"]
    RUNNER --> CONV["Conversation<br/>in-memory model-visible state"]
    RUNNER -->|"untrusted model tool proposal"| GATEWAY["RegistryExecutionGateway<br/>JSON schema · policy · budget · approval"]
    GATEWAY --> REG["request-local ToolRegistry<br/>(filtered per user)"]
    REG --> TOOLS["19 tools"]
    GATEWAY --> RUNS["durable agent runs<br/>actions · approvals · events"]
    RUNS --> INSPECTOR["Owner-scoped Run Inspector"]
    STREAM --> PG[("PostgreSQL + pgvector<br/>Alembic head 010")]
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
6. The strategy/`AgentRunner` runs the loop: LLM call → reasoning/text/tool
   proposal → execution gateway validation/policy/budget/approval → permitted
   tool dispatch (bounded by `tool_timeout`) → results appended to
   `Conversation` → next turn, up to `max_agent_turns`.
7. Each event is persisted progressively and emitted as SSE; the stream
   ends in exactly one terminal state (`done`, `limit_reached → usage →
   done`, or `error`).

Key distinction: **`Conversation` is the model-visible in-memory execution
state; PostgreSQL is the durable state.** Persistence is progressive —
there is no single transaction wrapping a whole generation.

## Core control guarantees and limitations

- Every model-originated tool call crosses the execution gateway; raw JSON is
  required to be an object and validated against the tool's existing schema.
- Server-built identity and immutable effective policy—not model arguments—set
  the authority boundary. Exact-action approval is durable, owner-scoped, and
  revalidated before dispatch.
- File tools receive a server-derived per-user workspace. Models supply only
  relative paths; legacy files in the former shared workspace are not migrated
  or exposed through authenticated file tools.
- Tool/turn/time budgets and normalized failure states make refusals and
  ambiguous outcomes observable. The gateway cannot provide exactly-once
  execution at a non-transactional external service.
- Audit/activity data is owner-scoped, redacted, and presentation-only. Action
  fingerprints identify a validated stored action; they are not signatures or
  tamper-evidence claims.

Read the detailed [threat model](docs/THREAT_MODEL.md) before treating any of
these controls as a security guarantee.

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

## Quick start

Prerequisites: Python 3.11+, Node.js 20+, Docker (for PostgreSQL). A
DeepSeek API key is required to send real chat/model requests, but not for
unit, integration, frontend, or scripted evaluator checks.

```bash
# 1. PostgreSQL with pgvector
docker compose up -d

# 2. Backend (copy .env only for a real local app demo)
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

## Approval/resume and governed-run inspection

Follow the [manual demo walkthrough](docs/DEMO_WALKTHROUGH.md) to show a safe
calculator request, an exact-action write approval/resume, a denied write, and
the thread Activity / Run Details lifecycle. The inspector deliberately shows
redacted durable facts rather than model reasoning or executable payloads.

## Testing and reproducible commands

| Category | Command | Requirements |
|---|---|---|
| Fast unit tests | `python -m pytest -m "not integration"` | None (deterministic fakes; no network/DB/LLM) |
| PostgreSQL integration | `python -m pytest -m integration` | Isolated pgvector test service (`docker compose -f docker-compose.test.yml up -d`) |
| Everything | `python -m pytest` | Integration tests self-skip when PostgreSQL is unreachable |
| Migration lifecycle | `python scripts/verify_migrations.py` | Running PostgreSQL |
| Architecture boundaries | `python scripts/check_imports.py` | None |
| Scripted evaluations | See `docs/AGENT_CONTROL_EVALUATIONS.md` | Temporary/in-memory stubs; no provider credentials |
| Frontend unit checks | `npm test` in `apps/assistant-web` | Lockfile-pinned npm install |

The frontend uses its committed `package-lock.json` with `npm ci`. Python uses
the constraints in `pyproject.toml` and currently has no separate lockfile;
release verification records the resolved environment with `python -m pip
check` rather than implying a lock that does not exist.

The integration suite creates a dedicated `agent_platform_test` database
from the real migration chain and drops it afterwards; override with
`ACA_TEST_DATABASE_URL` only when it still targets that exact test-only name.
It refuses destructive setup for any other name. It covers message ordering/isolation, toggle
uniqueness and user isolation, LLM-call-log round-trips, transaction
recovery after failed writes, database constraints (FK, NOT NULL, CHECK,
uniqueness, HNSW index preservation), idempotency races, and health/
readiness (including schema-mismatch detection).

### Local command reference

Use the development stack only for local application work:

```powershell
docker compose up -d
python -m alembic upgrade head
python -m uvicorn assistant.server:app --host 127.0.0.1 --port 8000
```

Use the visibly separate disposable test service for integration tests. The
fixture refuses destructive work unless the database name is exactly
`agent_platform_test`.

```powershell
docker compose -f docker-compose.test.yml up -d
$env:ACA_TEST_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5433/agent_platform_test"
$env:DATABASE_URL = $env:ACA_TEST_DATABASE_URL
python -m pytest -m integration
docker compose -f docker-compose.test.yml down -v
Remove-Item Env:ACA_TEST_DATABASE_URL -ErrorAction SilentlyContinue
Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
```

Run deterministic checks without model/search/weather credentials:

```powershell
python scripts/check_imports.py
python -m pip check
python -m pytest -m "not integration"
python -m evaluations.agent_controls run --mode scripted --configuration all --output evaluation-results/
python -m evaluations.agent_controls run --mode scripted --strategy react --configuration enforced_policy_with_approval --scenario-category strategy --output evaluation-results/
python -m evaluations.agent_controls run --mode scripted --strategy plan_react --configuration enforced_policy_with_approval --scenario-category strategy --output evaluation-results/
python -m evaluations.agent_controls run --mode scripted --strategy react --configuration enforced_policy_with_approval --scenario-category recovery --output evaluation-results/
python -m evaluations.agent_controls run --mode scripted --strategy plan_react --configuration enforced_policy_with_approval --scenario-category recovery --output evaluation-results/
```

In `apps/assistant-web`, run `npm ci`, `npx tsc --noEmit`, `npm test`, and
`npm run build`. Scripted evaluator artifacts are ignored by Git; live mode is
optional and must be invoked explicitly with configured credentials.

See [approval workflow testing](docs/APPROVAL_WORKFLOW_TESTING.md) for the
isolated database commands and deterministic chat â†’ approval â†’ resume demo.

For offline control conformance and optional live-model behaviour evaluation,
see [governed agent evaluations](docs/AGENT_CONTROL_EVALUATIONS.md). The
default scripted suite uses only temporary/in-memory evaluation tools.

For exact-action approval and durable resume behaviour, see
[approval workflow testing](docs/APPROVAL_WORKFLOW_TESTING.md). The
[governed run inspector](docs/GOVERNED_RUN_INSPECTOR.md) documents the
owner-scoped, redacted activity view. Strategy contracts and trade-offs are
documented in [governed agent evaluations](docs/AGENT_CONTROL_EVALUATIONS.md).

## Strategy and evaluation evidence

Across 80 live v1.2 scenario executions (React and plan-react), each strategy
completed 35/40 tasks. That evaluation observed zero prohibited executions,
approval bypasses, or duplicate dispatches; plan-react used more model calls,
tokens, and latency without improving completion. Scripted v1.3 bounded-
recovery conformance passed 9/9 scenarios for both strategies. These are
bounded observations under the tested scenarios, model, configuration, and
stub tools—not universal safety claims or formal verification. Live replan
utility remains unestablished.

See [governed agent evaluations](docs/AGENT_CONTROL_EVALUATIONS.md) for
commands, artifacts, and methodology.

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

## CI and release readiness

GitHub Actions uses read-only repository permissions and separate backend,
isolated `agent_platform_test` PostgreSQL, scripted evaluator, and frontend
jobs. It uses no credentials and prohibits live/provider evaluation. See the
[release checklist](docs/RELEASE_CHECKLIST.md) for clean-checkout checks,
manual secret inspection, demo capture, migration/rollback review, and the
explicit version/tag decision.

## Known limitations

Honest, current limitations (details and ADRs in `docs/`):

- **First-request idempotency gap** — if a request dies before the client
  receives the newly created thread ID, a retry cannot be identified as a
  duplicate (uniqueness is thread-scoped) and creates a new thread.
- **External exactly-once is unavailable** — a durable resume claim prevents
  a second local resumer, but a crash after external dispatch can be uncertain.
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
- **Frontend dependency audit** — `npm audit` reports advisories in the
  lockfile. It is informational rather than a CI gate because the dependency
  set has not yet been reviewed and upgraded as a separate change; several
  `package.json` ranges remain `latest`.
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
- [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) — assets, trust boundaries,
  controls, and residual risks.
- [`docs/DEMO_WALKTHROUGH.md`](docs/DEMO_WALKTHROUGH.md) — safe manual
  approval/resume/activity demonstration.
- [`docs/RELEASE_CHECKLIST.md`](docs/RELEASE_CHECKLIST.md) — release-candidate
  verification and remaining manual steps.
- [`docs/GOVERNED_RUN_INSPECTOR.md`](docs/GOVERNED_RUN_INSPECTOR.md) —
  owner-scoped Activity and detailed audit view.
- [`docs/decisions/`](docs/decisions/) — ADR-001…ADR-008.
- [`docs/PROJECT_STATE.md`](docs/PROJECT_STATE.md) — current technical
  snapshot.
- `tests/` — deterministic unit tests and the PostgreSQL integration suite.
- `scripts/` — `check_imports.py`, `verify_migrations.py`.
