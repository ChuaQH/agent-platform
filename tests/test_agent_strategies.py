"""Focused contracts for the optional evaluator-only agent strategies."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Any

import pytest

from assistant.agent_runner import AgentRunner, RunnerContinuation
from assistant.execution_gateway import ExecutionContext, RegistryExecutionGateway, RunPolicy
from assistant.strategy import (
    MAX_PLAN_STEPS,
    PlanBeforeReactStrategy,
    PlanReactStrategy,
    PlanValidationError,
    ReactStrategy,
    parse_plan,
)
from core.conversation import Conversation
from core.llm_client import LLMResponse
from core.tool_framework import Tool, ToolCapability, ToolRegistry
from core.tools.calculator import CalculatorTool
from evaluations.agent_controls.models import EvaluationConfiguration, EvaluationMode
from evaluations.agent_controls.metrics import aggregate
from evaluations.agent_controls.runner import evaluate_scenario, run_scenarios
from evaluations.agent_controls.scenarios import load_scenarios
from evaluations.agent_controls.strategy_scenarios import load_strategy_scenarios


class SequenceModel:
    """Local deterministic model that records whether planner tools were offered."""

    def __init__(self, *responses: LLMResponse) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[list[dict[str, Any]], object]] = []

    async def chat(self, messages, tools=None, **kwargs) -> LLMResponse:
        self.calls.append((list(messages), tools))
        return self.responses.pop(0)


class RecordingWriteTool(Tool):
    name = "write"
    description = "Record a write for a strategy test."
    capabilities = frozenset({ToolCapability.WORKSPACE_WRITE})
    parameters = {
        "type": "object", "properties": {"value": {"type": "string"}},
        "required": ["value"], "additionalProperties": False,
    }

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, value: str) -> str:
        self.calls += 1
        return value


def _plan(*, suggested_tool: str | None = None) -> str:
    step: dict[str, object] = {
        "id": "understand", "description": "Understand the requested task.",
        "depends_on": [], "success_condition": "Task requirements are known.",
    }
    if suggested_tool is not None:
        step["suggested_tool"] = suggested_tool
    return json.dumps({"version": 1, "goal": "Complete the task.", "steps": [step]})


def _conversation() -> Conversation:
    conversation = Conversation()
    conversation.set_static_prefix(["Follow governed execution rules."])
    conversation.add_user_message("Please complete the task.")
    return conversation


def _gateway(registry: ToolRegistry, policy: RunPolicy) -> RegistryExecutionGateway:
    return RegistryExecutionGateway(registry, ExecutionContext(1, "thread", "request"), policy)


async def _events(strategy, conversation: Conversation):
    return [event async for event in strategy.run(conversation)]


def _tool_call(name: str, arguments: dict[str, object], call_id: str = "call") -> dict[str, object]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def test_react_strategy_preserves_runner_events_and_execution() -> None:
    policy = RunPolicy(frozenset({ToolCapability.COMPUTE}), None, 2, None, 10.0)
    first_registry, second_registry = ToolRegistry(), ToolRegistry()
    first_registry.register(CalculatorTool())
    second_registry.register(CalculatorTool())
    responses = (
        LLMResponse(tool_calls=[_tool_call("calculator", {"expression": "6*7"})]),
        LLMResponse(content="42"),
    )
    direct = AgentRunner(SequenceModel(*responses), first_registry, 3, executor=_gateway(first_registry, policy))
    wrapped = ReactStrategy(AgentRunner(SequenceModel(*responses), second_registry, 3, executor=_gateway(second_registry, policy)))

    direct_events = asyncio.run(_events(direct, _conversation()))
    wrapped_events = asyncio.run(_events(wrapped, _conversation()))

    assert [event.type for event in wrapped_events] == [event.type for event in direct_events]
    assert wrapped.result and direct.result
    assert wrapped.result.final_text == direct.result.final_text == "42"
    assert [event.data.get("result") for event in wrapped_events if event.type == "tool_result"] == ["6*7 = 42"]


def test_planner_has_no_executable_tool_access_and_valid_plan_is_emitted() -> None:
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    policy = RunPolicy(frozenset({ToolCapability.COMPUTE}), None, 1, None, 10.0)
    model = SequenceModel(
        LLMResponse(content=_plan(), usage={"prompt_tokens": 2, "completion_tokens": 3}),
        LLMResponse(content="Done", usage={"prompt_tokens": 5, "completion_tokens": 7}),
    )
    strategy = PlanBeforeReactStrategy(llm=model, tools=registry, executor=_gateway(registry, policy), max_turns=2, policy=policy)

    events = asyncio.run(_events(strategy, _conversation()))

    assert model.calls[0][1] is None
    assert {entry["name"] for entry in json.loads(model.calls[0][0][1]["content"])["available_tools"]} == {"calculator"}
    assert [event.type for event in events][:1] == ["plan_created"]
    assert strategy.planning.plan_valid is True and strategy.planning.step_count == 1
    assert strategy.result and strategy.result.final_text == "Done"


@pytest.mark.parametrize("payload", [
    {"version": 1, "goal": "g", "steps": []},
    {"version": 1, "goal": "g", "steps": [{"id": "same", "description": "d", "depends_on": [], "success_condition": "s"}] * 2},
    {"version": 1, "goal": "g", "steps": [{"id": f"s{index}", "description": "d", "depends_on": [], "success_condition": "s"} for index in range(MAX_PLAN_STEPS + 1)]},
])
def test_plan_rejects_missing_duplicate_or_excessive_steps(payload: dict[str, object]) -> None:
    with pytest.raises(PlanValidationError):
        parse_plan(json.dumps(payload))


def test_plan_rejects_unknown_dependencies_and_cycles() -> None:
    unknown = {"version": 1, "goal": "g", "steps": [{"id": "one", "description": "d", "depends_on": ["missing"], "success_condition": "s"}]}
    cycle = {"version": 1, "goal": "g", "steps": [
        {"id": "one", "description": "d", "depends_on": ["two"], "success_condition": "s"},
        {"id": "two", "description": "d", "depends_on": ["one"], "success_condition": "s"},
    ]}
    with pytest.raises(PlanValidationError):
        parse_plan(json.dumps(unknown))
    with pytest.raises(PlanValidationError):
        parse_plan(json.dumps(cycle))


def test_invalid_plan_has_one_repair_then_recorded_react_fallback() -> None:
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    policy = RunPolicy(frozenset({ToolCapability.COMPUTE}), None, 1, None, 10.0)
    model = SequenceModel(LLMResponse(content="not json"), LLMResponse(content="still not json"), LLMResponse(content="Fallback answer"))
    strategy = PlanBeforeReactStrategy(llm=model, tools=registry, executor=_gateway(registry, policy), max_turns=2, policy=policy)

    events = asyncio.run(_events(strategy, _conversation()))

    assert len(model.calls) == 3
    assert [event.type for event in events][:2] == ["plan_invalid", "plan_fallback"]
    assert strategy.planning.validation_failures == 2
    assert strategy.planning.repair_attempts == strategy.planning.fallbacks == 1
    assert strategy.result and strategy.result.final_text == "Fallback answer"


def test_plan_cannot_widen_policy_or_bypass_gateway() -> None:
    write = RecordingWriteTool()
    registry = ToolRegistry()
    registry.register(write)
    policy = RunPolicy(frozenset({ToolCapability.COMPUTE}), None, 1, None, 10.0)
    model = SequenceModel(
        LLMResponse(content=_plan(suggested_tool="write")),
        LLMResponse(tool_calls=[_tool_call("write", {"value": "no"})]),
        LLMResponse(content="Write was unavailable."),
    )
    strategy = PlanBeforeReactStrategy(llm=model, tools=registry, executor=_gateway(registry, policy), max_turns=2, policy=policy)

    events = asyncio.run(_events(strategy, _conversation()))

    result = next(event for event in events if event.type == "tool_result")
    assert result.data["status"] == "policy_rejection"
    assert write.calls == 0


def test_planner_usage_is_separate_summed_and_does_not_consume_tool_budget() -> None:
    scenario = next(item for item in load_scenarios() if item.scenario_id == "benign_calculator")
    model = SequenceModel(
        LLMResponse(content=_plan(), usage={"prompt_tokens": 2, "completion_tokens": 3, "cached_tokens": 1}),
        LLMResponse(tool_calls=[_tool_call("calculator", {"expression": "2+3"})], usage={"prompt_tokens": 5, "completion_tokens": 7, "cached_tokens": 2}),
        LLMResponse(content="Done", usage={"prompt_tokens": 11, "completion_tokens": 13, "cached_tokens": 3}),
    )

    outcome = asyncio.run(evaluate_scenario(scenario, EvaluationConfiguration.ENFORCED_POLICY, model=model, strategy_name="plan_before_react"))

    assert outcome.tool_attempts == 1
    assert outcome.planner_usage["model_calls"] == 1
    assert outcome.executor_usage["turns"] == 2
    assert outcome.usage == {"prompt_tokens": 18, "completion_tokens": 23, "cached_tokens": 6}
    assert outcome.plan_valid is True and outcome.plan_steps == 1 and outcome.plan_fallback is False


def test_plan_react_creates_initial_plan_and_success_does_not_replan() -> None:
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    policy = RunPolicy(frozenset({ToolCapability.COMPUTE}), None, 2, None, 10.0)
    model = SequenceModel(
        LLMResponse(content=_plan()),
        LLMResponse(tool_calls=[_tool_call("calculator", {"expression": "1+1"})]),
        LLMResponse(content="Done"),
    )
    strategy = PlanReactStrategy(llm=model, tools=registry, executor=_gateway(registry, policy), max_turns=3, policy=policy)

    events = asyncio.run(_events(strategy, _conversation()))

    assert [event.type for event in events].count("plan_created") == 1
    assert not [event for event in events if event.type == "plan_updated"]
    assert strategy.controller and strategy.controller.state.revision == 1
    assert model.calls[0][1] is None


def test_plan_react_replans_once_and_replaces_transient_guidance() -> None:
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    policy = RunPolicy(frozenset({ToolCapability.COMPUTE}), None, 3, None, 10.0)
    replacement = json.dumps({"version": 1, "goal": "Replacement goal.", "steps": [{
        "id": "recover", "description": "Use an available alternative.", "depends_on": [],
        "success_condition": "An alternative is selected.",
    }]})
    model = SequenceModel(
        LLMResponse(content=_plan()),
        LLMResponse(tool_calls=[_tool_call("missing", {})]),
        LLMResponse(content=replacement),
        LLMResponse(content="Recovered."),
    )
    strategy = PlanReactStrategy(llm=model, tools=registry, executor=_gateway(registry, policy), max_turns=3, policy=policy)

    events = asyncio.run(_events(strategy, _conversation()))

    update = next(event for event in events if event.type == "plan_updated")
    assert update.data["revision"] == 2 and update.data["trigger_reason"] == "unavailable_tool"
    assert strategy.controller and strategy.controller.state.replan_count == 1
    # The final executor call contains the replacement only; plans were not
    # appended to conversation and older revisions cannot accumulate.
    final_messages = model.calls[-1][0]
    guidance = [message["content"] for message in final_messages if "untrusted_plan_guidance" in message.get("content", "")]
    assert len(guidance) == 1 and "Replacement goal." in guidance[0] and "Complete the task." not in guidance[0]
    assert all(message["role"] == "user" for message in final_messages if "untrusted_plan_guidance" in message.get("content", ""))


def test_plan_guidance_is_transient_user_context_not_assistant_history() -> None:
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    policy = RunPolicy(frozenset({ToolCapability.COMPUTE}), None, 1, None, 10.0)
    planner_reasoning = "planner-private-reasoning"
    model = SequenceModel(
        LLMResponse(content=_plan(), reasoning_content=planner_reasoning),
        LLMResponse(content="Done"),
    )
    conversation = _conversation()
    strategy = PlanReactStrategy(llm=model, tools=registry, executor=_gateway(registry, policy), max_turns=2, policy=policy)

    asyncio.run(_events(strategy, conversation))

    executor_messages = model.calls[1][0]
    guidance = [message for message in executor_messages if "untrusted_plan_guidance" in message.get("content", "")]
    assert len(guidance) == 1 and guidance[0]["role"] == "user"
    assert "reasoning_content" not in guidance[0]
    assert not [message for message in conversation.messages if "untrusted_plan_guidance" in message.get("content", "")]
    assert not [message for message in conversation.messages if message.get("reasoning_content") == planner_reasoning]


def test_plan_react_invalid_replan_retains_last_plan_and_budget_suppresses_replan() -> None:
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    policy = RunPolicy(frozenset({ToolCapability.COMPUTE}), None, 2, None, 10.0)
    model = SequenceModel(
        LLMResponse(content=_plan()), LLMResponse(tool_calls=[_tool_call("missing", {})]),
        LLMResponse(content="bad"), LLMResponse(content="still bad"), LLMResponse(content="Done"),
    )
    strategy = PlanReactStrategy(llm=model, tools=registry, executor=_gateway(registry, policy), max_turns=3, policy=policy)
    events = asyncio.run(_events(strategy, _conversation()))
    assert any(event.type == "plan_invalid" and event.data.get("reason") == "invalid_replan" for event in events)
    assert strategy.controller and strategy.controller.state.revision == 1

    limited = SequenceModel(LLMResponse(content=_plan()), LLMResponse(tool_calls=[_tool_call("missing", {})]), LLMResponse(content="Done"))
    budget = RunPolicy(frozenset({ToolCapability.COMPUTE}), None, 1, None, 10.0)
    limited_strategy = PlanReactStrategy(llm=limited, tools=registry, executor=_gateway(registry, budget), max_turns=3, policy=budget)
    asyncio.run(_events(limited_strategy, _conversation()))
    assert limited_strategy.controller and limited_strategy.controller.state.replan_count == 0


def test_plan_react_state_round_trips_in_v2_and_rejects_corruption() -> None:
    state = {
        "strategy_schema_version": 1, "strategy_name": "plan_react", "original_goal": "Goal",
        "current_plan": json.loads(_plan()), "plan_revision": 1, "replan_count": 0, "max_replans": 2,
        "trigger_history": [], "planning_usage": {"model_calls": 1, "prompt_tokens": 0,
            "completion_tokens": 0, "cached_tokens": 0, "latency_ms": 0, "validation_failures": 0,
            "repair_attempts": 0, "fallbacks": 0, "step_count": 1, "plan_valid": True,
            "replans_requested": 0, "valid_replans": 0, "invalid_replans": 0},
        "trigger_counts": {}, "observation_summary": "", "fallback": False,
    }
    persisted = {
        "version": 2, "conversation_messages": [], "static_prefix_length": 0, "static_prefix_hash": "",
        "turn": 1, "pending_action_id": "action", "pending_tool_call_id": "call",
        "remaining_tool_calls": [], "budget": {}, "strategy_state": state,
    }
    restored = RunnerContinuation.from_persisted(persisted)
    assert restored.version == 2 and restored.strategy_state == state
    persisted["strategy_state"] = {**state, "strategy_name": "unsafe.import.path"}
    with pytest.raises(ValueError):
        RunnerContinuation.from_persisted(persisted)


def test_approval_continuation_keeps_genuine_tool_turn_reasoning_private() -> None:
    write = RecordingWriteTool()
    registry = ToolRegistry(); registry.register(write)
    write_capability = frozenset({ToolCapability.WORKSPACE_WRITE})
    policy = RunPolicy(write_capability, None, 1, None, 10.0, write_capability)
    model = SequenceModel(
        LLMResponse(content=_plan()),
        LLMResponse(
            reasoning_content="genuine-tool-turn-reasoning",
            tool_calls=[_tool_call("write", {"value": "approved"})],
        ),
    )
    strategy = PlanReactStrategy(llm=model, tools=registry, executor=_gateway(registry, policy), max_turns=3, policy=policy)

    asyncio.run(_events(strategy, _conversation()))

    assert strategy.continuation is not None
    persisted = asdict(strategy.continuation)
    # The no-recorder unit gateway has no durable action id; production
    # gateways supply one before a continuation can be persisted.
    persisted["pending_action_id"] = "durable-action"
    restored = RunnerContinuation.from_persisted(persisted)
    tool_turn = next(message for message in restored.conversation_messages if message.get("tool_calls"))
    assert tool_turn["reasoning_content"] == "genuine-tool-turn-reasoning"
    assert not [message for message in restored.conversation_messages if "untrusted_plan_guidance" in message.get("content", "")]


def test_plan_react_approval_resume_restores_plan_and_genuine_reasoning_metadata() -> None:
    scenario = next(item for item in load_scenarios() if item.scenario_id == "benign_workspace_write_approval")
    model = SequenceModel(
        LLMResponse(content=_plan()),
        LLMResponse(
            reasoning_content="resume-private-reasoning",
            tool_calls=[_tool_call("write_file", {"path": "allowed.txt", "content": "approved"})],
        ),
        LLMResponse(content="Write completed."),
    )

    outcome = asyncio.run(evaluate_scenario(
        scenario, EvaluationConfiguration.ENFORCED_POLICY_WITH_APPROVAL,
        model=model, strategy_name="plan_react",
    ))

    resumed_messages = model.calls[-1][0]
    tool_turn = next(message for message in resumed_messages if message.get("tool_calls"))
    guidance = next(message for message in resumed_messages if "untrusted_plan_guidance" in message.get("content", ""))
    assert outcome.task_completed and outcome.passed
    assert tool_turn["reasoning_content"] == "resume-private-reasoning"
    assert guidance["role"] == "user"


def test_plan_guidance_directs_sensitive_actions_to_gateway_approval_flow() -> None:
    guidance = parse_plan(_plan()).to_guidance()
    assert "submit its exact tool call" in guidance
    assert "allow the gateway to pause" in guidance
    assert "ordinary chat response" in guidance


@pytest.mark.asyncio
async def test_strategy_scenarios_share_tasks_and_controls_for_both_strategies() -> None:
    scenarios = load_strategy_scenarios()
    assert len(scenarios) == 8
    assert [item.scenario_id for item in scenarios] == [item.scenario_id for item in load_scenarios(include_strategy=True)[-8:]]
    scenario = scenarios[0]
    react = await evaluate_scenario(scenario, EvaluationConfiguration.ENFORCED_POLICY, strategy_name="react")
    plan = await evaluate_scenario(scenario, EvaluationConfiguration.ENFORCED_POLICY, strategy_name="plan_before_react")
    assert (react.scenario_id, react.configuration, react.tool_attempts) == (plan.scenario_id, plan.configuration, plan.tool_attempts)
    assert react.passed and plan.passed


@pytest.mark.asyncio
async def test_existing_react_control_metrics_remain_reproducible() -> None:
    outcomes = await run_scenarios(
        load_scenarios(), list(EvaluationConfiguration),
        mode=EvaluationMode.SCRIPTED, strategy_name="react",
    )
    metrics = {
        configuration.value: aggregate([item for item in outcomes if item.configuration == configuration.value])
        for configuration in EvaluationConfiguration
    }
    assert metrics["prompt_only_baseline"]["policy_violation_execution_rate"] == {
        "numerator": 9, "denominator": 9, "rate": 1.0,
    }
    assert metrics["enforced_policy"]["policy_violation_execution_rate"] == {
        "numerator": 0, "denominator": 9, "rate": 0.0,
    }
    assert metrics["enforced_policy_with_approval"]["policy_violation_execution_rate"] == {
        "numerator": 0, "denominator": 9, "rate": 0.0,
    }
