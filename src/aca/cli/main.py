"""CLI entry point for the Autonomous Coding Agent.

Usage:
    aca "Fix the password reset bug"
    aca --project-path /path/to/project "Add rate limiting"
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from core.config import load_config, Config
from core.llm_client import LLMClient
from core.conversation import Conversation

app = typer.Typer(
    name="aca",
    help="Autonomous Coding Agent — keeps project context across sessions.",
)
console = Console()


def setup_logging(config: Config) -> None:
    """Configure structured logging."""
    log_dir = config.project_root / "agent-data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "aca.log"),
            logging.StreamHandler(sys.stderr),
        ],
    )


def build_static_prefix(config: Config) -> list[str]:
    """Build the static (cacheable) prefix of the system prompt.

    This includes the system prompt, tool definitions, Layer 1 (project identity),
    and Layer 3 (decision index). This prefix NEVER changes during a session.

    Args:
        config: Agent configuration.

    Returns:
        List of system prompt strings forming the static prefix.
    """
    prompts: list[str] = []

    # --- Core system prompt ---
    prompts.append(
        "You are an autonomous coding agent (ACA). "
        "You help users build and maintain software projects by reading, "
        "writing, and editing code. You have access to tools for file operations, "
        "codebase search, and memory management.\n\n"
        "Your goal is to be helpful, precise, and safe. "
        "Read-only actions are always allowed. File edits are allowed. "
        "Destructive actions require user confirmation."
    )

    # --- Layer 1: Project identity ---
    identity = _load_layer1(config)
    if identity:
        prompts.append(identity)

    # --- Layer 3: Decision index ---
    decisions = _load_decision_index(config)
    if decisions:
        prompts.append(decisions)

    return prompts


def _load_layer1(config: Config) -> str:
    """Load Layer 1 (project identity) from agent-context/ directory."""
    context_dir = config.agent_context_dir
    if not context_dir.exists():
        return ""

    parts = []
    for filename in ["PROJECT.md", "CONVENTIONS.md", "CONSTRAINTS.md"]:
        filepath = context_dir / filename
        if filepath.exists():
            content = filepath.read_text(encoding="utf-8").strip()
            if content:
                parts.append(f"## {filename}\n\n{content}")

    if parts:
        return "## Project Context (Layer 1)\n\n" + "\n\n".join(parts)

    return ""


def _load_decision_index(config: Config) -> str:
    """Load Layer 3 (decision index) from agent-context/decisions/."""
    index_path = config.agent_context_dir / "decisions" / "INDEX.md"
    if index_path.exists():
        content = index_path.read_text(encoding="utf-8").strip()
        if content:
            return f"## Past Decisions (Layer 3)\n\n{content}"
    return ""


@app.command()
def chat(
    prompt: Annotated[
        str, typer.Argument(help="The task or question for the agent.")
    ],
    project_path: Annotated[
        Optional[Path],
        typer.Option(
            "--project-path", "-p",
            help="Path to the project root. Defaults to current directory.",
        ),
    ] = None,
    no_stream: Annotated[
        bool,
        typer.Option(
            "--no-stream",
            help="Disable streaming output.",
        ),
    ] = False,
) -> None:
    """Send a prompt to the autonomous coding agent."""
    project_root = project_path.resolve() if project_path else Path.cwd()

    # Load config
    config = load_config(project_root)
    setup_logging(config)
    logger = logging.getLogger(__name__)

    # Validate API key
    if not config.llm_api_key:
        console.print(
            "[red]Error:[/red] DEEPSEEK_API_KEY environment variable is not set.\n"
            "Set it with: [bold]export DEEPSEEK_API_KEY=sk-...[/bold]"
        )
        raise typer.Exit(code=1)

    console.print(
        Panel.fit(
            f"[bold]ACA[/bold] — Autonomous Coding Agent\n"
            f"Project: [cyan]{project_root}[/cyan]\n"
            f"Model: [cyan]{config.llm_model}[/cyan]",
            border_style="blue",
        )
    )

    # Run the chat
    asyncio.run(_run_chat(config, prompt))


async def _run_chat(config: Config, user_prompt: str) -> None:
    """Run a single-turn chat with the agent."""
    logger = logging.getLogger(__name__)

    # Initialize client and conversation
    client = LLMClient(config)
    conv = Conversation()

    # Build and set static prefix
    static_prefix = build_static_prefix(config)
    conv.set_static_prefix(static_prefix)

    logger.info(
        "Session started. Static prefix: %d messages, ~%d tokens",
        conv.static_prefix_length,
        conv.estimate_tokens(),
    )

    # Add user message
    conv.add_user_message(user_prompt)

    # Send to DeepSeek
    try:
        response = await client.chat(
            messages=conv.get_messages_for_api(),
        )
    except Exception as e:
        console.print(f"[red]Error calling DeepSeek API:[/red] {e}")
        raise typer.Exit(code=1)

    # Display response
    if response.content:
        console.print()
        console.print(Markdown(response.content))
        console.print()

    # Show metrics
    metrics = client.cache_metrics_summary
    table = Table(title="Session Metrics", show_header=False)
    table.add_column(style="dim")
    table.add_column()
    table.add_row("Tokens (prompt)", str(metrics["total_prompt_tokens"]))
    table.add_row("Tokens (cached)", str(metrics["cached_tokens"]))
    table.add_row("Cache hit rate", metrics["cache_hit_rate"])
    console.print(table)


@app.command()
def version() -> None:
    """Show the ACA version."""
    from aca import __version__
    console.print(f"ACA version [bold]{__version__}[/bold]")


if __name__ == "__main__":
    app()
