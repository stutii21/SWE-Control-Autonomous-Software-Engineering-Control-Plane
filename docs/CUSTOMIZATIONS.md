# Customizations — Technology Traceability

Populated from actual files and symbols in this repository. Nothing below claims
ownership of upstream code.

## Technology ownership table

| Technology | Upstream component (Open SWE) | SWE-Forge implementation |
|---|---|---|
| **LangGraph** | `deepagents.create_deep_agent` via `agent/server.py`; graph registry in `langgraph.json`; the single upstream `StateGraph` in `agent/scheduler.py` (cron) | `agent/sweforge/graph/workflow.py` — `build_workflow()`, `build_nodes()`, 17 nodes, 5 routing functions (`route_after_intake`, `make_route_after_verification`, `make_route_after_failure_analysis`, `make_route_after_review`, `route_after_risk_gate`), bounded recovery/review loops, 4 terminal nodes. State schema `agent/sweforge/state/graph_state.py` — `SWEForgeState`, custom reducer `_merge_metrics`, `initial_state()` |
| **LangChain (tools)** | ~30 tools in `agent/tools/` (Linear, GitHub PR, sandbox files, HTTP) | `agent/sweforge/tools/registry.py` — 12 `StructuredTool`s: `analyze_repository`, `build_repository_graph`, `find_relevant_files`, `find_dependencies`, `find_callers`, `find_related_tests`, `run_validation`, `analyze_failure`, `inspect_git_diff`, `calculate_change_risk`, `security_scan`, `retrieve_similar_tasks`; typed schemas (`TaskQueryArgs`, `ChangeRiskArgs`, …), `_guard` error wrapper, `ToolCallLedger` |
| **LangChain (structured output)** | Not used for control flow | `agent/sweforge/schemas.py` — `TaskPlan`, `Subtask`, `FileEdit`, `VerificationResult`, `FailureDiagnosis`, `RecoveryAttempt`, `ReviewResult`, `ReviewFinding`, `RiskScore`, `RiskFactor`, `ExperienceRecord`. `agent/sweforge/agents/roles.py` — `ImplementationOutput`, `RepairOutput`. All obtained via `with_structured_output` |
| **LangChain (model layer)** | `agent/utils/model.py` — `make_model()`, `fallback_model_id_for()`, provider reasoning config | `agent/sweforge/routing/model_router.py` — `ModelRouter`, `ModelSpec`, `RoutingDecision`, `ModelUsageLedger`, `TIER_ENV_VARS`, `ROLE_TIER`, `track()` budget/telemetry context manager, `extract_usage()` |
| **LangChain (BaseChatModel)** | Provider integrations via `langchain-anthropic`, `langchain-openai`, … | `agent/sweforge/models/scripted.py` — `ScriptedChatModel(BaseChatModel)` implementing `_generate`, `_llm_type`, `with_structured_output`; `ScriptedModelFactory` |
| **LangChain (middleware)** | 24 modules in `agent/middleware/` — `model_fallback`, `model_call_timeout`, `tool_error_handler`, `task_retry`, `sanitize_*`, `sandbox_circuit_breaker` | Deliberately **not** duplicated. SWE-Forge-owned equivalents are narrower and separate: retry/failure escalation in `ModelRouter.resolve_tier`, tool-error handling in `registry.py::_guard`, execution budget in `ModelRouter.track` |
| **LangSmith** | `agent/utils/langsmith.py`, `agent/integrations/langsmith.py`, `langsmith_tools.py` | `agent/sweforge/observability/tracing.py` — `trace_run()`, `tracing_enabled()`, `project_name()`, `run_metadata()`, `describe_configuration()`; optional by construction, no-op without credentials |
| **Deep Agents** | `create_deep_agent`, `GENERAL_PURPOSE_SUBAGENT`, browser subagent, `deepagents.backends.protocol.SandboxBackendProtocol` | Reused as infrastructure, not reimplemented. Consumed via `agent/sweforge/verification/backends.py::OpenSWESandboxBackend`, which duck-types the sandbox protocol. SWE-Forge's specialised agents (`ImplementationAgent`, `IndependentReviewer`, `Diagnostician`) are graph nodes with structured contracts, not deep-agent subagents |
| **MCP** | `langchain-mcp-adapters`; `agent/integrations/corridor_mcp.py`, `datadog_mcp.py`, `notion_mcp.py` | Transport reused, not duplicated. Orchestration added in `agent/sweforge/mcp/orchestration.py`: `MCPCapabilityRegistry`, `MCPToolSelector`, `MCPInvocationPolicy` (deny-by-default allowlist, retry, budget), plus deterministic fixtures in `agent/sweforge/mcp/fixtures.py` |
| **Sandboxing** | `agent/runtime/sandbox.py`, `agent/integrations/{daytona,modal,e2b,runloop,local}.py` | `agent/sweforge/verification/backends.py` — `ExecutionBackend` protocol, `OpenSWESandboxBackend` adapter, gated `LocalSubprocessBackend` for shipped fixtures only |
| **Pydantic** | Used throughout upstream | `agent/sweforge/schemas.py` — validators enforcing DAG acyclicity, review/finding consistency, repo-relative edit paths, score bounds |
| **pytest** | 267 upstream tests, `tests/conftest.py` (needs full server stack) | `tests_sweforge/` — 478 tests, zero API keys, zero network; `pytest-sweforge.ini` isolates them from upstream's conftest |

## MCP position

An honest note rather than a padded claim.

Upstream already ships working MCP clients and `langchain-mcp-adapters`. Adding a
parallel MCP gateway would be duplication for the sake of a longer technology list,
which the project brief explicitly warns against.

SWE-Forge's contribution around external tools is therefore **orchestration**, not new
transport: the graph decides *when* external capability is consulted, and every call is
counted in `ToolCallLedger` so the cost appears in evaluation. SWE-Forge's own 12 tools
are exposed through the standard LangChain `StructuredTool` interface, which is what
`langchain-mcp-adapters` consumes — so they can be surfaced over MCP without new code.

**Phase 23 update.** SWE-Forge now owns an MCP *orchestration* layer
(`agent/sweforge/mcp/`) reached from the graph's `external_context` node: it
discovers capabilities and schemas over host-supplied adapters, decides
deterministically from the task whether external context is warranted, and
enforces a deny-by-default allowlist, retry policy and call budget.

**Still not claimed:** that SWE-Forge implements an MCP server or transport. It
does not — upstream clients and `langchain-mcp-adapters` provide that. No live
MCP server exists in this environment, so the adapter path is exercised against
the deterministic fixtures in `agent/sweforge/mcp/fixtures.py`, whose payloads
are explicitly labelled `_fixture: true`. No external result is fabricated.


## Phase 23 traceability: requirement to source, symbol, test

Every row was verified by running the named test. No documentation-only claims.

| Requirement | Source file | Symbol | Test | Status |
|---|---|---|---|---|
| Explicit LangGraph workflow | `agent/sweforge/graph/workflow.py` | `build_workflow`, `build_nodes` | `test_graph.py::TestEndToEnd` | IMPLEMENTED |
| Custom state + reducers | `agent/sweforge/state/graph_state.py` | `SWEForgeState`, `_merge_metrics` | `test_core.py::TestState` | IMPLEMENTED |
| Bounded recovery loop | `agent/sweforge/graph/workflow.py` | `make_route_after_failure_analysis` | `test_graph.py::test_recovery_loop_terminates_when_repair_never_works` | IMPLEMENTED |
| Bounded review loop | `agent/sweforge/graph/workflow.py` | `_review_budget_left` | `test_graph.py::test_review_budget_exhaustion_skips_further_review` | IMPLEMENTED |
| Plan-driven agent dispatch | `agent/sweforge/graph/workflow.py` | `implementation` node + `build_agent` | `test_phase23.py::TestDynamicDispatch::test_different_plans_produce_different_execution_paths` | IMPLEMENTED (Phase 23) |
| TestAgent | `agent/sweforge/agents/specialized.py` | `TestAgent`, `TestChanges` | `test_phase23.py::test_test_agent_produces_test_changes` | IMPLEMENTED (Phase 23) |
| BackendAgent | `agent/sweforge/agents/specialized.py` | `BackendAgent`, `BackendChanges` | `test_phase23.py::test_backend_agent_reports_contract_change` | IMPLEMENTED (Phase 23) |
| DatabaseAgent | `agent/sweforge/agents/specialized.py` | `DatabaseAgent`, `MigrationChanges` | `test_phase23.py::test_database_agent_reports_reversibility` | IMPLEMENTED (Phase 23) |
| DocumentationAgent | `agent/sweforge/agents/specialized.py` | `DocumentationAgent`, `DocChanges` | `test_phase23.py::test_documentation_agent_lists_updated_docs` | IMPLEMENTED (Phase 23) |
| SecurityAgent (assesses, never edits) | `agent/sweforge/agents/specialized.py` | `SecurityAgent`, `SecurityAssessment` | `test_phase23.py::test_security_agent_returns_findings_not_edits` | IMPLEMENTED (Phase 23) |
| FrontendAgent | `agent/sweforge/agents/specialized.py` | `FrontendAgent`, `FrontendChanges` | `test_phase23.py::test_build_agent_resolves_role` | IMPLEMENTED (Phase 23) |
| Independent reviewer | `agent/sweforge/agents/roles.py` | `IndependentReviewer` | `test_graph.py::test_reviewer_failure_does_not_approve` | IMPLEMENTED |
| Diagnostician / recovery agent | `agent/sweforge/agents/roles.py` | `Diagnostician` | `test_graph.py::test_diagnostician_returns_repair` | IMPLEMENTED |
| Real LangChain tool calling | `agent/sweforge/agents/tool_loop.py` | `ToolCallingLoop`, `_bind` | `test_phase23.py::TestToolCallingLoop::test_bind_tools_path_executes` | IMPLEMENTED (Phase 23) |
| ToolMessage contract | `agent/sweforge/agents/tool_loop.py` | `ToolCallingLoop.run` | `test_phase23.py::test_tool_message_contract_is_used` | IMPLEMENTED (Phase 23) |
| Tool-calling loop bounded | `agent/sweforge/agents/tool_loop.py` | `max_iterations` | `test_phase23.py::test_loop_is_bounded_by_max_iterations` | IMPLEMENTED (Phase 23) |
| All 12 tools load-bearing | `agent/sweforge/tools/registry.py`, `graph/workflow.py` | `build_tools`, `SWEForgeRuntime.call_tool` | `test_phase23.py::test_all_twelve_tools_exercised_end_to_end` | IMPLEMENTED (Phase 23) |
| Tool ledger provenance | `agent/sweforge/tools/registry.py` | `ToolCall`, `summarize_args` | `test_phase23.py::test_ledger_records_node_and_error_category` | IMPLEMENTED (Phase 23) |
| Tool error policy | `agent/sweforge/tools/errors.py` | `ToolErrorPolicy`, `ToolErrorAction` | `test_phase23.py::TestToolErrorPolicy` | IMPLEMENTED (Phase 23) |
| Execution budgets (hard) | `agent/sweforge/budget.py` | `ExecutionBudget`, `BudgetLimits` | `test_phase23.py::TestExecutionBudget` | IMPLEMENTED (Phase 23) |
| `budget_exhausted` terminal | `agent/sweforge/graph/workflow.py` | `budget_exhausted` node | `test_phase23.py::test_routing_sends_exhausted_run_to_terminal` | IMPLEMENTED (Phase 23) |
| Model retry | `agent/sweforge/routing/execution_policy.py` | `ModelExecutionPolicy.execute` | `test_phase23.py::test_retries_the_same_model_on_timeout` | IMPLEMENTED (Phase 23) |
| Model fallback | `agent/sweforge/routing/execution_policy.py` | `fallback_chain`, `ModelAttempt` | `test_phase23.py::test_falls_back_to_a_different_model` | IMPLEMENTED (Phase 23) |
| Per-attempt model records | `agent/sweforge/routing/execution_policy.py` | `ModelExecutionPolicy.summary` | `test_phase23.py::test_fallback_counted_as_separate_attempts` | IMPLEMENTED (Phase 23) |
| Adaptive model routing | `agent/sweforge/routing/model_router.py` | `ModelRouter.resolve_tier` | `test_core.py::TestModelRouter` | IMPLEMENTED |
| MCP capability registry | `agent/sweforge/mcp/orchestration.py` | `MCPCapabilityRegistry.discover` | `test_phase23.py::test_discovery_exposes_capabilities_and_schemas` | IMPLEMENTED (Phase 23) |
| MCP selection (deterministic) | `agent/sweforge/mcp/orchestration.py` | `MCPToolSelector.select` | `test_phase23.py::test_selector_detects_issue_reference` | IMPLEMENTED (Phase 23) |
| MCP deny-by-default | `agent/sweforge/mcp/orchestration.py` | `MCPInvocationPolicy.invoke` | `test_phase23.py::test_deny_by_default` | IMPLEMENTED (Phase 23) |
| MCP graph node | `agent/sweforge/graph/workflow.py` | `external_context` node | `test_phase23.py::TestMCPOrchestration` | IMPLEMENTED (Phase 23) |
| Repository intelligence (AST) | `agent/sweforge/repository/analyzer.py` | `RepositoryAnalyzer` | `test_subsystems.py::TestRepositoryAnalyzer` | IMPLEMENTED |
| Import graph + queries | `agent/sweforge/repository/graph_index.py` | `RepositoryGraph` | `test_subsystems.py::TestRepositoryGraph` | IMPLEMENTED |
| Verification via tool layer | `agent/sweforge/graph/workflow.py` | `verification` node → `run_validation` | `test_phase23.py::test_all_twelve_tools_exercised_end_to_end` | IMPLEMENTED (Phase 23) |
| Git diff used by reviewer | `agent/sweforge/graph/workflow.py` | `independent_review` → `inspect_git_diff` | `test_phase23.py::test_all_twelve_tools_exercised_end_to_end` | IMPLEMENTED (Phase 23) |
| Failure classifier | `agent/sweforge/recovery/classifier.py` | `FailureClassifier` | `test_subsystems.py::TestFailureClassifier` | IMPLEMENTED |
| Security scanning | `agent/sweforge/security/risk.py` | `SecurityScanner` | `test_core.py::TestSecurityScanner` | IMPLEMENTED |
| Risk gate (deterministic) | `agent/sweforge/security/risk.py` | `RiskEngine.assess` | `test_core.py::TestRiskEngine` | IMPLEMENTED |
| Experience memory (retrieval) | `agent/sweforge/memory/store.py` | `ExperienceStore.retrieve` | `test_core.py::TestExperienceStore` | IMPLEMENTED |
| LangSmith run tracing | `agent/sweforge/observability/tracing.py` | `trace_run` | `test_evaluation.py::TestObservability` | IMPLEMENTED |
| LangSmith node metadata | `agent/sweforge/observability/tracing.py` | `trace_node`, `node_metadata` | `test_phase23.py::TestTraceMetadata` | IMPLEMENTED (Phase 23) |
| Open SWE baseline adapter | `evaluation/baselines/open_swe_baseline.py` | `OpenSWEBaseline.run`, `preflight` | `test_phase23.py::test_adapter_really_invokes_the_upstream_entry_point` | IMPLEMENTED (Phase 23); live run UNAVAILABLE |
| Experiment A / B separation | `evaluation/runner.py`, `evaluation/experiment_b.py` | `variant_configs`, `run_experiment_b` | `test_phase23.py::TestExperimentSeparation` | IMPLEMENTED (Phase 23) |
| Live-model track | `evaluation/live/config.py` | `LiveEvalConfig.from_env` | `test_phase23.py::TestLiveEvaluationConfig` | IMPLEMENTED (Phase 23); run UNAVAILABLE |
| Risk-gated PR preparation | `agent/sweforge/github/finalization.py` | `prepare_pull_request` | `test_phase23.py::TestPullRequestFinalization` | IMPLEMENTED (Phase 23); live GitHub UNAVAILABLE |
| Evaluation metrics | `evaluation/metrics.py` | `aggregate`, `VariantMetrics` | `test_evaluation.py::TestMetrics` | IMPLEMENTED |
| Real-world benchmark repos | — | — | — | **NOT IMPLEMENTED** (toy fixtures only) |

## Files added

### `agent/sweforge/` — the SWE-Forge layer
```
__init__.py                      cli.py                    runner.py
schemas.py                       state/graph_state.py
graph/workflow.py                planning/planner.py
repository/analyzer.py           repository/graph_index.py
agents/roles.py                  verification/backends.py  verification/verifier.py
recovery/classifier.py           security/risk.py
routing/model_router.py          memory/store.py
observability/tracing.py         tools/registry.py         models/scripted.py
```

### `evaluation/` — benchmark and ablation harness
```
scenarios.py   runner.py   evaluator.py   metrics.py
fixtures/{billing,inventory,textutil,pipeline}/
results/results.json
reports/{EVALUATION_REPORT.md,variant_metrics.csv,run_details.csv,summary.json}
```

### `tests_sweforge/` — 478 tests
```
test_core.py         schemas, state reducers, routing policy, memory, risk
test_subsystems.py   AST analysis, graph queries, backends, verifier, classifier, tools
test_graph.py        scripted model, routing functions, bounded loops, planner, agents, e2e
test_evaluation.py   metrics aggregation, report rendering, scenarios, observability
```

### `docs/` and root
```
docs/UPSTREAM_AUDIT.md   docs/ARCHITECTURE.md   docs/LANGCHAIN_LANGGRAPH.md
docs/CUSTOMIZATIONS.md   docs/EVALUATION.md     docs/SECURITY.md   docs/DEMO.md
.env.example             pytest-sweforge.ini
```

## Upstream files modified

**One**, additively:

| File | Change | Why |
|---|---|---|
| `.gitignore` | Appended `!.env.example` and `.sweforge/` | Upstream's `.env.*` rule silently swallowed the placeholder template, which must be committed; experience-memory artefacts are machine-local run history |

No upstream Python module, test, configuration, entry point or license was edited or
deleted. `LICENSE` retains `Copyright (c) LangChain, Inc.` unchanged. Verify with
`git status --porcelain`.
