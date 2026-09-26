# Project State — Assistant Agent

Technical snapshot of the repository at the completion of the engineering
hardening effort (Phases 1–4) and the documentation phase (Phase 5).
Written so another engineer can understand exactly what exists today.

- Migration head: **`007`**
- Branch: `main` (all hardening work uncommitted at time of writing — see
  the final Phase 5 report for the recommended commit split)
- Python: 3.11+ (async) · Frontend: Next.js 16 / React 19 / TypeScript

---

## 1. Architecture

```
apps/assistant-web          Next.js + assistant-ui chat frontend (port 3000)
src/assistant               FastAPI backend (port 8000)
src/core                    Shared library (LLM, tools, config, DB, conversation)
src/aca                     Early coding-agent CLI prototype (not the focus)
migrations/                 Alembic history 001→007
scripts/                    check_imports.py, verify_migrations.py
```

Ownership boundaries (see `docs/ARCHITECTURE.md` for the diagram):

| Component | Owns |
|---|---|
| `server.py` | HTTP transport, auth, thread setup, idempotency gate, SSE wiring, health/readiness |
| `AgentRunner` | ReAct turn loop, `max_agent_turns` budget, `tool_timeout`, in-memory `Conversation` mutation |
| `chat_stream.py` | SSE wire format, progressive persistence coordination, terminal protocol |
| `ContextBuilder` | Static-prefix assembly, token estimation, two-pass compaction |
| `ToolRegistry` | Tool registration, non-mutating `filtered()`, execution-level authorization, error scrubbing |
| `thread_store`/`skill_store`/`tool_store` | All SQLAlchemy persistence |
| `Conversation` | Append-only model-visible in-memory state (with static-prefix hash) |
| `LLMClient` | OpenAI-compatible SDK calls (DeepSeek default), timeout, usage/cache metrics |

Dependency rule (enforced by `scripts/check_imports.py`):
`core` imports nothing internal; `assistant`/`aca` import only `core`;
`assistant` and `aca` never import each other.

## 2. Request flow

`POST /api/chat` → auth (Bearer session token) → thread resolve/create →
idempotency gate (409 on duplicate `(thread_id, client_message_id)`) →
user message persisted (committed immediately) → enabled tools/skills
loaded → request-local registry built and filtered → `ContextBuilder`
compacts if needed and assembles the static prefix → `AgentRunner` runs the
ReAct loop (`LLMClient` + tools) → `chat_stream` persists events
progressively and emits SSE → exactly one terminal event (`done`,
`limit_reached → usage → done`, or `error`).

Two state layers: `Conversation` is the model-visible in-memory execution
state; PostgreSQL is the durable state. Persistence is progressive, not one
wrapping transaction.

## 3. Persistence

PostgreSQL (asyncpg via async SQLAlchemy), schema managed by Alembic
(head `007`), pgvector for memory embeddings (HNSW index).

| Table | Purpose |
|---|---|
| `users`, `sessions` | Auth (email/password pbkdf2, Google OAuth, 7-day session tokens) |
| `thread_state` | Threads (title, summary, latest checkpoint id) |
| `conversation_messages` | Durable messages; `client_message_id` + unique `(thread_id, client_message_id)` for idempotency; CHECK on `role` |
| `thread_compaction_checkpoints` | Compaction summaries (tool-output pruning / full compaction) |
| `llm_call_logs` | Per-turn LLM call metadata (tokens, cached tokens, latency, finish reason) |
| `memories` | Personal memory facts + 1024-dim embeddings (pgvector HNSW) |
| `feedback` | Per-response feedback with context snapshots |
| `skills` | Skill metadata (name, description, markdown content) |
| `user_skill_toggles` | Per-user skill enablement, unique `(user_id, skill_id)` |
| `user_tool_toggles` | Per-user tool enablement, unique `(user_id, tool_name)` |

Key constraints: FKs with `ON DELETE CASCADE` from thread-owned tables;
`uq_user_skill`/`uq_user_tool`; `uq_conversation_messages_thread_client`;
`ck_conversation_messages_role`; `ck_checkpoint_strategy`;
`ck_memories_type`; unique `users.email`/`users.google_id`.

## 4. Reliability behaviour

- **Execution budget:** `max_agent_turns` (default 10) bounds LLM calls per
  request; tools are refused on the final turn (`limit_reached`).
- **Timeouts:** LLM at the SDK (`config.llm_timeout`, default 120 s); tools
  via `asyncio.wait_for` (`config.tool_timeout`, default 30 s). Blocking-
  sync tools get best-effort cancellation only.
- **Cancellation:** client disconnect → `CancelledError` propagates through
  the SSE generator (never converted to an application error); the request
  DB session is closed by the dependency, rolling back any pending work and
  returning the connection to the pool.
- **Idempotency:** client message ID + DB unique index as the final race
  guard; 409 + frontend thread reload on duplicates (ADR-008).
- **Terminal SSE states:** exactly one per stream — `done`, `limit_reached
  → usage → done`, or `error` (no `done` after). `error` payloads carry
  only a stable code and user-safe message.
- **Error scrubbing:** unhandled tool exceptions are logged with
  `exc_info=True` server-side but surfaced to model/client as controlled
  strings.

## 5. Testing

| Category | Command | Covers |
|---|---|---|
| Fast unit tests | `pytest -m "not integration"` | Tool-registry isolation (3), AgentRunner loop semantics (12), chat-stream terminal protocol (4) — deterministic fakes, no network/DB |
| PostgreSQL integration | `pytest -m integration` | 19 tests against a real migration-built database: message ordering/isolation, skill/tool toggle uniqueness and isolation, LLM log round-trip, transaction recovery, FK/NOT NULL/CHECK/unique constraints, HNSW index preservation, idempotency uniqueness + concurrent race + route 409, health/readiness (incl. schema-mismatch) |
| Migration lifecycle | `scripts/verify_migrations.py` | Empty DB → `upgrade head` → `alembic check` → downgrade newest migration → re-upgrade → cleanup |

Integration fixtures create/drop a dedicated `agent_platform_test`
database (override with `ACA_TEST_DATABASE_URL`) and self-skip when
PostgreSQL is unreachable. The dev database is never touched.

## 6. Operational behaviour

- **Startup:** FastAPI lifespan initializes the engine, runs the readiness
  gate (DB reachable + `alembic_version == head`), and seeds global skills.
  A missing/unmigrated DB aborts startup with an explicit error. The server
  never creates or mutates schema.
- **`/health`:** dependency-free liveness — always `200 {"status": "ok"}`.
- **`/ready`:** PostgreSQL connectivity + migration compatibility; `503`
  with per-check details otherwise. Never calls the LLM provider.
- **Config:** `.env` → environment → `.agent-config.yml` → defaults
  (`core/config.py`). Key vars: `DEEPSEEK_API_KEY`, `BRAVE_API_KEY`,
  `DATABASE_URL`, `ACA_*` overrides (turns, timeouts, model, log level).
- **CI:** GitHub Actions — backend job (Python 3.11 + pgvector/pgvector:pg17
  service): install, import check, compileall, `alembic upgrade head`,
  `alembic check`, `verify_migrations.py`, unit + integration pytest;
  frontend job: `npm ci`, `tsc --noEmit`, `npm run build`.

## 7. Known limitations

See the README's "Known Limitations" for the full list. The headline items:

- First-request idempotency gap when the thread ID is never received.
- Duplicate requests do not resume interrupted generations.
- Client message IDs are page-session generated.
- Blocking-sync tools can't be forcibly cancelled by asyncio.
- Model-provider availability is intentionally not part of readiness.
- Single-node assumptions; no distributed execution or async job resume.
- Frontend: `npm audit` is informational; review its current advisories before
  a dependency-upgrade release. `package.json` still uses `latest` ranges
  while the lockfile pins CI's actual dependency resolution.
- Personal memory scaling is bounded by pgvector-in-PostgreSQL at this
  scale.

## 8. Future possibilities (not implemented)

- Stateless API replicas behind a load balancer (mutable state is already
  request-local; durable state is external).
- Connection pooling/proxying (PgBouncer-style), read replicas, or
  partition/archive strategies for large message/log tables.
- Redis only for ephemeral concerns (rate limiting, short-lived caches) —
  not currently required.
- Kafka/event streaming only for decoupled telemetry/audit/analytics or
  long-running async workflows — deliberately absent from the synchronous
  chat path.
- OLAP export (e.g., ClickHouse) for large-scale LLM-call analytics.
- Separating execution workers from SSE connection gateways if long-lived
  connections become a constraint.

## 9. Where to look next

- `docs/ARCHITECTURE.md` — component map + chat request flow (diagrams).
- `docs/decisions/` — ADR-001 … ADR-008 for the "why" behind the design.
- `docs/API.md` — endpoint reference + SSE protocol.
- `tests/` — deterministic unit tests and the PostgreSQL integration suite.
- `scripts/verify_migrations.py` — the clean-database lifecycle proof.
