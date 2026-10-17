# Governed assistant demo walkthrough

This is a manual 3–5 minute demonstration of the actual application. It uses
only harmless text and the dedicated `demo-phase6b/` directory under the local
workspace. It is not an evaluator run and does not require a production test
endpoint.

## Prerequisites

- Python 3.11+, Node.js 20, Docker, and a local PostgreSQL/pgvector service.
- A local `.env` copied from `.env.example`, with the credentials required for
  the configured model and the optional local sign-in path you use. Do not use
  personal data or production credentials in the demo content.
- Start the development database with `docker compose up -d`, run
  `python -m alembic upgrade head`, start the backend, then run
  `npm ci && npm run dev` in `apps/assistant-web`.

The configured workspace root is normally `agent-data/workspace`. The server
creates each authenticated user's files below `users/<safe-user-directory>/`;
the model sees and supplies only relative paths such as
`demo-phase6b/hello.txt`. Legacy shared files are not migrated or available
through normal file tools. This is application-level directory segregation,
not an OS sandbox.

## Sequence

1. Sign in with a local demonstration account and open a new thread.
2. Ask: `What is 17 * 23?` Confirm the calculator response is harmless and
   that **Activity** shows a completed run even though no approval was needed.
3. Ask: `Write the exact text "hello governed demo" to demo-phase6b/hello.txt.`
   Observe the exact-action approval card. Confirm the requested path/content,
   required capability, fingerprint, and expiry before deciding.
4. Open **Run details** or **Activity**. Confirm the action says **Awaiting
   exact-action approval** and **Not dispatched** before approval.
5. Approve once. Observe the assistant resume, then open Activity and select
   the completed run. Confirm the action lifecycle includes model proposal,
   approval, resumed dispatch, and successful result. Optionally ask the
   assistant to read the same demo file to verify its harmless contents.
6. Ask for a second safe write, for example `Write "second demo" to
   demo-phase6b/denied.txt.` Deny the proposal. Inspect its run and confirm
   **Not dispatched** and the denial lifecycle; do not expect that file to
   exist.
7. Explain that the model proposes calls, while the server execution gateway
   independently validates schema, effective policy, budgets, and exact-action
   approval before tool dispatch. The inspector is a redacted presentation
   view, not cryptographic tamper evidence.
8. End with the bounded evaluation evidence and known limitations in the
   README and threat model.

## Cleanup

Stop the backend/frontend. If needed, delete only the demonstrated
`demo-phase6b` directory under the local user's server-derived workspace; do
not treat the legacy shared root as user-visible. Stop the disposable
test stack with `docker compose -f docker-compose.test.yml down -v`; do not
use that command against the development compose file unless you deliberately
intend to remove its data.

## Screenshot checklist

Capture real local screenshots manually before a portfolio release. Redact any
account, message, file-path, or credential information that is not part of the
safe demo.

- Exact-action approval card.
- Activity while approval is pending.
- Successful approved-action timeline.
- Denied action marked **Not dispatched**.
- Effective authority and budget view.
