# ADR-002 — PostgreSQL as the durable application store

**Status:** Accepted

## Context

The assistant persists users, sessions, threads, conversation messages,
compaction checkpoints, LLM call logs, feedback, and per-user skill/tool
toggles. These are relational records with cross-row invariants (a message
belongs to a thread and a user; a toggle is unique per `(user_id, ...)`).
The system also needs vector similarity search for personal memory.

## Decision

PostgreSQL is the single durable application store, accessed through async
SQLAlchemy (asyncpg driver), with schema managed by Alembic and pgvector
providing the `vector` type plus an HNSW index for memory embeddings.

## Alternatives considered

- **SQLite** — used in the earliest prototype; dropped because the app
  depends on PostgreSQL-specific behaviour (JSONB, pgvector, constraint
  semantics) and server-grade concurrency.
- **A document store (MongoDB)** — the data is naturally relational
  (threads/messages/toggles/logs); no flexible-schema need justifies the
  loss of transactions and foreign keys.
- **Redis / Kafka as part of the source of truth** — they solve different
  problems (caching, ephemeral coordination, event streaming). The
  transactional source of truth here is small, relational, and needs ACID
  semantics, which is exactly what PostgreSQL provides.

## Advantages

- Transactions, foreign keys, unique constraints, and CHECK constraints
  enforce correctness at the database rather than relying on application
  discipline (e.g., the idempotency unique index — ADR-008).
- One store for both relational data and vectors (pgvector), keeping the
  operational surface small.
- Alembic gives versioned, reviewable schema evolution (ADR-007).

## Trade-offs

- A running PostgreSQL instance (with pgvector) is a hard requirement for
  the application; tests need it too (integration suite).
- Vector search is embedded in the transactional store — fine at this
  scale, but a dedicated vector index at much larger corpus sizes would be
  a separate evolution (see "Scaling and Production Considerations" in the
  README).

## Revisit when

- Memory corpus or telemetry volume grows to the point where pgvector-in-
  Postgres or querying transactional tables for analytics becomes the
  bottleneck; a dedicated vector store or OLAP export would then be
  considered.
