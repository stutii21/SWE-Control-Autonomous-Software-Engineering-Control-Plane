# Phase 23 — Implementation Gap Audit

> **HISTORICAL DOCUMENT.** This is point-in-time evidence from an earlier phase,
> retained for traceability. Numbers here reflect the repository *at that time*
> and may not match current state. For current, source-verified figures see
> `docs/FINAL_PROJECT_STATUS.md` and `docs/PROJECT_CLAIMS.md`.

**Method:** direct source and test inspection of the repository as it stands. README and
prior documentation claims were treated as *unverified assertions* and checked against
code. Where a claim did not survive inspection, it is marked below and the documentation
is corrected as part of remediation.

**Bar applied.** A requirement counts as IMPLEMENTED only if all five hold:
reachable in a real workflow · actually invoked · has tests · its output affects
execution · documentation matches reality. A class existing is not implementation.

**Audit commands used** (reproducible):

```bash
# tool usage outside the registry that defines them
for t in analyze_repository build_repository_graph find_relevant_files find_dependencies \
         find_callers find_related_tests run_validation inspect_git_diff analyze_failure \
         retrieve_similar_tasks calculate_change_risk security_scan; do
  echo "$t: $(grep -rn "\"$t\"" agent/sweforge --include=*.py | grep -v 'tools/registry.py' | wc -l)"
done

grep -rn "bind_tools\|ToolMessage" agent/sweforge --include=*.py      # → 0 hits
grep -rln "MCP" agent/sweforge --include=*.py                        # → 0 files
grep -rn "OpenSWESandboxBackend" --include=*.py .                    # → 0 instantiations, 0 tests
grep -rn "selected_agents" agent/sweforge --include=*.py             # → only cli.py display
grep -rn "create_deep_agent\|agent.server" evaluation/ --include=*.py # → 0 hits
```

---

## Summary verdict

| # | Requirement | Status |
|---|---|---|
| 1 | Open SWE baseline comparison | **NOT IMPLEMENTED** (misdescribed) |
| 2 | Real LLM execution | **PARTIAL** (code path exists, never executed) |
| 3 | Dynamic multi-agent execution | **NOT IMPLEMENTED** (3 classes for 8 roles) |
| 4 | Dynamic graph construction | **PARTIAL** (flag-time only, not plan-driven) |
| 5 | LangChain tool usage | **PARTIAL** (5 of 12 used; 7 dead) |
| 6 | MCP integration | **NOT IMPLEMENTED** (zero SWE-Forge code) |
| 7 | Model fallback | **NOT IMPLEMENTED** (tier escalation ≠ fallback) |
| 8 | Model retry | **NOT IMPLEMENTED** |
| 9 | Execution budgets | **NOT IMPLEMENTED** (accounting only, no enforcement) |
| 10 | Tool-error handling | **PARTIAL** (uniform `{"ok": false}`, no classification) |
| 11 | LangSmith tracing | **PARTIAL** (run-level only, no node/agent metadata) |
| 12 | Request metadata | **PARTIAL** (5 keys at run root only) |
| 13 | Repository-intelligence tool usage | **PARTIAL** (1 of 6 graph tools used) |
| 14 | Verification-tool usage | **NOT LOAD-BEARING** (`run_validation` never called) |
| 15 | Git-diff-tool usage | **NOT LOAD-BEARING** (`inspect_git_diff` never called) |
| 16 | Test-discovery-tool usage | **NOT LOAD-BEARING** (`find_related_tests` never called) |
| 17 | GitHub/PR integration | **NOT IMPLEMENTED** |
| 18 | Real-world benchmark capability | **NOT IMPLEMENTED** (4 toy fixtures) |
| 19 | Evaluation methodology | **IMPLEMENTED** (strongest existing area) |
| 20 | Security/risk integration | **IMPLEMENTED** |

**14 of 20 requirements are not implemented at the required depth.** The prior phase
produced a sound architectural skeleton with an honest evaluation harness, but several
headline capabilities were documentation rather than code.

---

## Detailed audit

| Requirement | Current implementation | Evidence | Status | Remediation |
|---|---|---|---|---|
| **1. Open SWE baseline comparison** | `variant_configs()["A_baseline"]` is a stripped-down *SWE-Forge* graph (`WorkflowConfig.baseline()` disables flags). It never invokes upstream. | `grep -rn "create_deep_agent\|agent.server" evaluation/` → **0 hits**. `evaluation/runner.py:107` builds `WorkflowConfig`, not an upstream agent. | **NOT IMPLEMENTED** — and previously *misdescribed*: `docs/EVALUATION.md` §3 called A "the fixed single-agent comparison", implying an Open SWE baseline. | Add `evaluation/baselines/open_swe_baseline.py` invoking the real `agent.server.get_agent` path; mark UNAVAILABLE with a reason when deps/credentials absent. Split into Experiment A (ablation) and Experiment B (system baseline). |
| **2. Real LLM execution** | `ModelRouter.build_model` calls `init_chat_model`; `cli.py run` exists. | `agent/sweforge/routing/model_router.py:236`. Never executed — no credentials in any environment used. All 237 tests use `ScriptedModelFactory`. | **PARTIAL** — code path plausible but unexercised. | Add `evaluation/live/` track with config-from-env, and tests that verify the *configuration and unavailability* path without credentials. |
| **3. Dynamic multi-agent execution** | 8 roles in the `AgentRole` Literal; **3** agent classes exist. | `grep -n "^class " agent/sweforge/agents/roles.py` → `ImplementationAgent`, `IndependentReviewer`, `Diagnostician`. `workflow.py:~265` constructs **one** `ImplementationAgent` and runs every subtask through it regardless of `subtask.agent`. `test_agent`, `backend_agent`, `frontend_agent`, `database_agent`, `documentation_agent`, `security_agent` all execute the identical prompt. | **NOT IMPLEMENTED** — role names are a Pydantic enum, not behaviour. | Implement `TestAgent`, `BackendAgent`, `FrontendAgent`, `DatabaseAgent`, `DocumentationAgent`, `SecurityAgent` with distinct system contracts, structured outputs, tool sets and model tiers. Dispatch on `subtask.agent`. |
| **4. Dynamic graph construction** | `build_workflow` varies topology by `WorkflowConfig` flags. Plan content does not affect the path. | `workflow.py:build_workflow` reads only `config.*`. No routing function reads `plan.subtasks[].agent`. | **PARTIAL** — ablation-time variation is real; plan-driven variation is absent. | Add an agent-dispatch node/router driven by the validated plan, so different plans produce different execution paths. Keep control flow deterministic. |
| **5. LangChain tool usage** | 12 `StructuredTool`s defined and unit-tested; **5** invoked by the graph. | Non-registry reference counts: `find_relevant_files` 1, `analyze_failure` 1, `retrieve_similar_tasks` 1, `calculate_change_risk` 1, `security_scan` 1; **`analyze_repository` 0, `build_repository_graph` 0, `find_dependencies` 0, `find_callers` 0, `find_related_tests` 0, `run_validation` 0, `inspect_git_diff` 0**. | **PARTIAL** — 7 dead demonstration tools, which the brief explicitly forbids. | Wire all 12 into the workflow at the architecturally correct point. |
| **6. MCP integration** | None. `docs/CUSTOMIZATIONS.md` honestly stated MCP was *not* implemented, so this is a gap against the spec rather than a false claim. | `grep -rln "MCP" agent/sweforge` → **0 files**. | **NOT IMPLEMENTED** | Add `agent/sweforge/mcp/` with `MCPCapabilityRegistry`, `MCPToolSelector`, `MCPInvocationPolicy` (allowlist, timeout, budget, structured errors) plus a deterministic in-process fixture server. Reuse upstream adapters; build no transport. |
| **7. Model fallback** | Only *tier escalation* after 2 recorded role failures. No per-call fallback. | `model_router.py:resolve_tier` escalates on `_role_failures`; a raised provider error propagates out of `track()` and is caught by callers as total failure (`planner.py:183`). No alternate model is attempted. | **NOT IMPLEMENTED** — escalation and fallback were conflated. | Add `ModelExecutionPolicy` (primary + ordered fallbacks, retryable exception set, backoff, timeout) recording each attempt separately. |
| **8. Model retry** | None. | No retry loop anywhere in `agent/sweforge/`. | **NOT IMPLEMENTED** | Bounded retry inside `ModelExecutionPolicy`. |
| **9. Execution budgets** | `ModelUsageLedger` *accounts* for cost/tokens; nothing enforces a limit. Only recovery/review loops are bounded. | `model_router.py` ledger has no `check()`. `grep -n "ExecutionBudget"` → 0. | **NOT IMPLEMENTED** — "execution budgets" in `docs/LANGCHAIN_LANGGRAPH.md` §2.3 overstated `track()`, which only measures. | Add `ExecutionBudget` with hard limits and a `budget_exhausted` terminal state, checked before every expensive op. |
| **10. Tool-error handling** | `_guard` converts every exception to `{"ok": false, "error": str}`. | `tools/registry.py:_guard`. No classification, no retry, no escalation; a validation error and a transient timeout are indistinguishable. | **PARTIAL** | Add `ToolErrorPolicy` classifying validation/timeout/permission/not_found/transient/permanent → retry/fallback/skip/escalate, bounded. |
| **11. LangSmith tracing** | `trace_run` wraps a whole run with 5 metadata keys. No per-node/agent/model/attempt metadata. | `runner.py:185` metadata = variant, repository, 3 flags, tracing. `observability/tracing.py` has no node-level helper. | **PARTIAL** | Add span metadata for task_id, node, agent, model, tier, attempt, recovery attempt, tool, budget remaining, risk score, final status. |
| **12. Request metadata** | `run_metadata()` exists and is **never called**. | `grep -rn "run_metadata" agent/sweforge` → defined in `tracing.py`, used only in tests. | **PARTIAL** | Use it; attach per-node metadata. |
| **13. Repository-intelligence tool usage** | Graph calls `find_relevant_files` only. `find_dependencies`/`find_callers`/`find_related_tests`/`analyze_repository`/`build_repository_graph` unused by the workflow; `repository_analysis` calls `RepositoryAnalyzer` directly. | See row 5 counts. `workflow.py:~230`. | **PARTIAL** | Build a full evidence chain: analyze → graph → relevant files → deps/callers → related tests → planning. |
| **14. Verification-tool usage** | `verification` node calls `runtime.verifier.verify()` directly; the `run_validation` tool is never invoked outside tests. | `workflow.py:~330`; `run_validation` non-registry refs = 0. | **NOT LOAD-BEARING** | Route verification through the tool; keep `Verifier` as the engine beneath it. |
| **15. Git-diff-tool usage** | Reviewer receives `_render_change_summary(runtime.written_files)` — an in-memory dict, not a diff. `inspect_git_diff` never invoked. | `workflow.py:_render_change_summary`; tool refs = 0. | **NOT LOAD-BEARING** | Reviewer must obtain the change via `inspect_git_diff`, falling back to written-file rendering only when git is unavailable (fixtures are not git repos). |
| **16. Test-discovery-tool usage** | `Verifier.build_plan` calls `graph.find_tests_for_file` directly; the tool is unused. | `verification/verifier.py:~95`. | **NOT LOAD-BEARING** | Invoke via tool in the evidence chain; keep the direct call inside the verifier engine. |
| **17. GitHub/PR integration** | None in SWE-Forge. Terminal states describe PR *eligibility* but nothing prepares one. | `ls agent/sweforge/github` → does not exist. | **NOT IMPLEMENTED** | Add `agent/sweforge/github/finalization.py::prepare_pull_request()` gated by risk level, mock-tested, live path marked unavailable. |
| **18. Real-world benchmark capability** | 4 single-module toy fixtures (billing, inventory, textutil, pipeline). | `evaluation/fixtures/`. | **NOT IMPLEMENTED** | Out of scope to fix credibly this phase; document explicitly as a limitation rather than implying otherwise. |
| **19. Evaluation methodology** | 30-run ablation, fixture isolation, honest denominators (`n/a`), synthetic metrics labelled, negative result published, 6/6 routing check. | `evaluation/{runner,evaluator,metrics}.py`; 237 tests pass. | **IMPLEMENTED** | Keep intact. Add Experiment B and stronger metrics alongside. |
| **20. Security/risk integration** | `SecurityScanner` + `RiskEngine` deterministic, wired into `security_analysis`/`risk_gate`, drives `awaiting_human_approval`, measured blocking a credential at 90/100. | `security/risk.py`; `workflow.py` nodes; scenario `pipeline_secret_risk_gate`. | **IMPLEMENTED** | Keep. Extend with budget/PR-gate interaction. |

---

## Additional defects found (not in the requested list)

| Defect | Evidence | Action |
|---|---|---|
| `OpenSWESandboxBackend` is **untested dead code** — never instantiated anywhere, including tests. Yet it is documented as "the production path". | `grep -rn "OpenSWESandboxBackend" --include=*.py .` → only its own definition and a docstring/error string. 0 tests. | Add duck-typed unit tests against a fake sandbox object; soften the doc claim to "adapter, exercised only against a live Open SWE thread". |
| Stray directory `evaluation/{tasks,fixtures,results,reports}` and empty `evaluation/tasks/` | Artefacts of an earlier shell brace-expansion failure. | Removed during this audit (empty, untracked). |
| `docs/EVALUATION.md` §6.3 table headed "Fixed single-agent vs adaptive workflow" | Compares A_baseline vs E_full — both SWE-Forge. Not a single-agent system. | Retitle to "stripped SWE-Forge vs full SWE-Forge"; reserve system-baseline language for Experiment B. |

---

## Remediation plan (priority order)

Sequenced by how much each closes the gap between *claimed* and *actual* differentiation.
Where a requirement cannot be honestly completed in this environment, it will be marked
UNAVAILABLE with a stated reason rather than faked.

1. **Specialized agents + plan-driven dispatch** (rows 3, 4) — the largest credibility gap.
2. **Make all 12 tools load-bearing, with real `bind_tools` tool-calling for agent roles**
   (rows 5, 13–16).
3. **`ExecutionBudget` as hard limits + `budget_exhausted` terminal state** (row 9).
4. **`ModelExecutionPolicy`: retry + fallback with per-attempt records** (rows 7, 8).
5. **`ToolErrorPolicy` classification** (row 10).
6. **Open SWE baseline adapter; split Experiment A / Experiment B** (row 1).
7. **MCP orchestration layer + deterministic fixture server** (row 6).
8. **LangSmith node/agent metadata** (rows 11, 12).
9. **GitHub PR finalization adapter, mock-tested** (row 17).
10. **Live-model evaluation track, unavailable without credentials** (row 2).
11. **Retrieval comparison A/B/C with precision@k, recall@k, latency** (row 11 of spec).
12. **Documentation corrections** so every claim matches code (all rows).

Known constraints for this phase, stated up front:

- **No LLM credentials** exist in this environment, so the live track and any real-model
  comparison will be implemented and then **marked UNAVAILABLE**. No real-model number
  will be reported.
- **Upstream Open SWE cannot execute here** without `fastapi`, `deepagents`, provider
  SDKs, a GitHub App and a sandbox provider. The baseline adapter will invoke the genuine
  upstream import path and report precisely which dependency or credential is missing.
- **Real-world benchmarks** (SWE-bench-style) remain out of scope; the fixtures stay toy
  fixtures and will be documented as such.
