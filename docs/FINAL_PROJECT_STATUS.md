# Final Project Status

**STATUS: ARCHITECTURE FROZEN**

No further architectural phases. Future work is driven by experimental evidence:
if live-model credentials become available, run the benchmark; if the benchmark
reveals a weakness, make a targeted, evidence-backed change.

All values below were produced by executing code in this repository and are
verified against source by `python -m evaluation.check_docs` (13/13 passing) and
by `tests_sweforge/test_docs_consistency.py` in CI.

**Benchmark version** 1.0.0 · **Seed** 0 · **Model mode** scripted-deterministic

---

## TESTS

**478 passing** · ~42 s · no API key, no network · stable across repeated runs.

Verified in a **clean virtual environment with no inherited `PYTHONPATH`**.

| Suite | Tests |
|---|---|
| `test_core.py` | 62 |
| `test_subsystems.py` | 73 |
| `test_graph.py` | 62 |
| `test_evaluation.py` | 40 |
| `test_phase23.py` | 121 |
| `test_invariants.py` | 40 |
| `test_experiments.py` | 29 |
| `test_phase25.py` | 40 |
| `test_docs_consistency.py` | 11 |

## DETERMINISTIC EVALUATION (Experiment A)

30 runs · 6 scenarios × 5 variants · 0 unavailable · **deterministic**

Outcomes reported separately so the security gate is never mistaken for failure:

| Variant | completed | awaiting_human | escalated | failed | Task success | Verification | Recovery success |
|---|---|---|---|---|---|---|---|
| A_baseline | 3 | 0 | 0 | 3 | 50% | 50% | n/a |
| B_repo_intel | 3 | 0 | 0 | 3 | 50% | 50% | n/a |
| C_recovery | 5 | 0 | 1 | 0 | **83%** | **83%** | 67% |
| D_reviewer | 5 | 0 | 1 | 0 | 83% | 83% | 75% |
| E_full | 4 | **1** | 1 | 0 | 67%* | 83% | 75% |

**Routing correctness: 6/6.** **Determinism: 3/3 repeats identical** (terminal
state, routing path, recovery count, tool sequence).

\* E's lower headline figure is the risk gate working — the single run that
changes category attempted to commit a credential.

## LIVE EVALUATION

**UNAVAILABLE** — no model provider credential.
`LIVE_EVALUATION_UNAVAILABLE`. Track implemented (`evaluation/live/`), runnable
once configured, with a hard cost ceiling. **No live-model result is claimed.**

## OPEN SWE HEAD-TO-HEAD

**UNAVAILABLE** — `comparable_pairs = 0`.

The adapter resolves the genuine upstream `agent.server.get_agent`
(`__module__ == "agent.server"`, verified). Execution requires model **and**
sandbox credentials. **No Open SWE vs SWE-Forge performance claim exists
anywhere in this repository.**

## REAL-WORLD BENCHMARK

**UNAVAILABLE** — `REAL_WORLD_BENCHMARK = NOT_AVAILABLE`.

Harness implemented and dry-run validated (schema, loader, paired scorer with
McNemar + bootstrap CI, seed 0). Dry run parses tasks and names blockers without
downloading or executing anything (`executed: False`). The scorer returns
`INSUFFICIENT_SAMPLE` below 20 paired tasks and refuses a verdict.

The four fixtures are **deterministic architectural fixtures**, not a benchmark.

## REPOSITORY INTELLIGENCE

*Deterministic · n=4 · no LLM in the causal path · ground truth by construction*

| Strategy | P@1 | P@3 | P@5 | R@5 | MRR |
|---|---|---|---|---|---|
| A_lexical | 1.0 | 1.0 | 1.0 | 0.875 | 1.0 |
| B_graph | 1.0 | 1.0 | 1.0 | **1.0** | 1.0 |
| C_hybrid | 1.0 | 1.0 | 1.0 | **1.0** | 1.0 |

Graph-aware retrieval recovers a covering test that lexical matching misses.
**Precision saturates at 1.0 for all three strategies — the fixtures are too easy
to discriminate on precision.** Reported, not spun.

Indexing the current repository (upstream + overlay): **907 files, 7,429 symbols,
1,036 import edges, 278 test files, ~1.5 s.**

**NOT MEASURED:** effect on task success (variants A and B are identical).

## MEMORY

*Deterministic · n=2 follow-up tasks · corpus 3 records*

| Metric | M0 (none) | M1 (BM25) |
|---|---|---|
| Planner context from experience | 0 chars | 313.5 chars mean |
| Top-1 retrieved record relevant | n/a | **2/2 (100%)** |

**NOT MEASURED:** effect on task success. No improvement is claimed.

## ROUTING

*Deterministic · n=4 complexity levels · identical call pattern and token counts*

**Mean cost ratio R1/R0 = 0.40** — adaptive routing costs ~40% of a fixed
reasoning-tier model, rising with complexity (0.16 trivial → 0.64 complex).

**NOT MEASURED:** whether routing preserves reliability. The reliability half of
the trade-off is **UNTESTED**.

## RECOVERY

**Detection accuracy: 10/10 categories.**

| Measured | Untested |
|---|---|
| syntax, test_assertion, unknown (3) | dependency, type, runtime, configuration, environment, lint, timeout (7) |

Untested categories are marked as such, never assumed to work.

## SECURITY

- **0 non-PEM secret matches** across the SWE-Forge tree. The 5 PEM headers carry
  no key material (3 in the scanner's own tests, 2 in upstream GitHub App docs).
- No `.env`, credentials, virtualenv, cache or temporary benchmark output tracked.
- **14 security invariant tests**: HIGH risk can never auto-finalize (verified at
  55/70/90/100); an approving reviewer cannot release a HIGH-risk change; no
  structured-output field maps to a budget limit; MCP deny-by-default resists
  name-variation bypass; host execution refused without opt-in; PR preparation
  cannot bypass the gate; secrets redacted from traces at write time.
- Worked example: `pipeline_secret_risk_gate` → **90/100 HIGH** →
  `awaiting_human_approval`, while variants A–D shipped the change.

## REPRODUCIBILITY

**PASS.**

- Run manifests record commit SHA, dirty flag, Python version, package versions,
  benchmark version, seed, budget config, and credential *presence* (never values).
- Artifacts per run: `manifest.json`, `results.json`, `metrics.csv`,
  `traces.jsonl`, `summary.md`.
- `--repeat 3` → **IDENTICAL** signatures.
- Full suite, showcase and evaluation re-verified in a clean venv with no
  inherited `PYTHONPATH`.
- Environment pinned in `requirements-sweforge.txt` (pip only; upstream keeps its
  own `uv.lock`).

## PACKAGING

**PASS.** `sweforge-pyproject.toml` (name, version, `sweforge` console script,
`dev`/`openswe`/`live` extras), `agent/sweforge/__main__.py` for
`python -m agent.sweforge`. Upstream is an *optional* extra, never vendored.

## CI

**LOCAL VALIDATION ONLY.**

The credential-free workflow (`.github/workflows/sweforge.yml`, 14 steps) parses,
and **every step has been executed locally**: lint, format, mypy, tests, import
isolation, secret scan, documentation consistency, graph registration,
deterministic evaluation smoke test, benchmark dry-run.

**CI workflow validated locally; not yet observed on GitHub Actions.**

Live evaluation is a separate, manually triggered, approval-gated workflow, so a
fork PR cannot spend money or reach secrets.

## PERFORMANCE (measured, no before/after claims)

| Operation | Time |
|---|---|
| Showcase (full pipeline, 15 nodes) | ~2.5 s |
| Deterministic evaluation (30 runs) | ~38 s |
| Full test suite (478) | ~42 s |
| Repository indexing (907 files) | ~1.5 s |
| Retrieval query | <1 ms |

Scripted-execution counts per showcase run: 6 model calls, 17 tool calls.
**No performance improvement is claimed** — there is no before/after comparison.

## KNOWN LIMITATIONS

**Unavailable** (implemented, not executable here): Open SWE head-to-head, live
model, real-world benchmark, live MCP server, live GitHub PR creation, Open SWE
sandbox backend.

**Statistical:** 6 scenarios on 4 single-module fixtures; one run per cell; no
confidence intervals (none meaningful at n=6); token counts synthetic under
scripted models, so cost is ledger accounting rather than spend.

**Measurement:** repository intelligence, memory and routing have **no measured
end-to-end task-success effect**; retrieval precision saturates at 1.0; 7 of 10
recovery categories untested.

**Implementation:** Python-only AST; `find_callers` does not resolve
aliased/dynamic call sites; regex risk screening is defeatable by obfuscation;
risk weights are hand-tuned judgement, not fitted; concurrency isolation is
tested but **no speedup is measured or claimed**.

**Process:** CI has not run on a GitHub runner.

## NEXT EXPERIMENT

**Real-model + Open SWE paired evaluation.**

Roughly $20 of credits: run the 6 fixtures against a real model in variants A and
E, then Experiment B on ≥20 paired tasks where McNemar and the bootstrap CI
(both implemented and tested) become meaningful.

This is the only way to test the three components currently unmeasurable
end-to-end, and it would convert the routing finding from a cost claim into a
genuine cost/reliability Pareto result.

---

## Upstream integrity

**3 upstream files modified, all additive:**

| File | Change |
|---|---|
| `.gitignore` | `!.env.example`, `.sweforge/` — upstream's `.env.*` rule was swallowing the template |
| `README.md` | Replaced; upstream original preserved verbatim at `docs/UPSTREAM_README.md` |
| `langgraph.json` | +2 SWE-Forge entries; upstream's 5 entries untouched |

No upstream Python module, test, config or license was edited or deleted.
`LICENSE` retains `Copyright (c) LangChain, Inc.`

## Project tree (SWE-Forge overlay)

```
agent/sweforge/          45 Python files — the SWE-Forge layer
  graph/                 workflow.py (StateGraph) · entrypoint.py (registration)
  state/                 graph_state.py — typed state + custom reducers
  agents/                roles.py · specialized.py (9 agents) · tool_loop.py
  repository/            analyzer.py (AST) · graph_index.py (import graph)
  verification/          backends.py (sandbox) · verifier.py
  recovery/              classifier.py — 10-category failure taxonomy
  security/              risk.py — scanner + deterministic risk engine
  routing/               model_router.py · execution_policy.py (retry/fallback)
  mcp/                   orchestration.py · fixtures.py
  memory/                store.py — BM25 experience retrieval
  observability/         trace.py (local, always on) · tracing.py (LangSmith)
  tools/                 registry.py (12 tools) · errors.py
  budget.py  schemas.py  runner.py  cli.py  __main__.py

evaluation/              harness, experiments, benchmarks, reproducibility
  runner.py evaluator.py metrics.py scenarios.py experiments.py
  baselines/  live/  benchmarks/  configs/  fixtures/  artifacts/
  check_docs.py          documentation-vs-source verification

tests_sweforge/          478 tests, no credentials, no network
scripts/                 sync_test_count.py
docs/                    architecture, evaluation, security, claims, diagrams
.github/workflows/       sweforge.yml (credential-free) · sweforge-live.yml (manual)
```
