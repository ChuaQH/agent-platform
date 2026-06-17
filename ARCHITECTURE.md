# ACA Ecosystem — Architecture

## Overview

The ACA Ecosystem contains two standalone agents sharing a common core library:

| Agent | Delivery | Purpose |
|-------|----------|---------|
| **ACA** (Autonomous Coding Agent) | CLI → VS Code Extension | Coding agent with project-level memory across sessions |
| **Assistant** | Web server (FastAPI) | General-purpose chatbot with personal memory |

Both agents are **fully standalone** — each works without the other running.
They can **collaborate via MCP** (Model Context Protocol) when both are active.

---

## Project Structure

```
autonomous-coding-agent/
├── src/
│   ├── core/                    # Shared library
│   │   ├── __init__.py
│   │   ├── config.py            # Config loader (YAML + env + defaults)
│   │   ├── llm_client.py        # LLM client (DeepSeek via OpenAI SDK)
│   │   ├── conversation.py      # Append-only conversation state
│   │   ├── tool_framework.py    # Tool base class + registry
│   │   └── cache/               # Prompt + data caching
│   │
│   ├── aca/                     # Autonomous Coding Agent
│   │   ├── cli/                 # CLI entry point
│   │   ├── orchestrator.py      # Coding orchestrator
│   │   ├── subagent.py          # Coding subagent
│   │   ├── memory/              # Layers 1-5 (code-specific)
│   │   ├── tools/               # Code tools + ToolRegistry
│   │   └── mcp_server.py        # (Future) MCP exposure
│   │
│   └── assistant/               # General Assistant
│       ├── server.py            # FastAPI app
│       ├── orchestrator.py      # General orchestrator
│       ├── memory/              # Personal + conversation memory
│       ├── tools/               # General tools (web search, etc.)
│       └── mcp_client.py        # (Future) Connects to ACA via MCP
│
├── aca-agent/                   # ACA-specific documentation
│   ├── PLAN.md
│   ├── ARCHITECTURE.md
│   └── ROADMAP.md
│
├── pyproject.toml               # Project config + dependencies
├── .agent-config.yml            # Agent behavior configuration
├── .gitignore
└── README.md
```

---

## Dependency Flow

```
         ┌──────────┐
         │  core/   │  ← LLM client, config, conversation, tools
         └────┬─────┘
         ┌────┴─────┐
    ┌────▼───┐ ┌───▼──────┐
    │  aca/  │ │assistant/│
    └────────┘ └──────────┘
    ↑ imports   ↑ imports
    from core   from core
    ONLY        ONLY

    ✗ aca/ never imports from assistant/
    ✗ assistant/ never imports from aca/
```

This enforced dependency direction keeps the agents cleanly separated
while sharing infrastructure through `core/`.

---

## Collaboration via MCP

When both agents need to collaborate, they communicate through
Model Context Protocol (MCP):

```
┌──────────┐  MCP (stdio/HTTP)  ┌───────────┐
│   ACA    │◄──────────────────►│ Assistant  │
│ (server) │                    │  (client)  │
└──────────┘                    └───────────┘
   exposes:                       sees ACA tools as:
   aca_search_codebase            native function calls
   aca_edit_file
   aca_spawn_subagent
   aca_get_outcomes
```

- ACA runs as an MCP **server**, exposing its coding tools
- Assistant runs as an MCP **client**, discovering and calling ACA's tools
- The assistant's LLM self-selects tools — no explicit routing
- Each agent remains fully functional standalone

---

## Usage Modes

| Mode | What Runs | Use Case |
|------|-----------|----------|
| **ACA CLI** | `aca chat "fix bug"` | Quick coding task from terminal |
| **Assistant server** | `uvicorn assistant.server:app` | General chat via web UI |
| **Assistant + ACA MCP** | Both running, MCP connected | Full platform: coding + general chat |

---

## Key Design Principles

1. **Shared core, separate agents.** `core/` has zero agent-specific logic.
2. **No cross-imports.** `aca/` and `assistant/` never import each other.
3. **MCP for collaboration.** Standard protocol, no custom delegation code.
4. **Standalone-first.** Each agent works without the other. MCP is additive.
5. **Append-only conversation.** Maximizes LLM prompt caching across turns.
6. **Tool-based memory retrieval.** Memory layers are queried via tools, not preloaded into prompts.

---

## Tech Stack

| Component | Choice |
|-----------|--------|
| Language | Python 3.11+ |
| LLM | DeepSeek V4 (API, OpenAI-compatible) |
| LLM SDK | openai (pointed at api.deepseek.com) |
| Vector DB | LanceDB (embedded) |
| Embeddings | bge-m3 (local) |
| CLI | Typer + Rich |
| Server | FastAPI + uvicorn |
| Async | asyncio |
| Database | SQLite (aiosqlite) |
| Agent protocol | MCP (Model Context Protocol) |
