# Agent Platform

**Current focus: Assistant Agent**

Agent Platform is a personal learning project for building and experimenting with AI agents. The current working application is a chatbot-style **Assistant Agent** that supports tool calling, semantic memory, persistent conversation history, reasoning visibility, and a web-based chat interface.

The project originally started as an autonomous coding agent, but I shifted focus toward building a strong assistant agent first. This gives me a practical environment to experiment with the core design problems behind useful agents: tool definitions, memory retrieval, prompt preprocessing, context management, skills, and different orchestration patterns.

The long-term goal is to learn through real usage and iteration, rather than assuming that any one agent architecture is always best.

---

## Features

* **Assistant chat interface** built on assistant-ui with custom UI components for reasoning, tool calls, feedback, and thread management
* **FastAPI backend** with Server-Sent Events chat streaming
* **ReAct-style tool loop** for tool calling and response generation
* **Custom tool framework** for defining tool schemas, usage rules, and execution logic
* **Semantic memory** using PostgreSQL, pgvector, and bge-m3 embeddings
* **Persistent conversation history** for user messages, assistant responses, reasoning, tool calls, and tool results
* **Reasoning / thinking display** in the chat UI
* **LLM call logging** for token usage, latency, cached tokens, and tool-call metadata
* **Feedback collection** for improving and debugging assistant responses
* **Authentication** with email/password and optional Google OAuth
* **Shared core library** for LLM calls, tools, config, database access, and embeddings

---

## Learning Focus

This project is also a hands-on environment for exploring what makes agents useful in practice.

Areas I am using it to experiment with include:

* How tools should be described and exposed to the model
* When the assistant should call tools versus answer directly
* How memory should be stored, retrieved, and used
* When prompts should be preprocessed or compacted
* How much control should be handled by the model versus the surrounding system
* How reusable **skills** can guide higher-level workflows beyond raw tool calls
* When more complex architectures are useful, such as planner-executor flows, orchestrator-led ReAct loops, supervisor/sub-agent systems, or parallel agent/tool calls

The goal is not to make the architecture more complex for its own sake, but to test which patterns are actually useful for my own workflows as the primary user.

---

## Architecture Overview

```text
User
  ↓
Next.js Assistant Web UI
  ↓ REST + SSE
FastAPI Backend
  ↓
Assistant Agent Loop
  ↓
LLM Client
  ↓
Tools / Memory / PostgreSQL
  ↓
Assistant Response
```

Main components:

```text
apps/assistant-web/    Next.js frontend built on assistant-ui
src/assistant/         Assistant backend and assistant-specific logic
src/core/              Shared LLM, tool, config, database, and embedding utilities
src/aca/               Early coding-agent prototype
PostgreSQL + pgvector  Persistent storage and semantic memory
```

The repository is named **Agent Platform** because the codebase is structured to support reusable agent infrastructure over time. However, the current practical focus is the **Assistant Agent**.

---

## Quick Start

### Prerequisites

* Python 3.11+
* Node.js 18+
* Docker / Docker Compose
* DeepSeek API key
* Brave Search API key, if using web search

### 1. Start PostgreSQL

```bash
docker compose up -d
```

### 2. Backend Setup

```bash
pip install -e .

cp .env.example .env
```

Fill in the required environment variables:

```env
DEEPSEEK_API_KEY=your_deepseek_api_key
BRAVE_API_KEY=your_brave_api_key
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/agent_platform
```

Optional Google OAuth variables:

```env
OAUTH_GOOGLE_CLIENT_ID=your_google_client_id
OAUTH_GOOGLE_CLIENT_SECRET=your_google_client_secret
FRONTEND_URL=http://localhost:3000
```

Run migrations and start the backend:

```bash
alembic upgrade head

python -m uvicorn assistant.server:app --host 0.0.0.0 --port 8000
```

### 3. Frontend Setup

```bash
cd apps/assistant-web

npm install
npm run dev
```

Open:

```text
http://localhost:3000
```

---

## Project Structure

```text
agent-platform/
├── apps/
│   └── assistant-web/        # Next.js assistant UI
│
├── src/
│   ├── core/                 # Shared LLM, tool, config, DB, and embedding utilities
│   ├── assistant/            # Main assistant agent backend
│   └── aca/                  # Early coding-agent prototype
│
├── migrations/               # Alembic migrations
├── scripts/                  # Development scripts
├── docker-compose.yml        # PostgreSQL + pgvector
├── pyproject.toml
└── README.md
```

---

## Tech Stack

| Area          | Tech                                 |
| ------------- | ------------------------------------ |
| Backend       | FastAPI, Uvicorn                     |
| Frontend      | Next.js, React                       |
| Chat UI base  | assistant-ui                         |
| Streaming     | Server-Sent Events                   |
| LLM provider  | DeepSeek via OpenAI-compatible SDK   |
| Database      | PostgreSQL                           |
| Vector search | pgvector                             |
| ORM           | SQLAlchemy async                     |
| Migrations    | Alembic                              |
| Embeddings    | bge-m3 via sentence-transformers     |
| Auth          | Email/password, Google OAuth         |
| Tool system   | Custom Tool / ToolRegistry framework |

---

## Development Notes

Run the architecture boundary check before committing:

```bash
python scripts/check_imports.py
```

Package boundaries:

```text
src/core/       Shared infrastructure. Should not import from assistant or aca.
src/assistant/  Assistant agent. May import from core.
src/aca/        Coding-agent prototype. May import from core.
```

---

## Current Limitations

This project is under active development and is currently optimized for personal use and experimentation.

Known limitations:

* The assistant agent is the main working application; the coding agent is still an early prototype.
* MCP integration is not currently implemented.
* Adaptive context compaction is still being improved.
* Test coverage and production hardening are not yet complete.
* There is no admin dashboard for reviewing feedback or LLM usage logs.

---

## Roadmap

Short-term:

* Improve memory retrieval and deduplication
* Continue refining prompt/context management
* Add more useful assistant tools and skills
* Add tests for core backend flows

Longer-term:

* Experiment with orchestrator-led and multi-agent architectures
* Expand the coding-agent prototype after the assistant foundation is stronger
* Explore MCP or another integration protocol for future agents/tools
* Support more model providers or local models
