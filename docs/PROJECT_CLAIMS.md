# Project Claims Ledger

Internal accuracy record. Every claim is sorted into exactly one of three
buckets. If a claim is not in **VERIFIED CLAIMS**, it must not appear as a
result in a README, a report, or a CV.

Last validated: Phase 25. Test suite: **478 passing**.

---

## VERIFIED CLAIMS

Backed by executed source, tests and results in this repository.

### Architecture
| Claim | Evidence |
|---|---|
| Explicit LangGraph `StateGraph` with 17 domain nodes, 5 conditional routers, 4 terminal nodes | `describe_graph()`; `test_invariants.py::TestGraphInvariants` |
| Graph registered in `langgraph.json` alongside 5 upstream graphs, resolves and compiles | `test_phase25.py::TestGraphRegistration` (478 tests) |
| Every non-terminal node has an outgoing edge; every node reachable from START; terminals route only to END | `TestGraphInvariants` |
| Recovery loop bounded; verified at limits 0, 1, 3, 5 | `test_recovery_can_never_exceed_its_bound` |
| Review loop bounded | `test_review_loop_cannot_run_forever` |
| 9 agent classes; 6/6 distinct prompts, 6/6 distinct output models, 5 tool grants, 4 model roles | `test_phase23.py::TestSpecializedAgents` |
| Plan-driven dispatch: different plans execute different agents | `test_different_plans_produce_different_execution_paths` |
| Real `bind_tools` → `tool_calls` → `ToolMessage` → structured output | `TestToolCallingLoop` (478 tests) |
| All 12 LangChain tools exercised in one end-to-end run | `test_all_twelve_tools_exercised_end_to_end` |
| 8 hard execution budgets enforced; `budget_exhausted` terminal state | `TestExecutionBudget` (478 tests) |
| Model retry then fallback to a *different* model; non-retryable failures spend no fallback | `TestModelExecutionPolicy` (478 tests) |
| MCP orchestration with deny-by-default allowlist, retry, per-run cap, budget | `TestMCPOrchestration` (478 tests) |
| Local trace artifact exists with LangSmith disabled (30 events per showcase run) | `TestLocalTrace`; `traces.jsonl` |
| Secrets redacted from traces and observability metadata | `test_secrets_are_redacted_from_local_traces` |

### Safety
| Claim | Evidence |
|---|---|
| HIGH risk can never auto-finalize (verified at scores 55/70/90/100) | `test_high_risk_can_never_auto_finalize` |
| An approving reviewer cannot release a HIGH-risk change | `test_model_output_cannot_lower_the_risk_gate` |
| No structured-output field maps to a budget limit | `test_budget_limits_are_not_reachable_from_model_output` |
| MCP deny-by-default resists name-variation bypass attempts | `test_mcp_deny_by_default_cannot_be_bypassed_by_model_text` |
| Host execution refused without explicit opt-in | `test_untrusted_execution_is_refused_without_explicit_optin` |
| PR preparation cannot bypass the risk gate | `test_pr_preparation_cannot_bypass_the_risk_gate` |
| Secret scan: 0 non-PEM matches across the SWE-Forge tree | Phase 24/25 scans |

### Measured results (deterministic, benchmark v1.0.0)
| Claim | Value | Evidence |
|---|---|---|
| Experiment A ablation, 30 runs, 0 unavailable | see `docs/EVALUATION.md` | `evaluation/results/results.json` |
| Bounded self-repair raises task success 50% → 83% | +33pp | Experiment A, variants A→C |
| Risk gate blocks a credential-committing change every other variant shipped | 90/100 HIGH | scenario `pipeline_secret_risk_gate` |
| Graph routing correctness | 6/6 | `evaluation/reports/summary.json` |
| Repeated identical runs produce identical outcomes | 3/3 repeats IDENTICAL | `--repeat 3` signature check |
| Repository intelligence: graph/hybrid retrieval R@5 = 1.0 vs lexical 0.875 | +0.125 R@5 | `run_retrieval_experiment`, n=4 |
| Failure-classifier detection accuracy | 10/10 categories | `run_recovery_matrix` |
| Adaptive routing costs 40% of a fixed reasoning-tier model on an identical call pattern | ratio 0.40 | `run_routing_experiment`, n=4 |
| Memory retrieves relevant prior experience for related tasks | 2/2 top-1 relevant | `run_memory_experiment`, n=2 |
| Import isolation: 41 modules load without `fastapi`/`deepagents` | 0 failures | CI step + Phase 24/25 |

---

## IMPLEMENTED BUT NOT LIVE-VALIDATED

Infrastructure exists, is tested, and is reachable — but requires credentials or
external services to execute. **None of these may be reported as results.**

| Capability | Implemented | Blocker |
|---|---|---|
| Open SWE head-to-head (Experiment B) | `evaluation/baselines/open_swe_baseline.py`; adapter resolves the genuine `agent.server.get_agent` | Model **and** sandbox credentials. `comparable_pairs = 0` |
| Live-model evaluation (Experiment C) | `evaluation/live/config.py`, manual CI workflow | No provider credential → `LIVE_EVALUATION_UNAVAILABLE` |
| Real-world benchmark (SWE-bench Lite format) | `evaluation/benchmarks/harness.py`: schema, loader, dry-run, paired scorer | Nothing downloaded or executed. `REAL_WORLD_BENCHMARK = NOT_AVAILABLE` |
| Paired statistics (McNemar, bootstrap CI) | `_paired_statistics` | Needs ≥20 paired tasks; scorer returns `INSUFFICIENT_SAMPLE` |
| MCP against a live server | `mcp/orchestration.py` | No MCP server; exercised against fixtures labelled `_fixture: true` |
| GitHub PR creation | `github/finalization.py` | No App installation; mock-tested only |
| Open SWE sandbox backend | `verification/backends.py::OpenSWESandboxBackend` | Requires a live Open SWE thread |
| End-to-end effect of repo intelligence / memory / routing on task success | components measured directly | Requires live models; **explicitly untested** |

---

## NOT IMPLEMENTED

| Item | Note |
|---|---|
| Real-world benchmark **results** | Harness only. No public benchmark has been run. |
| Live-model **results** | No live run has occurred. |
| Open SWE vs SWE-Forge **performance comparison** | Never executed; no such claim exists anywhere. |
| TypeScript/multi-language AST analysis | Python only; other languages inventoried, not parsed. |
| Semantic code understanding | Static analysis only; stated in `docs/ARCHITECTURE.md`. |
| Aliased/dynamic call-site resolution | `find_callers` resolves definitions and importers only. |
| Empirically fitted risk weights | Hand-tuned judgement. |
| Measured concurrency speedup | Isolation tested; **no speedup measured or claimed**. |
| Statistical significance for Experiment A | n=6 scenarios; deterministic architectural verification, not a statistical study. |
