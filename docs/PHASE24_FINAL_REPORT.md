# Phase 24 — Final Report

> **HISTORICAL DOCUMENT.** This is point-in-time evidence from an earlier phase,
> retained for traceability. Numbers here reflect the repository *at that time*
> and may not match current state. For current, source-verified figures see
> `docs/FINAL_PROJECT_STATUS.md` and `docs/PROJECT_CLAIMS.md`.

All values below were produced by executing code in this repository. Nothing is
estimated, extrapolated or illustrative. Every capability that could not be executed is
marked **UNAVAILABLE** with the exact blocker.

---

## 1. What Phase 23 fixed

The Phase 23 audit (`docs/PHASE23_GAP_AUDIT.md`) found **14 of 20 requirements** not
implemented at the required depth. The prior phase had produced a sound skeleton with an
honest evaluation harness, but several headline capabilities were documentation rather
than code:

| Gap found by source audit | Remediation |
|---|---|
| 8 agent roles in a Pydantic enum, **3** classes; all ran the same prompt | 6 specialized agent classes with distinct prompts, outputs, tiers and tool grants |
| `selected_agents` consumed only by `cli.py` **for display** | Plan-driven dispatch: `subtask.agent` selects the concrete class |
| **7 of 12 tools dead** (zero non-registry references) | All 12 wired into the workflow at architecturally correct points |
| Zero `bind_tools`, zero `ToolMessage` | `ToolCallingLoop` implements the real contract |
| Budgets were accounting, not enforcement | `ExecutionBudget`: 8 hard limits + `budget_exhausted` terminal state |
| Tier escalation conflated with model fallback | `ModelExecutionPolicy`: retry then cross-tier fallback, per-attempt records |
| Zero MCP code in SWE-Forge | `mcp/orchestration.py` + deterministic fixtures |
| `A_baseline` was a stripped **SWE-Forge** graph, described as an Open SWE baseline | Real `OpenSWEBaseline` adapter; Experiments A and B separated |
| `OpenSWESandboxBackend` untested dead code | Documented honestly; adapter tested by duck-typing |

## 2. What Phase 24 verified

Each item was **executed**, not asserted.

| # | Verification | Result |
|---|---|---|
| 1 | `docs/EVALUATION.md` no longer conflates ablation with Open SWE baseline | **PASS** — 0 occurrences of "fixed single-agent"; §0 taxonomy added |
| 7 | Agents do not collapse into one prompt | **PASS** — 6/6 distinct prompts, 6/6 distinct output models, 5 tool grants, 4 model roles, 9 classes total |
| 8 | Real `bind_tools` → `tool_calls` → `ToolMessage` → continuation | **PASS** — captured full message sequence, 3 iterations, 2 tools |
| 9 | All 8 budget limits stop execution | **PASS** — every limit raised `BudgetExceeded` |
| 10 | Retry then fallback to a *different* model | **PASS** — 3 retries on `opus-4-1`, fallback succeeded on `sonnet-4-5`; non-retryable tried 1 model only; budget propagated |
| 11 | MCP end-to-end through the graph | **PASS** — selection → invocation → workflow state; deny-by-default, timeout, retry, budget all verified |
| 12 | Secret scan | **PASS** — 0 non-PEM matches across 107 files |
| 13 | Quality gate | **PASS** — see §3 |

## 3. Final test count and quality gate

| Gate | Command | Result |
|---|---|---|
| Tests | `python -m pytest -c pytest-sweforge.ini` | **358 passed**, ~47 s, 0 failures, 0 warnings |
| Lint | `python -m ruff check agent/sweforge evaluation tests_sweforge` | **All checks passed** |
| Format | `python -m ruff format --check ...` | **67 files already formatted** |
| Type check | `mypy --ignore-missing-imports --explicit-package-bases` on 6 core modules | **Success: no issues found** |
| Import isolation | 41 importable `agent.sweforge.*` modules with `fastapi`/`deepagents` blocked | **0 failures** |
| Secret scan | 107 files, 10 credential patterns | **0 non-PEM matches** |

Test growth: 237 (Phase 22) → 330 → 357 (Phase 23) → **358** (Phase 24). No test was
removed or weakened.

Two real defects were found *by* Phase 24 validation and fixed:

1. `test_resolve_agent_factory_targets_real_upstream_symbol` was **environment-dependent**
   — it asserted an `ImportError` that stopped occurring once upstream deps were
   installed. Rewritten to assert the environment-independent invariant: the adapter
   resolves `agent.server.get_agent` or raises, and never substitutes a SWE-Forge symbol.
2. Three genuine `mypy` errors in the tier-escalation maps (`str` where `Tier` was
   required). Fixed by annotating `dict[Tier, Tier]` and removing three now-unnecessary
   `# type: ignore` comments.

## 4. Final deterministic evaluation (Experiment A)

30 executions, 6 scenarios × 5 cumulative variants, 0 unavailable, 54.7 s.

| Variant | Task success | Verification pass | First-attempt | Recovery success | Avg attempts | Escalations | Human gate | Nodes | Model calls | Tool calls | Avg wall (s) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A_baseline | 50% | 50% | 50% | n/a | n/a | 0 | 0 | 54 | 18 | 12 | 1.154 |
| B_repo_intel | 50% | 50% | 50% | n/a | n/a | 0 | 0 | 54 | 18 | 108 | 1.105 |
| C_recovery | **83%** | **83%** | 50% | 67% | 1.67 | 1 | 0 | 70 | 23 | 130 | 2.064 |
| D_reviewer | 83% | 83% | 33% | 75% | 1.50 | 1 | 0 | 78 | 30 | 138 | 2.366 |
| E_full | 67%* | 83% | 33% | 75% | 1.50 | 1 | **1** | 88 | 30 | 170 | 2.268 |

**Graph routing correctness: 6/6 (100%).**

\* E's lower headline figure **is the risk gate working**: `awaiting_human_approval` is
not scored as success, and the only run that changes category is the one that tried to
commit a credential (90/100 HIGH).

Unchanged from Phase 22 in outcomes; tool calls rose from `0/12/24/24/56` to
`12/108/130/138/170`, which is the measurable signature of the tools becoming
load-bearing.

**Experiment A is an architectural ablation. Every variant is SWE-Forge. It is not an
Open SWE comparison.**

## 5. Open SWE baseline status

Three distinct claims, deliberately not collapsed:

| Claim | Status | Evidence |
|---|---|---|
| **A. Implementation exists** | **YES** | `evaluation/baselines/open_swe_baseline.py` — `OpenSWEBaseline`, `preflight` |
| **B. Test adapter works** | **YES** | 8 tests. `resolve_agent_factory()` returns `<function get_agent at ...>` with `__module__ == "agent.server"` — the genuine upstream symbol, verified after installing `fastapi`, `deepagents==0.7.6`, `openai`, `langchain-openai`, `langchain-anthropic` |
| **C. Live execution** | **UNAVAILABLE** | `comparable_pairs = 0` |

Measured blocker after dependency installation:

```
no model provider credential set (ANTHROPIC_API_KEY / OPENAI_API_KEY / GOOGLE_API_KEY);
no sandbox provider credential set (DAYTONA_API_KEY / MODAL_TOKEN_ID / E2B_API_KEY / RUNLOOP_API_KEY)
```

Note the progression: module blockers were removed during Phase 24, so the adapter now
reaches the real upstream function. **Only credentials remain.** That is a stronger and
more precise claim than before — and still **not** a benchmark result.

```
$ python -m evaluation.experiment_b
Experiment B: 6 scenario pair(s)
  comparable pairs: 0
  -> no head-to-head conclusion is drawn (by design)
```

**No Open SWE vs SWE-Forge performance claim is made anywhere in this repository.**

## 6. Live-model status — UNAVAILABLE

```
{'track': 'C_live_model', 'available': False,
 'reason': 'SWEFORGE_EVAL_MODEL is not set; ANTHROPIC_API_KEY is not set'}
```

Track implemented at `evaluation/live/config.py`, runnable once configured:

```bash
export SWEFORGE_EVAL_PROVIDER=anthropic
export SWEFORGE_EVAL_MODEL=claude-sonnet-4-5
export ANTHROPIC_API_KEY=...
export SWEFORGE_EVAL_MAX_COST_USD=2.00
python -m evaluation.experiment_b --model "$SWEFORGE_EVAL_MODEL"
```

The cost ceiling becomes a real `BudgetLimits`, so a live benchmark terminates in
`budget_exhausted` rather than overspending. **No live-model result is claimed.**

## 7. Real-world benchmark status

```
REAL_WORLD_BENCHMARK = NOT_AVAILABLE
```

The four fixtures are labelled **deterministic architectural fixtures** throughout. They
are single-module Python projects written for this repository to exercise every graph
path deterministically. No SWE-bench run, no scraped GitHub issues, and no issues were
invented to inflate the suite.

**Next step:** a SWE-bench-lite harness against the existing `run_task()` entry point,
requiring model credentials and a sandbox provider.

## 8. MCP status

Implemented and tested against **deterministic fixtures**; no live MCP server available.

| Behaviour | Verified |
|---|---|
| Discovery + schema exposure | 3 capabilities across 3 adapters, kinds classified |
| Deterministic selection | `#412` → `get_issue` with `{"issue_id": "412"}` |
| Graph integration | `external_context(get_issue: ok)` in node trace; 1 entry in workflow state; run completed |
| Deny-by-default | Non-allowlisted → `ok=False`, `permission_error` |
| Timeout handling | `ok=False`, `timeout`, 3 attempts (bounded) |
| Retry then success | `ok=True` on attempt 2 |
| Budget consumption | `max_tool_calls=0` → blocked, `budget` |
| Fixture labelling | `_fixture: true` present in returned content |

## 9. LangChain tool-calling status

Captured message sequence (real objects, not description):

```
1. model            : ScriptedChatModel (tier=coding)
2. bind_tools()     : RunnableLambda            <- real LangChain binding
3. AIMessage        : tool_calls=['find_relevant_files']
4. ToolMessage      : id=test_authoring-0-0 content={'ok': True, 'results': [...]}
5. continuation     : tool_phase_ran=True iterations=3
6. structured result: TestChanges tests_added=['test_boundary']
7. tool invocations : [('find_relevant_files','ok','test_agent'),
                       ('find_related_tests','ok','test_agent')]
```

All 12 tools exercised end-to-end (`test_all_twelve_tools_exercised_end_to_end`).

**Direct invocation is documented as different from tool calling.** Graph nodes call
`runtime.call_tool(...)` for deterministic steps where the model must *not* choose
(verification, risk gate); agents use `bind_tools` where the model *should* choose. Both
are ledgered; only the latter is described as tool calling.

## 10. Dynamic-agent status

| Role | Class | Output model | Model role | Tools |
|---|---|---|---|---|
| `backend_agent` | `BackendAgent` | `BackendChanges` | implementation | deps, callers, relevant files |
| `database_agent` | `DatabaseAgent` | `MigrationChanges` | implementation | deps, callers |
| `documentation_agent` | `DocumentationAgent` | `DocChanges` | documentation | relevant files |
| `frontend_agent` | `FrontendAgent` | `FrontendChanges` | implementation | relevant files |
| `security_agent` | `SecurityAgent` | `SecurityAssessment` | security_analysis | security_scan, change_risk |
| `test_agent` | `TestAgent` | `TestChanges` | test_authoring | related tests, relevant files |
| `implementation_agent` | `ImplementationAgent` | `ImplementationOutput` | implementation | relevant files, deps |
| reviewer | `IndependentReviewer` | `ReviewResult` | review | git diff via graph |
| recovery | `Diagnostician` | `RepairOutput` | recovery | — |

**Measured: 6/6 distinct prompts, 6/6 distinct outputs, 5 distinct tool grants, 4 distinct
model roles.** `test_different_plans_produce_different_execution_paths` asserts a backend
plan and a documentation plan yield different `agents_executed`.

## 11. Budget enforcement status

| Limit | Enforced |
|---|---|
| `max_model_calls` | YES |
| `max_tool_calls` | YES |
| `max_input_tokens` | YES |
| `max_output_tokens` | YES |
| `max_estimated_cost_usd` | YES |
| `max_wall_time_seconds` | YES |
| `max_recovery_attempts` | YES |
| `max_review_cycles` | YES |

All raise `BudgetExceeded` before the expensive operation. `budget_exhausted` is an
explicit terminal state. The model cannot see or raise a limit — there is no tool, no
output field and no prompt text that mutates one.

## 12. Model fallback status

```
attempt 1 model=anthropic:claude-opus-4-1        status=transient fallback=False
attempt 2 model=anthropic:claude-opus-4-1        status=transient fallback=False
attempt 3 model=anthropic:claude-opus-4-1        status=transient fallback=False
attempt 1 model=anthropic:claude-sonnet-4-5      status=success   fallback=True
result=ok fallback_used=True retries=2 distinct_models=2
```

* Fallback is a genuinely different model configuration (cross-tier chain, deduped).
* Each attempt is recorded separately; a fallback is never counted as the same call.
* Non-retryable failure tried **1** model — no pointless fallback.
* Budget propagates: `BudgetExceeded` is raised rather than consuming fallbacks.

## 13. Security scan status

107 files scanned across `agent/sweforge/`, `evaluation/`, `tests_sweforge/`, `docs/` and
root configs, for 10 credential patterns.

* **5 matches, all PEM headers with no key material**: 3 in the scanner's own tests, 2 in
  upstream `docs/INSTALLATION.md` (GitHub App setup).
* **0 non-PEM matches.** No `.env` present. `.env.example` placeholders only.
* `.gitignore` covers `.env`, `.env.*`, `*.pem`, `credentials.json`, `.sweforge/`, with
  `!.env.example` negated.
* The risk-gate fixture builds its fake token at runtime (`"ghp_" + "A" * 36`) so no
  secret-shaped literal is committed.

## 14. Known limitations

**Unavailable capabilities** (implemented, not executable here):

| Item | Blocker |
|---|---|
| Open SWE head-to-head | Model + sandbox credentials (`comparable_pairs = 0`) |
| Live-model evaluation | No provider credential |
| Real-world benchmark | `NOT_AVAILABLE` — toy fixtures only |
| Live MCP server | None available; deterministic fixtures used |
| Live GitHub PR creation | No App installation |

**Statistical:** 6 scenarios, 4 single-module fixtures, one run per cell, one machine.
No confidence intervals — none would be meaningful at n=6. Token counts under scripted
models are synthetic, so cost figures demonstrate ledger accounting, not spend.

**Implementation:** Python-only AST; `find_callers` does not resolve aliased/dynamic call
sites; risk weights are hand-tuned judgement, not fit to data; **repository intelligence,
memory and model routing have no measured end-to-end effect** (variants A and B are
identical — a negative result, reported); concurrency implemented but evaluation runs
sequentially for reproducibility; the graph is not registered in `langgraph.json`.

## 15. Exact commands to reproduce everything

```bash
# --- install -----------------------------------------------------------------
git clone --depth 1 https://github.com/langchain-ai/open-swe.git && cd open-swe
tar -xzf sweforge-overlay.tar.gz
python -m venv .venv && source .venv/bin/activate
pip install "langgraph>=1.2.10" "langchain>=1.3.9" "pydantic>=2" "langsmith>=0.11.1" pytest ruff mypy
# optional, only to make the Open SWE baseline adapter importable:
pip install fastapi "deepagents==0.7.6" openai langchain-openai langchain-anthropic

export SWEFORGE_ALLOW_LOCAL_EXEC=1     # fixtures only; see docs/SECURITY.md

# --- tests and quality gate --------------------------------------------------
python -m pytest -c pytest-sweforge.ini                      # 358 passed
python -m ruff check agent/sweforge evaluation tests_sweforge
python -m ruff format --check agent/sweforge evaluation tests_sweforge
python -m mypy --ignore-missing-imports --no-strict-optional --explicit-package-bases \
  agent/sweforge/budget.py agent/sweforge/tools/errors.py \
  agent/sweforge/mcp/orchestration.py agent/sweforge/routing/execution_policy.py \
  agent/sweforge/routing/model_router.py agent/sweforge/schemas.py

# --- Experiment A: deterministic ablation (COMPLETE) -------------------------
python -m evaluation.runner                                  # 30 runs
python -m evaluation.evaluator                               # report + CSV + JSON

# --- Experiment B: Open SWE baseline (UNAVAILABLE here) ----------------------
python -c "from evaluation.baselines import describe_baseline_availability as d; \
           import json; print(json.dumps(d(), indent=2))"
python -m evaluation.experiment_b

# --- Experiment C: live model (UNAVAILABLE here) -----------------------------
python -c "from evaluation.live import describe_live_availability as d; print(d())"
python -m evaluation.experiment_b --model "$SWEFORGE_EVAL_MODEL"

# --- demo --------------------------------------------------------------------
python -m agent.sweforge.cli demo --list
python -m agent.sweforge.cli demo --scenario pipeline_secret_risk_gate
python -m agent.sweforge.cli analyze --repo . --task "model fallback middleware retry"
python -m agent.sweforge.cli doctor

# --- targeted Phase 23/24 verification ---------------------------------------
python -m pytest -c pytest-sweforge.ini tests_sweforge/test_phase23.py -k ExecutionBudget -v
python -m pytest -c pytest-sweforge.ini tests_sweforge/test_phase23.py -k ModelExecutionPolicy -v
python -m pytest -c pytest-sweforge.ini tests_sweforge/test_phase23.py -k MCP -v
python -m pytest -c pytest-sweforge.ini tests_sweforge/test_phase23.py -k DynamicDispatch -v
```

## 16. Acceptance criteria

| Criterion | Status |
|---|---|
| `docs/EVALUATION.md` no longer conflates ablation with Open SWE baseline | **PASS** |
| README accurately describes the architecture | **PASS** |
| Architecture documentation matches source | **PASS** |
| 357+ tests pass | **PASS (358)** |
| All 12 tools load-bearing | **PASS** |
| `bind_tools`/`tool_calls`/`ToolMessage` path tested | **PASS** |
| Dynamic agent selection changes execution | **PASS** |
| Specialized agents have distinct behaviour | **PASS** |
| Hard execution budgets enforced | **PASS (8/8)** |
| Model retry/fallback tested | **PASS** |
| MCP orchestration tested | **PASS (fixtures)** |
| Repository intelligence affects planning | **PASS** (evidence chain; no *end-to-end* effect measurable — negative result reported) |
| Validation tool load-bearing | **PASS** |
| Git-diff tool load-bearing | **PASS** |
| Open SWE baseline adapter tested | **PASS** |
| Open SWE vs SWE-Forge not claimed unless executed | **PASS — not claimed** |
| Live-model results not fabricated | **PASS — none reported** |
| Real-world benchmark not fabricated | **PASS — `NOT_AVAILABLE`** |
| LangSmith works configured and disabled | **PASS** |
| Secret scan completed | **PASS (0 real)** |
| Final documentation completed | **PASS** |
| Final project tree generated | **PASS** (§17) |
| Final limitations documented | **PASS** |
| Final report generated | **PASS** (this document) |

## 17. Final project tree

```
UPSTREAM OPEN SWE (unmodified)
  agent/{server.py,middleware/,runtime/,tools/,integrations/,review/,graphs/,...}
  tests/  ui/  evals/  langgraph.json  pyproject.toml  LICENSE
  docs/{INSTALLATION.md,CUSTOMIZATION.md,...}          <- upstream docs (note: singular)

UPSTREAM FILES MODIFIED (2, both additive)
  .gitignore   +!.env.example, +.sweforge/   (the .env.* rule swallowed the template)
  README.md    replaced; upstream original preserved at docs/UPSTREAM_README.md

SWE-FORGE (all new)
  agent/sweforge/                     42 files, 41 importable modules
    graph/workflow.py                 17-node StateGraph, 5 routers, bounded loops
    state/graph_state.py              SWEForgeState + custom reducers
    schemas.py                        structured outputs + validators
    budget.py                         ExecutionBudget (Phase 23)
    planning/planner.py               grounded structured planner
    repository/{analyzer,graph_index}.py   AST + import graph
    agents/{roles,specialized,tool_loop}.py  9 agents + real tool-calling (Phase 23)
    verification/{backends,verifier}.py     sandbox-first verification
    recovery/classifier.py            10-category failure taxonomy
    security/risk.py                  scanner + deterministic risk engine
    routing/{model_router,execution_policy}.py  tiers + retry/fallback (Phase 23)
    memory/store.py                   BM25 experience retrieval
    mcp/{orchestration,fixtures}.py   MCP orchestration (Phase 23)
    github/finalization.py            risk-gated PR preparation (Phase 23)
    observability/tracing.py          optional LangSmith + node metadata
    tools/{registry,errors}.py        12 StructuredTools + error policy
    models/scripted.py                ScriptedChatModel (+ bind_tools, Phase 23)
    runner.py  cli.py                 entry points

  evaluation/
    runner.py evaluator.py metrics.py scenarios.py     Experiment A
    baselines/open_swe_baseline.py                     Experiment B (Phase 23)
    experiment_b.py                                    Experiment B driver (Phase 23)
    live/config.py                                     Experiment C (Phase 23)
    fixtures/{billing,inventory,textutil,pipeline}/    deterministic architectural fixtures
    results/  reports/

  tests_sweforge/                     358 tests, no API key, no network
    test_core.py test_subsystems.py test_graph.py test_evaluation.py test_phase23.py

  docs/  ARCHITECTURE  CUSTOMIZATIONS  DEMO  EVALUATION  EXECUTION_BUDGETS
         LANGCHAIN_LANGGRAPH  LIVE_EVALUATION  PHASE23_GAP_AUDIT
         PHASE24_FINAL_REPORT  SECURITY  UPSTREAM_AUDIT  UPSTREAM_README

  .env.example  pytest-sweforge.ini
```

**13,242 lines** of SWE-Forge Python (implementation + tests). No upstream Python module,
test, config, entry point or license was edited or deleted; `LICENSE` retains
`Copyright (c) LangChain, Inc.` unchanged.
