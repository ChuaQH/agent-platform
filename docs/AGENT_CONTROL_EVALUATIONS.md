# Governed agent evaluation suite

`evaluations.agent_controls` is an offline, reproducible evaluator for two
separate questions:

1. **Control conformance:** whether the real gateway blocks actions outside
   policy, budget, schema, and exact-action approval constraints.
2. **Agent behaviour:** whether a selected model completes the task under
   those same constraints.

Scripted conformance outcomes are not evidence of real-model safety or
reasoning quality. They prove that deliberately cooperative and
non-cooperative tool-call scripts traverse the real `AgentRunner`,
`RegistryExecutionGateway`, budget counters, in-memory recorder, and durable
resume service correctly.

## Safe defaults

All evaluator tools use an OS temporary directory, in-memory memory, a stub
catalog, and deterministic scripted responses. They cannot access the normal
workspace, production memory, production credentials, websites, messaging,
or payment systems. `prompt_only_baseline` exists only in this package; it is
not an assistant API option.

## Scenarios and configurations

Suite version `1.1` contains 27 typed Python control scenarios in five categories:

- benign actions (calculator, read, write, memory, multi-step);
- policy violations;
- prompt/tool-output attacks;
- schema and budget enforcement;
- approval integrity.

Every scenario declares its task, tools, policy, budgets, controlled state,
expected permitted/prohibited actions, approval decisions, completion check,
and scripted response sequence.

An opt-in `strategy` category adds eight deterministic comparison tasks: a
simple calculation, read/compare/write, catalogue/calculate/write, failed
first choice with an alternative, read-before-write ordering, denied write,
tight tool budget, and irrelevant-instruction resistance. The same scenario
contract, registry, gateway, policy, approval requirements, and environment
are supplied to both strategies.

Configurations keep the user prompt/task fixed:

| Configuration | Gateway policy | Approval |
|---|---|---|
| `prompt_only_baseline` | permissive | disabled |
| `enforced_policy` | scenario capability/tool/budget limits | disabled |
| `enforced_policy_with_approval` | scenario capability/tool/budget limits | exact-action decisions from the scenario |

## Run scripted conformance

```powershell
python -m evaluations.agent_controls run --mode scripted --configuration all --output evaluation-results/
```

To compare the two strategy plumbing paths without a live model:

```powershell
python -m evaluations.agent_controls run --mode scripted --strategy react `
  --configuration enforced_policy_with_approval --scenario-category strategy --output evaluation-results/
python -m evaluations.agent_controls run --mode scripted --strategy plan_before_react `
  --configuration enforced_policy_with_approval --scenario-category strategy --output evaluation-results/
```

The command writes a unique child directory containing `results.json` and a
Markdown report generated solely from that JSON. It exits non-zero when a
configuration's conformance expectations fail. Select scenarios with
`--scenario-id`, categories with `--scenario-category`, and a configuration
with `--configuration`.

## Optional live-model behaviour evaluation

Live mode is disabled until the ordinary configured model credentials are
available. It records provider/model, temperature, timestamps, turn/token and
latency data, and continues after an individual scenario error. Start with at
least five repeats; do not infer performance differences from a single run.

```powershell
python -m evaluations.agent_controls run --mode live --strategy react `
  --configuration enforced_policy --repeats 5 `
  --scenario-category prompt_injection --output evaluation-results/
```

`plan_before_react` makes one separate, tool-free planning call before the
normal ReAct executor loop. Its planner call has a separate limit of two
attempts (one initial attempt and at most one repair), so it does not consume
the executor turn or tool-call budgets. Its validated plan is untrusted
guidance only: the gateway still decides every tool's availability, policy,
approval, schema, and budget checks. Planning token/latency usage is retained
separately from executor usage and included in the overall totals.

The production assistant continues to use `react`. `plan_before_react` is
evaluator-only in this phase because approval resume constructs the durable
continuation with the established `AgentRunner`; the validated guidance is
preserved in that continuation, but the strategy's separate planning telemetry
is not rehydrated as a first-class production run state.

`plan_react` is the separately allowlisted production-capable strategy. It is
off by default and may be selected only as `strategy: "plan_react"` in a chat
request; omitted requests remain `react`. Resume requests do not accept a
strategy value: they restore the server-stored continuation state instead.
The initial plan is revision 1. A replacement is requested only for normalized
`execution_failure`, `timeout`, `unavailable_tool`, `policy_rejection`,
`approval_denied`, `approval_expired`, or `validation_rejection` results, and
only while a tool budget remains. Successful and uncertain actions never
replan. The default maximum is two replacements.

Plan-react continuations use version 2 and include a typed, server-written
strategy state: original goal, current validated plan, revision/replan counts,
trigger history, planning usage, safe observations, and fallback status.
Version 1 continuations remain valid React continuations. A corrupt or unknown
v2 state fails before the resolved action is dispatched.

## Plan-before-ReAct contract

The planner must return one JSON object with exactly `version`, `goal`, and
`steps`. Version is `1`; `goal` is 1–500 characters; and there must be 1–8
ordered steps. A step has a stable lowercase ID, a 1–400 character
description, dependency IDs, a 1–300 character deterministic success
condition, and an optional suggested tool name. IDs must be unique,
dependencies must exist, self-dependencies and dependency cycles are rejected,
and unsupported fields, code-style payload containers, and tool arguments are
not accepted.

The strategy emits `plan_created` for a valid plan, `plan_invalid` before its
single repair attempt, and `plan_fallback` when repair fails. It records
planning model calls, prompt/completion/cached tokens, latency, validation
failures, repair attempts, fallbacks, and step count. Planner and executor
usage are separate result fields; their token and latency totals are included
in the overall record.

No cost is reported: pricing configuration and a calculation date are not
part of this suite.

## Metrics

Every rate stores and renders `numerator/denominator`, including prohibited
attempts/executions, policy-violation execution rate, approval and budget
integrity, duplicate dispatches, schema enforcement, legitimate completion,
task completion, false blocks, turns, tool attempts, approvals, latency, and
input/output/cached tokens. Human approval wait time is not included in model
or gateway execution latency; the initial suite records model-loop latency
only and does not claim precision from one run.

Machine records exclude executable approval arguments, bearer tokens, and
sensitive argument values. Result metadata includes schema/suite versions,
git commit and dirty status, timestamp, strategy, configuration, model
configuration, and random seed (currently `null`: scripted runs are
deterministic).
