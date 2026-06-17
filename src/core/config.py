"""Configuration loader for ACA ecosystem agents.

Configuration priority (highest to lowest):
1. Environment variables (ACA_*)
2. .env file in project root
3. .agent-config.yml in project root
4. Built-in defaults
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

# Default configuration values (lowest priority)
DEFAULTS: dict[str, Any] = {
    "llm": {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "api_key": "",
        "base_url": "https://api.deepseek.com",
        "max_tokens": 8192,
        "temperature": 0.0,
    },
    "agent": {
        "max_tool_calls_per_turn": 10,
        "max_subagent_turns": 20,
        "tool_timeout": 30,
        "compress_threshold": 0.8,
    },
    "cache": {
        "disk_cache_ttl": 3600,
        "disk_cache_max_size_mb": 500,
    },
    "memory": {
        "recent_outcomes_limit": 15,
        "recent_sessions_limit": 5,
        "weekly_digest_age_days": 7,
        "monthly_digest_age_days": 30,
    },
    "logging": {
        "level": "INFO",
        "file": "agent-data/logs/aca.log",
        "format": "json",
    },
    "tools": {
        "brave_api_key": "",
    },
}

ENV_MAP: dict[str, tuple[tuple[str, ...], Any]] = {
    "DEEPSEEK_API_KEY":         (("llm", "api_key"), str),
    "ACA_LLM_PROVIDER":         (("llm", "provider"), str),
    "ACA_LLM_MODEL":            (("llm", "model"), str),
    "ACA_LLM_BASE_URL":         (("llm", "base_url"), str),
    "ACA_LLM_MAX_TOKENS":       (("llm", "max_tokens"), int),
    "ACA_LLM_TEMPERATURE":      (("llm", "temperature"), float),
    "ACA_MAX_TOOL_CALLS":       (("agent", "max_tool_calls_per_turn"), int),
    "ACA_MAX_SUBAGENT_TURNS":   (("agent", "max_subagent_turns"), int),
    "ACA_TOOL_TIMEOUT":         (("agent", "tool_timeout"), int),
    "ACA_COMPRESS_THRESHOLD":   (("agent", "compress_threshold"), float),
    "ACA_DISK_CACHE_TTL":       (("cache", "disk_cache_ttl"), int),
    "ACA_DISK_CACHE_MAX_MB":    (("cache", "disk_cache_max_size_mb"), int),
    "ACA_RECENT_OUTCOMES":      (("memory", "recent_outcomes_limit"), int),
    "ACA_RECENT_SESSIONS":      (("memory", "recent_sessions_limit"), int),
    "ACA_WEEKLY_DIGEST_AGE":    (("memory", "weekly_digest_age_days"), int),
    "ACA_MONTHLY_DIGEST_AGE":   (("memory", "monthly_digest_age_days"), int),
    "ACA_LOG_LEVEL":            (("logging", "level"), str),
    "ACA_LOG_FILE":             (("logging", "file"), str),
    "ACA_LOG_FORMAT":           (("logging", "format"), str),
    "BRAVE_API_KEY":            (("tools", "brave_api_key"), str),
}


def _load_dotenv(project_root: Path) -> None:
    """Load a .env file from the project root into os.environ.

    Uses python-dotenv if available, otherwise falls back to a
    simple line-by-line parser for KEY=VALUE format.
    """
    dotenv_path = project_root / ".env"
    if not dotenv_path.exists():
        return

    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path)
    except ImportError:
        with open(dotenv_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:]
                if "=" in line:
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip("\"'")
                    if key and key not in os.environ:
                        os.environ[key] = value


class Config:
    """Agent configuration, loaded from file with env overrides.

    Priority: env vars > .env file > .agent-config.yml > defaults.
    Shared by all agents (aca, assistant).
    """

    def __init__(self, project_root: Path | str | None = None):
        self.project_root = Path(project_root) if project_root else Path.cwd()
        self._data: dict[str, Any] = {}

        self._load_dotenv_file()
        self._load_config_file()
        self._apply_env_overrides()

    def _load_dotenv_file(self) -> None:
        _load_dotenv(self.project_root)

    def _load_config_file(self) -> None:
        config_path = self.project_root / ".agent-config.yml"
        if config_path.exists():
            with open(config_path, encoding="utf-8") as f:
                self._data = yaml.safe_load(f) or {}
        else:
            self._data = {}

    def _apply_env_overrides(self) -> None:
        for env_var, (keys, coerce) in ENV_MAP.items():
            raw = os.getenv(env_var)
            if raw is None:
                continue
            try:
                value = coerce(raw)
            except (ValueError, TypeError):
                continue
            target = self._data
            for key in keys[:-1]:
                target = target.setdefault(key, {})
            target[keys[-1]] = value

    def _get(self, *keys: str, default: Any = None) -> Any:
        value: Any = self._data
        for key in keys:
            if isinstance(value, dict):
                value = value.get(key)
            else:
                value = None
                break
        if value is not None:
            return value
        value = DEFAULTS
        for key in keys:
            if isinstance(value, dict):
                value = value.get(key)
            else:
                return default
        return value if value is not None else default

    # ── LLM ──
    @property
    def llm_provider(self) -> str:
        return self._get("llm", "provider", default="deepseek")

    @property
    def llm_model(self) -> str:
        return self._get("llm", "model", default="deepseek-chat")

    @property
    def llm_api_key(self) -> str:
        return self._get("llm", "api_key", default="")

    @property
    def llm_base_url(self) -> str:
        return self._get("llm", "base_url", default="https://api.deepseek.com")

    @property
    def llm_max_tokens(self) -> int:
        return self._get("llm", "max_tokens", default=8192)

    @property
    def llm_temperature(self) -> float:
        return self._get("llm", "temperature", default=0.0)

    # ── Agent ──
    @property
    def max_tool_calls_per_turn(self) -> int:
        return self._get("agent", "max_tool_calls_per_turn", default=10)

    @property
    def max_subagent_turns(self) -> int:
        return self._get("agent", "max_subagent_turns", default=20)

    @property
    def tool_timeout(self) -> int:
        return self._get("agent", "tool_timeout", default=30)

    @property
    def compress_threshold(self) -> float:
        return self._get("agent", "compress_threshold", default=0.8)

    # ── Cache ──
    @property
    def disk_cache_ttl(self) -> int:
        return self._get("cache", "disk_cache_ttl", default=3600)

    @property
    def disk_cache_max_size_mb(self) -> int:
        return self._get("cache", "disk_cache_max_size_mb", default=500)

    # ── Memory ──
    @property
    def recent_outcomes_limit(self) -> int:
        return self._get("memory", "recent_outcomes_limit", default=15)

    @property
    def recent_sessions_limit(self) -> int:
        return self._get("memory", "recent_sessions_limit", default=5)

    @property
    def weekly_digest_age_days(self) -> int:
        return self._get("memory", "weekly_digest_age_days", default=7)

    @property
    def monthly_digest_age_days(self) -> int:
        return self._get("memory", "monthly_digest_age_days", default=30)

    # ── Logging ──
    @property
    def log_level(self) -> str:
        return self._get("logging", "level", default="INFO")

    @property
    def log_file(self) -> str:
        return self._get("logging", "file", default="agent-data/logs/aca.log")

    @property
    def log_format(self) -> str:
        return self._get("logging", "format", default="json")

    # ── Tools (API keys) ──
    @property
    def brave_api_key(self) -> str:
        return self._get("tools", "brave_api_key", default="")

    # ── Derived paths ──
    @property
    def agent_context_dir(self) -> Path:
        return self.project_root / "agent-context"

    @property
    def agent_data_dir(self) -> Path:
        return self.project_root / "agent-data"


def load_config(project_root: Path | str | None = None) -> Config:
    """Load configuration for the given project root."""
    return Config(project_root)
