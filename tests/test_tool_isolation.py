"""Regression tests for request-level tool isolation.

Guards the invariant that one user's tool toggles can never modify the
tools available to another concurrent request, and that execution rejects
tools outside the request's permitted registry (defense in depth beyond
just hiding schemas from the LLM).
"""

import asyncio

from core.tool_framework import Tool, ToolCapability, ToolRegistry
from core.tools.calculator import CalculatorTool
from core.tools.datetime_tools import CurrentDatetimeTool
from core.tools.user_location import UserLocationTool
from assistant.tool_store import ALWAYS_ENABLED


class RecordingTool(Tool):
    """Stub tool that records whether it was executed."""

    name = "recording_probe"
    capabilities = frozenset({ToolCapability.COMPUTE})
    description = "Probe tool that records executions."
    parameters = {}
    when_to_use = "Only when explicitly probed."

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, **params: object) -> str:
        self.calls += 1
        return "probe executed"


def build_master_registry() -> tuple[ToolRegistry, RecordingTool]:
    """Full tool set as built per request by server._build_tool_registry."""
    probe = RecordingTool()
    master = ToolRegistry()
    master.register_many([CalculatorTool(), CurrentDatetimeTool(), UserLocationTool()])
    master.register(probe)
    return master, probe


def test_filtering_one_request_does_not_affect_another_or_master() -> None:
    master, _ = build_master_registry()

    # Simulate two concurrent requests, each with the user's enabled set as
    # returned by tool_store.get_enabled_tool_names (always-enabled tools are
    # unioned in exactly as server.py does).
    user_a_enabled = {"calculator"} | ALWAYS_ENABLED
    user_b_enabled = {"get_current_datetime", "get_user_location"} | ALWAYS_ENABLED

    request_a = master.filtered(user_a_enabled)
    request_b = master.filtered(user_b_enabled)

    assert request_a.tool_names == ["calculator"]
    assert request_b.tool_names == ["get_current_datetime", "get_user_location"]

    # The master registry is untouched by either derivation.
    assert master.tool_names == [
        "calculator", "get_current_datetime", "get_user_location", "recording_probe",
    ]
    assert master.get("recording_probe") is not None

    # Schemas and tool-use policy are compiled from the request-local set only.
    assert [s["function"]["name"] for s in request_a.to_openai_schemas()] == ["calculator"]
    policy_a = request_a.compile_tool_use_policy()
    assert "calculator" in policy_a
    assert "get_current_datetime" not in policy_a


def test_execution_rejects_tools_outside_request_registry() -> None:
    master, probe = build_master_registry()

    request = master.filtered({"calculator"} | ALWAYS_ENABLED)

    # The probe is registered in the master but not permitted for this request.
    result = asyncio.run(request.execute("recording_probe"))
    assert result.startswith("Error: tool 'recording_probe' not found")
    assert probe.calls == 0

    # The same tool remains executable from a registry that includes it.
    full = master.filtered(set(master.tool_names))
    assert asyncio.run(full.execute("recording_probe")) == "probe executed"
    assert probe.calls == 1


def test_always_enabled_tools_survive_filtering() -> None:
    """Infrastructure tools stay available even if toggle data were inconsistent."""
    probe = RecordingTool()
    probe.name = "assistant_recall"
    master = ToolRegistry()
    master.register_many([CalculatorTool(), probe])

    # Even an empty (or corrupt) enabled set cannot remove always-enabled tools.
    request = master.filtered(set() | ALWAYS_ENABLED)
    assert "assistant_recall" in request.tool_names
    assert "calculator" not in request.tool_names
