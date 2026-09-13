"""Provider-protocol regression tests for transient strategy guidance."""

from __future__ import annotations

from pathlib import Path

from core.config import Config
from core.llm_client import LLMClient


def _client(tmp_path: Path, provider: str = "deepseek") -> LLMClient:
    config = Config(project_root=tmp_path)
    config._data = {"llm": {"provider": provider, "reasoning": True, "temperature": 0.0}}
    return LLMClient(config)


def test_deepseek_round_trips_reasoning_only_on_genuine_assistant_tool_turn(tmp_path: Path) -> None:
    converted = _client(tmp_path)._convert_messages([
        {"role": "user", "content": "Calculate."},
        {
            "role": "assistant", "content": None,
            "reasoning_content": "opaque-provider-value",
            "tool_calls": [{"id": "call", "type": "function", "function": {"name": "calculator", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call", "content": "{}"},
    ])

    assert converted[1]["reasoning_content"] == "opaque-provider-value"


def test_reasoning_metadata_is_omitted_when_absent_or_provider_is_not_deepseek(tmp_path: Path) -> None:
    message = {
        "role": "assistant", "content": None,
        "reasoning_content": "opaque-provider-value",
        "tool_calls": [{"id": "call", "type": "function", "function": {"name": "calculator", "arguments": "{}"}}],
    }
    no_reasoning = {**message}; no_reasoning.pop("reasoning_content")

    assert "reasoning_content" not in _client(tmp_path)._convert_messages([no_reasoning])[0]
    assert "reasoning_content" not in _client(tmp_path, "other-provider")._convert_messages([message])[0]


def test_deepseek_thinking_records_temperature_as_requested_but_ineffective(tmp_path: Path) -> None:
    assert _client(tmp_path).effective_parameters() == {
        "temperature_requested": 0.0,
        "temperature_effective": None,
        "temperature_note": "ignored_by_provider_thinking_mode",
    }
