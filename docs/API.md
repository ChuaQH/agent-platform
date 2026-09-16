# API Reference

The backend is a FastAPI application served on port 8000 (dev).
All endpoints under `/api/` (except auth-register/login and the Google
OAuth flow) require an `Authorization: Bearer <token>` header. Session
tokens are opaque 64-hex strings stored in the `sessions` table with a
7-day TTL.

## Endpoints

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| POST | `/api/auth/register` | — | `{email, password, display_name?}` → `{user, token}` (400 on duplicate/invalid) |
| POST | `/api/auth/login` | — | `{email, password}` → `{user, token}` (401 on bad credentials) |
| GET | `/api/auth/me` | token | Current user object |
| POST | `/api/auth/logout` | token | Invalidates the presented session token |
| GET | `/api/auth/google/login` | — | Redirects to Google OAuth consent (500 if not configured) |
| GET | `/api/auth/google/callback` | — | OAuth callback; redirects to `FRONTEND_URL/auth/callback?token=...` |
| GET | `/api/threads` | token | List the user's threads (by `updated_at` desc) |
| POST | `/api/threads` | token | `{title?}` → new thread |
| GET | `/api/threads/{thread_id}` | token | `{thread, messages, usage}` — usage is the LLM-call summary string |
| DELETE | `/api/threads/{thread_id}` | token | Deletes thread + cascaded rows |
| PUT | `/api/threads/{thread_id}/title` | token | `{title}` → updated thread (404 if absent) |
| POST | `/api/threads/{thread_id}/feedback` | token | `{feedback_text, message_id}` → `{ok}` (404 if thread/message missing) |
| GET | `/api/skills` | token | `{platform: [...], user: [...]}` with `enabled` state |
| PUT | `/api/skills/{skill_id}/toggle` | token | `{enabled}` → `{ok}` |
| GET | `/api/tools` | token | Tool definitions merged with the user's toggle state |
| PUT | `/api/tools/{tool_name}/toggle` | token | `{enabled}` → `{ok}`; 404 for unknown or always-enabled tools |
| POST | `/api/chat` | token | See below (SSE streaming) |
| GET | `/api/threads/{thread_id}/runs` | token | Owner-scoped, bounded governed-run summaries for thread Activity |
| GET | `/api/runs/{run_id}/audit` | token | Owner-scoped, redacted governed-run read model (404 for unknown or foreign run) |
| POST | `/api/geo` | — | `{latitude, longitude}` → `{ok}` (browser geolocation echo) |
| GET | `/health` | — | Liveness: `{status: "ok"}` — no dependencies touched |
| GET | `/ready` | — | Readiness: `{status, database, schema}` or 503 with failure details |

## Chat endpoint (`POST /api/chat`)

Request body:

```json
{
  "messages": [{ "role": "user", "content": "..." }],
  "thread_id": "optional-existing-thread-id",
  "client_message_id": "stable-client-id-for-the-latest-user-message"
}
```

- `thread_id` omitted → a new thread is created; its id is returned in the
  `X-Thread-Id` response header.
- `client_message_id` is the idempotency key (see ADR-008). A duplicate
  `(thread_id, client_message_id)` returns **409**
  `{"detail": {"code": "duplicate_message", "message_id": ..., "thread_id": ...}}`
  before any LLM work.
- Response is `text/event-stream`; each event is
  `data: {"type": "<type>", ...}\n\n`.

### SSE events

| Event | Payload | Terminal? | `done` after? |
|---|---|---|---|
| `reasoning` | `{text, turn}` | no | — |
| `text` | `{content}` | no | — |
| `tool_call` | `{id, name, arguments}` | no | — |
| `tool_result` | `{id, name, result}` (arguments omitted from the wire) | no | — |
| `usage` | `{prompt_tokens, completion_tokens, total_tokens, cached_tokens, turns, latency_ms}` | no | yes |
| `limit_reached` | `{turns}` | no | yes |
| `done` | `{}` | **yes** | — |
| `error` | `{code, message}` (`llm_error` \| `internal_error`) | **yes** | **never** |

Every started stream ends in exactly one terminal state:

- `done` — success;
- `limit_reached → usage → done` — turn budget exhausted (tools were
  refused on the final turn);
- `error` — LLM failure (`llm_error`) or unexpected failure
  (`internal_error`); no `usage`, no `done`. Error payloads carry only a
  stable code and a user-safe message — never tracebacks or exception
  text.

Client cancellation (aborting the fetch) is not an error: the stream simply
stops, and no terminal event is emitted.

## Governed-run inspector (`GET /api/runs/{run_id}/audit`)

Returns safe presentation data for the authenticated owner only:

- `run`: status, strategy, timestamps, safe terminal category, effective
  policy summary, and whitelisted budget/usage counters;
- `actions`: proposal sequence, tool/capabilities, redacted capped canonical
  argument preview, lifecycle/result/reason, dispatch indication, and safe
  approval state/timestamps; and
- `timeline`: deterministic lifecycle display events ordered by durable action
  sequence and event insertion order.

It excludes raw model arguments, executable payloads, continuation JSON,
conversation content, provider reasoning, approval decision reasons, user IDs,
credentials, and internal exception text. An action fingerprint identifies the
exact durable action in the approval flow; it is not a digital signature or
tamper-evidence claim. See [the inspector guide](GOVERNED_RUN_INSPECTOR.md).

## Thread run activity (`GET /api/threads/{thread_id}/runs`)

Returns a bounded, newest-first list of safe run summaries for the authenticated
thread owner. Unknown and foreign threads both return `404 Thread not found`.
`page_size` defaults to 20 and must be between 1 and 50. Follow the opaque
`next_cursor` with the same page size to retrieve the next page; ordering is
`updated_at DESC, run_id DESC`, so the cursor prevents duplicates or omissions
where timestamps match.

Each summary contains only run identity, strategy, lifecycle timestamps and
safe terminal category, aggregate action counts, and a pending-approval flag.
It deliberately excludes policies, arguments, executable payloads,
continuations, messages, reasoning, request/user IDs, approval reasons, and
exception text. Fetch `GET /api/runs/{run_id}/audit` only after a user selects
a summary for detailed activity.

New chat streams begin with a transport-only `run_started` event containing
only `run_id`, strategy, and `running` status. It is emitted after the durable
run is created and before model/tool events; it is neither model context nor
persisted chat content. Resume streams continue the same run and do not emit a
second `run_started` event.

## Google OAuth setup (local)

1. Google Cloud Console → create an OAuth 2.0 Client ID (Web application).
2. Add `http://localhost:3000/api/auth/google/callback` as an authorized
   redirect URI.
3. Set `OAUTH_GOOGLE_CLIENT_ID` and `OAUTH_GOOGLE_CLIENT_SECRET` in `.env`.
4. `FRONTEND_URL` (default `http://localhost:3000`) is used to build both
   the redirect URI and the post-login redirect target.
