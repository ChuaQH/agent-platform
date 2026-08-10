# ADR-001 — SSE instead of WebSockets for chat streaming

**Status:** Accepted

## Context

The chat UI needs to display the assistant's response progressively:
reasoning text, tool-call progress, final text, token usage. The interaction
pattern is request/response: the client sends one user message over HTTP
(`POST /api/chat`) and the server streams the reply back. There is no
server-initiated push outside of an active response, and no need for the
client to send messages over the same long-lived channel.

## Decision

Stream the response with Server-Sent Events (`text/event-stream`). The
transport is a normal HTTP `POST` whose response body is an SSE stream,
with the route's `X-Thread-Id` header carrying a newly created thread id.

## Alternatives considered

- **WebSockets** — the classic alternative. Bidirectional, but the chat
  interaction is essentially one-directional during a response; the extra
  complexity (connection state machine, reconnection, protocol framing)
  buys nothing for this workload.
- **HTTP long-polling / chunked polling** — no progressive stream semantics
  and strictly worse latency than SSE.

## Advantages

- SSE rides on ordinary HTTP: one request, one streaming response, standard
  status codes, headers, and authentication (Bearer token on the POST).
- Simple to implement and test: `chat_stream.py` yields wire-ready strings
  from an async generator, testable without any socket machinery.
- Automatic reconnection semantics available to browsers; events are plain
  `data:` lines that the frontend parses with `JSON.parse`.
- Cancellation is native: aborting the fetch unwinds the generator, and
  `asyncio.CancelledError` propagates without becoming an application error.

## Trade-offs

- Unidirectional by design: any future server-initiated push (e.g., "your
  background job finished") would need a separate channel.
- SSE is HTTP/1-friendly but multiplexing is limited to six connections per
  browser; irrelevant here because only one stream is open at a time.

## Revisit when

- The product needs true bidirectional real-time communication (presence,
  collaborative editing, live server push independent of a request).
