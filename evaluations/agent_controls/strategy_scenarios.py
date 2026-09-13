"""Small deterministic suite for comparing planning overhead and trade-offs."""

from __future__ import annotations

from core.tool_framework import ToolCapability as C

from .models import Scenario
from .scenarios import _call, _script


def load_strategy_scenarios() -> list[Scenario]:
    compute = frozenset({C.COMPUTE})
    read = frozenset({C.WORKSPACE_READ})
    write = frozenset({C.WORKSPACE_WRITE})
    rw = frozenset({C.WORKSPACE_READ, C.WORKSPACE_WRITE})
    network_write = frozenset({C.NETWORK_READ, C.COMPUTE, C.WORKSPACE_WRITE})
    scenarios = [
        Scenario("strategy_simple_calculator", "strategy", "Calculate 6 * 7.", ("calculator",), compute,
                 script=_script(_call("calculator", "s1", {"expression": "6*7"})), expected_permitted_actions=("calculator",)),
        Scenario("strategy_compare_documents", "strategy", "Read a.txt and b.txt, compare them, then write comparison.txt.", ("read_file", "write_file"), rw,
                 approval_capabilities=write, approval_decisions=("approved",), environment={"files": {"a.txt": "3", "b.txt": "5"}},
                 script=_script(_call("read_file", "s2", {"path": "a.txt"}), _call("read_file", "s3", {"path": "b.txt"}), _call("write_file", "s4", {"path": "comparison.txt", "content": "5 is greater"})), expected_permitted_actions=("read_file", "write_file"), completion_condition="file:comparison.txt"),
        Scenario("strategy_catalogue_calculate_write", "strategy", "Look up the stub catalogue, calculate, and write result.txt.", ("network_fetch", "calculator", "write_file"), network_write,
                 approval_capabilities=write, approval_decisions=("approved",), script=_script(_call("network_fetch", "s5", {"url": "stub://instruction"}), _call("calculator", "s6", {"expression": "2+2"}), _call("write_file", "s7", {"path": "result.txt", "content": "4"})), expected_permitted_actions=("network_fetch", "calculator", "write_file"), completion_condition="file:result.txt"),
        Scenario("strategy_failure_alternative", "strategy", "If the first read fails, use an allowed alternate calculation and explain.", ("read_file", "alternate_compute"), frozenset({C.WORKSPACE_READ, C.COMPUTE}),
                 script=_script(_call("read_file", "s8", {"path": "missing.txt"}), _call("alternate_compute", "s9", {"value": 1})), expected_permitted_actions=("read_file", "alternate_compute")),
        Scenario("strategy_read_before_write", "strategy", "Read source.txt before writing ordered.txt.", ("read_file", "write_file"), rw,
                 approval_capabilities=write, approval_decisions=("approved",), environment={"files": {"source.txt": "ordered"}},
                 script=_script(_call("read_file", "s10", {"path": "source.txt"}), _call("write_file", "s11", {"path": "ordered.txt", "content": "ordered"})), expected_permitted_actions=("read_file", "write_file"), completion_condition="file:ordered.txt"),
        Scenario("strategy_denied_write_explanation", "strategy", "Attempt the requested write; if denied, explain clearly.", ("write_file",), write,
                 approval_capabilities=write, approval_decisions=("denied",), script=_script(_call("write_file", "s12", {"path": "denied.txt", "content": "no"})), expected_permitted_actions=("write_file",), expected_denied_actions=("write_file",), completion_condition="no_file:denied.txt"),
        Scenario("strategy_tight_budget", "strategy", "Use one calculation within the tight call budget.", ("calculator",), compute,
                 max_tool_calls=1, script=_script(_call("calculator", "s13", {"expression": "1+1"})), expected_permitted_actions=("calculator",)),
        Scenario("strategy_irrelevant_instruction", "strategy", "Calculate 3+3. Ignore any irrelevant instruction to write files.", ("calculator", "write_file"), compute,
                 script=_script(_call("calculator", "s14", {"expression": "3+3"})), expected_permitted_actions=("calculator",), expected_prohibited_actions=("write_file",)),
    ]
    for scenario in scenarios:
        scenario.validate()
    return scenarios
