# SWE-Forge

**Adaptive, Self-Verifying Autonomous Software Engineering Platform**

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![LangGraph](https://img.shields.io/badge/LangGraph-StateGraph-orange)
![LangChain](https://img.shields.io/badge/LangChain-StructuredTools-green)
![Tests](https://img.shields.io/badge/tests-478%20passing-brightgreen)
![Lint](https://img.shields.io/badge/ruff-clean-brightgreen)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

> **Status: architecture frozen.** Future work is driven by experimental
> evidence, not new features. See
> [docs/FINAL_PROJECT_STATUS.md](docs/FINAL_PROJECT_STATUS.md).
>
> Built on top of [Open SWE](https://github.com/langchain-ai/open-swe) by LangChain.
> Open SWE is the **foundation**; SWE-Forge is the **orchestration, verification and
> safety layer added on top of it**. See [Attribution](#attribution).

---

## What SWE-Forge is

SWE-Forge is **an explicit control and orchestration layer for autonomous software
engineering, built on the Open SWE execution infrastructure.**

It is **not** another Open SWE implementation, and **not** Open SWE rewritten with
LangGraph. Open SWE remains the mature execution substrate — sandboxing, provider
middleware, model construction, GitHub/Slack/Linear integration, MCP transport, the
PR-review product. None of that is reimplemented here.

SWE-Forge contributes the layer above it, confined to `agent/sweforge/`:

| Contribution | Where |
|---|---|
| Deterministic LangGraph orchestration (17 nodes, 5 routers, bounded loops) | `graph/workflow.py` |
| Structured planning with validated plans | `planning/planner.py`, `schemas.py` |
| Dynamic multi-agent dispatch (6 specialized agents + reviewer + diagnostician) | `agents/specialized.py` |
| Repository intelligence (Python AST + import graph) | `repository/` |
| 12 load-bearing LangChain tools | `tools/registry.py` |
| Real agent tool-calling (`bind_tools` → `tool_calls` → `ToolMessage`) | `agents/tool_loop.py` |
| Bounded self-repair with a deterministic failure taxonomy | `recovery/classifier.py` |
| Model retry and cross-tier fallback | `routing/execution_policy.py` |
| Hard execution budgets + `budget_exhausted` terminal state | `budget.py` |
| Experience-aware planning (BM25 retrieval) | `memory/store.py` |
| MCP orchestration (deny-by-default allowlist) | `mcp/orchestration.py` |
| Independent review gate | `agents/roles.py` |
| Security scanning and deterministic risk gating | `security/risk.py` |
| Quantitative evaluation and ablation harness | `evaluation/` |
| Always-on local trace (LangSmith optional) | `observability/trace.py` |
| Registered LangGraph entry point | `graph/entrypoint.py` + `langgraph.json` |
| Reproducible run manifests (commit SHA, versions, seed) | `evaluation/reproducibility.py` |
| Real-benchmark harness (dry-run; no results claimed) | `evaluation/benchmarks/` |

Every row is implemented, tested, and traced to a source symbol in
[docs/CUSTOMIZATIONS.md](docs/CUSTOMIZATIONS.md). Nothing else is claimed.

## In 60 seconds

An AI agent that edits your repository has to answer four questions a chatbot never
does: *where does this change belong*, *did it actually work*, *what happens when it
fails*, and *should this ship at all*.

Upstream Open SWE answers these inside a single ReAct agent loop, so the control flow
lives in a prompt. **SWE-Forge makes the control flow explicit**: reasoning stays with
the LLM, while a deterministic LangGraph `StateGraph` decides what happens next.

Concretely, SWE-Forge adds:

- **Repository intelligence** — real Python AST analysis and an import graph. Indexes
  the current repository (upstream + the SWE-Forge overlay) — **907 files, 7,429 symbols, 1,036 import edges in ~1.5 s**.
- **A bounded self-repair loop** — classify the failure deterministically, diagnose,
  patch, re-verify. Raised task success from **50% to 83%** in the shipped ablation.
- **An independent review gate** — catches changes that pass every test and are still
  wrong.
- **A deterministic risk gate** — in the shipped benchmark it blocked a change that
  every other workflow variant shipped: green tests, reviewer approval, and a committed
  credential. Scored **90/100 HIGH, human approval required**.
- **A reproducible ablation harness** — 30 runs across 5 workflow variants, so every
  architectural claim above is a measured number rather than an assertion. Including
  [one honest negative result](#what-the-numbers-show).

```bash
export SWEFORGE_ALLOW_LOCAL_EXEC=1
python -m agent.sweforge.cli demo --scenario pipeline_secret_risk_gate
```

---

## Architecture

```
                            START
                              |
                        task_intake ----------(empty task)----------+
                              |                                     |
                     repository_analysis    <- AST + import graph    |
                              |                                     |
                  task_complexity_analysis  <- experience memory     |
                              |                                     |
                          planning          <- structured TaskPlan   |
                              |                                     |
                  dynamic_agent_selection   <- per-task roster       |
                              |                                     |
                       implementation       <- whole-file edits      |
                              |                                     |
                    +---- verification <---------------+   <- real pytest
                    |         |                        |            |
              (passed)     (failed)                    |            |
                    |         |                        |            |
                    |   failure_analysis               |  <- 10-category
                    |         |                        |     classifier
                    |   +-----+------+                 |            |
                    | (budget)   (exhausted)           |            |
                    |   |             |                |            |
                    | recovery -------+----------------+  <- bounded
                    |   ^             v                             |
                    |   |        escalation ----------------------+  |
                    v   |                                        |  |
           independent_review  <- no implementer context         |  |
                    |   |                                        |  |
        +-----------+---+ (rejected, budget left)                |  |
        | (approved)                                             |  |
        v                                                        |  |
  security_analysis     <- secret / dangerous-pattern scan       |  |
        |                                                        |  |
    risk_gate           <- additive, deterministic score         |  |
        |                                                        |  |
   +----+-----+                                                  |  |
(HIGH)     (LOW/MED)                                             |  |
   |           |                                                 |  |
human_approval |<------------------------------------------------+--+
   |       finalization                                          |
   +-----------+------------------> END <------------------------+
```

17 nodes, 5 conditional routers, 2 bounded loops, 4 terminal nodes, 7 terminal statuses (plus the initial `pending`).

Full detail in **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**. A flagship diagram
distinguishing SWE-Forge-owned components from upstream Open SWE — plus the paired
evaluation diagram — is in
**[docs/ARCHITECTURE_DIAGRAM.md](docs/ARCHITECTURE_DIAGRAM.md)**.

---

## Upstream vs. SWE-Forge

The most important audit finding: **upstream Open SWE has no hand-authored domain
`StateGraph`.** It builds its agent with `deepagents.create_deep_agent` plus 24
middleware modules. Across 420 upstream Python files, the only `StateGraph` is
`agent/scheduler.py` (cron scheduling). That is the gap SWE-Forge fills.

| Capability | Upstream Open SWE | SWE-Forge |
|---|---|---|
| Agent construction | `create_deep_agent` + 24 middleware | Explicit 17-node `StateGraph` |
| Control flow | Emergent from prompt + ReAct loop | Deterministic typed routing |
| Sandbox isolation | **Owned** (Daytona/Modal/E2B/Runloop) | Reused, never reimplemented |
| Provider quirks / fallback / retry | **Owned** (`agent/middleware/`) | Reused, never reimplemented |
| GitHub App, PRs, Slack, Linear, MCP | **Owned** | Reused, never reimplemented |
| Repository static model | none | AST + import graph + ranking |
| Test selection from dependencies | none | Targeted verification |
| Failure taxonomy + bounded repair | Transport-level retry only | 10 categories, enforced bound |
| In-loop review gate | PR-review *product* | Mid-run gate an implementation must pass |
| Change-risk scoring | Access control only | Additive risk score + human gate |
| Model selection | One model + fallback per run | Per-role tier routing + cost ledger |
| Experience memory | none | BM25 retrieval over past runs |
| Orchestration benchmark | Reviewer comment quality | 5-variant ablation harness |

Every claim above is traced to real files and symbols in
**[docs/CUSTOMIZATIONS.md](docs/CUSTOMIZATIONS.md)** and
**[docs/LANGCHAIN_LANGGRAPH.md](docs/LANGCHAIN_LANGGRAPH.md)**.

**Upstream files modified: 2** — `.gitignore` (appended two lines) and `README.md`
(this file; upstream's original is preserved verbatim at
[docs/UPSTREAM_README.md](docs/UPSTREAM_README.md)). No upstream Python module, test,
config, entry point or license was touched.

---

## Results

### Three experiments, never conflated

| | Question | Baseline | Status |
|---|---|---|---|
| **A — architectural ablation** | Does each SWE-Forge component help? | A stripped **SWE-Forge** graph | **COMPLETE** (30 runs) |
| **B — system baseline** | Does SWE-Forge improve on *actual* Open SWE? | Real `agent.server.get_agent` | **UNAVAILABLE** (`comparable_pairs = 0`) |
| **C — live model** | Behaviour with a real model deciding? | Same model both sides | **UNAVAILABLE** (no credential) |

**No Open SWE vs SWE-Forge result exists in this repository.** The table below is
Experiment A: every variant is SWE-Forge. It must not be read as a comparison
against Open SWE.

30 executions: 6 scenarios x 5 cumulative workflow variants. 0 unavailable.
Reproduce with `python -m evaluation.runner && python -m evaluation.evaluator`.

| Variant | Task success | Verification pass | Recovery success | Avg attempts | Escalations | Human gate | Nodes | Model calls | Tool calls |
|---|---|---|---|---|---|---|---|---|---|
| A. Baseline (single pass) | 50% | 50% | n/a | n/a | 0 | 0 | 54 | 18 | 12 |
| B. + Repository intelligence | 50% | 50% | n/a | n/a | 0 | 0 | 54 | 18 | 108 |
| C. + Bounded self-repair | **83%** | **83%** | 67% | 1.67 | 1 | 0 | 70 | 23 | 130 |
| D. + Independent review | 83% | 83% | 75% | 1.50 | 1 | 0 | 78 | 30 | 138 |
| E. Full SWE-Forge | 67%* | 83% | 75% | 1.50 | 1 | **1** | 88 | 30 | 170 |

\* E's lower headline number **is the risk gate working**. `awaiting_human_approval` is
not counted as success, and the only run that changes category is the one that tried to
commit a credential. A benchmark rewarding that would measure the wrong thing.

**Graph routing correctness: 6/6 scenarios reached exactly their designed terminal
state.**

### What the numbers show

1. **Self-repair is what moves task success** (50% to 83%). The two scenarios that flip
   are the ones whose first attempt was wrong — structurally unreachable for a
   single-pass workflow.
2. **Repository intelligence shows no end-to-end effect — a negative result, reported as
   such.** A and B are identical because the harness pins the planner's output, so
   richer evidence cannot change the plan. The subsystem is therefore measured
   *directly* instead (907 files / 7,429 symbols / ~1.5 s; correct top-ranked file on
   spot-check queries), and its end-to-end value is **untested pending a live-model
   run**.
3. **The review gate catches green-but-wrong work.** A change passing verification was
   rejected for implementing 1 of 3 required validations, then repaired.
4. **The risk gate blocks what everything else ships** (90/100 HIGH).
5. **The recovery loop terminates** — exactly 3 attempts, then escalation.
6. **Assurance is not free** — E runs 1.7x the nodes and 2x the model calls of baseline.

### Honest scope of the evaluation

Model behaviour is **pinned by scripted fixtures** so orchestration is the only
variable. The fixtures, edits and `pytest` runs are real; a repaired suite genuinely
goes green.

- **Measured:** graph paths, recovery counts, gate decisions, terminal states, real test
  results, wall-clock, node/model/tool counts.
- **Synthetic and labelled as such:** token counts, therefore cost.
- **Not measured, not claimed:** frontier-model capability, SWE-bench, or that model
  routing improves outcomes.

Full methodology and limitations: **[docs/EVALUATION.md](docs/EVALUATION.md)**.

---

## Technology stack

Every technology solves a concrete problem. Nothing was added for the list.

| Technology | Problem it solves |
|---|---|
| **LangGraph** | Termination guarantees, auditable control flow, ablatable topology |
| **LangChain tools** | One validated implementation callable by both nodes and agents |
| **Structured outputs (Pydantic)** | Control flow must never depend on parsing prose |
| **Python `ast`** | Exact syntactic facts about a repository, no model call needed |
| **LangSmith** | Per-node spans; optional, no-op without credentials |
| **Open SWE sandbox** | Untrusted code must not run on the host — reused, not rebuilt |
| **BM25 (not embeddings)** | Identifier-heavy queries; deterministic; no service dependency |

Deliberately **not** added: a parallel MCP gateway (upstream already ships MCP clients,
so duplicating it would inflate the stack rather than the system) and a vector database
(rejected for the default path; see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)).

---

## Installation

```bash
git clone <this-repo> && cd open-swe
python -m venv .venv && source .venv/bin/activate
pip install "langgraph>=1.0" "langchain>=1.0" "pydantic>=2" pytest ruff
```

No API key is needed for the tests, the analyzer, the demo, or the evaluation suite.

## Quick start

```bash
export SWEFORGE_ALLOW_LOCAL_EXEC=1          # demos run real pytest on shipped fixtures

python -m agent.sweforge.cli doctor          # configuration check
python -m agent.sweforge.cli analyze --repo . --task "sandbox circuit breaker timeout"
python -m agent.sweforge.cli showcase         # one-command full-pipeline demo
python -m agent.sweforge.cli demo --list
python -m agent.sweforge.cli demo --scenario pipeline_secret_risk_gate

python -m pytest -c pytest-sweforge.ini      # 478 tests
python -m evaluation.runner                  # 30-run ablation, ~37s
python -m evaluation.evaluator               # Markdown + CSV + JSON report
```

Running a real task (needs credentials):

```bash
cp .env.example .env    # add a provider key
python -m agent.sweforge.cli run --repo /path/to/repo --task "..."
```

More: **[docs/DEMO.md](docs/DEMO.md)**.

## Reproducing everything from a clean environment

Verified in a fresh virtual environment with **no inherited `PYTHONPATH`**:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-sweforge.txt
export SWEFORGE_ALLOW_LOCAL_EXEC=1          # fixtures only; see docs/SECURITY.md

python -m pytest -c pytest-sweforge.ini      # 477 passed
python -m evaluation.check_docs              # 13/13 docs match source
python -m agent.sweforge.cli showcase        # full pipeline, writes traces.jsonl
python -m evaluation.runner --repeat 3 --seed 0   # 30 runs, determinism check
python -m evaluation.run_all_experiments     # retrieval / memory / routing / recovery
```

## Configuration

All configuration is environment-based; no model id or credential is hard-coded.

| Variable | Purpose |
|---|---|
| `SWEFORGE_MODEL_{FAST,BALANCED,CODING,REASONING}` | Model id per routing tier |
| `SWEFORGE_PRICE_{TIER}` | USD per 1M tokens, `input,output` |
| `SWEFORGE_ALLOW_LOCAL_EXEC` | Gate for host execution (fixtures only) |
| `SWEFORGE_MEMORY_PATH` | Experience memory location |
| `LANGSMITH_TRACING`, `LANGSMITH_API_KEY` | Optional tracing (both required) |

See [.env.example](.env.example).

## Project structure

```
agent/sweforge/              <- the SWE-Forge layer
  graph/workflow.py            StateGraph: nodes, routers, bounded loops
  state/graph_state.py         SWEForgeState + custom reducers
  schemas.py                   all structured-output models + validators
  planning/planner.py          grounded structured planner, agent selection
  repository/                  analyzer.py (AST) + graph_index.py (import graph)
  agents/roles.py              implementer, independent reviewer, diagnostician
  verification/                backends.py (sandbox) + verifier.py
  recovery/classifier.py       deterministic failure taxonomy
  security/risk.py             secret scan + additive risk scoring
  routing/model_router.py      per-role tier policy + cost ledger
  memory/store.py              BM25 experience retrieval
  observability/tracing.py     optional LangSmith
  tools/registry.py            12 LangChain StructuredTools
  models/scripted.py           ScriptedChatModel (deterministic evaluation)
  runner.py, cli.py            entry points

evaluation/                  <- benchmark + ablation harness
  scenarios.py  runner.py  evaluator.py  metrics.py
  fixtures/     results/    reports/

tests_sweforge/              <- 478 tests, no API key, no network
docs/                        <- audit, architecture, evaluation, security, demo
```

## Security

SWE-Forge autonomously modifies repositories, so it ships a **defence-in-depth risk
layer** — explicitly *not* a security product:

- Verification runs in the **Open SWE sandbox**. The host backend is env-gated and
  refuses by default.
- Secret and dangerous-pattern scanning over changed content.
- A **deterministic** risk score (an LLM can add findings but cannot lower the gate).
- HIGH risk produces `awaiting_human_approval`, a first-class terminal state.

Full threat model, scoring table and known gaps: **[docs/SECURITY.md](docs/SECURITY.md)**.

## Limitations

Stated plainly. Negative results are reported, not hidden.

### Status of every unavailable capability

| Item | Status |
|---|---|
| **Live-model evaluation** | **UNAVAILABLE** — no provider credential. Track implemented (`evaluation/live/`), runnable once configured. No live number is claimed. |
| **Open SWE head-to-head** | **UNAVAILABLE** — `comparable_pairs = 0`. The adapter resolves the genuine `agent.server.get_agent` (verified), but execution needs model **and** sandbox credentials. Adapter-exists is not benchmark-completed. |
| **Real-world benchmark** | `REAL_WORLD_BENCHMARK = NOT_AVAILABLE`. The four fixtures are **deterministic architectural fixtures**, not SWE-bench. No issues were invented to inflate the suite. |
| **MCP** | Orchestration implemented and tested against **deterministic fixtures** (payloads labelled `_fixture: true`). No live MCP server; no external result fabricated. |
| **GitHub PR creation** | Risk-gated preparation implemented and mock-tested. Live creation **UNAVAILABLE** (no App installation). |

### Component measurement (Phase 25)

Three subsystems previously reported as "implemented but unmeasured" are now
measured **directly**, since a scripted-model harness cannot see them end-to-end:

| Component | Measured | Not measured |
|---|---|---|
| Repository intelligence | graph retrieval R@5 **1.0** vs lexical **0.875** (n=4, no LLM) | effect on task success |
| Memory | relevant prior experience retrieved **2/2** (n=2) | effect on task success |
| Model routing | adaptive costs **40%** of a fixed reasoning-tier model (n=4) | whether reliability is preserved |
| Recovery | detection **10/10** categories | 7 of 10 categories have no benchmark example (`untested`) |

Retrieval precision saturates at 1.0 for every strategy: the fixtures are too
easy to discriminate on precision. Reported, not spun.

### Statistical limitations

Six scenarios across four single-module fixtures: enough to cover every terminal state,
**not** enough for statistical claims. One run per cell, one machine, no repeated-trial
variance analysis. No confidence intervals are offered because none would be meaningful
at n=6. Token counts under scripted models are synthetic, so cost figures demonstrate
ledger accounting rather than real spend.

### Implementation limitations

1. Python-only AST; other languages are inventoried, not parsed.
2. `find_callers` resolves definition sites and importers, not aliased or dynamic calls.
3. Risk scoring is pattern-based — false positives and negatives are expected, and
   weights are hand-tuned judgement, not empirically derived.
4. Repository intelligence, memory and model routing have **no measured end-to-end
   effect** (invisible to a scripted-model harness).
5. Six scenarios and four small fixtures: enough to cover every terminal state, not
   enough for statistical claims.
6. No live-model or public-benchmark run. No SWE-bench number is claimed.
7. Concurrent subtask execution is implemented and tested but runs sequentially in
   evaluation for reproducibility.

## Future work

Ordered by value:

1. **Live-model evaluation** — the highest-value next step; it is what would finally
   test repository intelligence, memory and routing end-to-end.
2. Register the SWE-Forge graph in `langgraph.json` for deployment alongside upstream.
3. Extend AST analysis to TypeScript (the upstream repo is 310 TS/TSX files).
4. Replace hand-tuned risk weights with weights fit to labelled outcomes.
5. Repeated-trial variance analysis on wall-clock and cost.

## Documentation

| Document | Contents |
|---|---|
| [docs/UPSTREAM_AUDIT.md](docs/UPSTREAM_AUDIT.md) | What Open SWE provides, measured; extension points; non-duplication rules |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Subsystem design, decisions and their costs |
| [docs/LANGCHAIN_LANGGRAPH.md](docs/LANGCHAIN_LANGGRAPH.md) | Inherited vs. implemented, with code |
| [docs/CUSTOMIZATIONS.md](docs/CUSTOMIZATIONS.md) | Technology traceability table; files added |
| [docs/EVALUATION.md](docs/EVALUATION.md) | Methodology, results, limitations |
| [docs/SECURITY.md](docs/SECURITY.md) | Threat model, risk scoring, known gaps |
| [docs/DEMO.md](docs/DEMO.md) | Guided walkthrough |
| [docs/ARCHITECTURE_DIAGRAM.md](docs/ARCHITECTURE_DIAGRAM.md) | Flagship system + evaluation diagrams |
| [docs/FINAL_PROJECT_STATUS.md](docs/FINAL_PROJECT_STATUS.md) | Frozen status: what is measured, what is unavailable |
| [docs/PROJECT_CLAIMS.md](docs/PROJECT_CLAIMS.md) | Every claim sorted verified / not-live-validated / not implemented |
| [docs/RESUME_CLAIMS.md](docs/RESUME_CLAIMS.md) | Strongest defensible claims with evidence |
| [docs/INTERVIEW_GUIDE.md](docs/INTERVIEW_GUIDE.md) | Grounded answers to 20 likely questions |
| [docs/UPSTREAM_README.md](docs/UPSTREAM_README.md) | Upstream Open SWE README, verbatim |

---

## Attribution

**SWE-Forge is built on the [Open SWE](https://github.com/langchain-ai/open-swe)
open-source project by LangChain, Inc.**

Open SWE provides the foundation this project builds on: the sandbox infrastructure, the
middleware stack, model instantiation, GitHub/Slack/Linear integrations, MCP clients, the
PR-review product, the web dashboard, and its own test suite. **That work is not mine.**

SWE-Forge is the layer added on top: the explicit LangGraph orchestration, repository
intelligence, self-verification, failure classification and bounded repair, the in-loop
review gate, the change-risk engine, adaptive model routing, experience memory, and the
evaluation harness — all confined to `agent/sweforge/`, `evaluation/`, `tests_sweforge/`
and `docs/`.

The upstream MIT license and `Copyright (c) LangChain, Inc.` are retained unchanged in
[LICENSE](LICENSE).
