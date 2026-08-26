# Interview Guide

Every answer below is grounded in code in this repository. File paths and symbols
are real; numbers come from executed runs. Where something is unmeasured, the
answer says so — that is usually the more interesting conversation anyway.

---

## 1. Thirty-second explanation

> SWE-Forge is an explicit control plane for autonomous software engineering,
> built on LangChain's Open SWE. Open SWE runs a single deep-agent loop, so
> whether it verifies its work, retries, or stops is emergent from a prompt.
> SWE-Forge keeps the reasoning in the LLM but moves the *control flow* into a
> deterministic LangGraph state machine: 17 nodes, bounded self-repair, an
> independent review gate, and a risk gate that can withhold a change from
> shipping. Then I built an ablation harness to measure whether each of those
> components actually helps.

## 2. Two-minute explanation

An agent that edits your repository must answer four questions a chatbot never
does: where does the change belong, did it actually work, what happens when it
fails, and should this ship at all.

- **Where** — repository intelligence: real Python `ast` parsing plus an import
  graph (907 files, 7,429 symbols, 1,036 edges on this repository in ~1.5s).
- **Did it work** — a verification engine that selects targeted tests from the
  import graph and parses the runner's output structurally, so "passed" is a fact
  from pytest, not the model's opinion of its own work.
- **When it fails** — a deterministic 10-category failure classifier runs *before*
  any LLM sees the failure, then a repair loop bounded by a routing function.
  Structural, not a prompt request.
- **Should it ship** — an independent reviewer that never sees the implementer's
  reasoning, plus an additive risk score. HIGH risk routes to
  `awaiting_human_approval`, a first-class terminal state.

The measured headline: bounded self-repair moved task success from 50% to 83%,
and the risk gate blocked a credential-committing change that every other
workflow variant shipped.

## 3. Ten-minute architecture

```
task_intake → repository_analysis → external_context(MCP) →
task_complexity_analysis → planning → dynamic_agent_selection → implementation
   → verification ──passed──→ independent_review → security_analysis → risk_gate
   │                              │ rejected (budget) → recovery
   └──failed──→ failure_analysis ─┤ budget left → recovery → verification
                                  └ exhausted → escalation
risk_gate: HIGH → human_approval | otherwise → finalization
budget exhausted anywhere → budget_exhausted
```

Layers: `state/` (typed state + custom reducers), `graph/` (topology),
`planning/`, `repository/`, `agents/`, `verification/`, `recovery/`, `security/`,
`routing/`, `memory/`, `mcp/`, `budget.py`, `tools/`, `observability/`.

Upstream owns: sandbox isolation, 24 provider middleware, model construction,
GitHub/Slack/Linear, MCP transport, the PR-review product. **None reimplemented.**

## 4. Why LangGraph

Three properties a ReAct loop cannot guarantee:

1. **Termination.** "At most 3 repairs, then ask a human" must be topology, not a
   prompt. `make_route_after_failure_analysis` is the only path to `recovery`, and
   it checks the budget. Tested at limits 0/1/3/5.
2. **Auditability.** After a run you must be able to say *why* a change was
   withheld. `node_trace` gives a literal answer: `risk_gate(HIGH, score=90) →
   human_approval`.
3. **Ablatability.** To claim a component helps you must be able to remove it.
   `WorkflowConfig` flags build a *structurally different graph*, which is what
   Experiment A varies.

## 5. Why LangChain

Repository intelligence, verification, risk scoring and memory must be callable
both by graph nodes and by a tool-calling agent. Expressing them once as
`StructuredTool`s with Pydantic schemas gives argument validation, model-readable
descriptions, uniform error handling and one ledger — instead of two
implementations that drift.

**The distinction I'd want to be asked about:** graph nodes call
`runtime.call_tool()` for deterministic steps where the model *must not* choose
(verification, risk gate); agents use `bind_tools` where the model *should*
choose. Both are ledgered. Only the second is "tool calling", and the docs never
conflate them.

## 6. Why Open SWE

Sandbox isolation and provider quirk handling are exactly the infrastructure you
should never rebuild — the `sanitize_*` middleware encodes real provider bugs
someone already paid for in production. Building on it also made the
contribution *falsifiable*: I audited upstream and found it has no hand-authored
domain `StateGraph` (across 420 Python files the only one is a cron scheduler),
which is precisely the gap SWE-Forge fills.

## 7. What SWE-Forge owns

`agent/sweforge/` (45 files), `evaluation/`, `tests_sweforge/` (478 tests), docs.
**Two upstream files modified, both additively:** `.gitignore` (its `.env.*` rule
was swallowing `.env.example`) and `README.md` (upstream's preserved verbatim at
`docs/UPSTREAM_README.md`). `langgraph.json` gained two entries and lost none.

## 8. Recovery algorithm

1. Verification fails → `VerificationResult` with structured counts.
2. **Deterministic classification first** (`recovery/classifier.py`): ordered
   regex rules over 10 categories, zero model calls. A `ModuleNotFoundError` is a
   dependency problem; no LLM needed to say so.
3. Rule *order* encodes real subtleties — `runtime` is checked before
   `test_assertion` because pytest echoes the failing source line even when the
   true cause is a `ValueError`.
4. The LLM answers only the open question — *what should change* — receiving the
   classification, failing output, suspect files and **strategies already tried**,
   so attempt 3 does not repeat attempt 1.
5. Re-verify. Loop entry is gated by the routing function.

**Bug worth mentioning:** the Phase 25 recovery matrix caught a configuration
rule ending in `\b` after an apostrophe — a word boundary after a non-word
character can never match, so `KeyError: 'DATABASE_URL'` fell through to
`runtime`. Fixed with a regression test; detection is now 10/10.

## 9. Budget enforcement

Eight limits (`budget.py`), all checked **before** the expensive operation, all
raising `BudgetExceeded`. The model cannot see or raise one: a test asserts no
structured-output field name collides with a limit name. Exhaustion routes to the
`budget_exhausted` terminal state.

One deliberate asymmetry: a run whose verification is already green is *not*
diverted to `budget_exhausted` — finishing the gates on completed work costs
little and is more useful than discarding it.

## 10. Model routing

`ROLE_TIER` maps roles to `fast|balanced|coding|reasoning`; complexity escalates
or de-escalates plan/implement/review while leaving bookkeeping cheap; two
failures on a role earn a stronger tier.

**Escalation ≠ fallback**, and I previously conflated them. Escalation happens
*between* operations; fallback happens *within* one when the chosen model fails.
`ModelExecutionPolicy` retries the same model on retryable errors, then falls back
across tiers to a genuinely different model, recording each attempt separately.

**Measured:** adaptive routing costs 40% of a fixed reasoning-tier model on an
identical call pattern (n=4). **Not measured:** whether it preserves task success
— that needs live models, and I make no reliability claim.

## 11. Repository intelligence

Static analysis only — exact syntactic facts, no type inference, no dynamic
imports, no semantic understanding. Ranking is deliberately inspectable
(+3 path token, +2 symbol, +1 docstring, ×1.15 non-test, +1.5 one import hop),
and every result carries *why* it ranked.

**Measured (n=4, deterministic, no LLM):** graph and hybrid retrieval reach
R@5 = 1.0 vs lexical 0.875. Precision is 1.0 for all three — the fixtures are too
easy to discriminate on precision, which I report rather than spin.

## 12. Security architecture

Defence-in-depth screening, explicitly *not* a security product. The threat model
is mostly **the agent itself** — a well-intentioned model taking a plausible
action with bad consequences.

The score is deterministic **by design**: routing a safety boundary through an
LLM would make it nondeterministic. An LLM can attach findings; it cannot lower
the gate. Worked example: `pipeline_secret_risk_gate` scores 60 (committed
credential) + 30 (CI workflow edit) = 90 → HIGH → human approval, while variants
A–D all shipped it.

## 13. MCP architecture

Upstream owns transport; SWE-Forge owns the *decision*. `MCPToolSelector` reads
explicit external references in the task (an issue number, a URL, a production
signal) rather than asking a model whether it would like to browse — keeping the
control-flow decision deterministic. `MCPInvocationPolicy` is **deny-by-default**:
an autonomous agent reaching arbitrary external services is a security problem,
not a feature.

## 14. Evaluation methodology

Three experiments, never conflated:

| | Question | Status |
|---|---|---|
| **A** ablation | Does each component help? | COMPLETE, 30 runs |
| **B** system baseline | Better than actual Open SWE? | UNAVAILABLE, `comparable_pairs = 0` |
| **C** live model | Behaviour with a real model? | UNAVAILABLE, no credential |

Model behaviour is pinned by `ScriptedChatModel` so graph topology is the only
variable. The fixtures, edits and pytest runs are real — a repaired suite
genuinely goes green. Token counts are synthetic and labelled as such.

Reproducibility: run manifests record commit SHA, package versions, seed and
benchmark version; `--repeat 3` verified identical terminal states, routing paths,
recovery counts and tool sequences.

## 15. Limitations

n=6 scenarios on four toy fixtures — enough to cover every terminal state, not
enough for statistics, so no confidence intervals are offered. Python-only AST.
Regex-based risk screening, defeatable by obfuscation. Hand-tuned risk weights.
No live-model, Open SWE head-to-head, or real-benchmark result exists.

## 16. Negative results (kept deliberately)

1. **Repository intelligence showed no end-to-end effect** — variants A and B are
   identical. The harness pins the planner's output, so richer evidence cannot
   change the plan. Reported, then measured directly instead.
2. **Memory and routing likewise have no measured task-success effect.**
3. **Precision saturates at 1.0 across all retrieval strategies** — the fixtures
   are too easy to discriminate.
4. **Variant E's headline success (67%) is *lower* than D's (83%)** — because the
   risk gate withheld a change. A benchmark rewarding that shipment would measure
   the wrong thing.

## 17. What I'd test with more compute and credentials

1. **Live-model Experiment C** — the highest-value next step; the only way to test
   whether repository intelligence, memory and routing help end-to-end.
2. **Experiment B head-to-head** on ≥20 paired tasks, with McNemar and bootstrap
   CI (both implemented; the scorer refuses to conclude below 20).
3. **SWE-bench Lite** through the existing harness.
4. **Risk weights fitted to labelled outcomes** instead of judgement.
5. **Repeated-trial variance** on latency and cost.

---

# Quick answers — the 20 questions

Condensed versions of the sections above. Every answer matches source.

**1. What did you build?** An explicit control plane for autonomous software
engineering on LangChain's Open SWE: a deterministic 17-node LangGraph state
machine with bounded self-repair, an independent review gate and a risk gate,
plus an ablation harness measuring whether each component helps.

**2. Why build on Open SWE?** Sandbox isolation and provider-quirk middleware are
infrastructure you should never rebuild. It also made the contribution
falsifiable: I audited upstream and found no hand-authored domain `StateGraph`.

**3. What is actually yours?** `agent/sweforge/` (45 files), `evaluation/`,
`tests_sweforge/` (478 tests), docs. Three upstream files modified, all additive.
Upstream's `LICENSE` and 5 graph entries untouched.

**4. Why LangGraph?** Termination guarantees, auditability, ablatability. "At most
3 repairs then ask a human" must be topology, not a prompt request.

**5. Why LangChain?** One validated tool implementation callable by both graph
nodes and tool-calling agents, instead of two that drift.

**6. Why structured outputs?** Control flow must never depend on parsing prose.
`ReviewResult`'s validator corrects a model that reports a blocker and approves
anyway, because the risk gate downstream trusts that boolean.

**7. How does dynamic agent selection work?** The planner emits a validated
`TaskPlan`; `subtask.agent` selects a concrete class from `AGENT_CLASSES`. A
backend plan and a documentation plan produce different `agents_executed`.

**8. How does recovery work?** Deterministic classification first (10 categories,
zero model calls), then the LLM answers only *what to change*, receiving the
classification and the strategies already tried.

**9. How do you prevent infinite loops?** `recovery` is reachable only through a
routing function that checks the attempt budget; review cycles are capped
separately. Tested at limits 0/1/3/5.

**10. How do you enforce budgets?** Eight limits checked *before* each expensive
operation, raising `BudgetExceeded` → `budget_exhausted` terminal state. A test
asserts no structured-output field name collides with a limit name.

**11. How does model fallback work?** Retry the same model on retryable errors,
then fall back across tiers to a genuinely different model, recording each
attempt separately. Escalation (between operations) ≠ fallback (within one).

**12. How does repository intelligence work?** stdlib `ast` parsing plus an
import graph, with inspectable ranking that reports *why* each file ranked.
Static only — no type inference, no semantic understanding.

**13. How does memory work?** Append-only JSONL of completed runs, BM25 retrieval
over identifier tokens, injected as planner context. Retrieval, not learning.

**14. How does MCP fit in?** Upstream owns transport; SWE-Forge owns the
decision. Selection is deterministic (reads explicit external references in the
task), and invocation is deny-by-default.

**15. How does security gating work?** Additive, deterministic risk score. An LLM
can attach findings but cannot lower the gate. HIGH → `awaiting_human_approval`.

**16. How did you evaluate it?** Three experiments, never conflated: A
(ablation, complete), B (Open SWE baseline, UNAVAILABLE), C (live model,
UNAVAILABLE). Model behaviour pinned so topology is the only variable.

**17. What did the ablation prove?** Bounded self-repair moves task success
50% → 83%; the review gate catches green-but-wrong work; the risk gate blocks a
credential-committing change every other variant shipped; routing is 6/6 correct
and runs are 3/3 reproducible.

**18. What did it NOT prove?** That repository intelligence, memory or routing
improve task success — they are structurally invisible to a scripted harness, and
variants A and B are identical. Nor anything about frontier-model capability.

**19. What is the biggest limitation?** No live-model, Open SWE head-to-head or
real-benchmark result exists. Six scenarios on four toy fixtures is enough to
verify architecture, not enough for statistics.

**20. What experiment would you run next?** Live-model Experiment C (~$20), then
Experiment B on ≥20 paired tasks where McNemar and the bootstrap CI become
meaningful.
