# ACA Ecosystem

Autonomous coding agent + general assistant sharing a common core library.

## Quick Start — Assistant Web App

### 1. Backend (Python)

```bash
# Install dependencies
pip install -e .

# Copy and fill in environment variables
cp .env.example .env
# Required: DEEPSEEK_API_KEY, BRAVE_API_KEY
# Optional (for Google sign-in): OAUTH_GOOGLE_CLIENT_ID, OAUTH_GOOGLE_CLIENT_SECRET

# Start the API server
python -m uvicorn assistant.server:app --host 0.0.0.0 --port 8000
```

### 2. Frontend (Next.js)

```bash
cd apps/assistant-web

# Install dependencies
npm install

# Start dev server
npm run dev
```

Open http://localhost:3000.

### 3. Google OAuth Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
2. Create an OAuth 2.0 Client ID (Web application)
3. Add `http://localhost:3000/api/auth/google/callback` as an authorized redirect URI
4. Copy Client ID and Client Secret to `.env`:
   ```
   OAUTH_GOOGLE_CLIENT_ID=xxx.apps.googleusercontent.com
   OAUTH_GOOGLE_CLIENT_SECRET=GOCSPX-xxx
   ```

---

## Project Structure

```
autonomous-coding-agent/
├── apps/
│   └── assistant-web/        # Next.js frontend (assistant-ui)
│       ├── app/              # Pages: root, login, auth callback
│       ├── components/       # Chat UI components (reasoning, tools, thread)
│       └── lib/              # Auth provider + thread context
│
├── src/
│   ├── core/                 # Shared library (LLM client, config, tools)
│   ├── aca/                  # Coding agent
│   └── assistant/            # General chatbot
│       ├── server.py         # FastAPI server (auth, threads, chat SSE)
│       ├── orchestrator.py   # ReAct loop with 18 tools
│       ├── auth.py           # Email/password + Google OAuth, thread storage
│       ├── memory/           # Personal + conversation memory
│       └── tools/            # Web search, web fetch, memory tools
│
├── pyproject.toml
├── .agent-config.yml
└── scripts/check_imports.py
```

---

## Backend API

| Method | Endpoint                          | Auth   | Description                           |
|--------|-----------------------------------|--------|---------------------------------------|
| POST   | `/api/auth/register`              | No     | Create account with email + password  |
| POST   | `/api/auth/login`                 | No     | Email + password sign in              |
| GET    | `/api/auth/google/login`          | No     | Redirect to Google OAuth              |
| GET    | `/api/auth/google/callback`       | No     | Google OAuth callback                 |
| GET    | `/api/auth/me`                    | Token  | Get current user                      |
| POST   | `/api/auth/logout`                | Token  | Invalidate session                    |
| GET    | `/api/threads`                    | Token  | List user's threads                   |
| POST   | `/api/threads`                    | Token  | Create new thread                     |
| GET    | `/api/threads/:id`                | Token  | Get thread with messages              |
| DELETE | `/api/threads/:id`                | Token  | Delete thread                         |
| PUT    | `/api/threads/:id/title`          | Token  | Update thread title                   |
| POST   | `/api/chat`                       | Token  | Send message, receive SSE stream      |
| POST   | `/api/geo`                        | Token  | Submit browser geolocation            |

Chat SSE events: `reasoning` → `tool_call` → `tool_result` → `text` → `done`

---

## Architecture Rules

```
src/core/      ← Shared lib. May NOT import from aca or assistant.
src/aca/       ← Coding agent. Imports from core ONLY.
src/assistant/ ← General chatbot. Imports from core ONLY.
```

Enforce before committing:
```bash
python scripts/check_imports.py
```

Agents collaborate via MCP at runtime — never import each other.

---

## Configuration

Configuration priority (highest to lowest):
1. Shell environment variables (`ACA_*` prefixed)
2. `.env` file in project root
3. `.agent-config.yml` in project root
4. Built-in defaults in `core/config.py`

## Key Tech

| Component     | Choice                      |
|---------------|-----------------------------|
| LLM           | DeepSeek V4 Pro (reasoning) |
| Python server | FastAPI + uvicorn           |
| Frontend      | Next.js + assistant-ui      |
| Chat UI       | ExternalStoreRuntime        |
| Database      | SQLite (auth, threads)      |
| Auth          | Email/Password + Google OAuth |
