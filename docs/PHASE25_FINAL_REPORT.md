# Phase 25 — Final Report

> **HISTORICAL DOCUMENT.** This is point-in-time evidence from an earlier phase,
> retained for traceability. Numbers here reflect the repository *at that time*
> and may not match current state. For current, source-verified figures see
> `docs/FINAL_PROJECT_STATUS.md` and `docs/PROJECT_CLAIMS.md`.

All values are from executed runs in this repository. Every unavailable
capability is marked UNAVAILABLE with its exact blocker. Nothing is estimated.

**Benchmark version:** 1.0.0 · **Model mode:** scripted-deterministic ·
**Seed:** 0 (scripted runs are deterministic by construction)

---

## 1. Source audit

`docs/PHASE25_FINAL_GAP_AUDIT.md`, written before any code changed, inspected 20
areas by source rather than documentation:

**7 missing · 8 partial · 5 present.**

Most significant finding: **`grep -rn "traces.jsonl\|local_trace\|TraceWriter"
agent/sweforge/` returned 0 hits** — the only trace was LangSmith, so with
tracing disabled a run produced *no durable record of what happened*. That is the
wrong dependency direction for a system that edits code autonomously.

Also missing: packaging metadata, LangGraph registration (a limitation Phase 24
had itself flagged), run manifests/seeds, benchmark infrastructure, statistical
methodology, component-level experiments for the three subsystems previously
reported as "implemented but unmeasured", CI, and graph/security invariant tests.

## 2. Changes made

| Area | Change |
|---|---|
| Observability | `TraceRecorder`: always-on local JSONL trace with write-time redaction; LangSmith demoted to optional sink |
| Tool tracing | Centralised in the tool guard so graph-owned *and* agent `bind_tools` calls are recorded once, with node/agent attribution |
| LangGraph registration | `graph/entrypoint.py`; `sweforge` + `sweforge_baseline` added to `langgraph.json`, all 5 upstream entries preserved |
| Packaging | `sweforge-pyproject.toml` (name, version, `sweforge` console script, `dev`/`openswe`/`live` extras); `agent/sweforge/__main__.py` |
| Showcase | `cli.py showcase` — vertical execution flow, metrics, JSON artifact + `traces.jsonl` |
| Reproducibility | `reproducibility.py` (manifest with commit SHA, versions, seed, credential *presence*), `--seed/--benchmark-version/--output-dir/--repeat` |
| Experiments | `experiments.py`: retrieval (P@k/R@k/MRR), memory M0/M1, routing R0/R1, recovery matrix |
| Benchmark harness | `benchmarks/harness.py`: schema, loader, dry-run, paired scorer with McNemar + bootstrap CI |
| Invariants | `test_invariants.py`: 40 graph/security/budget/trace properties |
| CI | `sweforge.yml` (credential-free) + `sweforge-live.yml` (manual, gated environment) |
| Bug fix | Failure classifier `configuration` rule regex |

**One implementation change, and only because validation proved a defect:** the
`configuration` rule ended in `\b` after an apostrophe. A word boundary following
a non-word character requires a following word character, so
`KeyError: 'DATABASE_URL'` at end-of-line could **never** match and fell through
to `runtime`. Found by the recovery matrix, fixed, 5 regression tests added.
Detection went from 9/10 to **10/10**.

## 3. Files added

```
agent/sweforge/observability/trace.py       TraceRecorder, redact
agent/sweforge/graph/entrypoint.py          LangGraph registration factory
agent/sweforge/__main__.py                  python -m agent.sweforge
evaluation/experiments.py                   4 component experiments
evaluation/reproducibility.py               RunManifest, RunArtifacts
evaluation/run_all_experiments.py           experiment driver + artifacts
evaluation/benchmarks/{__init__,harness}.py real benchmark harness
evaluation/benchmarks/manifest.json         benchmark versioning
evaluation/benchmarks/example_swebench_lite.jsonl   schema example only
evaluation/configs/experiment_a.json        versioned configuration
tests_sweforge/test_invariants.py           40 invariant tests
tests_sweforge/test_experiments.py          28 experiment/regression tests
tests_sweforge/test_phase25.py              40 infrastructure tests
sweforge-pyproject.toml                     packaging metadata
.github/workflows/sweforge.yml              credential-free CI
.github/workflows/sweforge-live.yml         manual live evaluation
docs/PHASE25_FINAL_GAP_AUDIT.md  docs/PROJECT_CLAIMS.md
docs/INTERVIEW_GUIDE.md          docs/PHASE25_FINAL_REPORT.md
```

## 4. Files modified

| File | Change | Why |
|---|---|---|
| `langgraph.json` | **+2** graph entries | Registration; upstream's 5 entries untouched |
| `agent/sweforge/graph/workflow.py` | tracer on runtime, node wrapping | Local trace |
| `agent/sweforge/tools/registry.py` | tracer in guard, context back-ref | Single tracing point |
| `agent/sweforge/agents/tool_loop.py` | agent attribution | Trace agent tool calls |
| `agent/sweforge/runner.py` | attach tracer at construction | Capture the first tool call |
| `agent/sweforge/cli.py` | `showcase` command | Part 4 |
| `agent/sweforge/recovery/classifier.py` | regex fix | Real defect |
| `evaluation/runner.py` | seed/manifest/repeat flags | Reproducibility |
| `evaluation/scenarios.py` | `script_target()` helper | Showcase tool calls |

Upstream files modified remains **3** total: `.gitignore`, `README.md`,
`langgraph.json` — all additive. `LICENSE` retains `Copyright (c) LangChain, Inc.`

## 5. Test count

**466 passing** (Phase 24: 358 → **+108**), ~35s, no API key, no network.

| Suite | Tests |
|---|---|
| `test_core.py` | 62 |
| `test_subsystems.py` | 73 |
| `test_graph.py` | 62 |
| `test_evaluation.py` | 40 |
| `test_phase23.py` | 121 |
| `test_invariants.py` | 40 |
| `test_experiments.py` | 28 |
| `test_phase25.py` | 40 |

No test was removed or weakened.

## 6. Quality results

| Gate | Command | Result |
|---|---|---|
| Tests | `pytest -c pytest-sweforge.ini` | **466 passed** |
| Lint | `ruff check agent/sweforge evaluation tests_sweforge` | **All checks passed** |
| Format | `ruff format --check ...` | **77 files already formatted** |
| Types | `mypy` on 7 modules | **Success: no issues found** |
| Import isolation | 41 modules, `fastapi`/`deepagents` blocked | **0 failures** |
| Secret scan | 10 patterns across the SWE-Forge tree | **0 non-PEM matches** |

## 7. Experiment A results

*Benchmark v1.0.0 · n=6 scenarios × 5 variants = 30 runs · 0 unavailable ·
deterministic · scripted model · seed 0*

Outcomes reported **separately**, so the security gate never looks like a task failure:

| Variant | completed | awaiting_human | escalated | failed | budget_exhausted |
|---|---|---|---|---|---|
| A_baseline | 3 | 0 | 0 | 3 | 0 |
| B_repo_intel | 3 | 0 | 0 | 3 | 0 |
| C_recovery | 5 | 0 | 1 | 0 | 0 |
| D_reviewer | 5 | 0 | 1 | 0 | 0 |
| E_full | 4 | **1** | 1 | 0 | 0 |

| Variant | Task success | Verification | First-attempt | Recovery success | Nodes | Model calls | Tool calls | Avg latency |
|---|---|---|---|---|---|---|---|---|
| A_baseline | 50% | 50% | 50% | n/a | 54 | 18 | 12 | 0.817s |
| B_repo_intel | 50% | 50% | 50% | n/a | 54 | 18 | 108 | 0.821s |
| C_recovery | **83%** | **83%** | 50% | 67% | 70 | 23 | 130 | 1.446s |
| D_reviewer | 83% | 83% | 33% | 75% | 78 | 30 | 138 | 1.593s |
| E_full | 67%* | 83% | 33% | 75% | 88 | 30 | 170 | 1.603s |

**Routing correctness: 6/6.** **Determinism: 3/3 repeats IDENTICAL** (terminal
state, routing path, recovery count, tool sequence).

\* E's lower headline figure is the risk gate working — the one run that changes
category tried to commit a credential.

## 8. Repository intelligence results

*Deterministic · n=4 tasks · **no LLM in the causal path** · ground truth by construction*

| Strategy | P@1 | P@3 | P@5 | R@5 | MRR | Latency |
|---|---|---|---|---|---|---|
| A_lexical | 1.0 | 1.0 | 1.0 | 0.875 | 1.0 | ~0.0ms |
| B_graph | 1.0 | 1.0 | 1.0 | **1.0** | 1.0 | ~0.1ms |
| C_hybrid | 1.0 | 1.0 | 1.0 | **1.0** | 1.0 | ~0.1ms |

Graph-aware retrieval recovers the covering test that pure lexical matching
misses (R@5 1.0 vs 0.875). **Precision saturates at 1.0 for all three strategies:
the fixtures are too easy to discriminate on precision.** That is a limitation of
the benchmark, reported rather than spun.

## 9. Memory results

*Deterministic · n=2 follow-up tasks · corpus 3 records*

| Metric | M0 (no retrieval) | M1 (BM25) |
|---|---|---|
| Planner context from experience | 0 chars | 313.5 chars mean |
| Top-1 retrieved record relevant | n/a | **2/2 (100%)** |

**MEASURED:** relevant prior experience is retrieved for related tasks.
**NOT MEASURED:** whether that context improves task success — under scripted
models the plan is pinned, so no end-to-end effect is observable. No success
claim is made.

## 10. Routing results

*Deterministic · n=4 complexity levels · identical call pattern and token counts*

| Complexity | R0 fixed | R1 adaptive | Ratio | R1 tier distribution |
|---|---|---|---|---|
| trivial | $1.200 | $0.192 | **0.16** | balanced 7, fast 3 |
| simple | $1.200 | $0.480 | 0.40 | reasoning 3, coding 3, fast 3, balanced 1 |
| moderate | $1.200 | $0.480 | 0.40 | reasoning 3, coding 3, fast 3, balanced 1 |
| complex | $1.200 | $0.768 | 0.64 | reasoning 6, fast 3, balanced 1 |

**Mean cost ratio R1/R0 = 0.40.** Adaptive routing costs ~40% of a fixed
reasoning-tier model, and spends progressively more as complexity rises — the
intended behaviour.

**NOT MEASURED:** whether routing preserves task success. The reliability half of
the trade-off is **UNTESTED** and no such claim is made.

## 11. Recovery results

*Detection measured from real runner output; recovery from executed benchmark runs*

**Detection accuracy: 10/10 categories (was 9/10 before the regex fix).**

| Failure type | Detection | Status | Success rate | Avg attempts |
|---|---|---|---|---|
| syntax | correct | measured | 1.0 | 1.0 |
| test_assertion | correct | measured | 1.0 | 1.0 |
| unknown | correct | measured | 0.0 | 3.0 |
| dependency | correct | **untested** | n/a | n/a |
| type | correct | **untested** | n/a | n/a |
| runtime | correct | **untested** | n/a | n/a |
| configuration | correct | **untested** | n/a | n/a |
| environment | correct | **untested** | n/a | n/a |
| lint | correct | **untested** | n/a | n/a |
| timeout | correct | **untested** | n/a | n/a |

**3 measured, 7 untested.** Untested categories are marked as such, never assumed
to work.

## 12. Open SWE baseline status — UNAVAILABLE

| Claim | Status |
|---|---|
| A. Implementation exists | **YES** — `OpenSWEBaseline`, `preflight` |
| B. Test adapter works | **YES** — resolves the genuine `agent.server.get_agent` (`__module__ == "agent.server"`) |
| C. Live execution | **UNAVAILABLE** — `comparable_pairs = 0` |

Blocker: no model provider credential and no sandbox provider credential.
**No Open SWE vs SWE-Forge performance claim exists anywhere in this repository.**

## 13. Live-model status — UNAVAILABLE

```
{'track': 'C_live_model', 'available': False,
 'reason': 'SWEFORGE_EVAL_MODEL is not set; ANTHROPIC_API_KEY is not set'}
```

Runnable once configured (`docs/LIVE_EVALUATION.md`); the cost ceiling becomes a
real `BudgetLimits` so a live run terminates in `budget_exhausted` rather than
overspending. Manual CI workflow gated behind an approval environment.

## 14. Real-world benchmark status — NOT_AVAILABLE

```
REAL_WORLD_BENCHMARK = NOT_AVAILABLE
```

Harness implemented and dry-run validated: schema, loader, paired scorer with
McNemar and bootstrap CI (seed 0). Dry run parses tasks and names blockers
**without downloading or executing anything** (`executed: False`). The scorer
returns `INSUFFICIENT_SAMPLE` below 20 paired tasks and refuses a verdict.

The four fixtures are labelled **deterministic architectural fixtures**
throughout. No GitHub issues were invented to inflate the suite.

## 15. MCP status

Implemented, graph-integrated (`external_context` node), tested against
deterministic fixtures whose payloads carry `_fixture: true`. Verified: discovery
+ schemas, deterministic selection, deny-by-default, timeout (3 bounded attempts),
retry→success, per-run cap, execution-budget consumption. **No live MCP server;
no external result fabricated.**

## 16. Security status

- **0 non-PEM secret matches**; the 5 PEM headers carry no key material (3 in the
  scanner's own tests, 2 in upstream's GitHub App docs).
- 14 security invariant tests, including: HIGH risk can never auto-finalize
  (verified at 55/70/90/100); an approving reviewer cannot release a HIGH-risk
  change; no structured-output field maps to a budget limit; MCP deny-by-default
  resists name-variation bypass; host execution refused without opt-in; PR
  preparation cannot bypass the gate; secrets redacted from traces.
- Trace redaction happens at **write time**, so a secret never reaches disk.

## 17. Reproducibility status

Manifests record commit SHA, dirty flag, Python version, package versions,
upstream presence, benchmark version, seed, budget config and credential
*presence* (never values). Artifacts land in `evaluation/artifacts/<run_id>/`
with `manifest.json`, `results.json`, `metrics.csv`, `traces.jsonl`, `summary.md`.

`--repeat 3` verified **IDENTICAL** signatures across repeats. Benchmark
versioning (`manifest.json`) prevents apples-to-oranges comparisons.

## 18. CI status

`sweforge.yml` runs 13 credential-free steps: lint, format, mypy, tests, import
isolation, secret scan, graph-registration check, deterministic evaluation smoke
test, benchmark dry-run, artifact upload. **No API keys, sandbox, GitHub App, MCP
server or LangSmith required.** Live evaluation is a separate manual workflow
behind a gated environment, so a fork PR cannot spend money or reach secrets.

Both workflows parse; step logic was executed locally, but **GitHub Actions
itself has not run** — CI is validated locally, not observed green on a runner.

## 19. Known limitations

**Unavailable** (implemented, not executable here): Open SWE head-to-head, live
model, real benchmark, live MCP, live GitHub PR creation, Open SWE sandbox
backend.

**Statistical:** n=6 scenarios on 4 toy fixtures; one run per cell; no confidence
intervals (none meaningful at n=6); token counts synthetic so cost is ledger
accounting, not spend.

**Measurement:** repository intelligence, memory and routing have **no measured
end-to-end task-success effect** — structurally invisible to a scripted harness.
Retrieval precision saturates at 1.0 (fixtures too easy). 7 of 10 recovery
categories untested.

**Implementation:** Python-only AST; `find_callers` does not resolve
aliased/dynamic call sites; regex risk screening defeatable by obfuscation;
hand-tuned risk weights; concurrency isolation tested but **no speedup measured
or claimed**.

## 20. Verified resume claims

Only from `docs/PROJECT_CLAIMS.md` § VERIFIED:

- Built a deterministic LangGraph control plane (17 nodes, 5 routers, 4 terminal
  states, bounded recovery/review loops) on LangChain's Open SWE, with **40
  architecture-level invariant tests** proving properties like "HIGH-risk changes
  can never auto-finalize" and "recovery cannot exceed its bound".
- **466 tests**, zero API keys or network; lint, format, mypy and secret scan
  clean; credential-free CI.
- Ablation across 5 workflow variants (30 runs): bounded self-repair raised task
  success **50% → 83%**; the risk gate blocked a credential-committing change
  (**90/100 HIGH**) that every other variant shipped; **6/6** routing correctness;
  **3/3 identical** repeat runs.
- Measured components the end-to-end harness could not: graph retrieval **R@5 1.0
  vs 0.875** lexical; adaptive routing at **40% the cost** of a fixed
  reasoning-tier model; failure detection **10/10** categories.
- Published negative results rather than hiding them, including a component with
  **no measurable end-to-end effect**.

## 21. Recommended next experiment

**Live-model Experiment C**, ~$20 of credits: run the 6 fixtures against a real
model in both variant A and variant E. It is the only way to test the three
components currently unmeasurable end-to-end, and it converts the routing result
from a cost claim into a genuine cost/reliability Pareto result. Then Experiment
B on ≥20 paired tasks, where McNemar and the bootstrap CI (both already
implemented) become meaningful.

---

## Reproduce everything

```bash
export SWEFORGE_ALLOW_LOCAL_EXEC=1

python -m pytest -c pytest-sweforge.ini                    # 466 passed
python -m ruff check agent/sweforge evaluation tests_sweforge
python -m ruff format --check agent/sweforge evaluation tests_sweforge
python -m mypy --ignore-missing-imports --explicit-package-bases \
  agent/sweforge/budget.py agent/sweforge/observability/trace.py \
  agent/sweforge/schemas.py

python -m agent.sweforge.cli showcase                      # one-command demo
python -m evaluation.runner --repeat 3 --seed 0            # Experiment A + determinism
python -m evaluation.evaluator
python -m evaluation.run_all_experiments                   # retrieval/memory/routing/recovery
python -m evaluation.experiment_b                          # UNAVAILABLE, by design
python -c "from evaluation.benchmarks import load_tasks, dry_run; \
           print(dry_run(load_tasks('evaluation/benchmarks/example_swebench_lite.jsonl')))"
python -c "from agent.sweforge.graph.entrypoint import describe_graph; print(describe_graph())"
```
