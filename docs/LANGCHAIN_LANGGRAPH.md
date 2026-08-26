# LangChain & LangGraph — Inherited vs. Implemented

This document exists so a reviewer can locate, in minutes, concrete SWE-Forge code
demonstrating hands-on LangChain and LangGraph work — and can tell it apart from
upstream Open SWE's own use of the same libraries.

Every file path and symbol below is real and present in this repository. Nothing here
describes code that does not exist.

---

## 1. LangChain — inherited from Open SWE

| Capability | Upstream location |
|---|---|
| Agent construction | `deepagents.create_deep_agent` in `agent/server.py` |
| Middleware stack (24 modules) | `agent/middleware/` — `model_fallback`, `model_call_timeout`, `tool_error_handler`, `task_retry`, `sanitize_*`, `sandbox_circuit_breaker`, … |
| Model instantiation | `agent/utils/model.py` → `make_model()`, `fallback_model_id_for()` |
| Provider reasoning config | `openai_reasoning_for()`, `anthropic_thinking_for()`, `google_thinking_level_for()` |
| ~30 agent tools | `agent/tools/` — Linear, GitHub PR, sandbox files, HTTP |
| MCP adapters | `langchain-mcp-adapters`; `agent/integrations/{corridor,datadog,notion}_mcp.py` |

SWE-Forge treats all of the above as infrastructure and does not reimplement it.

## 2. LangChain — implemented by SWE-Forge

### 2.1 Tools (`agent/sweforge/tools/registry.py`)

Twelve `langchain_core.tools.StructuredTool` instances, each with a typed Pydantic
`args_schema`, a description written for a model rather than a human, validation, and
structured error returns.

| Tool | Argument schema | Purpose |
|---|---|---|
| `analyze_repository` | `AnalyzeRepositoryArgs` | Inventory files, languages, symbols, AST parse errors |
| `build_repository_graph` | — | Build/return the import graph and report its size |
| `find_relevant_files` | `TaskQueryArgs` | Rank files by relevance to a task, with reasons |
| `find_dependencies` | `GraphQueryArgs` | Imports and importers of one file (blast radius) |
| `find_callers` | `SymbolQueryArgs` | Definition sites and in-repo importers of a symbol |
| `find_related_tests` | `GraphQueryArgs` | Tests covering a file |
| `run_validation` | `RunValidationArgs` | Execute targeted tests/lint in the sandbox |
| `analyze_failure` | `AnalyzeFailureArgs` | Classify failing output into a category |
| `inspect_git_diff` | `GitDiffArgs` | `git diff --stat` and changed-file list |
| `calculate_change_risk` | `ChangeRiskArgs` | Score a change 0–100 → LOW/MEDIUM/HIGH |
| `security_scan` | `SecurityScanArgs` | Screen content for secrets and dangerous patterns |
| `retrieve_similar_tasks` | `SimilarTasksArgs` | Retrieve prior experience for planning context |

**Two design decisions worth discussing in an interview:**

*Errors are returned, not raised.* Every tool is wrapped by `_guard`, which converts an
exception into `{"ok": false, "error": "..."}`. A raised exception inside a tool call
tends to derail an agent turn; a structured error is something a model can read and
route around.

```python
# agent/sweforge/tools/registry.py
def _guard(name: str, fn: Any) -> Any:
    def wrapper(**kwargs: Any) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            payload = fn(**kwargs)
            context.ledger.record(ToolCall(name, ..., True))
            return {"ok": True, **payload}
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            context.ledger.record(ToolCall(name, ..., False, message))
            return {"ok": False, "error": message}
    return wrapper
```

*Tools are load-bearing, not decorative.* Graph nodes invoke them through
`SWEForgeRuntime.call_tool`, so usage is validated and counted. Measured tool calls per
variant in the shipped evaluation: **A_baseline 0, B 12, C 24, D 24, E_full 56.** A
baseline with no repository intelligence, memory or security gate legitimately makes
zero tool calls; that spread is evidence the tools are wired into the workflow rather
than sitting unused.

| Node | Tool it calls |
|---|---|
| `repository_analysis` | `find_relevant_files` |
| `task_complexity_analysis` | `retrieve_similar_tasks` |
| `failure_analysis` | `analyze_failure` |
| `security_analysis` | `security_scan` |
| `risk_gate` | `calculate_change_risk` |

### 2.2 Structured output

Every consequential decision uses `with_structured_output` against an explicit Pydantic
model. Models are defined in `agent/sweforge/schemas.py` and
`agent/sweforge/agents/roles.py`.

| Decision | Model | Called from |
|---|---|---|
| Task planning | `TaskPlan` (+ `Subtask`) | `planning/planner.py` |
| Implementation | `ImplementationOutput` (+ `FileEdit`) | `agents/roles.py::ImplementationAgent` |
| Failure diagnosis + repair | `RepairOutput` (+ `FailureDiagnosis`) | `agents/roles.py::Diagnostician` |
| Code review | `ReviewResult` (+ `ReviewFinding`) | `agents/roles.py::IndependentReviewer` |
| Risk assessment | `RiskScore` (+ `RiskFactor`) | `security/risk.py` (deterministic, not LLM) |

**Validators do real work — they are not decoration.** Three examples that turn model
incoherence into a caught error instead of a silently wrong graph transition:

```python
# TaskPlan: reject dependency cycles via Kahn's algorithm, so an impossible
# plan fails at the planning node rather than deadlocking execution.
if seen != len(self.subtasks):
    raise ValueError("subtask dependency graph contains a cycle")
```

```python
# ReviewResult: a model that reports a blocker and approves anyway is corrected,
# because the risk gate downstream trusts this boolean.
if worst >= SEVERITY_ORDER["major"]:
    object.__setattr__(self, "approved", False)
```

```python
# FileEdit: an edit path must stay inside the repository.
if cleaned.startswith("/") or ".." in cleaned.split("/"):
    raise ValueError(f"edit path must be repository-relative: {value!r}")
```

### 2.3 Model wrapper / routing layer (`agent/sweforge/routing/model_router.py`)

SWE-Forge-owned, clearly separate from upstream `agent/middleware/`:

- **Tier policy** — `ROLE_TIER` maps a logical role to `fast | balanced | coding | reasoning`.
- **Complexity adaptation** — planning/implementation/review escalate on `complex`
  tasks and de-escalate on `trivial` ones; cheap bookkeeping roles never escalate.
- **Failure-driven escalation** — two failures on a role earn a stronger tier, driven
  by observed outcomes rather than a static table.
- **Execution budget & telemetry** — `ModelRouter.track()` is a context manager that
  times each call and appends a `ModelCallRecord` (tier, latency, tokens, estimated
  cost, ok/error) to `ModelUsageLedger`.
- **Injectable factory** — `model_factory` lets tests and the evaluation harness
  substitute a deterministic model while routing logic runs unchanged.

No model id is hard-coded; each tier resolves through an env var (`TIER_ENV_VARS`) with
a documented default. No API key is read or logged by this module, and there is a test
asserting a credential never reaches a routing decision
(`test_no_api_key_is_read_from_env`).

### 2.4 A real `BaseChatModel` subclass (`agent/sweforge/models/scripted.py`)

`ScriptedChatModel` subclasses `langchain_core.language_models.BaseChatModel`,
implementing `_generate`, `_llm_type`, and overriding `with_structured_output` to return
validated schema instances. It exists to make orchestration measurable by holding LLM
behaviour constant — see `docs/EVALUATION.md` for the scope limits that come with it.

---

## 3. LangGraph — inherited from Open SWE

| Capability | Upstream location |
|---|---|
| Deep-agent graph compilation | `deepagents.create_deep_agent` via `agent/server.py` |
| Graph entry-point registry | `langgraph.json` (`agent`, `reviewer`, `analyzer`, `chat`, `scheduler`) |
| Checkpointer / TTL config | `langgraph.json` `checkpointer` block |
| The one upstream `StateGraph` | `agent/scheduler.py` — cron scheduling, not engineering work |

## 4. LangGraph — implemented by SWE-Forge

All of the following lives in `agent/sweforge/graph/workflow.py` and
`agent/sweforge/state/graph_state.py`.

### 4.1 Custom state schema with reducers

`SWEForgeState` is a `TypedDict` carrying typed, inspectable task state. Reducer choice
is deliberate: single-writer fields use last-write-wins, while fields written by
concurrently executing subtask branches use append or merge reducers so a fan-out never
drops a sibling's result.

```python
# agent/sweforge/state/graph_state.py
class SWEForgeState(TypedDict, total=False):
    task: str
    repository_map: dict[str, Any]
    plan: TaskPlan | None
    implementation_results: Annotated[list[ImplementationResult], operator.add]
    test_results: VerificationResult | None
    failures: Annotated[list[FailureDiagnosis], operator.add]
    recovery_attempts: list[RecoveryAttempt]
    review_results: ReviewResult | None
    risk_score: RiskScore | None
    model_usage: Annotated[list[ModelCallRecord], operator.add]
    execution_metrics: Annotated[ExecutionMetrics, _merge_metrics]
    node_trace: Annotated[list[str], operator.add]
    final_status: FinalStatus
```

`_merge_metrics` is a custom reducer: additive for counters (`model_calls`,
`tool_calls`, cost), `max` for high-water marks (`recovery_attempts`, `wall_time`),
logical OR for `security_gate_triggered`.

### 4.2 Custom nodes (15)

`task_intake`, `repository_analysis`, `task_complexity_analysis`, `planning`,
`dynamic_agent_selection`, `implementation`, `verification`, `failure_analysis`,
`recovery`, `independent_review`, `security_analysis`, `risk_gate`, plus three terminal
nodes: `finalization`, `human_approval`, `escalation`.

Nodes are built by `build_nodes(runtime)` and close over an injected
`SWEForgeRuntime` — which is what makes the graph testable without patching globals.

### 4.3 Custom conditional edges and routing functions

Five routing functions, each reading **typed state only** — booleans, counters, enum
levels. None parses model prose to make a control-flow decision.

```python
builder.add_conditional_edges(
    "verification",
    make_route_after_verification(config),
    {
        "independent_review": "independent_review",
        "security_analysis": "security_analysis",
        "failure_analysis": "failure_analysis",
        "finalization": "finalization",
    },
)
```

| Routing function | Decision |
|---|---|
| `route_after_intake` | Empty task → terminal, else proceed |
| `make_route_after_verification` | Green → review/security/finalize; red → failure analysis or stop |
| `make_route_after_failure_analysis` | Recovery budget remaining → `recovery`, else `escalation` |
| `make_route_after_review` | Approved → security; rejected → recovery within budget, else escalation |
| `route_after_risk_gate` | HIGH → `human_approval`; otherwise → `finalization` |

### 4.4 Bounded loops

There are exactly two cycles, and both are gated:

**Recovery loop.** `recovery → verification` is an unconditional edge, but `recovery` is
*reachable* only through a routing function that checks the budget:

```python
def make_route_after_failure_analysis(config: WorkflowConfig):
    def route(state: SWEForgeState) -> Literal["recovery", "escalation"]:
        attempts = len(state.get("recovery_attempts", []))
        if attempts < config.max_recovery_attempts:
            return "recovery"
        return "escalation"
    return route
```

The bound is therefore *structural* — a property of the topology, not a prompt
instruction an LLM might ignore. Verified empirically: the
`inventory_recovery_exhausted` scenario scripts a repair that is always wrong, and every
recovery-enabled variant stops after exactly 3 attempts and escalates. Tests
`test_recovery_loop_terminates_when_repair_never_works` and
`test_recovery_bound_is_respected_at_one` assert this.

**Review loop.** `_review_budget_left` caps implement/review ping-pong at
`max_review_cycles`.

### 4.5 Explicit terminal states

`FinalStatus` is a closed `Literal`: `completed`, `completed_with_findings`,
`awaiting_human_approval`, `escalated_recovery_exhausted`, `escalated_review_rejected`,
`failed`, `pending`. `awaiting_human_approval` is a first-class outcome, not an error —
which is what lets the evaluation report distinguish "the gate worked" from "the task
failed".

### 4.6 Graph compilation and ablatable construction

```python
def build_workflow(runtime: SWEForgeRuntime) -> Any:
    config = runtime.config
    nodes = build_nodes(runtime)
    builder: StateGraph = StateGraph(SWEForgeState)
    for name, fn in nodes.items():
        builder.add_node(name, fn)
    builder.add_edge(START, "task_intake")
    ...
    return builder.compile()
```

`WorkflowConfig` flags cause a **different graph** to be built, which is what the
ablation study varies. This is not a runtime `if` inside one topology; the edges differ.

---

## 5. Why LangGraph was chosen

An autonomous system that edits code needs three properties a single agent loop cannot
guarantee:

1. **Termination guarantees.** "Try at most 3 repairs, then ask a human" must be
   enforced by the topology. In a ReAct loop it is a prompt request.
2. **Auditability.** After a run you must be able to say *why* a change was withheld.
   `node_trace` plus typed state gives a literal answer
   (`risk_gate(HIGH, score=90) → human_approval`).
3. **Ablatability.** To claim a component helps, you must be able to remove it and
   re-measure. Feature-flagged graph construction makes that a one-line change.

Reasoning stays with the LLM. Control flow does not.

## 6. Why LangChain tools

Repository intelligence, verification, risk scoring and memory need to be callable
*both* by graph nodes and by a tool-calling agent. Expressing them once as
`StructuredTool`s with Pydantic schemas gives argument validation, a description the
model can act on, uniform error handling, and one ledger for accounting — instead of
two parallel implementations that drift.

## 7. How state flows through the graph

```
task, repo_root
  → repository_analysis      writes repository_map, relevant_files
  → task_complexity_analysis writes complexity, memory_context
  → planning                 writes plan (TaskPlan), complexity
  → dynamic_agent_selection  writes selected_agents
  → implementation           writes implementation_results; applies edits to backend
  → verification             writes test_results (VerificationResult)
  → failure_analysis         appends failures (FailureDiagnosis)
  → recovery                 appends recovery_attempts; applies edits
  → independent_review       writes review_results
  → security_analysis        writes security_findings
  → risk_gate                writes risk_score
  → terminal                 writes final_status, final_summary
```

Throughout, `model_usage`, `node_trace` and `execution_metrics` accumulate through
their reducers.

## 8. How structured outputs prevent unreliable routing

A worked example. Suppose the reviewer emits `approved: true` alongside a blocker
finding — a genuinely common LLM inconsistency. Without structure, a text-parsing router
sees "approved" and ships the change. With SWE-Forge:

1. `ReviewResult`'s `_reconcile` validator sets `approved = False`, because a blocker
   finding contradicts approval.
2. `make_route_after_review` reads the boolean and routes to `recovery`.
3. If the recovery budget is spent, it routes to `escalation` instead.

The control-flow decision is a typed field validated by code, so the failure mode is
"the change is held" rather than "the change ships because the prose was ambiguous".

## 9. How LangSmith is used

`agent/sweforge/observability/tracing.py` wraps runs in a single root via
`langsmith.tracing_context`, activating **only** when a tracing switch *and* an API key
are both present. Because the graph is an explicit `StateGraph`, each node is its own
span with its own inputs and outputs — the diagnostic advantage over one opaque agent
loop. A trace shows:

```
sweforge:full
  task_intake → repository_analysis → planning → implementation
  → verification(FAIL) → failure_analysis(test_assertion) → recovery(attempt 1)
  → verification(PASS) → independent_review(APPROVED) → security_analysis
  → risk_gate(LOW) → finalization
```

Tracing failures are swallowed by design: observability must never fail a software
engineering task. Eleven tests cover the enabled/disabled matrix, including
`test_describe_configuration_hides_key_value`.

---

# Phase 23/24: closing the tool-calling gap

## The gap that existed

Before Phase 23, the graph called `StructuredTool.invoke()` from nodes and the
docs described that as agents using tools. It was not: the model never chose a
tool, never saw a result, and never revised. A source audit found **7 of 12 tools
had zero non-registry references** — dead demonstration tools.

## What was implemented

`agent/sweforge/agents/tool_loop.py::ToolCallingLoop` implements the real
contract:

```python
bound = model.bind_tools(tools)          # real LangChain binding
response = bound.invoke(conversation)    # AIMessage with .tool_calls
for call in response.tool_calls:
    content, ok = self._run_tool(by_name[call["name"]], call["args"])
    conversation.append(ToolMessage(content=content, tool_call_id=call["id"]))
result = model.with_structured_output(output_model).invoke(conversation)
```

A capability subtlety worth knowing: `BaseChatModel.bind_tools` **exists on every
chat model but raises `NotImplementedError`** unless the subclass implements it.
Probing the attribute is not a capability check — it has to be called. `_bind()`
does exactly that and degrades to structured-output-only when unsupported.

## All 12 tools are load-bearing

Verified by `test_all_twelve_tools_exercised_end_to_end`, which fails if any tool
is unused in a real run:

| Node | Tools invoked |
|---|---|
| `repository_analysis` | `analyze_repository` → `build_repository_graph` → `find_relevant_files` → `find_dependencies` / `find_callers` → `find_related_tests` |
| `task_complexity_analysis` | `retrieve_similar_tasks` |
| `verification` | `run_validation` (wraps the `Verifier`; sandbox remains the execution boundary) |
| `failure_analysis` | `analyze_failure` |
| `independent_review` | `inspect_git_diff` |
| `security_analysis` | `security_scan` |
| `risk_gate` | `calculate_change_risk` |
| agents (`bind_tools`) | per-role grants — see ARCHITECTURE.md |

Measured tool calls per variant rose from `0/12/24/24/56` to `12/108/130/138/170`
with outcomes and 6/6 routing correctness unchanged — evidence the tools are wired
in, not decorative.

## Ledger provenance

`ToolCall` now records `node`, `agent`, `model`, `status` and `args_summary`.
Arguments are summarised as shape (`content=str[5000]`), never values: a tool
payload can contain whole file contents, and logging it verbatim would bloat
traces and risk leaking repository content into observability.

## Node-level LangSmith metadata

`trace_node()` attaches `task_id`, `node`, `agent`, `model`, `tier`, `attempt`,
`recovery_attempt`, `tool`, `budget_remaining`, `risk_score`, `final_status`.
`node_metadata()` drops `None` values and filters any key that looks
credential-like, so a secret cannot reach a trace. Works identically with
LangSmith disabled (both paths tested).
