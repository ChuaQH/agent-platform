# ADR-003 — AgentRunner separated from the HTTP layer

**Status:** Accepted

## Context

The ReAct loop originally lived inside the FastAPI chat endpoint, which
made the orchestration logic untestable without a running web server, a
real LLM, and a database. The loop itself — call model, interpret response,
execute tools, append to conversation, repeat — has no inherent dependence
on HTTP, SQLAlchemy, or FastAPI.

## Decision

Extract the loop into `AgentRunner` (`src/assistant/agent_runner.py`), a
transport-agnostic class that:

- takes `llm`, a request-local `ToolRegistry`, `max_turns`, and an optional
  `tool_timeout` as constructor dependencies;
- yields application-level `AgentEvent` objects (not SSE strings);
- returns an `AgentResult` (final text, turns, token totals, limit flag);
- never touches the database and imports nothing from FastAPI.

The HTTP route (`server.py`) wires the runner into `chat_stream.py`, which
owns persistence coordination and the SSE wire format.

## Alternatives considered

- Keep the loop in the route — rejected: the orchestration semantics
  (budget, timeout, terminal states) were the hardest part to get right and
  were untestable.
- Put the loop in core — rejected: `assistant`-specific tool and skill
  semantics belong in `assistant`; the dependency rule (`assistant → core`)
  would break if core depended on assistant tools.

## Advantages

- Deterministic tests: a `ScriptedLLM` fake drives the runner and asserts
  turn counts, tool argument ordering, budget exhaustion, timeouts, and
  cancellation without network, LLM, or DB.
- The same `AgentEvent` vocabulary is consumed by `chat_stream` for
  persistence and SSE, keeping transport and protocol separate concerns.
- Each request constructs a fresh runner, so no shared mutable state.

## Trade-offs

- Persistence is coordinated outside the runner (in `chat_stream`/stores),
  so the runner's events must carry enough metadata (e.g., tool arguments
  alongside results) for the persistence layer to reconstruct the row.
- Two layers must agree on the event vocabulary; adding an event type
  touches both.

## Revisit when

- A second transport (e.g., a CLI mode or an MCP server) appears; the
  separation then pays off by reusing the runner unchanged.
