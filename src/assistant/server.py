"""FastAPI server for the general assistant agent.

Usage:
    uvicorn assistant.server:app --host 0.0.0.0 --port 8000
    uvicorn assistant.server:app --reload  # Development
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.config import load_config, Config
from assistant.orchestrator import AssistantOrchestrator

logger = logging.getLogger(__name__)

# ── Pydantic models ────────────────────────────────────────────────────────


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


class ChatResponse(BaseModel):
    response: str
    session_id: str


class MetricsResponse(BaseModel):
    session_id: str
    messages: int
    estimated_tokens: int
    llm_metrics: dict


# ── Global state ───────────────────────────────────────────────────────────

orchestrator: Optional[AssistantOrchestrator] = None
config: Optional[Config] = None


# ── Lifecycle ──────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown."""
    global orchestrator, config

    # Startup
    config = load_config()
    _setup_logging(config)
    orchestrator = AssistantOrchestrator(config)
    orchestrator.start_session()
    logger.info("Assistant server started. Session: %s", orchestrator.session_id)

    yield

    # Shutdown
    if orchestrator:
        orchestrator.end_session()
        logger.info("Assistant server shut down.")


def _setup_logging(config: Config) -> None:
    """Configure logging for the server."""
    log_dir = config.project_root / "agent-data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "assistant.log"),
            logging.StreamHandler(),
        ],
    )


# ── FastAPI app ────────────────────────────────────────────────────────────

app = FastAPI(
    title="Assistant Agent",
    description="General-purpose AI assistant with web search, calculator, and memory.",
    version="0.1.0",
    lifespan=lifespan,
)

# Mount static files (frontend)
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
async def root():
    """Serve the chat UI."""
    index_path = Path(__file__).parent / "static" / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return {"status": "running", "note": "static/index.html not found"}


@app.get("/health")
async def health():
    """Health check."""
    return {
        "service": "ACA Assistant",
        "version": "0.1.0",
        "status": "running",
        "session_id": orchestrator.session_id if orchestrator else "none",
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """Send a message to the assistant and get a response.

    Example:
        curl -X POST http://localhost:8000/chat \
          -H "Content-Type: application/json" \
          -d '{"message": "What is the capital of France?"}'
    """
    if not orchestrator:
        raise HTTPException(status_code=503, detail="Orchestrator not initialized")

    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    try:
        response_text = await orchestrator.handle_message(request.message)
    except Exception as e:
        logger.error("Error handling message: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    return ChatResponse(
        response=response_text,
        session_id=orchestrator.session_id,
    )


@app.get("/metrics", response_model=MetricsResponse)
async def metrics():
    """Get current session metrics."""
    if not orchestrator:
        raise HTTPException(status_code=503, detail="Orchestrator not initialized")

    m = orchestrator.metrics
    return MetricsResponse(
        session_id=m["session_id"],
        messages=m["messages"],
        estimated_tokens=m["estimated_tokens"],
        llm_metrics=m["llm_metrics"],
    )


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    """Send a message and stream the assistant's process via SSE.

    Events:
        thinking    — LLM's reasoning, streamed token by token
        thinking_end — thinking phase complete
        tool_call   — a tool is being executed
        tool_result — tool completed (with sources for web_search)
        response    — final answer
        done        — stream complete
        error       — something went wrong
    """
    if not orchestrator:
        raise HTTPException(status_code=503, detail="Orchestrator not initialized")

    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    async def generate():
        try:
            async for event in orchestrator.handle_message_stream(request.message):
                yield event
        except Exception as e:
            logger.error("Stream error: %s", e)
            import json
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "assistant.server:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
