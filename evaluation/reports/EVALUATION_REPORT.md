# SWE-Forge Evaluation Report

- Generated: `2026-08-26T00:07:37Z`
- Suite wall time: `37.701s`
- Model mode: `scripted-deterministic`
- Scenarios: 6
- Variants: 5
- Total executions: 30

## How to read this report

- Model behaviour is pinned by evaluation/scenarios.py so that graph orchestration is the only variable.
- Token counts and therefore cost figures are SYNTHETIC under scripted models; they demonstrate ledger accounting, not provider billing.
- Test results, recovery counts, routing paths, gate decisions and wall-clock times are REAL measurements from executed runs.

> **Scope of the claim.** This evaluation measures *orchestration*, not model capability. Model outputs are pinned by scripted fixtures so that the graph topology is the only variable across variants. Test results, routing paths, recovery counts, gate decisions and wall-clock times are real measurements from executed runs against real `pytest` suites. Token and cost columns are synthetic and exist to demonstrate ledger accounting only.

## Ablation results

| Variant | Runs | Task success | Verification pass | First-attempt | Recovery success | Avg recovery attempts | Escalations | Human-approval gate |
|---|---|---|---|---|---|---|---|---|
| A. Baseline (single pass, fixed path) | 6 | 50% | 50% | 50% | n/a | n/a | 0 | 0 |
| B. + Repository intelligence | 6 | 50% | 50% | 50% | n/a | n/a | 0 | 0 |
| C. + Bounded self-repair | 6 | 83% | 83% | 50% | 67% | 1.67 | 1 | 0 |
| D. + Independent review gate | 6 | 83% | 83% | 33% | 75% | 1.50 | 1 | 0 |
| E. Full SWE-Forge (+ security & risk gate) | 6 | 67% | 83% | 33% | 75% | 1.50 | 1 | 1 |

## Cost and effort per variant

| Variant | Nodes executed | Model calls | Verification runs | Tool calls | Avg wall time (s) | Tokens (synthetic) | Est. cost USD (synthetic) |
|---|---|---|---|---|---|---|---|
| A. Baseline (single pass, fixed path) | 54 | 18 | 6 | 12 | 0.784 | 5791 | 0.1021 |
| B. + Repository intelligence | 54 | 18 | 6 | 108 | 0.800 | 6240 | 0.1088 |
| C. + Bounded self-repair | 70 | 23 | 11 | 130 | 1.419 | 11056 | 0.1344 |
| D. + Independent review gate | 78 | 30 | 12 | 138 | 1.601 | 14728 | 0.1984 |
| E. Full SWE-Forge (+ security & risk gate) | 88 | 30 | 12 | 170 | 1.586 | 14728 | 0.1984 |

## Assurance activity

| Variant | Runs reviewed | Review interventions | Security findings | Risk-gate HIGH interventions |
|---|---|---|---|---|
| A. Baseline (single pass, fixed path) | 0 | 0 | 0 | 0 |
| B. + Repository intelligence | 0 | 0 | 0 | 0 |
| C. + Bounded self-repair | 0 | 0 | 0 | 0 |
| D. + Independent review gate | 5 | 1 | 0 | 0 |
| E. Full SWE-Forge (+ security & risk gate) | 5 | 1 | 1 | 1 |

## Per-scenario terminal states

| Scenario | A baseline | B repo intel | C recovery | D reviewer | E full |
|---|---|---|---|---|---|
| `billing_validation_first_try` | completed | completed | completed | completed | completed |
| `inventory_boundary_recovery` | failed | failed | completed | completed | completed |
| `textutil_syntax_recovery` | failed | failed | completed | completed | completed |
| `inventory_recovery_exhausted` | failed | failed | escalated recovery exhausted | escalated recovery exhausted | escalated recovery exhausted |
| `billing_review_rejection` | completed | completed | completed | completed | completed |
| `pipeline_secret_risk_gate` | completed | completed | completed | completed | awaiting human approval |

## Graph routing correctness (full variant)

Each scenario declares the terminal state its design should produce. This checks that the graph routed as intended, independently of task success.

**6/6 scenarios routed exactly as designed** (100%).

| Scenario | Expected terminal state | Observed | Match |
|---|---|---|---|
| `billing_validation_first_try` | completed | completed | PASS |
| `inventory_boundary_recovery` | completed | completed | PASS |
| `textutil_syntax_recovery` | completed | completed | PASS |
| `inventory_recovery_exhausted` | escalated_recovery_exhausted | escalated_recovery_exhausted | PASS |
| `billing_review_rejection` | completed | completed | PASS |
| `pipeline_secret_risk_gate` | awaiting_human_approval | awaiting_human_approval | PASS |

## What the numbers actually show

1. **Bounded self-repair is the component that moves task success.** Baseline reaches 50%; adding the recovery loop reaches 83%. The two scenarios that flip are the ones whose first implementation attempt was scripted wrong — precisely the case a single-pass workflow cannot address, because nothing re-reads the failing output.

2. **Repository intelligence shows no measurable effect on task success in this harness — a negative result, reported as such.** This is expected and is a limitation of the method, not evidence the subsystem is useless: the planner's output is pinned by the scripted fixture, so richer planning evidence cannot change the plan. Repository intelligence is measured directly instead of end-to-end (see `docs/EVALUATION.md`, static-analysis benchmarks), and its end-to-end value is untested until a live-model run is performed.

3. **The independent review gate catches work that tests call green.** The `billing_review_rejection` scenario passes verification under variant C and terminates as complete; under variant D the reviewer records 1 intervention(s), which routes the run back through recovery and produces the fully-validated implementation. Passing tests are necessary but not sufficient, and this is the measurement of that claim.

4. **The risk gate blocks a change that every other variant shipped.** In `pipeline_secret_risk_gate` the change is functionally correct and verification is green, so variants A-D all terminate as `completed`. Variant E scores it HIGH (committed credential plus a CI workflow edit) and terminates in `awaiting_human_approval` instead — 1 gate intervention(s). This is the clearest single argument for a deterministic risk layer in an autonomous system.

5. **The recovery loop terminates.** The `inventory_recovery_exhausted` scenario scripts an endlessly-wrong repair. Every recovery-enabled variant stops after exactly [3] attempt(s) and escalates, confirming the bound is structural (enforced by the routing function) rather than advisory.

6. **Variant E's lower headline task-success rate (67% vs 83%) is the risk gate working, not a regression.** `awaiting_human_approval` is not counted as success, and the only run that changes category is the one that tried to commit a credential. A benchmark that rewarded shipping that change would be measuring the wrong thing — which is why this report reports terminal states rather than a single success number.

7. **The assurance machinery is not free.** The full variant executes 88 nodes against the baseline's 54 (1.63x) and 30 model calls against 18. Whether that overhead is worth paying depends on the cost of shipping a bad change, which the `pipeline_secret_risk_gate` case illustrates.

## Unavailable runs

None. Every scenario x variant execution completed.
