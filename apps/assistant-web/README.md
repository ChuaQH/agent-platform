# assistant-web

The Next.js chat frontend for the Assistant Agent. Built on
[assistant-ui](https://www.assistant-ui.com/)'s
`useExternalStoreRuntime`, which drives a custom SSE backend
(`POST /api/chat` on the FastAPI server).

## Stack

- Next.js 16, React 19, TypeScript
- assistant-ui (`@assistant-ui/react`) — chat runtime + thread primitives
- Radix UI + Tailwind CSS 4 — settings dialog, dropdowns, switches
- Plain `fetch` with an `AbortController` for SSE streaming

## Key files

| File | Purpose |
|---|---|
| `app/MyRuntimeProvider.tsx` | SSE consumer; optimistically renders messages; converts backend events into assistant-ui message parts; sends `client_message_id` for idempotency and reloads the thread on `409` duplicates |
| `app/page.tsx` | Layout: sidebar thread list + settings dialog |
| `components/settings/` | Skills and Tools toggle tabs (optimistic updates with revert) |
| `components/thread.tsx` | Chat thread rendering (reasoning, tool calls, markdown) |
| `lib/auth.tsx` | `AuthProvider` + `useAuth`; token stored in `localStorage` |
| `lib/thread-list-adapter.tsx` | Sidebar ↔ backend threads API |

## Running

```bash
npm ci
npm run dev        # http://localhost:3000 (expects backend on :8000)
```

Production-style checks:

```bash
npx tsc --noEmit
npm run build
```

## Backend contract

- `POST /api/chat` returns a `text/event-stream`; events are
  `data: {"type": ..., ...}` lines (`reasoning`, `tool_call`,
  `tool_result`, `text`, `usage`, `limit_reached`, `done`, `error`).
- `client_message_id` is sent with every chat POST; a `409
  duplicate_message` means the message was already accepted and the thread
  is reloaded instead of re-sending.
- Full contract: see [`docs/API.md`](../../docs/API.md).
