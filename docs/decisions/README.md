# Architectural Decision Records

Concise records of the significant engineering decisions in this repository.
Each record states the context, the decision, alternatives considered,
advantages, trade-offs, and when the decision should be revisited.

| # | Decision | Status |
|---|----------|--------|
| [001](adr-001-sse-instead-of-websockets.md) | SSE instead of WebSockets for chat streaming | Accepted |
| [002](adr-002-postgresql-as-durable-state.md) | PostgreSQL as the durable application store | Accepted |
| [003](adr-003-agentrunner-separated-from-http.md) | `AgentRunner` separated from the HTTP layer | Accepted |
| [004](adr-004-request-local-tool-authorization.md) | Request-local tool registry with execution-level authorization | Accepted |
| [005](adr-005-on-demand-skill-content.md) | On-demand skill content via a tool instead of full injection | Accepted |
| [006](adr-006-bounded-execution-and-timeouts.md) | Bounded agent execution and timeout policy | Accepted |
| [007](adr-007-alembic-instead-of-runtime-schema-creation.md) | Alembic instead of runtime `create_all()` | Accepted |
| [008](adr-008-idempotency-client-ids-and-db-constraint.md) | Chat idempotency via client message IDs + DB uniqueness | Accepted |
