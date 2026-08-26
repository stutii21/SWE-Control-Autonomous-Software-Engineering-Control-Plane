# Phase 25 — Final Gap Audit

> **HISTORICAL DOCUMENT.** This is point-in-time evidence from an earlier phase,
> retained for traceability. Numbers here reflect the repository *at that time*
> and may not match current state. For current, source-verified figures see
> `docs/FINAL_PROJECT_STATUS.md` and `docs/PROJECT_CLAIMS.md`.

**Method:** direct source inspection. Documentation claims were treated as unverified and
checked against code. Commands used are shown so every finding is reproducible.

**Baseline state entering Phase 25:** 358 tests passing, lint/format/mypy clean,
Experiment A 30/30 with 6/6 routing correctness.

**Bar applied:** a capability counts as present only if it is reachable, used, tested, and
its documentation matches reality.

---

## Summary

| # | Area | Status |
|---|---|---|
| 1 | Package / installability | **MISSING** — no SWE-Forge packaging metadata |
| 2 | LangGraph registration | **MISSING** — absent from `langgraph.json` |
| 3 | Reproducibility (seed, manifest, commit SHA) | **MISSING** |
| 4 | Deterministic evaluation | **PRESENT** — Experiment A, 30/30 |
| 5 | Real-world benchmark infrastructure | **MISSING** — no `evaluation/benchmarks/` |
| 6 | Live-model infrastructure | **PARTIAL** — config only, no runner |
| 7 | Open SWE baseline execution | **PARTIAL** — adapter reaches `get_agent`; execution UNAVAILABLE |
| 8 | Statistical methodology | **MISSING** — no paired tests, no CI/bootstrap |
| 9 | Repository-intelligence evaluation | **MISSING** — no retrieval benchmark |
| 10 | Memory evaluation | **MISSING** — no M0/M1 experiment |
| 11 | Model-routing evaluation | **MISSING** — no R0/R1 experiment |
| 12 | Recovery evaluation | **PARTIAL** — aggregate only, no per-category matrix |
| 13 | Security evaluation | **PARTIAL** — unit-tested, no invariant tests |
| 14 | Observability | **PARTIAL** — LangSmith optional, **no local trace artifact** |
| 15 | CI | **MISSING** — no SWE-Forge workflow |
| 16 | Artifact generation | **PARTIAL** — reports overwrite; no per-run artifacts |
| 17 | Graph invariant tests | **MISSING** |
| 18 | Security invariant tests | **MISSING** |
| 19 | Budget invariant tests | **PARTIAL** — unit-tested, not asserted as graph invariants |
| 20 | Documentation/source traceability | **PRESENT** — 45-row table in `CUSTOMIZATIONS.md` |

**7 missing, 8 partial, 5 present.** Nothing found is architecturally wrong; the gaps are
in packaging, reproducibility, and measurement of components previously reported as
*implemented but unmeasured*.

---

## Detailed findings

| # | Requirement | Evidence | Status | Remediation |
|---|---|---|---|---|
| 1 | **Packaging** | `grep -n sweforge pyproject.toml` → 0 hits. No `setup.py`/`setup.cfg`. Users must know `PYTHONPATH` behaviour. | **MISSING** | Add SWE-Forge packaging metadata + `sweforge` console entry point + `__main__.py`. Do **not** package upstream Open SWE. |
| 2 | **LangGraph registration** | `langgraph.json` `graphs` = `agent`, `reviewer`, `analyzer`, `chat`, `scheduler`. **No SWE-Forge entry.** Phase 24 report admitted this. | **MISSING** | Register `sweforge` pointing at a real factory; leave the 5 upstream entries untouched. |
| 3 | **Reproducibility** | `grep -n "seed\|benchmark_version\|--output-dir" evaluation/runner.py` → 0 hits. Results carry no commit SHA, package versions or env metadata. | **MISSING** | Run manifest with commit SHA, versions, seed, benchmark version, budget config. Per-run artifact directory. |
| 4 | **Deterministic evaluation** | `evaluation/runner.py` → 30 runs, fixture isolation, honest denominators. | **PRESENT** | Keep. Add repeat-run determinism verification. |
| 5 | **Real-world benchmark** | `ls evaluation/benchmarks` → does not exist. Four toy fixtures only. | **MISSING** | Schema + loader + dry-run + scorer. **No fabricated results.** |
| 6 | **Live-model infrastructure** | `evaluation/live/config.py` exists (config + availability only). No runner executes a live task. | **PARTIAL** | Add live runner with budget/retry guard; `LIVE_EVALUATION_UNAVAILABLE` without credentials. |
| 7 | **Open SWE baseline** | `OpenSWEBaseline.resolve_agent_factory()` returns the genuine `agent.server.get_agent`. `comparable_pairs = 0` — needs model **and** sandbox credentials. | **PARTIAL** | Keep. Add paired statistics for when credentials exist. |
| 8 | **Statistical methodology** | No bootstrap, no McNemar, no pairing by `task_id`. | **MISSING** | Add paired analysis that **refuses** to conclude below a minimum sample size. |
| 9 | **Repository intelligence eval** | Phase 24 correctly reported "no measured end-to-end effect" (variants A and B identical). No retrieval benchmark exists. | **MISSING** | Ground-truth retrieval benchmark: P@1/3/5, R@5, MRR, latency, lexical vs graph vs hybrid. |
| 10 | **Memory eval** | `ExperienceStore` tested in isolation; no controlled M0/M1 experiment. | **MISSING** | M0 vs M1 on related tasks. Report neutral/negative honestly. |
| 11 | **Routing eval** | `ModelRouter` tested in isolation. No R0/R1, no cost-vs-success analysis. | **MISSING** | R0 fixed vs R1 adaptive; tier distribution, cost, fallback rate. |
| 12 | **Recovery eval** | `metrics.py` aggregates recovery success but not **by failure category**. | **PARTIAL** | Failure matrix over the classifier's 10 categories; mark untested categories `untested`. |
| 13 | **Security eval** | `TestSecurityScanner`/`TestRiskEngine` are unit tests. No test proves HIGH risk *cannot* reach finalization through the graph. | **PARTIAL** | Security invariant tests at graph level. |
| 14 | **Observability** | `grep -rn "traces.jsonl\|local_trace\|TraceWriter" agent/sweforge/` → **0 hits.** Tracing exists only via optional LangSmith; with it disabled, **no durable trace artifact is produced**. | **PARTIAL** | Local `TraceRecorder` writing `traces.jsonl`, always on; LangSmith becomes an optional sink. |
| 15 | **CI** | `.github/workflows/` has 6 upstream workflows; `grep -rln sweforge .github/workflows/` → 0. | **MISSING** | Add a SWE-Forge workflow needing **no** credentials. |
| 16 | **Artifact generation** | `evaluation/reports/` is overwritten each run; no `<run_id>` directory, no `traces.jsonl`. | **PARTIAL** | Per-run artifact directory with manifest, results, metrics, traces, summary. |
| 17 | **Graph invariants** | No test asserts topology properties. Graph *is* introspectable (`get_graph()` → 19 nodes, 29 edges), so this is testable. | **MISSING** | Property tests over the compiled graph. |
| 18 | **Security invariants** | No test proves budget limits are unreachable from model output, or MCP deny-by-default cannot be bypassed by model text. | **MISSING** | Explicit invariant suite. |
| 19 | **Budget invariants** | 12 unit tests exist (`TestExecutionBudget`). Not asserted as graph-level invariants. | **PARTIAL** | Add "budget exhaustion always reaches a terminal state". |
| 20 | **Doc traceability** | `docs/CUSTOMIZATIONS.md` has a 45-row requirement→symbol→test table; Phase 24 verified counts against source. | **PRESENT** | Extend for Phase 25 additions. |

## Additional observations

| Observation | Detail |
|---|---|
| Graph is introspectable | `get_graph()` → 19 nodes / 29 edges (includes `__start__`/`__end__`; `build_nodes()` → 17 domain nodes). Makes Part 16 invariants straightforward. |
| Concurrency untested | `subtask_workers > 1` code path exists; the only test asserts evaluation pins it to 1. No isolation test. |
| No stale generated artefacts committed | `git status --porcelain` shows only intended SWE-Forge files. `.sweforge/` and caches gitignored. |
| Upstream `uv.lock` present | Upstream pins its own deps. SWE-Forge needs its own minimal pin set, **without** adding a second package manager to the repo. |

## Remediation plan (priority order)

Ordered by how much each closes the gap between *claimed* and *demonstrable*:

1. **Local trace artifact** (14, 16, 18) — correctness must not depend on LangSmith.
2. **Graph + security + budget invariant tests** (17, 18, 19) — architecture-level proof.
3. **LangGraph registration** (2) + **packaging/entry point** (1).
4. **Reproducible run manifests** (3, 16, 21).
5. **Measurement experiments**: retrieval (9), memory (10), routing (11), recovery matrix (12) — turning three "implemented but unmeasured" components into measured ones, *reporting negative results if that is what they show*.
6. **Real benchmark harness with dry-run** (5) and **paired statistics** (8, 13).
7. **Live runner** (6, 14) with `LIVE_EVALUATION_UNAVAILABLE`.
8. **Showcase command** (Part 4) and **CI** (15).
9. **Concurrency isolation test** (22), cleanup (23), final docs (24, 27).

### Constraints carried into this phase

- **No credentials.** Live-model, Open SWE head-to-head and real-benchmark execution stay
  UNAVAILABLE. Harnesses will be implemented and dry-run validated only.
- **Toy fixtures stay toy fixtures.** `REAL_WORLD_BENCHMARK = NOT_AVAILABLE`.
- **Negative results stay.** If retrieval, memory or routing show no benefit, that is the
  reported finding.
