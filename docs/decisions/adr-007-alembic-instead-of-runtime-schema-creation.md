# ADR-007 — Alembic instead of runtime schema creation

**Status:** Accepted

## Context

Early development used `Base.metadata.create_all()` at server startup as a
schema bootstrap: it is idempotent, requires no bookkeeping, and was how the
first databases were created. As the schema grew (9+ tables, indexes,
constraints, pgvector), two problems appeared: there was no migration
history for existing databases (the first real migration had to be
"stamped" over already-created tables), and drift between the models and a
given database could go unnoticed.

## Decision

- Alembic is the **authoritative schema mechanism**. The complete history
  is `001` → `007` (head), covering all 11 tables plus indexes,
  constraints, and the pgvector extension.
- `create_all()` has been **removed from application startup**. The server
  expects the database to be migrated before boot and never runs
  `alembic upgrade head` itself.
- Startup runs the same readiness gate as `/ready`: database connectivity
  plus `alembic_version == migration head`. A missing or unmigrated
  database aborts startup with an explicit `RuntimeError` and an
  instructive message instead of booting against an incomplete schema.
- Tests build their database through the real migration chain
  (integration suite) and a dedicated script
  (`scripts/verify_migrations.py`) proves the empty-DB lifecycle:
  upgrade → check → downgrade of the newest migration → re-upgrade.
- `alembic check` (model/metadata vs. database comparison) is part of CI
  and the developer workflow, so model/schema drift is detected, not
  silently accumulated.

## Alternatives considered

- Keep `create_all()` at startup — rejected: silent schema mutation on
  boot, no history, drift undetectable.
- Run `alembic upgrade head` inside each server process — rejected: unsafe
  when several replicas boot concurrently; migration is an operator step,
  not an application concern.

## Advantages

- Versioned, reviewable, reversible schema history; `alembic check` keeps
  models and database honest.
- Fail-fast startup: misconfiguration is loud and immediate.
- A fresh environment is reproducible: empty DB + `alembic upgrade head` =
  the complete schema (verified end to end in CI).

## Trade-offs

- Every schema change now requires a migration review (the intended cost).
- A developer who forgets `alembic upgrade head` cannot start the server —
  deliberate, with a clear error message.

## Revisit when

- Deployments adopt a platform-managed schema rollout (e.g., database-as-a-
  service with its own migration runner); the gate would move into the
  deployment pipeline.
