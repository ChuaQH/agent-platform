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

## Google OAuth setup (local)

1. Google Cloud Console → create an OAuth 2.0 Client ID (Web application).
2. Add `http://localhost:3000/api/auth/google/callback` as an authorized
   redirect URI.
3. Set `OAUTH_GOOGLE_CLIENT_ID` and `OAUTH_GOOGLE_CLIENT_SECRET` in `.env`.
4. `FRONTEND_URL` (default `http://localhost:3000`) is used to build both
   the redirect URI and the post-login redirect target.
