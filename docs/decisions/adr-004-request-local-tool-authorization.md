# ADR-004 — Request-local tool authorization at execution time

**Status:** Accepted

## Context

Users can enable/disable tools (and skills). Each chat request must see
only the tools the authenticated user is allowed to use, and the model must
never be able to trigger a tool the user disabled — even if a prompt
injection or a stale schema fools it into requesting one.

## Decision

Two-layer design:

1. **Request-local registry derivation** — `server.py` builds the full
   19-tool registry fresh per request and derives the request's usable set
   with `ToolRegistry.filtered(enabled_names | ALWAYS_ENABLED)`. `filtered()`
   returns a *new* registry; the master is never mutated, so one user's
   toggles can never affect a concurrent request's tool set. Always-enabled
   infrastructure tools (`assistant_remember`, `assistant_recall`,
   `get_skill_content`) are unioned back as a defense-in-depth backstop.
2. **Execution-level enforcement** — `ToolRegistry.execute(name, ...)` only
   resolves names registered in the request-local registry and returns a
   controlled error for anything else. The LLM-visible schemas are treated
   as *not* an authorization boundary; the actual execution path is.

## Alternatives considered

- Filter schemas only (hide disabled tools from the model) — rejected as
  insufficient: schema visibility is not an access-control mechanism.
- Mutating a shared application-level registry per request — rejected: a
  race between two requests with different toggles would leak tools across
  requests.
- Authorizing inside each tool — rejected: 19 tools would each re-implement
  the same check, and the check would be easy to forget.

## Advantages

- Structural (not procedural) concurrency safety: there is no shared
  mutable registry state to get wrong.
- A single enforcement point in `execute()` that tests can pin down
  (test_tool_isolation proves a filtered-out tool's `calls == 0`).
- Execution errors are scrubbed server-side; the model sees only controlled
  strings.

## Trade-offs

- Small per-request allocation cost: each request builds and filters a
  registry (19 tool objects, most shared and cheap).
- Registry content is assembled in the route, so any new tool must be
  registered there too.

## Revisit when

- Tool registration moves to a data-driven source (e.g., DB-defined tools)
  or an MCP-based tool server; the enforcement point would then sit at the
  tool-provider boundary instead of the registry.
