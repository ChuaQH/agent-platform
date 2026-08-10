# ADR-005 — On-demand skill content via a tool

**Status:** Accepted

## Context

Skills are markdown instruction sets that guide the model for specific task
types (research, writing, code review, data analysis). Injecting the full
markdown of every enabled skill into the system prompt would consume a
large, mostly-unused slice of the context budget on every request.

## Decision

Two-tier exposure:

1. **Static context stays light** — only each enabled skill's *name* and
   *description* are injected into the static prefix as an
   `## Enabled Skills` section.
2. **Full content is fetched on demand** — a `get_skill_content` tool
   (always enabled) returns the full markdown for one skill when the model
   decides a task actually matches it.

## Alternatives considered

- Inject all full skill bodies into the system prompt — rejected: token
  cost on every turn, and most skills are irrelevant to most requests.
- Let the model know about skills purely via memory/search — rejected:
   skills are first-class guidance, not memory facts; they need a
   deterministic lookup path.

## Advantages

- The static prefix stays small and stable, which also helps prompt-cache
  hit rates for the repeated prefix.
- Skills remain additive: adding a skill only adds one line to the static
  section.
- The tool is a normal registered tool, so it inherits the request-local
  authorization and error-scrubbing machinery.

## Trade-offs

- Costs one extra tool round-trip when the model decides to use a skill.
- Depends on the model choosing to call `get_skill_content`; a model that
  never calls it behaves as if skills were absent.

## Revisit when

- Skills grow to a size where even name/description lists are heavy, or
  when retrieval-augmented skill selection (e.g., embedding-based matching)
  becomes warranted.
