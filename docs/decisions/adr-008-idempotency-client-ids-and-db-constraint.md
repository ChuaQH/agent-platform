# ADR-008 — Chat idempotency via client message IDs + DB constraint

**Status:** Accepted

## Context

A chat POST can be retried (network failure, double submit, client
reconnect). Without any guard, each retry creates a second durable user
message and triggers a second LLM execution — duplicating cost and corrupt
conversation history. The frontend already generates a stable per-message
identifier (`fid`) for the optimistic message list.

## Decision

- `ChatRequest` accepts an optional `client_message_id`.
- `conversation_messages` gains a nullable `client_message_id` column with a
  **unique index** `uq_conversation_messages_thread_client` on
  `(thread_id, client_message_id)` (migration `007`). PostgreSQL treats
  NULLs as distinct, so legacy rows and messages without a client ID never
  collide.
- The route checks the pair **before** any LLM work and returns
  `409 {code: "duplicate_message", message_id, thread_id}` for an
  already-accepted message. The frontend responds by reloading the thread,
  revealing the existing conversation.
- The unique index is the **concurrency authority**: a racing duplicate
  insert raises `IntegrityError`, which the route converts to the same 409
  after a rollback. A read-before-write check alone could not stop two
  concurrent requests.
- The same client ID may legitimately exist in **different threads**;
  uniqueness is deliberately thread-scoped.

## Alternatives considered

- Global unique `client_message_id` — rejected: frontend IDs are per-page-
  session (a module counter + timestamp), so the same ID could be
  legitimately generated again in another thread; a global constraint would
  create false rejections.
- Read-before-write only — rejected as insufficient for concurrent
  retries; the database constraint is the final guard.
- A replay/resume architecture (reconnect to an in-flight execution) —
  rejected as out of scope: too much state machinery for the actual risk.
  The guarantee is "a retry never creates a duplicate durable message or
  starts an accidental duplicate execution *when the server can identify
  it*."

## Advantages

- Database-enforced guarantee, race-proof by construction, with a friendly
  fast path (the pre-read) for the common retry case.
- Backward compatible: no client ID → old behaviour; the column is
  nullable and the index ignores NULLs.
- Cheap: one additional unique index; `alembic check` stays clean.

## Trade-offs

- **First-request gap:** if the first request dies before the client
  receives the newly created `X-Thread-Id`, a retry has no thread_id, so
  the server cannot identify the duplicate; it creates a new thread. This
  is inherent without a durable client→thread mapping (or a global client
  key), and is documented as a known limitation.
- A duplicate request does **not** resume an interrupted generation; it is
  rejected, and the user continues with a new message.
- Client-generated IDs are unique only within a page session; a reload
  resets the counter, so (in theory) a new message could collide with an
  old one at the same millisecond — the thread-scoped index limits any
  harm to a spurious 409.

## Revisit when

- The product needs durable client→thread correlation across sessions
  (e.g., offline retry queues), or automatic resume of interrupted
  generations; both would require a client-side stable key persisted
  outside the page session.
