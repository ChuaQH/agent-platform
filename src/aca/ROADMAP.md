# Autonomous Coding Agent — Implementation Roadmap

## Tech Stack (Finalized)

| Choice | Decision | Notes |
|--------|----------|-------|
| **Language** | **Python** | Fastest to prototype. Rich AI/LLM ecosystem. |
| **LLM** | **DeepSeek V4 (API)** | Per-token pricing. |
| **Vector DB** | **LanceDB** | Embedded, no server, columnar storage. Free (Apache 2.0). |
| **Embedding Model** | **bge-m3** | Local/offline, no API cost. Good quality for code. |
| **CLI Framework** | **Typer** | Rich output, modern Python. Free. |
| **Async** | **asyncio** | Python stdlib. |
| **Database** | **SQLite** | Zero setup, sufficient for metadata. Python stdlib. |
| **HTTP Client** | **httpx** | Modern async, HTTP/2 support. Free. |

---

## Phase 1: Project Scaffolding & Core Loop (Week 1-2)

**Goal:** A CLI that takes a prompt, calls DeepSeek V4, and responds. No memory. No tools. Just the loop.

### Milestone 1.1: Project Setup
- [x] Initialize Python project with `pyproject.toml` (multi-package: `src/core`, `src/aca`, `src/assistant`)
- [x] Set up monorepo structure:
  ```
  src/
  ├── core/                      # Shared library (all agents import from here)
  │   ├── config.py              #   Config loader
  │   ├── llm_client.py          #   LLM client (DeepSeek via OpenAI SDK)
  │   ├── conversation.py        #   Conversation state
  │   ├── tool_framework.py      #   Tool base class + registry
  │   └── cache/                 #   Caching infrastructure
  │
  ├── aca/                       # ACA — Autonomous Coding Agent
  │   ├── cli/                   #   CLI entry point (main.py)
  │   ├── orchestrator.py        #   (Phase 5) Coding orchestrator
  │   ├── subagent.py            #   (Phase 5) Subagent spawner
  │   ├── memory/                #   (Phase 2+) Memory layers 1-5
  │   ├── tools/                 #   (Phase 3+) Code tools
  │   └── mcp_server.py          #   (Future) MCP exposure
  │
  └── assistant/                 #   (Future) General chatbot assistant
  ```
- [x] Set up `.agent-config.yml` schema
- [x] Set up `.gitignore` (exclude `agent-data/`, `.env`, `__pycache__/`)
- [x] Set up `scripts/check_imports.py` to enforce import boundaries

### Milestone 1.2: DeepSeek Client (in core/)
- [x] Implement `LLMClient` in `src/core/llm_client.py` using OpenAI SDK pointed at DeepSeek
- [x] Handle API key from env/config
- [x] Basic chat completion: send messages, stream response
- [x] Error handling (rate limits, timeouts, auth errors)
- [x] **Cache-aware message sending:** Track message hashes + cache hit rate

### Milestone 1.3: CLI Entry Point
- [ ] `aca "your prompt"` → sends to DeepSeek → streams response to terminal
- [ ] Optional `--project-path` flag to specify the repo
- [ ] Rich formatting for tool calls / responses / errors
- [ ] Graceful exit on Ctrl+C

### Milestone 1.4: Conversation State (in core/)
- [x] `Conversation` class in `src/core/conversation.py` — append-only message history
- [x] Static prefix tracking with hash for cache integrity
- [x] System prompt builder (static prefix assembly)
- [x] Session ID generation
- [ ] Save raw conversation to `agent-data/sessions/` as JSON (for debugging)

**Phase 1 Checkpoint:** You can type `aca "Write a function that sorts a list"` and get a response. Core infrastructure (config, LLM client, conversation) is in `src/core/` and shared by all agents.

---

## Phase 2: Memory Layer 1 — Project Identity (Week 2)

**Goal:** Agent reads project context files and uses them to inform responses.

### Milestone 2.1: Agent Context File System
- [ ] Define `/agent-context/` directory schema (PROJECT.md, CONVENTIONS.md, etc.)
- [ ] Implement `Layer1Identity` class:
  - `load_all()` → returns all context files as concatenated string
  - `load_summary()` → returns only PROJECT.md (for static prefix)
  - `get_file_hash()` → returns hash for cache key
- [ ] Integration: static prefix now includes Layer 1 content

### Milestone 2.2: Bootstrapping Prompt
- [ ] Assemble full static prefix: system prompt + tools + Layer 1
- [ ] Hash the static prefix → use as cache key for cross-session tracking
- [ ] On session start, check if Layer 1 changed since last session (hash compare)

### Milestone 2.3: Test with a Real Project
- [ ] Pick a small test project (e.g., a simple Express/Flask app)
- [ ] Create its `/agent-context/PROJECT.md`, `/agent-context/CONVENTIONS.md`
- [ ] Start a session → verify agent references project details in responses
- [ ] Start a second session → verify agent still knows the project

**Phase 2 Checkpoint:** Agent knows what project it's working on across sessions.

---

## Phase 3: Tool System & File Operations (Week 2-3)

**Goal:** Agent can read and edit files. The orchestrator can use tools.

### Milestone 3.1: Tool Framework
- [ ] Define `Tool` base class / protocol:
  ```python
  class Tool:
      name: str
      description: str
      parameters: dict  # JSON Schema
      async def execute(self, **params) -> ToolResult
  ```
- [ ] Tool registry: register tools, generate OpenAI-compatible function definitions
- [ ] Tool result compression: summarize large results for conversation history

### Milestone 3.2: File Tools
- [ ] `read_file(path, start_line, end_line)` — read from the project repo
- [ ] `write_file(path, content)` — create new file
- [ ] `edit_file(path, old_string, new_string)` — replace in existing file
- [ ] `list_dir(path)` — list directory contents
- [ ] `search_files(pattern)` — glob search in project
- [ ] `grep_search(pattern)` — text search in project
- [ ] All file paths resolved relative to project root
- [ ] **Tier 1 actions** (read, list, search, grep): auto-allowed
- [ ] **Tier 2 actions** (write, edit): auto-allowed, logged

### Milestone 3.3: Git Tools (Tier 2)
- [ ] `git_status()` — check repo status
- [ ] `git_diff()` — show unstaged changes
- [ ] `git_log(n)` — recent commits
- [ ] `git_branch()` — current branch

### Milestone 3.4: Tool Execution Loop
- [ ] Orchestrator sends prompt → DeepSeek returns tool call → execute → append result → loop
- [ ] Timeout per tool call (default 30s)
- [ ] Max tool call depth per turn (default 10)

**Phase 3 Checkpoint:** Agent can read your codebase and make edits.

---

## Phase 4: Memory Layers 2 & 3 — Outcomes & Decisions (Week 3)

**Goal:** Agent remembers what it did and why across sessions.

### Milestone 4.1: Outcome Log (Layer 2)
- [ ] SQLite schema for outcomes:
  ```sql
  CREATE TABLE outcomes (
      id TEXT PRIMARY KEY,
      timestamp TEXT,
      summary TEXT,
      files_touched TEXT,  -- JSON array
      status TEXT,         -- done, partial, failed
      tags TEXT,           -- JSON array
      session_id TEXT
  );
  ```
- [ ] `write_outcome(summary, files, status, tags)` tool
- [ ] `get_recent_outcomes(n=15)` tool
- [ ] `OUTCOMES.md` file as human-readable mirror (git-tracked)
- [ ] Auto-write outcome after each subagent/spawned task completes

### Milestone 4.2: Decision Log (Layer 3)
- [ ] File-based decision records in `/agent-context/decisions/`
- [ ] `write_decision(title, context, decision, rationale, alternatives)` tool
- [ ] `find_decisions(topic)` tool — keyword search across decision files
- [ ] Decision index (`INDEX.md`) auto-generated from file titles
- [ ] Decision index included in static prefix

### Milestone 4.3: Integration
- [ ] After each user request is completed, write an outcome
- [ ] When agent makes a consequential choice (library, pattern, architecture), write a decision
- [ ] Verify: new session sees outcomes and decisions from previous sessions

**Phase 4 Checkpoint:** Agent remembers what it did and why.

---

## Phase 5: Subagent System (Week 3-4)

**Goal:** Orchestrator spawns stateless subagents for heavy work. Subagents share cached static prefix.

### Milestone 5.1: Subagent Implementation
- [ ] `Subagent` class:
  - Has its own tool set (file tools, codebase tools — subset of orchestrator tools)
  - Receives context bundle: task + code snippets + decisions + session history
  - Has its own conversation loop (multi-turn internally, invisible to orchestrator)
  - Returns structured outcome: `{summary, files_changed, decisions_made, problems}`
- [ ] Subagent system prompt (static, identical across all subagents)
- [ ] Subagent tool definitions (static, identical across all subagents)

### Milestone 5.2: Spawn Subagent Tool
- [ ] `spawn_subagent(task, context)` tool on the orchestrator
- [ ] Context bundle assembly:
  1. Query L4 for relevant code → include in context
  2. Query L3 for relevant decisions → include in context
  3. Query L5 for relevant sessions → include in context
  4. Package into single user message for subagent
- [ ] Timeout + max turns for subagent
- [ ] Error handling: subagent failure → orchestrator retries or reports

### Milestone 5.3: Subagent Prompt Caching
- [ ] Subagent static prefix built once, reused across all spawns
- [ ] Log cache hit rate for subagent calls
- [ ] Verify: spawning 3 subagents in a session → cache hits on calls 2 and 3

**Phase 5 Checkpoint:** Orchestrator delegates work. Subagent cache hits verified.

---

## Phase 6: Code Knowledge — Layer 4 (Week 4-5)

**Goal:** Agent can semantically search the codebase.

### Milestone 6.1: Embedding Pipeline
- [ ] Choose embedding model (bge-m3 or DeepSeek embedding if available)
- [ ] Code chunking strategy:
  - File-level chunks (small files < 200 lines)
  - Function/class-level chunks (parse with tree-sitter or AST)
  - Include file path, function signature, docstring in embedding text
- [ ] Indexer: walk project, chunk code, generate embeddings, store in vector DB

### Milestone 6.2: Vector DB Integration
- [ ] ChromaDB setup (embedded mode, no server needed)
- [ ] `search_codebase(query, n=5)` tool:
  - Embed query
  - Search vector DB for top-N chunks
  - Return file paths + code snippets + relevance scores
- [ ] File-level metadata filtering (search only in `src/`, exclude `node_modules/`)

### Milestone 6.3: Incremental Indexing
- [ ] File watcher detects changes → re-embed changed files
- [ ] On session start, do quick diff scan: index new files, re-index changed files
- [ ] Hash-based dedup: skip files whose hash hasn't changed

**Phase 6 Checkpoint:** Agent finds relevant code semantically, not just by filename.

---

## Phase 7: Session History — Layer 5 (Week 5)

**Goal:** Agent remembers past sessions and can retrieve them by relevance.

### Milestone 7.1: Session Summary Storage
- [ ] SQLite schema for sessions:
  ```sql
  CREATE TABLE sessions (
      id TEXT PRIMARY KEY,
      timestamp TEXT,
      summary TEXT,
      files_touched TEXT,
      decisions TEXT,
      problems TEXT,
      tags TEXT,
      embedding BLOB
  );
  ```
- [ ] `write_session_summary(data)` — called at session end
- [ ] Embed the summary text and store alongside

### Milestone 7.2: Session Retrieval
- [ ] `get_recent_sessions(n=5)` tool — returns last N summaries
- [ ] `search_sessions(query, n=5)` tool — semantic search across session embeddings
- [ ] Both tools called during bootstrapping (first turn)

### Milestone 7.3: Hierarchical Compression
- [ ] Weekly digest: summarize last week's sessions into one summary
- [ ] Monthly digest: summarize last month's weekly digests
- [ ] Compression subagent: spawned periodically to run compression
- [ ] Archival: raw sessions > 4 weeks old → stored as compressed digest only

**Phase 7 Checkpoint:** Agent recalls relevant past work across sessions.

---

## Phase 8: Data-Tier Caching (Week 5-6)

**Goal:** Avoid redundant queries to vector DB and memory layers.

### Milestone 8.1: L1 Session Memory Cache
- [ ] In-memory dictionary, keyed by query hash
- [ ] Wrapped around `search_codebase`, `find_decisions`, `search_sessions`, `get_recent_outcomes`
- [ ] Auto-evicted at session end

### Milestone 8.2: L2 Disk Cache (SQLite)
- [ ] Cache schema for query results
- [ ] Key: `sha256(query_string + file_hashes)`
- [ ] Value: serialized result + timestamp
- [ ] TTL: 1 hour (configurable)
- [ ] File-hash-based invalidation on file change

### Milestone 8.3: L3 Vector DB (already done in Phase 6)
- [ ] Embeddings are permanent, re-indexed on file change

**Phase 8 Checkpoint:** Repeated queries hit cache, not vector DB.

---

## Phase 9: Prompt Caching Metrics & Tuning (Week 6)

**Goal:** Verify and optimize DeepSeek API cache hit rate.

### Milestone 9.1: Cache Hit Tracking
- [ ] Log every API call: total tokens, cache hit tokens, cache miss tokens
- [ ] Per-session cache hit rate
- [ ] Per-subagent-spawn cache hit rate
- [ ] Dashboard / summary at session end

### Milestone 9.2: Cache Optimization
- [ ] Verify static prefix is byte-identical across turns (hash check before sending)
- [ ] Verify subagent static prefix is byte-identical across spawns
- [ ] Tune static prefix size: not too large (wastes first-call tokens), not too small (misses context)
- [ ] Tune compression threshold: when to accept a cache miss to free context window

### Milestone 9.3: DeepSeek TTL Testing
- [ ] Test: how long does DeepSeek hold the cache? (1 min? 5 min? 1 hour?)
- [ ] Test: does cache survive between different API key scopes?
- [ ] Document findings for future optimization

**Phase 9 Checkpoint:** Cache hit rate measured and optimized. Known DeepSeek cache TTL.

---

## Phase 10: Daemon Mode (Week 6-7)

**Goal:** Agent runs continuously, listening for triggers.

### Milestone 10.1: Daemon Architecture
- [ ] Long-running process with event loop
- [ ] Watch for: new CLI commands, git commits, file changes, scheduled tasks
- [ ] Session management: multiple concurrent sessions? Or queued?
- [ ] Graceful shutdown

### Milestone 10.2: Git Hook Integration
- [ ] On new commit: agent reviews changes, writes outcome log
- [ ] On push: auto-write session summary
- [ ] Configurable: which hooks trigger what behavior

### Milestone 10.3: Scheduled Maintenance
- [ ] Daily: compress sessions → weekly digests
- [ ] Weekly: compress digests → monthly digests
- [ ] Daily: prune stale L2 cache entries
- [ ] On file change: re-index in L3

**Phase 10 Checkpoint:** Agent runs continuously, self-maintains memory.

---

## Phase 11: Polish & Production Readiness (Week 7-8)

### Milestone 11.1: Error Handling & Resilience
- [ ] DeepSeek API: retry with backoff, circuit breaker
- [ ] Tool failures: graceful degradation, don't crash the session
- [ ] Subagent failures: retry once, then report to user
- [ ] Corrupted memory: self-healing (re-index, rebuild)

### Milestone 11.2: Configuration
- [ ] Full `.agent-config.yml` schema documented
- [ ] Per-project config overrides
- [ ] Environment variable for secrets (API keys)

### Milestone 11.3: Logging & Observability
- [ ] Structured logging (JSON lines)
- [ ] Session metrics: turns, tokens, cache hits, tools called, subagents spawned
- [ ] Export session logs for debugging

### Milestone 11.4: Testing
- [ ] Unit tests: each memory layer, each tool, caching
- [ ] Integration tests: full session with a test project
- [ ] Mock DeepSeek responses for deterministic testing

### Milestone 11.5: Documentation
- [ ] README: setup, usage, configuration
- [ ] Architecture docs finalized
- [ ] Example projects with agent-context directories

---

## Future: VS Code Extension (Phase 12)

- [ ] Extension scaffold
- [ ] Sidebar panel for agent interaction
- [ ] Inline diff preview for agent edits
- [ ] Trust tier confirmation dialogs
- [ ] Agent context file editor UI

---

## Build Order Rationale

```
Phase 1: CLI + LLM        ← Get something working. Ship day 1.
Phase 2: Layer 1          ← First taste of cross-session context.
Phase 3: Tools            ← Now the agent can DO things.
Phase 4: Layers 2-3       ← Now it REMEMBERS what it did.
Phase 5: Subagents        ← Scale to complex tasks.
Phase 6: Layer 4          ← Understand the codebase.
Phase 7: Layer 5          ← Remember past sessions.
Phase 8: Data Caching     ← Speed up repeated queries.
Phase 9: Prompt Caching   ← Measure & optimize API costs.
Phase 10: Daemon          ← Run continuously.
Phase 11: Polish          ← Production-ready.
```

Each phase builds on the previous. You can stop after any phase and have a working (if simpler) agent. Phases 1-5 give you the core value prop: an agent that works across sessions. Phases 6-9 are optimization. Phase 10+ is polish and advanced features.

---

## Key Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| **DeepSeek API changes / instability** | Abstract LLM client behind interface. Can swap to Claude/GPT if needed. |
| **Context window overflow** | DeepSeek V4 has 1M tokens. Monitor usage. Compression at 80% threshold. |
| **Cache TTL too short for cross-session** | Accept intra-session caching as baseline. Cross-session is bonus. |
| **Vector DB complexity** | LanceDB is embedded (no server). Start simple, add complexity only as needed. |
| **Tool result bloat** | Compress aggressively. Subagents get full results; orchestrator keeps summaries. |
| **Subagent going off-rails** | Max turns + timeout + structured output format. Kill and retry on failure. |
