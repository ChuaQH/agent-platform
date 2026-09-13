# Approval workflow: local reproducible test

This guide uses only local test data.  It never calls a real model, embedding,
search, weather, or other external API.

## Prerequisites

Install Python 3.11+, Docker, Node.js 20+, and the project development
dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Start the disposable pgvector service.  It is separate from the development
compose service: it uses port `5433`, container `agent-platform-test-db`, and
database `agent_platform_test`.

```powershell
docker compose -f docker-compose.test.yml up -d
$env:ACA_TEST_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5433/agent_platform_test"
$env:DATABASE_URL = $env:ACA_TEST_DATABASE_URL
python -m alembic upgrade head
```

The test fixture validates that `ACA_TEST_DATABASE_URL` targets exactly
`agent_platform_test` before it creates, truncates, or drops a database.  It
refuses any other database name.

## Scripted approval demonstration

The integration test is the deterministic demo runner.  It injects a
scripted LLM and a unique temporary workspace through normal Python
dependencies; no test-mode HTTP endpoint exists.

```powershell
python -m pytest -m integration tests/test_approval_e2e.py -s
```

It executes and validates this sequence against the real FastAPI API and
migrated PostgreSQL schema:

```text
chat SSE: approval_required -> waiting_for_approval
approval API: pending presentation -> approve or deny
bodyless resume SSE: tool_result -> text -> usage -> done
```

For the frontend, start the normal backend and UI in separate terminals (the
scripted model is intentionally pytest-only, so a real configured model is
needed for interactive use):

```powershell
python -m uvicorn assistant.server:app --host 127.0.0.1 --port 8000
cd apps/assistant-web
npm ci
npm run dev
```

When a configured model asks to write a file, the UI shows the exact-action
approval card.  Approving resumes the paused run; denying returns an
`approval_denied` tool result.  The ordinary development workspace and
database are not part of the scripted demonstration.

Run checks:

```powershell
python scripts/check_imports.py
python -m pytest -m "not integration"
python -m pytest -m integration
cd apps/assistant-web; npx tsc --noEmit; npm run build
pip check
```

Remove only the disposable test service and its data when finished:

```powershell
docker compose -f docker-compose.test.yml down -v
Remove-Item Env:ACA_TEST_DATABASE_URL -ErrorAction SilentlyContinue
Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
```
