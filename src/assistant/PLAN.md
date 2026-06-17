# Assistant Agent — Architecture & Plan

## Overview

A general-purpose AI assistant accessible via web UI (FastAPI server).
Handles conversation, web research, personal memory, and delegates coding
tasks to ACA via MCP when needed.

Architecturally simpler than ACA — no subagents, no code-specific memory
layers, no project context. A single orchestrator with a ReAct loop and
a curated tool set.

Shared infrastructure (config, LLM client, conversation, tool framework)
is imported from `src/core/`.

---

## Architecture

```
src/assistant/
├── server.py                  # FastAPI app (HTTP + WebSocket)
├── orchestrator.py            # ReAct loop: think → act → observe → repeat
├── memory/
│   ├── personal.py            # User preferences, facts, notes (persistent)
│   └── conversations.py       # Chat history per user/session
├── tools/
│   ├── web_search.py          # Search the web for current information
│   ├── web_fetch.py           # Fetch and extract content from a URL
│   ├── calculator.py          # Evaluate math expressions safely
│   ├── personal_memory.py     # Remember/recall facts about the user
│   └── aca_delegate.py        # (Future) Delegate coding tasks to ACA via MCP
└── mcp_client.py              # (Future) Connect to ACA or other agents
```

---

## How It Works (ReAct Loop)

```
User: "What's the latest Python version and what's new in it?"

Orchestrator (ReAct):
  Thought: I need current information. Let me search the web.
  Action: web_search("latest Python version 2026 what's new")
  Observation: [Search results: Python 3.14 released April 2026...]
  Thought: I have enough to answer. Let me also check if user
           wants to know how this affects their projects.
  Response: "Python 3.14 was released in April 2026. Key changes
             include..."
```

No subagents. No delegation. Just the orchestrator choosing tools in a loop
until it has enough to respond.

---

## Tool Set

### Tier 1: Core Tools (Build First)

These make the assistant useful on day one.

| Tool | Purpose | Complexity |
|------|---------|------------|
| **web_search** | Search the web via a search API (Brave, SerpAPI, or DuckDuckGo) | Low |
| **web_fetch** | Fetch a URL and extract readable text content | Low |
| **calculator** | Evaluate mathematical expressions safely (no `eval`) | Low |
| **personal_memory** | Store and recall user facts/preferences across sessions | Medium |

### Tier 2: Power Tools (Add Later)

These make the assistant more capable but aren't critical for launch.

| Tool | Purpose | Complexity |
|------|---------|------------|
| **code_runner** | Execute Python/JS snippets in a sandbox | Medium |
| **file_ops** | Read/write files on the user's machine (local only) | Medium |
| **image_gen** | Generate images via API (DALL-E, Stable Diffusion) | Low |
| **aca_delegate** | Delegate coding tasks to ACA via MCP | Low (imports ACA's ToolRegistry) |

### Tier 3: Platform Tools (Future)

These turn the assistant into a full platform.

| Tool | Purpose |
|------|---------|
| **calendar** | Read/create calendar events |
| **email** | Draft/send emails |
| **reminders** | Set reminders/notifications |
| **news_summary** | Summarize news on a topic |

---

## Tool Design Details

### web_search

```python
class WebSearchTool(Tool):
    name = "web_search"
    description = "Search the web for current information. Use when you need facts, news, or documentation that may have changed since your training cutoff."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query"},
            "n_results": {"type": "integer", "default": 5},
        },
        "required": ["query"],
    }
```

**Backend options:**
- **Brave Search API** (free tier: 2,000 queries/month, good quality)
- **SerpAPI** (paid, most comprehensive)
- **DuckDuckGo** (free, no API key needed, lower quality)

Recommendation: Start with DuckDuckGo (free, no account), add Brave later for quality.

### web_fetch

```python
class WebFetchTool(Tool):
    name = "web_fetch"
    description = "Fetch the content of a URL and extract readable text. Use after web_search to read a specific page in detail."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to fetch"},
        },
        "required": ["url"],
    }
```

**Implementation:** httpx GET → html2text or BeautifulSoup → return Markdown.
Timeout: 10s. Max content: 50K chars (summarize if longer).

### calculator

```python
class CalculatorTool(Tool):
    name = "calculator"
    description = "Evaluate a mathematical expression safely. Supports basic math, trigonometry, and statistics."
    parameters = {
        "type": "object",
        "properties": {
            "expression": {"type": "string", "description": "Math expression to evaluate, e.g. 'sqrt(144) + 3*5'"},
        },
        "required": ["expression"],
    }
```

**Implementation:** Python's `math` module with a safe expression parser.
NOT raw `eval()`. Use `ast.parse` with a whitelist of allowed operations.

### personal_memory

This is the assistant's version of "memory across sessions."
Much simpler than ACA's 5 layers — just a key-value store with semantic search.

```python
class RememberTool(Tool):
    name = "assistant_remember"
    description = "Store a fact about the user for future sessions."
    parameters = {
        "type": "object",
        "properties": {
            "fact": {"type": "string", "description": "The fact to remember"},
            "category": {"type": "string", "enum": ["preference", "personal", "project", "note"]},
        },
        "required": ["fact"],
    }

class RecallTool(Tool):
    name = "assistant_recall"
    description = "Recall facts about the user from past conversations."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for"},
            "n_results": {"type": "integer", "default": 5},
        },
        "required": ["query"],
    }
```

**Storage:** SQLite + bge-m3 embeddings (reuses the same embedding pipeline from core).
Stored in `agent-data/assistant/memory.db`.

### aca_delegate (Future — MCP-based)

When ACA is running as an MCP server, the assistant can call its tools:

```python
class ACADelegateTool(Tool):
    name = "aca_search_codebase"
    description = "Search the user's codebase for relevant code. Delegates to ACA."
    # This tool appears alongside assistant's own tools.
    # The LLM picks it when the user asks a coding question.
    # Internally, it calls ACA's MCP server.
```

The assistant doesn't import ACA directly. It connects via MCP, discovers ACA's
tools, and adds them to its own tool set. The LLM self-selects which to use.

---

## Memory Design

Unlike ACA (which has 5 layers for code-specific memory), the assistant
needs only 2:

| Memory | Storage | Purpose |
|--------|---------|---------|
| **Personal memory** | SQLite + embeddings | User preferences, facts, notes — cross-session |
| **Conversation memory** | In-memory (per session) + SQLite (archived) | Recent chat history for context |

### Personal Memory Schema

```sql
CREATE TABLE memories (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    fact TEXT NOT NULL,
    category TEXT,          -- 'preference', 'personal', 'project', 'note'
    embedding BLOB,         -- bge-m3 embedding for semantic search
    created_at TEXT,
    updated_at TEXT
);
```

### Conversation Memory

- **Active session:** Last N messages kept in-memory (in the orchestrator's Conversation object from `core/`)
- **Archived:** Older sessions summarized and stored for future reference
- **Summarization:** When a session exceeds 50 messages, compress the first 40 into a summary, keep the last 10 fresh

---

## Server Design

```
POST /chat          — Send a message, get a response (HTTP)
WS   /chat/ws       — Same but via WebSocket (streaming)
GET  /sessions      — List past sessions
GET  /sessions/{id} — Get a past session
DELETE /sessions/{id} — Delete a session
```

The server is stateless per request but maintains session state in memory
for active WebSocket connections.

---

## Build Plan (Minimal)

### Week 1: Core Assistant
- [ ] `orchestrator.py` — ReAct loop using `core.llm_client.LLMClient` and `core.conversation.Conversation`
- [ ] `tools/web_search.py` — DuckDuckGo or Brave Search
- [ ] `tools/web_fetch.py` — HTTP fetch + HTML extraction
- [ ] `tools/calculator.py` — Safe math evaluation
- [ ] `server.py` — FastAPI with POST /chat endpoint

### Week 2: Memory
- [ ] `memory/personal.py` — SQLite + embedding storage
- [ ] `tools/personal_memory.py` — remember/recall tools
- [ ] `memory/conversations.py` — Session archiving + summarization

### Week 3: Polish
- [ ] WebSocket streaming endpoint
- [ ] Multi-user support (basic)
- [ ] Simple web UI

### Future
- [ ] MCP client to connect to ACA
- [ ] Code runner sandbox
- [ ] Calendar/email integration
- [ ] Image generation

---

## Key Differences from ACA

| Concern | ACA | Assistant |
|---------|-----|-----------|
| **Delivery** | CLI → VS Code extension | Web server (FastAPI) |
| **Orchestration** | Orchestrator + subagents | Single orchestrator (ReAct) |
| **Memory** | 5 layers (code-specific) | 2 types (personal + conversation) |
| **Tools** | Code tools (read, edit, git, search) | General tools (web, math, memory) |
| **Project awareness** | Yes (agent-context/, Layers 1-5) | No (per-user, not per-project) |
| **MCP** | Server (exposes coding tools) | Client (consumes ACA's tools) |
