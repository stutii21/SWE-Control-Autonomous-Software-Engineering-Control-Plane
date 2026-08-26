# Resume Claims

Only the strongest **defensible** claims, each traceable to source, a test, and a
result. Anything requiring credentials that do not exist is confined to
section D and is explicitly marked as infrastructure, not a result.

Derived from `docs/PROJECT_CLAIMS.md`. If a claim is not here, do not use it.

---

## A. Architecture claims

### A1 — Deterministic orchestration layer over an existing autonomous SWE system
**CLAIM** Designed and built an explicit LangGraph control plane (17 nodes,
5 conditional routers, 4 terminal nodes, 2 bounded loops) on top of LangChain's
Open SWE, replacing prompt-emergent control flow with a deterministic state
machine — reusing upstream sandboxing, middleware and integrations rather than
reimplementing them.
**EVIDENCE** Source audit found upstream has no hand-authored domain `StateGraph`
(across 420 upstream Python files the only one is a cron scheduler).
**SOURCE** `agent/sweforge/graph/workflow.py`, `state/graph_state.py`
**TEST** `tests_sweforge/test_invariants.py::TestGraphInvariants`
**RESULT** Registered in `langgraph.json` as `sweforge`; resolves, compiles and
is inspectable. Upstream's 5 graph entries preserved.

### A2 — Architecture-level invariants, not just unit tests
**CLAIM** Proved architectural safety properties as executable invariants:
HIGH-risk changes can never auto-finalize, recovery cannot exceed its bound,
every node is reachable, terminal nodes route only to END, and no
structured-output field can reach a budget limit.
**SOURCE** `agent/sweforge/graph/workflow.py`, `budget.py`, `security/risk.py`
**TEST** `test_invariants.py` — 40 tests
**RESULT** 40/40 passing; HIGH risk verified blocked at scores 55/70/90/100.

### A3 — Plan-driven multi-agent dispatch
**CLAIM** Implemented 9 agent classes (6 specialized) dispatched from a validated
plan, so different plans execute different code — with distinct system contracts,
Pydantic output schemas, model tiers and tool grants per role.
**EVIDENCE** 6/6 distinct prompts, 6/6 distinct output models, 5 distinct tool
grants, 4 distinct model roles.
**SOURCE** `agent/sweforge/agents/specialized.py`
**TEST** `test_phase23.py::TestSpecializedAgents`,
`test_different_plans_produce_different_execution_paths`
**RESULT** A backend plan and a documentation plan produce different
`agents_executed`.

### A4 — Real LangChain tool-calling, with 12 load-bearing tools
**CLAIM** Implemented the genuine `bind_tools` → `tool_calls` → `ToolMessage` →
structured-output loop (bounded), with all 12 tools exercised in a single
end-to-end run and every call ledgered with node/agent provenance.
**SOURCE** `agents/tool_loop.py`, `tools/registry.py`
**TEST** `test_all_twelve_tools_exercised_end_to_end`, `TestToolCallingLoop`
**RESULT** Tool calls per variant rose 0/12/24/24/56 → 12/108/130/138/170 with
outcomes unchanged — evidence the tools are wired in, not decorative.

### A5 — Hard execution budgets an LLM cannot negotiate
**CLAIM** Enforced 8 resource limits (model calls, tool calls, input/output
tokens, cost, wall time, recovery attempts, review cycles), checked before every
expensive operation, with `budget_exhausted` as an explicit terminal state.
**SOURCE** `agent/sweforge/budget.py`
**TEST** `TestExecutionBudget` (478 tests)
**RESULT** All 8 verified to raise `BudgetExceeded`; a test asserts no
structured-output field name collides with a limit name.

### A6 — Model retry and cross-tier fallback (distinct from tier escalation)
**CLAIM** Implemented per-operation retry then fallback to a genuinely different
model, recording each attempt separately; non-retryable failures spend no
fallback, and budget errors propagate rather than consuming the chain.
**SOURCE** `routing/execution_policy.py`
**TEST** `TestModelExecutionPolicy` (478 tests)
**RESULT** 3 retries on the reasoning tier → success on the coding tier;
2 distinct models; non-retryable failure tried exactly 1 model.

---

## B. Measured experimental claims

All deterministic, benchmark v1.0.0, seed 0, scripted model.

### B1 — Component ablation quantifying each architectural decision
**CLAIM** Built a 5-variant ablation (30 runs) isolating each orchestration
component; bounded self-repair raised task success **50% → 83%**.
**SOURCE** `evaluation/runner.py`, `evaluation/scenarios.py`
**TEST** `test_evaluation.py`, `test_experiments.py`
**RESULT** 30/30 runs, 0 unavailable, **6/6 routing correctness**. Outcomes
reported separately (completed / awaiting_human / escalated / failed) so the
security gate is never mistaken for a task failure.

### B2 — A security gate that blocks a change every other variant shipped
**CLAIM** Deterministic risk engine scored a functionally-correct,
tests-green change at **90/100 HIGH** (committed credential + CI workflow edit)
and routed it to human approval; variants A–D all shipped it.
**SOURCE** `agent/sweforge/security/risk.py`
**TEST** `test_core.py::TestRiskEngine`, `test_invariants.py::TestSecurityInvariants`
**RESULT** Scenario `pipeline_secret_risk_gate` → `awaiting_human_approval`.

### B3 — Reproducibility verified, not asserted
**CLAIM** Made the evaluation reproducible with run manifests (commit SHA,
package versions, seed, benchmark version) and verified that repeated runs are
byte-identical on terminal state, routing path, recovery count and tool sequence.
**SOURCE** `evaluation/reproducibility.py`, `runner.py --repeat`
**TEST** `test_phase25.py::TestReproducibility`
**RESULT** **3/3 repeats IDENTICAL**, reconfirmed in a clean virtual environment.

### B4 — Measured components the end-to-end harness could not
**CLAIM** When the ablation could not observe repository intelligence, memory or
routing, built direct measurements rather than asserting benefit.
**SOURCE** `evaluation/experiments.py`
**TEST** `test_experiments.py` (478 tests)
**RESULT** Graph retrieval **R@5 1.0 vs 0.875** lexical (n=4, no LLM);
failure detection **10/10** categories; adaptive routing at **40% the cost** of a
fixed reasoning-tier model on an identical call pattern (n=4).

### B5 — Found and fixed a real defect through measurement
**CLAIM** The recovery matrix exposed a classifier regex ending in `\b` after an
apostrophe — a word boundary that can never match — causing
`KeyError: 'DATABASE_URL'` to be misclassified as a runtime error.
**SOURCE** `agent/sweforge/recovery/classifier.py`
**TEST** `test_experiments.py::TestConfigurationRuleRegression` (478 tests)
**RESULT** Detection accuracy 9/10 → **10/10**.

### B6 — Published negative results
**CLAIM** Reported that repository intelligence, memory and model routing have
**no measured end-to-end task-success effect**, and that retrieval precision
saturates at 1.0 because the fixtures are too easy to discriminate.
**SOURCE** `docs/EVALUATION.md` §5, `docs/PROJECT_CLAIMS.md`
**TEST** `test_docs_consistency.py::test_negative_results_are_retained`
**RESULT** Variants A and B are identical (50%/50%) — retained, not removed.

---

## C. Infrastructure claims

### C1 — Test suite requiring no credentials or network
**CLAIM** 477 tests covering schemas, graph topology, agents, tools, budgets,
security invariants, experiments and documentation consistency — no API key, no
network.
**TEST** `python -m pytest -c pytest-sweforge.ini`
**RESULT** **477 passed**; lint, format and mypy clean; validated in a clean venv.

### C2 — Observability that does not depend on a vendor
**CLAIM** Every run emits a local `traces.jsonl` (nodes, tools, agents, recovery,
budget, risk, final status) with write-time secret redaction; LangSmith is an
optional additional sink.
**SOURCE** `agent/sweforge/observability/trace.py`
**TEST** `test_invariants.py::TestLocalTrace`
**RESULT** 30+ events per showcase run with LangSmith disabled.

### C3 — Documentation drift made a build failure
**CLAIM** Built a checker that derives ground truth from source (graph topology,
tool registry, pytest collection) and fails CI when documentation disagrees —
after an audit found a test-count badge stale since an earlier phase.
**SOURCE** `evaluation/check_docs.py`
**TEST** `test_docs_consistency.py` (478 tests)
**RESULT** **13/13 documentation checks passing.**

### C4 — Credential-free CI
**CLAIM** GitHub Actions workflow running lint, format, mypy, tests, import
isolation, secret scan, graph-registration check, deterministic evaluation and
benchmark dry-run — with live evaluation isolated in a separate, manually
triggered, approval-gated workflow.
**SOURCE** `.github/workflows/sweforge.yml`, `sweforge-live.yml`
**RESULT** Workflows parse; **all steps executed locally**. Not yet observed on a
GitHub runner.

---

## D. Live-validation claims — INFRASTRUCTURE ONLY

**These are not results.** Each is implemented and tested; none has executed.
Describe them as *built*, never as *measured*.

| Capability | Honest phrasing | Status |
|---|---|---|
| Open SWE head-to-head | "Built a baseline adapter that resolves the genuine upstream `agent.server.get_agent`; head-to-head execution requires model and sandbox credentials." | `comparable_pairs = 0` |
| Real-world benchmark | "Built a SWE-bench-Lite-format harness (schema, loader, dry-run, paired scorer) whose scorer refuses a verdict below 20 paired tasks." | `NOT_AVAILABLE` |
| Live-model evaluation | "Built a configurable live-model track with hard cost ceilings; fails clearly with `LIVE_EVALUATION_UNAVAILABLE` when unconfigured." | UNAVAILABLE |
| Paired statistics | "Implemented McNemar and bootstrap CI (seed 0) for paired comparison." | Untriggered |
| MCP | "Built MCP orchestration with deny-by-default allowlist, tested against deterministic fixtures." | No live server |

**Never claim:** that SWE-Forge outperforms Open SWE, any SWE-bench score, any
live-model improvement, or that routing/memory/repository intelligence improves
task success.
