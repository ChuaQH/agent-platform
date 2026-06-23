"""CLI entry point for the Assistant agent.

Usage:
    assistant              # Start the Chainlit chat UI (port 8000)
    assistant --port 8080  # Custom port
    assistant -w            # Hot reload mode
    assistant -dev          # Development mode (verbose logging, instrumentation)
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    app_path = Path(__file__).parent / "app.py"

    filtered_args: list[str] = []
    dev_mode = False
    for arg in sys.argv[1:]:
        if arg in ("-dev", "--dev"):
            dev_mode = True
        else:
            filtered_args.append(arg)

    env = os.environ.copy()
    if dev_mode:
        env["ACA_DEV"] = "true"

    args = [
        sys.executable,
        "-m",
        "chainlit",
        "run",
        str(app_path),
        *filtered_args,
    ]
    subprocess.run(args, env=env)
