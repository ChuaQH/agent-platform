# ADR-006 — Bounded agent execution and timeout policy

**Status:** Accepted

## Context

An unbounded ReAct loop is a cost and latency hazard: one request could
keep calling the LLM and executing tools indefinitely. The loop also had a
legacy configuration key (`max_tool_calls_per_turn`) whose semantics were
ambiguous, and tool execution previously had no timeout at all.

## Decision

- **Turn budget** — `max_agent_turns` (default 10, env
  `ACA_MAX_AGENT_TURNS`) bounds the number of **LLM calls** per request,
  not tool executions. A tool-requesting turn requires a follow-up turn to
  consume its results, so on the *final* allowed turn tool requests are
  refused and the run terminates with an explicit `limit_reached` outcome —
  no tool result is ever produced that the model cannot observe. The legacy
  `max_tool_calls_per_turn` config key / `ACA_MAX_TOOL_CALLS` env var
  remain honoured as a fallback for backward compatibility.
- **LLM timeout** — enforced at the SDK level
  (`AsyncOpenAI(timeout=config.llm_timeout)`, default 120 s); no double
  wrapping.
- **Tool timeout** — each tool execution is bounded with
  `asyncio.wait_for(config.tool_timeout, default 30 s)`; a timeout becomes a
  controlled error-result string and the loop continues. Cancellation of
  blocking-synchronous tools is best-effort (asyncio cannot forcibly kill
  a thread stuck in sync I/O) — no stronger guarantee is claimed.

## Alternatives considered

- Bound by tool executions instead of LLM calls — rejected: it would not
  cap the dominant cost (model calls) and would change meaning as the loop
  evolved.
- Global wall-clock deadline for the whole request — rejected as a
  complement, not a replacement; the turn budget keeps per-turn semantics
  (usage reporting, checkpointing) deterministic.

## Advantages

- Bounded, deterministic cost per request, expressed in units (LLM calls)
  that are easy to reason about.
- Explicit terminal semantics: `done`, `limit_reached`, or `error` — the
  frontend and tests can rely on exactly one terminal state.
- Tool failures are recoverable (controlled strings the model can react
  to), while LLM failures are terminal (`AgentExecutionError` → SSE
  `error`), matching their actual recoverability.

## Trade-offs

- On the final turn, legitimate tool work is refused even if a follow-up
  turn might have been "wasted" — a deliberate bias toward terminating
  cleanly.
- Blocking tools can outlive their timeout slightly; the result is
  discarded but the thread continues until it returns.

## Revisit when

- The model provider or workload demands per-request wall-clock budgets,
  retries with backoff, or adaptive budgets.
