# Evaluation

This document states exactly what was measured, how, and what the numbers do and do not
support. Every figure here was produced by executing the shipped harness; none is
estimated, extrapolated or illustrative.

Reproduce everything with:

```bash
export SWEFORGE_ALLOW_LOCAL_EXEC=1
python -m evaluation.runner
python -m evaluation.evaluator
```

---

## 0. Three experiments, never conflated

| | Question | Baseline side | Status |
|---|---|---|---|
| **Experiment A** — architectural ablation | Does each SWE-Forge component help? | A stripped **SWE-Forge** graph | **COMPLETE** — 30 runs, reported below |
| **Experiment B** — system-level baseline | Does SWE-Forge improve on the *actual* Open SWE system? | Real upstream `agent.server.get_agent` | **UNAVAILABLE** — `comparable_pairs = 0` |
| **Experiment C** — live model | How does it behave with a real model deciding? | Same model on both sides | **UNAVAILABLE** — no credential |

Experiment A's variants are **all SWE-Forge**. It measures orchestration
components against each other. It is *not* a comparison against Open SWE, and no
number in it may be read as one.

Experiment B is the only experiment that invokes upstream code. Its current
status, measured by running it:

```
$ python -m evaluation.experiment_b
Experiment B: 6 scenario pair(s)
  comparable pairs: 0
  baseline UNAVAILABLE: missing Python modules: deepagents, fastapi;
    upstream agent factory not importable; no model provider credential set;
    no sandbox provider credential set
  -> no head-to-head conclusion is drawn (by design)
```

**Therefore: no Open SWE vs SWE-Forge result exists in this repository.** The
adapter is implemented and tested; live execution is unavailable. Those are
different claims and are kept separate throughout.

---

## 1. The central methodological problem

SWE-Forge's contribution is **orchestration**: conditional routing, a bounded repair
loop, an independent review gate, a deterministic risk gate. Evaluating orchestration
with a live LLM measures two things at once — the architecture and the model's sampling
noise — and the second dominates. Run the same task twice against a frontier model and
you get different plans, different edits, and different outcomes.

So the harness holds the model constant.

`ScriptedChatModel` (a real `BaseChatModel` subclass) replays a pinned sequence of
validated structured outputs per role. Scenarios in `evaluation/scenarios.py` declare
exactly what the "model" returns at each step — including *deliberately wrong* first
attempts. With model behaviour fixed, the only variable across workflow variants is the
graph topology, so a difference in outcome is attributable to architecture.

**The code in every scripted edit is real.** The harness copies a fixture repository to
a temp directory, writes those files to disk, and runs real `pytest`. "Recovery
succeeded" means a genuinely failing suite genuinely went green.

### What this design measures

- Which graph path executed (`node_trace`)
- How many recovery attempts ran, and whether the bound held
- Whether the review gate fired and what it changed
- Whether the risk gate withheld a change
- Which terminal state was reached
- Real `pytest` pass/fail counts
- Wall-clock time, node counts, model-call counts, tool-call counts

### What it does not measure

- **Whether a frontier model would produce these edits unaided.** Nothing in this report
  speaks to model capability.
- **Real token usage or cost.** Token counts under scripted models are synthetic
  (derived from prompt length). Cost columns demonstrate that the ledger accounts
  correctly; they are not a provider bill. They are labelled synthetic everywhere.
- **SWE-bench or any public benchmark.** Not run. No such number is claimed anywhere in
  this repository.
- **End-to-end value of repository intelligence** (see the negative result in §5).

This split is the honest boundary of the work. A live-model evaluation is the single
most valuable next step and is listed as such in the README's future work.

---

## 2. Benchmark suite — deterministic architectural fixtures

> **These are `deterministic architectural fixtures`, not a real-world SWE benchmark.**
> Four single-module Python projects written for this repository, each with a real test
> suite and (in three cases) a real seeded bug. They exist to exercise every graph path
> deterministically. `REAL_WORLD_BENCHMARK = NOT_AVAILABLE` — no SWE-bench, no scraped
> GitHub issues, and none were invented to inflate the suite. Next step is recorded in
> §7 and the README's future work.


Six scenarios across four fixture repositories (`evaluation/fixtures/`). Each fixture is
a small real Python project with a real test suite; three ship with a genuine bug whose
test fails before the fix.

| Scenario | Fixture | What it exercises | Expected terminal state |
|---|---|---|---|
| `billing_validation_first_try` | billing | Clean first-attempt success | `completed` |
| `inventory_boundary_recovery` | inventory | Off-by-one → assertion failure → repair | `completed` |
| `textutil_syntax_recovery` | textutil | Syntax error introduced → repair | `completed` |
| `inventory_recovery_exhausted` | inventory | Repair always wrong → bound must hold | `escalated_recovery_exhausted` |
| `billing_review_rejection` | billing | Tests green but change incomplete → reviewer catches it | `completed` |
| `pipeline_secret_risk_gate` | pipeline | Green tests, but commits a credential + edits CI | `awaiting_human_approval` |

The suite deliberately covers **every terminal state**, including the two failure
outcomes. A benchmark that only exercises the happy path cannot demonstrate that a
safety bound works.

Each run gets a pristine fixture copy in a temp directory, so a mutating run cannot
contaminate the next — necessary because each scenario is executed once per variant.

---

## 3. Workflow variants (the ablation axis)

Cumulative, defined in `evaluation/runner.py::variant_configs`:

| Variant | Repo intel | Recovery | Reviewer | Security gate | Memory |
|---|---|---|---|---|---|
| **A_baseline** | — | — | — | — | — |
| **B_repo_intel** | yes | — | — | — | — |
| **C_recovery** | yes | yes | — | — | — |
| **D_reviewer** | yes | yes | yes | — | — |
| **E_full** | yes | yes | yes | yes | yes |

**A_baseline is a stripped SWE-Forge graph — NOT an Open SWE baseline.** It is one
linear pass (plan → implement → verify → stop) with no adaptive routing, no repair and
no gates. It isolates SWE-Forge's own components and stands in for the class of workflow
where control flow is not explicit.

> **Correction (Phase 23/24).** Earlier revisions called `A_baseline` "the fixed
> single-agent comparison", which implied a comparison against Open SWE. It is not one:
> **nothing in Experiment A invokes upstream code.** The genuine system-level baseline is
> **Experiment B** (`evaluation/experiment_b.py`), which invokes the real
> `agent.server.get_agent` path and is currently **UNAVAILABLE** in this environment with
> `comparable_pairs = 0`. The 30 scripted runs below are an architectural ablation and do
> **not** constitute an Open SWE vs SWE-Forge comparison. See
> `docs/PHASE23_GAP_AUDIT.md` row 1 and §9 of this document.

Each variant builds a *structurally different graph*, not the same graph with runtime
conditionals.

---

## 4. Measured results

30 executions (6 scenarios × 5 variants), suite wall time ≈ 37 s. **0 unavailable.**

### 4.1 Outcomes

| Variant | Runs | Task success | Verification pass | First-attempt | Recovery success | Avg recovery attempts | Escalations | Human-approval gate |
|---|---|---|---|---|---|---|---|---|
| A_baseline | 6 | 50% | 50% | 50% | n/a | n/a | 0 | 0 |
| B_repo_intel | 6 | 50% | 50% | 50% | n/a | n/a | 0 | 0 |
| C_recovery | 6 | 83% | 83% | 50% | 67% | 1.67 | 1 | 0 |
| D_reviewer | 6 | 83% | 83% | 33% | 75% | 1.50 | 1 | 0 |
| E_full | 6 | 67% | 83% | 33% | 75% | 1.50 | 1 | 1 |

`n/a` means the rate is undefined (no run entered recovery), not zero. The harness never
prints a rate without a denominator.

### 4.2 Cost and effort

| Variant | Nodes executed | Model calls | Verification runs | Tool calls | Avg wall time (s) | Tests passed/run |
|---|---|---|---|---|---|---|
| A_baseline | 48 | 12 | 6 | 0 | 0.830 | 7/10 |
| B_repo_intel | 48 | 12 | 6 | 12 | 0.823 | 7/10 |
| C_recovery | 64 | 17 | 11 | 24 | 1.515 | 10/11 |
| D_reviewer | 72 | 24 | 12 | 24 | 1.579 | 10/11 |
| E_full | 82 | 24 | 12 | 56 | 1.607 | 10/11 |

*(Token and cost columns are omitted here because they are synthetic; see the generated
report `evaluation/reports/EVALUATION_REPORT.md` for the full table with explicit
synthetic labelling.)*

### 4.3 Assurance activity

| Variant | Runs reviewed | Review interventions | Security findings | Risk-gate HIGH interventions |
|---|---|---|---|---|
| A–C | 0 | 0 | 0 | 0 |
| D_reviewer | 5 | 1 | 0 | 0 |
| E_full | 5 | 1 | 1 | 1 |

### 4.4 Per-scenario terminal states

| Scenario | A | B | C | D | E |
|---|---|---|---|---|---|
| `billing_validation_first_try` | completed | completed | completed | completed | completed |
| `inventory_boundary_recovery` | **failed** | **failed** | completed | completed | completed |
| `textutil_syntax_recovery` | **failed** | **failed** | completed | completed | completed |
| `inventory_recovery_exhausted` | failed | failed | escalated | escalated | escalated |
| `billing_review_rejection` | completed | completed | completed | completed | completed |
| `pipeline_secret_risk_gate` | completed | completed | completed | completed | **awaiting human** |

### 4.5 Graph routing correctness

Each scenario declares the terminal state its design should produce. This validates that
the graph routed as intended, independently of task success.

**6/6 scenarios routed exactly as designed (100%).**

---

## 5. Interpretation — including a negative result

**1. Bounded self-repair is the component that moves task success.** Baseline reaches
50%; adding the recovery loop reaches 83%. The two scenarios that flip are exactly the
ones whose first implementation attempt was scripted wrong — the case a single-pass
workflow structurally cannot address, because nothing re-reads the failing output.

**2. Repository intelligence shows no measurable end-to-end effect in this harness.
This is a negative result and is reported as such.** A_baseline and B_repo_intel produce
identical outcomes (50%, same node count). The reason is methodological: the planner's
output is *pinned by the scripted fixture*, so richer planning evidence cannot change
the plan. This does not show the subsystem is useless — it shows this harness cannot
test it end-to-end. Repository intelligence is therefore measured **directly** instead
(§6), and its end-to-end value remains **untested pending a live-model run**. Claiming
otherwise from these numbers would be dishonest.

**3. The independent review gate catches work that tests call green.** In
`billing_review_rejection` the implementation passes verification and terminates as
`completed` under variant C. Under variant D the reviewer records a `major` finding
("only subtotal is validated; the task also required tax_rate and discount range"),
which routes the run back through recovery and yields the fully-validated
implementation. Note the *deliberate* cost: first-attempt success drops from 50% to 33%,
because the reviewer reclassifies a run that C considered done on the first try. That is
the gate working, not a regression.

**4. The risk gate blocks a change every other variant shipped.** In
`pipeline_secret_risk_gate` the change is functionally correct and verification is green,
so A–D all terminate `completed`. Variant E scores it **90/100 HIGH** — 60 for a
committed credential (`github_token` blocker) plus 30 for a CI workflow edit — and
terminates in `awaiting_human_approval`. This is the clearest single argument for a
deterministic risk layer in a system that autonomously edits repositories.

**5. E_full's lower headline task success (67% vs 83%) is the gate working, not a
regression.** `awaiting_human_approval` is not counted as success, and the only run that
changes category is the one that tried to commit a credential. A benchmark that rewarded
shipping that change would be measuring the wrong thing — which is why this report leads
with terminal states rather than a single success number.

**6. The recovery loop terminates.** `inventory_recovery_exhausted` scripts an
endlessly-wrong repair. Every recovery-enabled variant stops after exactly **3**
attempts and escalates, confirming the bound is structural (enforced by the routing
function) rather than advisory.

**7. Assurance is not free.** E_full executes 82 nodes against the baseline's 48
(1.71×), 24 model calls against 12 (2×), and ~1.94× the wall time. Whether that overhead
is worth paying depends on the cost of shipping a bad change — which
`pipeline_secret_risk_gate` quantifies as "one leaked credential".

---

## 6. Technology-level evaluation (component-direct measurements)

Because the end-to-end harness cannot test repository intelligence (§5.2), these
subsystems are measured directly. Reproduce with `sweforge analyze` and the test suite.

### 6.1 Repository intelligence on the real Open SWE codebase

`python -m agent.sweforge.cli analyze --repo . --task "..."` against this repository:

| Metric | Measured |
|---|---|
| Files indexed | 907 |
| Symbols extracted (Python AST) | 7,429 |
| In-repo import edges resolved | 1,036 |
| Test files identified | 278 |
| Full analysis wall time | ~1.5 s |

> **Scope note (Final Freeze).** These figures are re-measured against the
> repository *as it stands*, i.e. upstream Open SWE **plus** the SWE-Forge
> overlay. Earlier revisions quoted 812/6,385/843, measured against the pristine
> upstream clone before the overlay existed; those numbers no longer reproduce
> and have been retired rather than reconstructed. Verify with
> `python -m agent.sweforge.cli analyze --repo .`

Relevance ranking, qualitatively verified against a codebase neither the ranker nor its
author had special knowledge of:

| Query | Top-ranked file |
|---|---|
| "model fallback middleware retry on provider error" | `agent/middleware/model_fallback.py` |
| "sandbox circuit breaker timeout" | `agent/middleware/sandbox_circuit_breaker.py` |

Both are the correct file. This is a spot check on two queries, not a precision/recall
study — stated plainly rather than dressed up as a benchmark.

### 6.2 Failure classifier accuracy on real runner output

The classifier was validated against genuine `pytest` output produced by running real
broken code (not hand-written strings): **6/6 categories correct** — `test_assertion`,
`syntax`, `dependency`, `runtime`, `type`, and correctly declining to classify a passing
run. Parametrised regression tests in `tests_sweforge/test_subsystems.py` cover 11
category cases plus the ordering subtlety that `pytest` echoes the failing source line
(`assert add(2,3) == 5`) even when the real cause is a `ValueError`.

### 6.3 Stripped SWE-Forge vs full SWE-Forge (technology-level comparison)

This compares A_baseline (a stripped, linear, single-pass **SWE-Forge** graph)
against E_full (the adaptive graph). Both sides are SWE-Forge — this is *not*
an Open SWE comparison, which is Experiment B and is UNAVAILABLE here.

| Measure | A_baseline (fixed) | E_full (adaptive) |
|---|---|---|
| Task completion | 50% | 67% (+1 held by gate) |
| Verification success | 50% | 83% |
| Recovery success | n/a (impossible) | 75% |
| Avg recovery attempts | n/a | 1.50 |
| Nodes executed | 48 | 82 |
| Model calls | 12 | 24 |
| Tool calls | 0 | 56 |
| Avg wall time | 0.83 s | 1.61 s |
| Unsafe change shipped | **yes** | **no** |

The purpose is not to argue LangGraph is universally better. It is to show these
architectural decisions can be evaluated empirically at all — and that each one's cost
and benefit is visible rather than asserted.

---

## 7. Limitations of this evaluation

Stated plainly, because a reviewer will find them anyway:

1. **Six scenarios is a small suite.** Enough to cover every terminal state; not enough
   for statistical claims. No confidence intervals are offered because none would be
   meaningful at n=6.
2. **Scripted models cannot evaluate planning quality.** Any component whose value flows
   through the *content* of an LLM decision (repository intelligence, memory, model
   routing) is invisible to this harness end-to-end.
3. **Fixtures are small.** Four single-module Python projects, not repositories with
   thousands of files and slow integration suites.
4. **Cost figures are synthetic.** Real cost requires a live-model run.
5. **Model routing is measured but unproven.** The harness reports which tier was
   selected and what it cost. It does **not** show routing improves outcomes — that
   claim is not made anywhere in this repository.
6. **No public benchmark.** SWE-bench was not run.
7. **Single machine, single run per cell.** Wall-clock figures are indicative; no
   repeated-trial variance analysis was performed.

## 8. Generated artefacts

| Path | Contents |
|---|---|
| `evaluation/results/results.json` | Raw per-run records (machine-readable) |
| `evaluation/reports/EVALUATION_REPORT.md` | Full generated Markdown report |
| `evaluation/reports/variant_metrics.csv` | Per-variant aggregates |
| `evaluation/reports/run_details.csv` | One row per (scenario × variant) |
| `evaluation/reports/summary.json` | Aggregated metrics + routing correctness |
