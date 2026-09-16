# Governed Run Inspector

The Governed Run Inspector is a read-only view of one durable assistant run.
It demonstrates how a model proposal moved through the independently enforced
execution gateway: schema validation, policy and budget checks, exact-action
approval, dispatch, and a normalized outcome.

## Trust boundary

`GET /api/runs/{run_id}/audit` is authenticated and owner-scoped. The server
loads the run with both `run_id` and the authenticated user ID; unknown and
foreign IDs receive the same `404 Run not found` response. The client supplies
only the path identifier. It never supplies a user ID, policy, thread, action,
arguments, fingerprint, or continuation state.

The inspector is a presentation read model. It does not resume a run, change a
policy, decide approval, or make an action executable. The database remains the
source of truth, and the inspector does not claim tamper evidence.

## Discovering runs in a thread

The thread-level **Activity** control lazily requests
`GET /api/threads/{thread_id}/runs`. It is owner-scoped just like the detailed
audit endpoint: unknown and foreign threads both receive `404 Thread not
found`. The endpoint returns summaries, not audit records: lifecycle status and
timestamps, strategy, action aggregates, and whether approval is pending. It
does not return policy snapshots, arguments, continuations, messages,
reasoning, request/user IDs, approval reasons, or exception text.

The list is bounded (default 20, maximum 50) and uses an opaque keyset cursor
ordered by `updated_at DESC, run_id DESC`. Selecting a summary opens the same
lazy detailed audit view described below; it does not duplicate or expand the
summary response into lifecycle data.

Each fresh chat stream sends one safe `run_started` control event after the
durable run is created and before model or tool events. It contains only the
run ID, strategy, and `running` status, is not persisted as chat text or model
context, and makes a current no-tool run discoverable immediately. Resume
continues the existing run and does not announce another one. The frontend
refreshes an open Activity list after completion, approval wait, resume, or a
safe stream failure; reopening it after a page refresh reads durable summaries.

## What is shown

- run status, selected strategy, timestamps, safe terminal category, policy,
  and safe budget/usage counters;
- actions in durable proposal sequence, required capabilities, redacted and
  size-capped canonical argument previews, lifecycle state, normalized result,
  safe reason code, and whether gateway dispatch began;
- approval requirement, decision state, and decision timestamps; and
- a deterministic timeline ordered by durable action sequence and event
  insertion order, not timestamps alone.

The UI calls a proposed operation a **Model proposal**, uses **Policy
rejected**, **Awaiting exact-action approval**, **Executed after approval**,
and **Not dispatched** precisely. `uncertain` means dispatch began but an
interruption left the outcome unknown.

## Fingerprints and redaction

An action fingerprint identifies the durable, validated exact action used by
the approval flow. It is not a digital signature, proof of a natural-language
intent, or cryptographic tamper evidence for the database log.

Argument previews reuse recorder redaction (`safe_argument_representation`):
sensitive fields are recursively replaced, and oversized values become a valid
truncation marker. The endpoint excludes raw malformed model arguments,
executable arguments, continuation JSON, conversation messages, provider
reasoning, user IDs, approval decision reasons, exception text, stack traces,
and credentials.

## Local demonstration

1. Run the backend and frontend with an isolated local PostgreSQL instance.
2. Request a calculator action, then open a pending approval card and select
   **Run details**. Observe a successful action or exact approval lifecycle.
3. Request an allowed write and approve it. The timeline records proposal,
   approval, resumed dispatch, and outcome.
4. Deny a write or submit a request under a narrower policy to observe a
   denied or policy-rejected action marked **Not dispatched**.
5. Reload while a run is waiting for approval, reopen the card, and inspect
   the same durable run before resuming it.

The frontend fetches audit data only when the Run details control is opened.
It displays generic previews as inert text and does not render model output as
HTML.

## Limitations

The inspector reflects durable state recorded by this service; it is neither a
forensic ledger nor a complete replay of model context. Existing records do
not retain separate durable run-transition events, so the timeline derives a
display-only “Run resumed” marker from the durable approved-to-executing action
transition. It intentionally does not reconstruct plans or hidden model
reasoning.
