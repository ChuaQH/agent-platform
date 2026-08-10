## Architecture (CRITICAL — read first)

This is a monorepo where `src/core/` is the shared library, and every other
top-level folder under `src/` is an independent agent:

```
src/
├── core/          ← Shared infrastructure. Does NOT import from any agent.
└── <agent>/       ← Each agent imports from core ONLY.
                     Agents NEVER import from each other.
                     Agents collaborate via MCP at runtime, not imports.
```

**Verify:** `python scripts/check_imports.py` after every change. Must pass.

## Tech stack

Python 3.11+ (async), DeepSeek V4 API (OpenAI SDK, base_url=https://api.deepseek.com), FastAPI + Uvicorn, Next.js frontend, async SQLAlchemy + Alembic, PostgreSQL + pgvector (bge-m3 embeddings), pytest (unit + integration).

## Editing rules

- Shared infrastructure → `src/core/`
- Agent-specific code → `src/<agent>/`
- Never import from one agent into another
- Run `python scripts/check_imports.py` after edits
- Secrets via `.env` (copy from `.env.example`). Never hardcode keys.
- New config keys: add to `.env.example` + `core/config.py` (DEFAULTS, ENV_MAP, property)
- API keys: `DEEPSEEK_API_KEY` (LLM), `BRAVE_API_KEY` (search)
