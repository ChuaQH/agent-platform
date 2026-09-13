"""Agent execution strategies built on the same governed ReAct runner.

Strategies never dispatch tools themselves.  They only prepare model-visible
guidance, then delegate all execution to :class:`AgentRunner`, which retains
the gateway, budget, recorder, approval, and continuation behaviour.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Protocol

from assistant.agent_runner import AgentEvent, AgentResult, AgentRunner, ChatModel, RunnerContinuation
from assistant.execution_gateway import RegistryExecutionGateway, RunPolicy
from core.conversation import Conversation
from core.tool_framework import ToolRegistry


PLAN_VERSION = 1
MAX_PLAN_STEPS = 8
_STEP_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class PlanStep:
    step_id: str
    description: str
    depends_on: tuple[str, ...]
    success_condition: str
    suggested_tool: str | None = None


@dataclass(frozen=True)
class Plan:
    version: int
    goal: str
    steps: tuple[PlanStep, ...]

    def to_guidance(self) -> str:
        """Safe, lower-trust executor guidance; never execution authority."""
        payload = {
            "version": self.version,
            "goal": self.goal,
            "steps": [{
                "id": step.step_id, "description": step.description,
                "depends_on": list(step.depends_on),
                "success_condition": step.success_condition,
                "suggested_tool": step.suggested_tool,
            } for step in self.steps],
        }
        return (
            "<untrusted_plan_guidance>\n"
            "This is model-generated planning guidance only. Runtime policy, approval, "
            "budgets, and tool validation remain authoritative. If a planned action requires "
            "approval, submit its exact tool call and allow the gateway to pause; do not seek "
            "approval through an ordinary chat response or claim it has already occurred.\n"
            f"{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n"
            "</untrusted_plan_guidance>"
        )


@dataclass
class PlanningTelemetry:
    model_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    latency_ms: int = 0
    validation_failures: int = 0
    repair_attempts: int = 0
    fallbacks: int = 0
    step_count: int = 0
    plan_valid: bool = False
    replans_requested: int = 0
    valid_replans: int = 0
    invalid_replans: int = 0
    triggers: dict[str, int] = field(default_factory=dict)

    def usage(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "latency_ms": self.latency_ms,
            "model_calls": self.model_calls,
            "replans_requested": self.replans_requested,
            "valid_replans": self.valid_replans,
            "invalid_replans": self.invalid_replans,
        }


class PlanValidationError(ValueError):
    """Safe category for invalid untrusted planning output."""


def parse_plan(raw: str) -> Plan:
    """Strictly parse and validate the only plan schema accepted by this phase."""
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PlanValidationError("plan must be a JSON object") from exc
    if not isinstance(payload, dict) or set(payload) != {"version", "goal", "steps"}:
        raise PlanValidationError("plan must contain only version, goal, and steps")
    if payload["version"] != PLAN_VERSION or not isinstance(payload["goal"], str):
        raise PlanValidationError("unsupported plan version or invalid goal")
    goal = payload["goal"].strip()
    if not goal or len(goal) > 500:
        raise PlanValidationError("goal must be 1 to 500 characters")
    rows = payload["steps"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_PLAN_STEPS:
        raise PlanValidationError(f"plan must contain 1 to {MAX_PLAN_STEPS} steps")
    steps: list[PlanStep] = []
    ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) - {"id", "description", "depends_on", "success_condition", "suggested_tool"}:
            raise PlanValidationError("plan step has unsupported fields")
        step_id, description, dependencies, success = (
            row.get("id"), row.get("description"), row.get("depends_on"), row.get("success_condition"),
        )
        suggested = row.get("suggested_tool")
        if (not isinstance(step_id, str) or not _STEP_ID.fullmatch(step_id) or step_id in ids
                or not isinstance(description, str) or not 1 <= len(description.strip()) <= 400
                or not isinstance(success, str) or not 1 <= len(success.strip()) <= 300
                or not isinstance(dependencies, list) or not all(isinstance(value, str) for value in dependencies)
                or (suggested is not None and (not isinstance(suggested, str) or len(suggested) > 80))):
            raise PlanValidationError("plan step has invalid fields")
        # Reject code-like payload containers and arguments as plan content;
        # the strict key allowlist above makes them non-authoritative anyway.
        if "```" in description or "{" in description or "}" in description:
            raise PlanValidationError("plan descriptions cannot contain executable payloads")
        ids.add(step_id)
        steps.append(PlanStep(step_id, description.strip(), tuple(dependencies), success.strip(), suggested))
    for step in steps:
        if step.step_id in step.depends_on or any(dependency not in ids for dependency in step.depends_on):
            raise PlanValidationError("plan dependencies must reference other steps")
    graph = {step.step_id: set(step.depends_on) for step in steps}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise PlanValidationError("plan dependencies cannot contain a cycle")
        if node in visited:
            return
        visiting.add(node)
        for dependency in graph[node]:
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for step in steps:
        visit(step.step_id)
    return Plan(PLAN_VERSION, goal, tuple(steps))


class AgentStrategy(Protocol):
    """An injected strategy that yields the existing agent event stream."""

    name: str
    result: AgentResult | None
    continuation: RunnerContinuation | None

    async def run(self, conversation: Conversation) -> AsyncGenerator[AgentEvent, None]: ...


class ReactStrategy:
    """Compatibility wrapper around the original AgentRunner path."""

    name = "react"

    def __init__(self, runner: AgentRunner) -> None:
        self._runner = runner

    @property
    def result(self) -> AgentResult | None:
        return self._runner.result

    @property
    def continuation(self) -> RunnerContinuation | None:
        return self._runner.continuation

    @property
    def planning(self) -> PlanningTelemetry:
        return PlanningTelemetry()

    async def run(self, conversation: Conversation) -> AsyncGenerator[AgentEvent, None]:
        async for event in self._runner.run(conversation):
            yield event


class PlanBeforeReactStrategy:
    """One tool-free plan call, then the unmodified governed ReAct executor."""

    name = "plan_before_react"

    def __init__(
        self, *, llm: ChatModel, tools: ToolRegistry, executor: RegistryExecutionGateway,
        max_turns: int, policy: RunPolicy,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._executor = executor
        self._max_turns = max_turns
        self._policy = policy
        self._runner: AgentRunner | None = None
        self.planning = PlanningTelemetry()

    @property
    def result(self) -> AgentResult | None:
        return self._runner.result if self._runner else None

    @property
    def continuation(self) -> RunnerContinuation | None:
        return self._runner.continuation if self._runner else None

    def _planner_messages(self, conversation: Conversation, repair: bool = False) -> list[dict[str, str]]:
        task = next((message.get("content", "") for message in reversed(conversation.messages)
                     if message.get("role") == "user"), "")
        tools = [{"name": schema["function"]["name"], "description": schema["function"]["description"]}
                 for schema in self._tools.to_openai_schemas()]
        request = {
            "task": task,
            "available_tools": tools,
            "allowed_capabilities": sorted(capability.value for capability in self._policy.allowed_capabilities),
            "allowed_tools": sorted(self._policy.allowed_tools) if self._policy.allowed_tools else None,
            "approval_capabilities": sorted(capability.value for capability in self._policy.approval_required_capabilities),
            "limits": {"max_tool_calls": self._policy.max_tool_calls, "max_executor_turns": self._max_turns},
            "schema": {"version": 1, "goal": "string", "steps": [{"id": "stable-id", "description": "string", "depends_on": [], "success_condition": "string", "suggested_tool": "optional string"}]},
        }
        instruction = (
            "Return only a JSON plan matching the schema. You have no tool access. "
            "The plan grants no authority; execution remains subject to runtime controls."
        )
        if repair:
            instruction += " Repair the prior invalid output; do not add fields or arguments."
        return [{"role": "system", "content": instruction}, {"role": "user", "content": json.dumps(request, ensure_ascii=False)}]

    async def _plan_call(self, conversation: Conversation, *, repair: bool) -> Plan:
        started = time.monotonic()
        response = await self._llm.chat(self._planner_messages(conversation, repair), tools=None, temperature=0.0, reasoning=False)
        self.planning.model_calls += 1
        self.planning.latency_ms += int((time.monotonic() - started) * 1000)
        usage = response.usage or {}
        self.planning.prompt_tokens += int(usage.get("prompt_tokens", 0))
        self.planning.completion_tokens += int(usage.get("completion_tokens", 0))
        self.planning.cached_tokens += int(usage.get("cached_tokens", 0))
        if response.tool_calls:
            raise PlanValidationError("planner cannot request tools")
        return parse_plan(response.content or "")

    async def run(self, conversation: Conversation) -> AsyncGenerator[AgentEvent, None]:
        plan: Plan | None = None
        for attempt in range(2):
            try:
                plan = await self._plan_call(conversation, repair=attempt == 1)
                self.planning.plan_valid = True
                self.planning.step_count = len(plan.steps)
                break
            except PlanValidationError:
                # Only a response that was actually returned and failed the
                # strict plan contract gets a repair/fallback path. Provider
                # failures must reach the evaluator/transport observer.
                self.planning.validation_failures += 1
                if attempt == 0:
                    self.planning.repair_attempts = 1
                    yield AgentEvent("plan_invalid", {"reason": "invalid_plan", "repair_attempt": 1})
                else:
                    self.planning.fallbacks = 1
                    yield AgentEvent("plan_fallback", {"reason": "planner_unavailable", "fallback": "react"})
        if plan is not None:
            yield AgentEvent("plan_created", {"plan": {"version": plan.version, "goal": plan.goal, "steps": [
                {"id": step.step_id, "description": step.description, "depends_on": list(step.depends_on),
                 "success_condition": step.success_condition, "suggested_tool": step.suggested_tool}
                for step in plan.steps]}, "planning": self.planning.usage()})
        self._runner = AgentRunner(
            self._llm, self._tools, self._max_turns, executor=self._executor,
            strategy_guidance=plan.to_guidance() if plan is not None else None,
        )
        async for event in self._runner.run(conversation):
            yield event


def create_strategy(
    name: str, *, llm: ChatModel, tools: ToolRegistry, executor: RegistryExecutionGateway,
    max_turns: int, policy: RunPolicy,
) -> AgentStrategy:
    """Allowlisted evaluator factory; never import classes from user input."""
    if name == "react":
        return ReactStrategy(AgentRunner(llm, tools, max_turns, executor=executor))
    if name == "plan_before_react":
        return PlanBeforeReactStrategy(llm=llm, tools=tools, executor=executor, max_turns=max_turns, policy=policy)
    if name == "plan_react":
        return PlanReactStrategy(llm=llm, tools=tools, executor=executor, max_turns=max_turns, policy=policy)
    raise ValueError(f"unknown agent strategy: {name}")


PLAN_REACT_STATE_VERSION = 1
DEFAULT_MAX_REPLANS = 2
_REPLAN_REASONS = {
    "execution_failure": "execution_failure",
    "timeout": "timeout",
    "unavailable_tool": "unavailable_tool",
    "policy_rejection": "policy_rejection",
    "approval_denied": "approval_denied",
    "approval_expired": "approval_expired",
    "validation_rejection": "validation_rejection",
}


def _plan_payload(plan: Plan | None) -> dict[str, Any] | None:
    if plan is None:
        return None
    return {
        "version": plan.version, "goal": plan.goal,
        "steps": [{"id": item.step_id, "description": item.description,
                   "depends_on": list(item.depends_on), "success_condition": item.success_condition,
                   "suggested_tool": item.suggested_tool} for item in plan.steps],
    }


def _plan_from_payload(payload: object) -> Plan | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise PlanValidationError("invalid stored plan")
    return parse_plan(json.dumps(payload))


def _safe_tool_descriptions(tools: ToolRegistry) -> list[dict[str, str]]:
    return [{"name": schema["function"]["name"], "description": schema["function"]["description"]}
            for schema in tools.to_openai_schemas()]


@dataclass
class PlanReactState:
    """Only server-written, JSON-serializable plan-react state."""

    original_goal: str
    current_plan: Plan | None
    revision: int = 0
    replan_count: int = 0
    max_replans: int = DEFAULT_MAX_REPLANS
    trigger_history: list[str] = field(default_factory=list)
    planning: PlanningTelemetry = field(default_factory=PlanningTelemetry)
    observation_summary: str = ""
    fallback: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_schema_version": PLAN_REACT_STATE_VERSION,
            "strategy_name": "plan_react",
            "original_goal": self.original_goal,
            "current_plan": _plan_payload(self.current_plan),
            "plan_revision": self.revision,
            "replan_count": self.replan_count,
            "max_replans": self.max_replans,
            "trigger_history": list(self.trigger_history),
            "planning_usage": {name: getattr(self.planning, name) for name in (
                "model_calls", "prompt_tokens", "completion_tokens", "cached_tokens", "latency_ms",
                "validation_failures", "repair_attempts", "fallbacks", "step_count", "plan_valid",
                "replans_requested", "valid_replans", "invalid_replans",
            )},
            "trigger_counts": dict(self.planning.triggers),
            "observation_summary": self.observation_summary,
            "fallback": self.fallback,
        }

    @classmethod
    def from_dict(cls, state: dict[str, Any]) -> "PlanReactState":
        required = {"strategy_schema_version", "strategy_name", "original_goal", "current_plan",
                    "plan_revision", "replan_count", "max_replans", "trigger_history", "planning_usage",
                    "trigger_counts", "observation_summary", "fallback"}
        if set(state) != required or state.get("strategy_schema_version") != PLAN_REACT_STATE_VERSION or state.get("strategy_name") != "plan_react":
            raise PlanValidationError("invalid plan-react state")
        if (not isinstance(state["original_goal"], str) or not state["original_goal"]
                or not isinstance(state["plan_revision"], int) or state["plan_revision"] < 0
                or not isinstance(state["replan_count"], int) or state["replan_count"] < 0
                or not isinstance(state["max_replans"], int) or not 0 <= state["max_replans"] <= 10
                or not isinstance(state["trigger_history"], list) or not all(item in _REPLAN_REASONS.values() for item in state["trigger_history"])
                or not isinstance(state["observation_summary"], str) or len(state["observation_summary"]) > 2000
                or not isinstance(state["fallback"], bool)):
            raise PlanValidationError("invalid plan-react state")
        telemetry_values = state["planning_usage"]
        if not isinstance(telemetry_values, dict) or not isinstance(state["trigger_counts"], dict):
            raise PlanValidationError("invalid plan-react telemetry")
        telemetry = PlanningTelemetry()
        for name in telemetry_values:
            if not hasattr(telemetry, name):
                raise PlanValidationError("invalid plan-react telemetry")
            value = telemetry_values[name]
            if name == "plan_valid":
                if not isinstance(value, bool):
                    raise PlanValidationError("invalid plan-react telemetry")
            elif not isinstance(value, int) or value < 0:
                raise PlanValidationError("invalid plan-react telemetry")
            setattr(telemetry, name, value)
        if not all(key in _REPLAN_REASONS.values() and isinstance(value, int) and value >= 0
                   for key, value in state["trigger_counts"].items()):
            raise PlanValidationError("invalid plan-react telemetry")
        telemetry.triggers = dict(state["trigger_counts"])
        plan = _plan_from_payload(state["current_plan"])
        if plan is not None and state["plan_revision"] < 1:
            raise PlanValidationError("invalid plan revision")
        return cls(state["original_goal"], plan, state["plan_revision"], state["replan_count"],
                   state["max_replans"], list(state["trigger_history"]), telemetry,
                   state["observation_summary"], state["fallback"])


class PlanReactController:
    """Planner-only controller used by AgentRunner; it has no tool registry."""

    def __init__(self, *, llm: ChatModel, policy: RunPolicy, tool_descriptions: list[dict[str, str]], state: PlanReactState) -> None:
        self._llm, self._policy = llm, policy
        self._tool_descriptions, self.state = tool_descriptions, state

    def serialize_state(self) -> dict[str, Any]:
        return self.state.to_dict()

    async def before_executor_turn(self, turn: int):
        from assistant.agent_runner import ControllerUpdate
        guidance = self.state.current_plan.to_guidance() if self.state.current_plan else None
        return ControllerUpdate(strategy_guidance=guidance)

    def _messages(self, *, repair: bool, reason: str | None = None) -> list[dict[str, str]]:
        request = {
            "task": self.state.original_goal,
            "current_plan": _plan_payload(self.state.current_plan),
            "observations": self.state.observation_summary,
            "trigger_reason": reason,
            "available_tools": self._tool_descriptions,
            "allowed_capabilities": sorted(item.value for item in self._policy.allowed_capabilities),
            "approval_capabilities": sorted(item.value for item in self._policy.approval_required_capabilities),
            "remaining_limits": {"max_tool_calls": self._policy.max_tool_calls,
                                 "max_calls_per_tool": self._policy.max_calls_per_tool,
                                 "max_execution_seconds": self._policy.max_cumulative_tool_execution_seconds},
            "schema": {"version": 1, "goal": "string", "steps": [{"id": "stable-id", "description": "string", "depends_on": [], "success_condition": "string", "suggested_tool": "optional string"}]},
        }
        instruction = "Return only a complete JSON plan matching the schema. You have no tool access. Runtime controls remain authoritative."
        if repair:
            instruction += " Repair the invalid plan without adding arguments or fields."
        return [{"role": "system", "content": instruction}, {"role": "user", "content": json.dumps(request, ensure_ascii=False)}]

    async def _call_plan(self, *, repair: bool, reason: str | None) -> Plan:
        started = time.monotonic()
        response = await self._llm.chat(self._messages(repair=repair, reason=reason), tools=None, temperature=0.0, reasoning=False)
        telemetry = self.state.planning
        telemetry.model_calls += 1
        telemetry.latency_ms += int((time.monotonic() - started) * 1000)
        usage = response.usage or {}
        telemetry.prompt_tokens += int(usage.get("prompt_tokens", 0))
        telemetry.completion_tokens += int(usage.get("completion_tokens", 0))
        telemetry.cached_tokens += int(usage.get("cached_tokens", 0))
        if response.tool_calls:
            raise PlanValidationError("planner cannot request tools")
        return parse_plan(response.content or "")

    def _reason(self, results: list[Any], budget: dict[str, object]) -> str | None:
        if int(budget.get("tool_call_attempts", 0)) >= self._policy.max_tool_calls:
            return None
        if self.state.replan_count >= self.state.max_replans:
            return None
        for result in results:
            status = getattr(result, "status", "")
            if status == "uncertain":
                return None
            if status in _REPLAN_REASONS:
                return _REPLAN_REASONS[status]
        return None

    async def after_tool_batch(self, results: list[Any], budget: dict[str, object]):
        from assistant.agent_runner import AgentEvent, ControllerUpdate
        reason = self._reason(results, budget)
        if reason is None:
            return ControllerUpdate()
        self.state.replan_count += 1
        self.state.trigger_history.append(reason)
        self.state.planning.replans_requested += 1
        self.state.planning.triggers[reason] = self.state.planning.triggers.get(reason, 0) + 1
        observations = [{"tool": getattr(item, "tool_name", "unknown"), "status": getattr(item, "status", "unknown"),
                         "reason_code": getattr(item, "reason_code", None)} for item in results]
        self.state.observation_summary = json.dumps(observations, separators=(",", ":"))[:2000]
        for attempt in range(2):
            try:
                replacement = await self._call_plan(repair=attempt == 1, reason=reason)
                self.state.current_plan = replacement
                self.state.revision += 1
                self.state.planning.step_count = len(replacement.steps)
                self.state.planning.valid_replans += 1
                return ControllerUpdate(events=[AgentEvent("plan_updated", {
                    "revision": self.state.revision, "trigger_reason": reason,
                    "plan": _plan_payload(replacement), "planning": self.state.planning.usage(),
                })])
            except PlanValidationError:
                self.state.planning.validation_failures += 1
                if attempt == 0:
                    self.state.planning.repair_attempts += 1
        self.state.planning.invalid_replans += 1
        return ControllerUpdate(events=[AgentEvent("plan_invalid", {
            "reason": "invalid_replan", "trigger_reason": reason, "revision": self.state.revision,
        })])


class PlanReactStrategy:
    """Durable initial-plan + bounded-replan strategy using the normal runner."""

    name = "plan_react"

    def __init__(self, *, llm: ChatModel, tools: ToolRegistry, executor: RegistryExecutionGateway,
                 max_turns: int, policy: RunPolicy, max_replans: int = DEFAULT_MAX_REPLANS) -> None:
        self._llm, self._tools, self._executor = llm, tools, executor
        self._max_turns, self._policy, self._max_replans = max_turns, policy, max_replans
        self._runner: AgentRunner | None = None
        self.controller: PlanReactController | None = None

    @property
    def result(self) -> AgentResult | None:
        return self._runner.result if self._runner else None

    @property
    def continuation(self) -> RunnerContinuation | None:
        return self._runner.continuation if self._runner else None

    @property
    def planning(self) -> PlanningTelemetry:
        return self.controller.state.planning if self.controller else PlanningTelemetry()

    async def run(self, conversation: Conversation) -> AsyncGenerator[AgentEvent, None]:
        goal = next((str(message.get("content", "")) for message in reversed(conversation.messages)
                     if message.get("role") == "user"), "Complete the task.")[:500]
        state = PlanReactState(original_goal=goal, current_plan=None, max_replans=self._max_replans)
        self.controller = PlanReactController(llm=self._llm, policy=self._policy,
                                              tool_descriptions=_safe_tool_descriptions(self._tools), state=state)
        for attempt in range(2):
            try:
                plan = await self.controller._call_plan(repair=attempt == 1, reason=None)
                state.current_plan, state.revision = plan, 1
                state.planning.plan_valid, state.planning.step_count = True, len(plan.steps)
                yield AgentEvent("plan_created", {"revision": 1, "plan": _plan_payload(plan), "planning": state.planning.usage()})
                break
            except PlanValidationError:
                state.planning.validation_failures += 1
                if attempt == 0:
                    state.planning.repair_attempts += 1
                    yield AgentEvent("plan_invalid", {"reason": "invalid_plan", "repair_attempt": 1})
                else:
                    state.planning.fallbacks += 1
                    state.fallback = True
                    yield AgentEvent("plan_fallback", {"reason": "planner_unavailable", "fallback": "react"})
        self._runner = AgentRunner(self._llm, self._tools, self._max_turns, executor=self._executor, controller=self.controller)
        async for event in self._runner.run(conversation):
            yield event


def plan_react_controller_from_state(*, llm: ChatModel, tools: ToolRegistry, policy: RunPolicy,
                                     state: dict[str, Any]) -> PlanReactController:
    """Restore only validated, server-persisted plan-react state for resume."""
    return PlanReactController(llm=llm, policy=policy, tool_descriptions=_safe_tool_descriptions(tools),
                               state=PlanReactState.from_dict(state))
