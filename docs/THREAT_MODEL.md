# Threat model

This is a research/portfolio assistant, not a claim of production security,
tamper-proof records, or formal verification. This document describes the
controls implemented today and their residual risk.

## Assets

- User messages, threads, feedback, and LLM usage records.
- Tool authority, canonical/executable tool arguments, and workspace or
  memory data reached through tools.
- Authenticated exact-action approval decisions and durable continuations.
- Credentials held in environment configuration for models, search, OAuth,
  and database access.
- Durable run/action/event/approval audit records.

## Trust boundaries

| Boundary | Inputs treated as untrusted | Primary controls |
|---|---|---|
| Browser -> authenticated API | request bodies, thread/run IDs, stream consumers | bearer-session authentication; owner-scoped queries; identical not-found responses for foreign/unknown thread and run IDs |
| Model output -> execution gateway | tool name and raw JSON arguments | request-local registry lookup; JSON-object requirement; JSON Schema validation; normalized rejection result |
| Gateway -> tools | validated canonical arguments | immutable effective policy, capability/tool checks, budgets, timeout/exception containment, approval gate |
| Gateway -> file tools | model-relative path | server-derived per-user workspace, cross-platform absolute/traversal rejection, resolved-path containment, existing-link escape checks |
| Application -> PostgreSQL | application writes and administrative access | SQLAlchemy parameterization, constraints, Alembic, separate guarded integration database |
| Application -> external providers | provider response, tool output, network failures | bounded calls/timeouts, controlled errors, provider failure handling; external output remains untrusted |
| Pause/resume lifecycle | approval decisions and durable continuation | owner scope, exact fingerprint matching, expiry, atomic claim before resume |
| Evaluator isolation | scenarios and scripted model responses | temporary workspace, in-memory memory, stub catalog/tools, provider mode explicitly separate from scripted mode |

## Threats, controls, and residual risk

| Threat | Implemented controls | Residual risk |
|---|---|---|
| Prompt injection or malicious tool output | Model proposals do not execute directly; gateway validates every call; policies/budgets/approvals are server-side; inspector renders previews as inert text | A model can still be misled into proposing an allowed action; prompt defenses are not authorization |
| Invalid model arguments | Raw JSON parsing, object requirement, JSON Schema validation, no empty-object fallback | A correct schema still depends on a correct tool contract |
| Capability escalation | Request-local tool registry, immutable effective policy, capability and allow-list checks at gateway | Incorrect capability metadata can under-classify a tool |
| Unauthorized thread/run access | Authenticated owner-scoped thread, run, action, approval, feedback, and audit queries; foreign IDs get normal not-found responses | Compromised sessions or privileged database access remain high impact |
| One user reading or overwriting another user's files | File tools resolve only relative paths beneath a directory derived from authenticated server identity; user IDs are not used as ordinary directory names | Application-level segregation is not an OS sandbox; a compromised process, filesystem races, storage exhaustion, or an unsafe future tool can bypass it |
| Approval bypass, replay, or mismatch | Exact canonical-action fingerprint, authenticated decision, expiry, revalidation on resume, durable events | A fingerprint identifies a stored exact action; it is not a signature or proof of intent |
| Duplicate/concurrent resume | Durable claim conflict prevents a second local resumer from dispatching | This is application-level coordination, not exactly-once external execution |
| Cancellation after dispatch or timeout ambiguity | Cancellation/timeout lifecycle records and `uncertain` normalized outcome | A crash or lost response after dispatch may leave the external result unknown |
| Secret leakage in arguments, errors, SSE, logs, or audit | Recursive redaction and capped previews; safe SSE payloads; normalized errors; audit whitelist | Redaction is defense-in-depth, not a guarantee for every secret format or tool output |
| Exhausted turn/tool/time budgets | Server-side call, per-tool, cumulative-time, and runner-turn limits | Limits trade utility for safety and do not undo an already dispatched action |
| Provider protocol/transport failure | Exception containment, controlled user-safe errors, evaluator provider-attempt limits/circuit handling | Provider behavior and availability are outside local control |
| Database changes by privileged operators | Database is durable source of truth; audit UI makes no cryptographic claim | Database administrators can alter stored state |
| Non-transactional external side effect | Pre-dispatch approval/revalidation and durable action lifecycle | External services may execute an action even if the local response is lost |

## Operational constraints

Credentials belong in `.env` or deployment-secret storage and must not be
committed. CI supplies empty credential variables and runs only scripted
evaluations. The PostgreSQL integration fixture refuses destructive setup unless
`ACA_TEST_DATABASE_URL` targets exactly `agent_platform_test`; it must never be
pointed at a development or production database.

Dependency reviews run both production and development npm audit views. At the
Phase 6B review point, the remaining advisory is the development-only Vitest
3.2.6 / `@vitest/mocker` moderate issue; the repository uses `vitest run`, not
the affected UI server, and npm's available remediation is a breaking Vitest 5
upgrade. This is tracked as a residual development-tooling risk and must be
reassessed before a later dependency refresh.

Live evaluation results are bounded observations under a particular model,
configuration, scenario set, and stub-tool environment. They are not proof of
universal safety or correctness.
