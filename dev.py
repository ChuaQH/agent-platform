"""Run the assistant backend and frontend together for local development.

Usage:
    python dev.py

Starts:
  - FastAPI backend  (uvicorn, http://localhost:8000)
  - Next.js frontend (npm run dev, http://localhost:3000)

Press Ctrl+C to stop both. On Windows the full process trees are killed
(taskkill /T), so no orphaned node/uvicorn processes keep the ports busy.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FRONTEND_DIR = ROOT / "apps" / "assistant-web"

NPM = "npm.cmd" if os.name == "nt" else "npm"


def venv_python() -> str:
    for candidate in (
        ROOT / ".venv" / "Scripts" / "python.exe",  # Windows
        ROOT / ".venv" / "bin" / "python",           # POSIX
    ):
        if candidate.exists():
            return str(candidate)
    return sys.executable


async def _pipe_stream(name: str, stream: asyncio.StreamReader) -> None:
    while True:
        line = await stream.readline()
        if not line:
            break
        print(f"[{name}] {line.decode(errors='replace').rstrip()}")


async def _run(
    name: str,
    args: list[str],
    cwd: Path,
    pipe_tasks: list[asyncio.Task],
) -> asyncio.subprocess.Process:
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=creationflags,
    )
    pipe_tasks.append(asyncio.create_task(_pipe_stream(name, proc.stdout)))
    pipe_tasks.append(asyncio.create_task(_pipe_stream(name, proc.stderr)))
    print(f"[{name}] started (pid {proc.pid})")
    return proc


def _stop_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill a child and (on Windows) its whole process tree."""
    if proc.returncode is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
        )
    else:
        proc.terminate()


async def main() -> None:
    pipe_tasks: list[asyncio.Task] = []
    backend = await _run(
        "backend",
        [
            venv_python(),
            "-m", "uvicorn",
            "assistant.server:app",
            "--host", "0.0.0.0",
            "--port", "8000",
        ],
        ROOT,
        pipe_tasks,
    )
    frontend = await _run(
        "frontend",
        [NPM, "run", "dev"],
        FRONTEND_DIR,
        pipe_tasks,
    )

    print("\nBackend:  http://localhost:8000  (API / SSE)")
    print("Frontend: http://localhost:3000")
    print("Press Ctrl+C to stop both.\n")

    waiters = [asyncio.create_task(backend.wait()), asyncio.create_task(frontend.wait())]
    try:
        # Stop both as soon as either one exits (e.g. an error at startup).
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    except KeyboardInterrupt:
        pass
    finally:
        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)

        _stop_process_tree(backend)
        _stop_process_tree(frontend)
        await asyncio.gather(backend.wait(), frontend.wait(), return_exceptions=True)

        # Let the pipe readers drain to EOF (the child is dead, so the pipe
        # write ends are closed) and finish while the event loop is still
        # open. Pending reader tasks with open transports are what cause
        # "Exception ignored ... Event loop is closed" warnings when the
        # loop shuts down on Windows.
        if pipe_tasks:
            await asyncio.wait(pipe_tasks, timeout=5)
            for task in pipe_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pipe_tasks, return_exceptions=True)

        print("\nBoth processes stopped.")


if __name__ == "__main__":
    asyncio.run(main())
