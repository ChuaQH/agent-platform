# ACA Ecosystem — Agent Instructions

This file is read by AI coding agents at session start.
Keep it concise — it sits in the context window for every session.

## Architecture Rule

```
src/core/      ← Shared lib. May NOT import from aca or assistant.
src/aca/       ← Coding agent. Imports from core ONLY.
src/assistant/ ← General chatbot. Imports from core ONLY.

aca and assistant NEVER import each other. They collaborate via MCP at runtime.
```

## Enforce It

Run before committing:
```
python scripts/check_imports.py
```

This script will fail if any file violates the dependency rules above.
Any agent working on this project must run it after making changes.

## Project Identity

- **Name:** ACA Ecosystem
- **Language:** Python 3.11+
- **LLM:** DeepSeek V4 API (via OpenAI SDK, base_url=api.deepseek.com)
- **Package manager:** pip (editable install: `pip install -e .`)
- **Config:** `.agent-config.yml` at project root
- **Secrets:** `.env` at project root (copy from `.env.example`). NEVER hardcode keys.

## Configuration Rules

All API keys and secrets go through `.env` → `core/config.py`. The flow:
```
.env → python-dotenv → os.environ → core.config.Config → tools/agents
```
- `DEEPSEEK_API_KEY` — required for LLM calls
- `BRAVE_API_KEY` — required for web search (assistant)
- Always add new configurable keys to: `.env.example`, `core/config.py` (DEFAULTS + ENV_MAP), and the Config class properties.

## Key Conventions

- Async everywhere (asyncio + httpx)
- Append-only conversation history for prompt caching
- Memory layers queried via TOOLS, never inserted into system prompt
- Typer CLI with Rich output
- LanceDB for vector DB, SQLite for metadata, bge-m3 for embeddings

## When Editing Files

- Place new tools in the correct agent's `tools/` directory
- Place shared infrastructure in `core/`
- Never add `from assistant...` in `aca/` (or vice versa)
- Run `python scripts/check_imports.py` after every edit session
