# Final Freeze Audit

> **HISTORICAL DOCUMENT.** This is point-in-time evidence from an earlier phase,
> retained for traceability. Numbers here reflect the repository *at that time*
> and may not match current state. For current, source-verified figures see
> `docs/FINAL_PROJECT_STATUS.md` and `docs/PROJECT_CLAIMS.md`.

Written **before** any changes, as required. Every quantitative claim in the
documentation was checked against a value extracted from source or from an
executed run. Documentation claims were treated as unverified.

**Method:** a ground-truth probe (`build_nodes`, `get_graph()`, `build_tools`,
`AGENT_CLASSES`, `FinalStatus`, pytest collection, `RepositoryAnalyzer`) produced
the reference values below; docs were then grepped for contradicting numbers.

---

## Ground truth extracted from source

| Quantity | Value | Source |
|---|---|---|
| Domain nodes | **17** | `build_nodes()` |
| Graph nodes incl. `__start__`/`__end__` | 19 | `get_graph().nodes` |
| Graph edges | 29 | `get_graph().edges` |
| Conditional-edge sources (routers) | **5** | `task_intake`, `verification`, `failure_analysis`, `independent_review`, `risk_gate` |
| Terminal nodes | **4** | `finalization`, `human_approval`, `escalation`, `budget_exhausted` |
| `FinalStatus` values | **8** (7 terminal + `pending`) | `typing.get_args(FinalStatus)` |
| LangChain tools | **12** | `build_tools()` |
| Specialized agent classes | **6** | `AGENT_CLASSES` |
| Total agent classes | **9** (+ Implementation, Reviewer, Diagnostician) | source |
| SWE-Forge Python files | **45** | `agent/sweforge/**/*.py` |
| Importable `agent.sweforge.*` modules | 41 | `pkgutil.walk_packages` |
| Tests collected | **466** | pytest |

## Repository-intelligence measurement (re-run)

| Scope | Files | Symbols | Import edges | Test files | Time |
|---|---|---|---|---|---|
| Upstream-only (overlay excluded) | 812 | 6,315 | 841 | 266 | ~1.25s |
| **Full repo (current, incl. overlay)** | **907** | **7,429** | **1,036** | **278** | **~1.5s** |
| *Documented* | *812* | *6,385* | *843* | *267* | *~1.1s* |

---

## Findings

| # | Claim | Documented | Actual | Files affected | Status |
|---|---|---|---|---|---|
| 1 | Test count | **237** | 466 | `README.md:331`, `docs/CUSTOMIZATIONS.md:21,124`, `docs/DEMO.md:148` | **STALE — must fix** |
| 2 | SWE-Forge file count | **42 files** | 45 | `docs/INTERVIEW_GUIDE.md:101` | **WRONG — must fix** |
| 3 | Terminal statuses | "7 terminal statuses" | 8 values (7 terminal + `pending`) | `README.md:133` | **AMBIGUOUS — clarify** |
| 4 | Repo-intel statistics | 812 / 6,385 / 843 / 267 / 1.1s | 907 / 7,429 / 1,036 / 278 / 1.5s | `README.md`, `docs/EVALUATION.md`, `docs/INTERVIEW_GUIDE.md`, `docs/DEMO.md`, `docs/UPSTREAM_AUDIT.md` | **NO LONGER REPRODUCES — must fix** |
| 5 | Node count (17) | 17 | 17 | README, ARCHITECTURE, CUSTOMIZATIONS, INTERVIEW_GUIDE, PHASE25 | CORRECT |
| 6 | Router count (5) | 5 | 5 | README, ARCHITECTURE, PHASE24/25 | CORRECT |
| 7 | Terminal nodes (4) | 4 | 4 | README, CUSTOMIZATIONS, PHASE25, PROJECT_CLAIMS | CORRECT |
| 8 | Tool count (12) | 12 | 12 | CUSTOMIZATIONS, LANGCHAIN_LANGGRAPH, PHASE23/24, README | CORRECT |
| 9 | Agent counts (6 specialized / 9 total) | 6 / 9 | 6 / 9 | README, PHASE24, PROJECT_CLAIMS | CORRECT |
| 10 | Experiment A: 30 runs, 6/6 routing | 30, 6/6 | 30, 6/6 | EVALUATION, PHASE25 | CORRECT |
| 11 | Retrieval R@5 1.0 vs 0.875 | as stated | as stated | PHASE25, PROJECT_CLAIMS | CORRECT |
| 12 | Routing cost ratio 0.40 | 0.40 | 0.40 | PHASE25, PROJECT_CLAIMS | CORRECT |
| 13 | Recovery detection 10/10 | 10/10 | 10/10 | PHASE25, PROJECT_CLAIMS | CORRECT |
| 14 | Risk score 90/100 HIGH | 90 | 90 | README, EVALUATION, SECURITY | CORRECT |

**4 defects: 3 stale/wrong numbers, 1 ambiguity.** All are documentation errors.
**No source change is warranted** — the code is correct; the prose drifted.

### Why finding 4 matters most

The repository-intelligence figures were measured against the *pristine upstream
clone*, before SWE-Forge added ~95 files. A reader who runs
`sweforge analyze --repo .` today sees 907/7,429/1,036, not 812/6,385/843. The
claim is therefore **not reproducible as written**, which is the exact failure
mode this freeze phase exists to catch. Fix: report the current full-repo
numbers and state explicitly what was measured.

Note also that the historical 6,385/843/267 figures do not reproduce even when
the overlay is excluded (6,315/841/266). The small drift is unexplained by the
overlay alone, so the historical numbers are retired rather than reconstructed.

---

## Claim-safety review (Part 2)

Checked the README for implied claims that no result supports:

| Forbidden implication | Present? | Evidence |
|---|---|---|
| Open SWE vs SWE-Forge superiority | **NO** | README states `comparable_pairs = 0`, "No Open SWE vs SWE-Forge result exists" |
| Real-world benchmark performance | **NO** | `REAL_WORLD_BENCHMARK = NOT_AVAILABLE` |
| Real-model improvement | **NO** | Live track marked UNAVAILABLE |
| Routing reliability improvement | **NO** | "NOT MEASURED: whether routing preserves task success" |
| Memory improvement | **NO** | "NOT MEASURED: whether that context improves task success" |
| Repo-intelligence task-success improvement | **NO** | Negative result retained (A and B identical) |

Negative results and limitations are intact. **No claim-safety defect found.**

---

## Structural gaps for this phase

| Item | Status |
|---|---|
| `docs/RESUME_CLAIMS.md` | **MISSING** |
| Architecture diagram (flagship) | **MISSING** as a standalone doc |
| Evaluation-comparison diagram | **MISSING** |
| Automated test-count verification (anti-staleness) | **MISSING** — finding 1 is exactly what its absence causes |
| Clean-environment install validation | **NOT RUN** |
| `docs/FINAL_PROJECT_STATUS.md` | **MISSING** |
| Historical phase reports labelled as historical | **MISSING** label |
| Reproducible environment pin (Part 20) | **PARTIAL** — versions documented, no pin file |

## Remediation plan

1. Fix findings 1–4 (documentation only).
2. Add an **automated** doc-consistency checker so numbers cannot silently rot
   again, and wire it into CI.
3. Add `RESUME_CLAIMS.md`, the two diagrams, `FINAL_PROJECT_STATUS.md`.
4. Add a requirements pin; validate a clean-environment install.
5. Label historical phase reports as historical evidence.
6. Re-run every local validation and record exact results.

**No architectural change.** The architecture is frozen.
