# Architecture

## The problem

An autonomous coding agent that can edit a repository has to answer four questions that
a chat assistant does not:

1. **Where does the change belong?** Without a structural model of the repository, the
   agent guesses file paths and hallucinates modules.
2. **Did it actually work?** The agent's own confidence is not evidence. Something must
   execute tests and read the result.
3. **What happens when it fails?** Retrying the same prompt is not repair. And an agent
   that repairs without a bound can loop indefinitely while burning money.
4. **Should this change ship?** A change can pass every test and still be one nobody
   would approve — a committed credential, a weakened auth check, an edited CI workflow.

Upstream Open SWE addresses these inside a single `deepagents` ReAct loop plus
middleware: the model decides whether to verify, whether to retry, and when to stop.
That is a sound design for interactive work, but it means the control flow lives in a
prompt.

**SWE-Forge inverts that.** Reasoning stays with the LLM; control flow becomes an
explicit, deterministic `StateGraph`.

## Layering

```
┌───────────────────────────────────────────────────────────────────────┐
│  SWE-FORGE LAYER  (agent/sweforge/)                                   │
│                                                                       │
│   graph/       StateGraph: 17 nodes, 5 routers, bounded loops          │
│   state/       SWEForgeState + custom reducers                         │
│   planning/    structured-output planner, agent selection              │
│   repository/  Python AST analysis, import graph, relevance ranking    │
│   agents/      implementer, independent reviewer, diagnostician        │
│   verification/ targeted test selection, structured results            │
│   recovery/    deterministic failure taxonomy                          │
│   security/    secret & pattern scanning, additive risk scoring        │
│   routing/     per-role model tier policy + cost ledger                │
│   memory/      BM25 experience retrieval                               │
│   observability/ optional LangSmith tracing                            │
│   tools/       12 LangChain StructuredTools                            │
│   models/      ScriptedChatModel (deterministic evaluation)            │
│   cli.py       demo / run / analyze / doctor                           │
└───────────────────────────────────────────────────────────────────────┘
                                    │ reuses, does not modify
                                    ▼
┌───────────────────────────────────────────────────────────────────────┐
│  UPSTREAM OPEN SWE  (unmodified)                                      │
│                                                                       │
│   sandbox isolation (Daytona / Modal / E2B / Runloop)                  │
│   24 middleware modules (fallback, timeout, provider sanitisation)     │
│   model instantiation + provider reasoning config                      │
│   GitHub App auth, proxy tokens, PR creation                           │
│   Slack / Linear / MCP integrations                                    │
│   PR-review product, dashboard, 267 tests                             │
└───────────────────────────────────────────────────────────────────────┘
```

## The graph

```
                            START
                              │
                        task_intake ──────────(empty task)──────────┐
                              │                                     │
                     repository_analysis                            │
                              │                                     │
                  task_complexity_analysis                          │
                              │                                     │
                          planning                                  │
                              │                                     │
                  dynamic_agent_selection                           │
                              │                                     │
                       implementation                               │
                              │                                     │
                    ┌──── verification ◄──────────────┐             │
                    │         │                       │             │
              (passed)     (failed)                   │             │
                    │         │                       │             │
                    │   failure_analysis              │             │
                    │         │                       │             │
                    │   ┌─────┴──────┐                │             │
                    │ (budget)   (exhausted)          │             │
                    │   │             │               │             │
                    │ recovery ───────┼───────────────┘             │
                    │   ▲             ▼                             │
                    │   │        escalation ─────────────────────┐  │
                    ▼   │                                        │  │
           independent_review                                    │  │
                    │   │                                        │  │
        ┌───────────┼───┘ (rejected, budget left)                │  │
        │ (approved)                                             │  │
        ▼                                                        │  │
  security_analysis                                              │  │
        │                                                        │  │
    risk_gate                                                    │  │
        │                                                        │  │
   ┌────┴─────┐                                                  │  │
(HIGH)     (LOW/MED)                                             │  │
   │           │                                                 │  │
human_approval │◄────────────────────────────────────────────────┼──┘
   │       finalization                                          │
   └───────────┴──────────────────► END ◄────────────────────────┘
```

Five terminal outcomes are reachable: `completed`, `completed_with_findings`,
`awaiting_human_approval`, `escalated_recovery_exhausted`,
`escalated_review_rejected` (plus `failed` for degenerate input).

## Subsystems

### Repository intelligence (`repository/`)

`RepositoryAnalyzer` walks the tree and parses every Python file with the stdlib `ast`
module, extracting classes, functions, methods (with parentage), imports and module
docstrings. Syntax errors are *recorded*, not raised — a repository with one broken file
must still be analysable.

`RepositoryGraph` builds a directed in-repo import graph, resolving both absolute and
relative imports via longest-prefix matching against the module index. It exposes:
`find_dependencies`, `find_dependents`, `find_callers`, `find_definition`,
`find_tests_for_file`, `find_related_files`, `find_relevant_modules`.

Relevance ranking is deliberately inspectable rather than clever:

```
+3.0 per task token matched in the file path
+2.0 per task token matched in a defined symbol name
+1.0 per task token matched in the module docstring
×1.15 for non-test files (implementation targets should lead)
+1.5 for files one import hop from a top-ranked seed
```

Every result carries the reasons it ranked (`"path matches ['fallback','middleware']"`),
so a wrong ranking is debuggable. Ranking is deterministic with ties broken on path
order — which is what makes the evaluation harness reproducible.

**Honest scope:** this is *static* analysis. It gives exact syntactic facts. It does
**not** do type inference, alias resolution, dynamic import tracking, or semantic
understanding of behaviour. Non-Python files are inventoried but not parsed. The
relevance ranker is a heuristic built on exact syntax, not comprehension.

### Planning (`planning/`)

The planner receives real repository evidence (ranked candidates *with reasons*, module
layout, discovered tests) plus retrieved prior experience, and returns a validated
`TaskPlan`. Three properties matter:

- **Grounding.** A planner that invents `src/utils/helpers.py` is worse than useless.
  `_sanitise` filters hallucinated paths against the real file list, while still
  permitting genuinely new files in directories that exist.
- **Structure.** `TaskPlan`'s validator rejects duplicate ids, unknown dependency
  references and dependency cycles (Kahn's algorithm), so an incoherent plan fails at
  the planning node rather than mid-execution.
- **Degradation, not death.** If structured output fails entirely, `_fallback_plan`
  derives a deterministic plan from static analysis alone.

`execution_layers()` turns declared dependency edges into concrete concurrency: subtasks
within a layer are independent and may execute in parallel (`subtask_workers > 1`). The
evaluation harness pins `subtask_workers = 1` for reproducibility.

`select_agents` builds the roster from the plan — and adds the reviewer *independently of
the planner's opinion*, because a plan that omits its own reviewer is precisely the case
the review gate exists to catch.

### Verification (`verification/`)

Command selection is driven by repository intelligence: given the files a change touched,
the import graph supplies the tests that actually cover them, so targeted tests run
before any full suite. That is the difference between a 4-second signal and a 6-minute
one.

Parsing is structural. `pytest` counts are read **only** from the final summary line,
because strings like `Interrupted: 1 error during collection` also contain `1 error` and
would double-count. Exit code alone is insufficient (exit 1 covers both assertion failure
and collection error), so both are used.

A lint or typecheck tool that is *not installed* yields `None`, not `False`. A missing
linter is an environment gap, not a defect in the change under test — and its stderr is
deliberately excluded from the diagnostic output, because "No module named ruff" would
otherwise be classified as a dependency failure. (Both of these were real bugs found by
testing against real runner output.)

### Failure classification and repair (`recovery/`)

Classification happens **before** any LLM sees the failure. A stack trace has an
unambiguous category — `ModuleNotFoundError` is a dependency problem, `SyntaxError` is a
syntax problem — and asking a model to label it wastes a call, adds latency, and injects
nondeterminism into control flow.

Ten categories, ordered regex rules, first match wins. Rule order encodes real
subtleties: `runtime` is checked before `test_assertion` because `pytest` echoes the
failing source line (`> assert add(2,3) == 5`) even when the true cause is a `ValueError`
raised inside the call.

The LLM is then used only for the genuinely open question: *what should we change?* The
`Diagnostician` receives the deterministic classification, the failing output, the
suspect files, and **the strategies already tried** — so attempt 3 does not repeat
attempt 1.

### Independent review (`agents/roles.py`)

Independence is structural, not just prompt-level. The reviewer receives the original
task, the plan, the change, the verification result and fresh repository context
(including static blast radius: "this file is imported by 7 others; NO covering tests
found") — but **never** the implementer's notes or self-justification. A reviewer told
"I fixed this by X for reason Y" grades the reasoning rather than the code.

A reviewer that cannot run returns `approved=False`. Failing closed is the only safe
default for a gate.

### Security and risk (`security/`)

Two stages. `SecurityScanner` pattern-matches changed content for committed secrets
(AWS/Anthropic/OpenAI/GitHub/Slack keys, private keys), destructive shell operations,
`shell=True`, unsafe deserialisation, disabled TLS verification, and auth functions that
unconditionally `return True`. Findings in `.env.example`, `examples/` or `fixtures/`
are downgraded to `info`, because placeholder credentials there are correct.

`RiskEngine` then computes an **additive, auditable** score from weighted factors:
security findings, sensitive paths (CI workflows, dependency manifests, auth/security
modules, IaC, `.env`), non-test deletions, diff size, verification state, review
rejection, and repeated recovery. Thresholds: ≥55 HIGH, ≥25 MEDIUM.

The score is deterministic **by design**. The gate decides whether a change may open a
PR automatically or must wait for a human; routing that through an LLM would make the
safety boundary nondeterministic. An LLM security *opinion* can attach findings, but it
cannot lower the gate.

**Scope claim:** defence-in-depth screening, not a security product. It will miss novel
or obfuscated issues and will occasionally flag benign code. See `docs/SECURITY.md`.

### Model routing (`routing/`)

Different steps have genuinely different requirements. Ranking candidate files is cheap
pattern work; judging whether a diff introduces an auth bypass is not. `ROLE_TIER` maps
each role to `fast | balanced | coding | reasoning`; complexity escalates or de-escalates
the plan/implement/review path while leaving cheap bookkeeping roles cheap; two failures
on a role earn a stronger tier.

No model id is hard-coded — tiers resolve through env vars with documented defaults. No
API key is read or logged. Every call is timed and recorded in `ModelUsageLedger` with
tier, latency, tokens and estimated cost.

**Routing is measured, not assumed.** Nothing in this repository claims routing improves
task success; the harness reports what it selected and what it cost.

### Experience memory (`memory/`)

An append-only JSONL log of completed runs, retrieved with BM25 over identifier tokens
and injected into the planner prompt.

This is **retrieval**, not learning — no weights change. A vector store was considered
and rejected for the default path: the corpus is small, queries are identifier-heavy
("fix the model fallback middleware retry"), and lexical scoring beats dense similarity
on exactly that kind of query while adding no service dependency and staying
deterministic. An `EmbeddingBackend` protocol exists for corpora that outgrow this.

Corrupt JSONL lines are skipped rather than fatal: a truncated log tail must never break
planning.

## Design decisions and their costs

| Decision | Why | What it costs |
|---|---|---|
| Explicit `StateGraph` over a ReAct loop | Termination guarantees, auditability, ablatability | More code; less emergent flexibility |
| Deterministic classification before LLM diagnosis | Saves a call, removes nondeterminism from routing | Regex rules need maintenance as tooling changes |
| Deterministic risk scoring | A safety boundary must not be nondeterministic | Cannot reason about novel risks |
| Whole-file edits, not diffs | A model-generated diff frequently fails to apply; a failed patch is a worse failure than a larger payload | More tokens per edit |
| BM25 over embeddings | Deterministic, no service dependency, better on identifier queries | Won't scale to very large corpora |
| Reuse upstream sandbox | Isolation is mature infrastructure; reimplementing it would weaken it | Coupled to upstream's provisioning |
| Scripted models in evaluation | Isolates orchestration from sampling noise | Cannot measure planning quality (see `docs/EVALUATION.md` §5) |

## Known limitations

1. Python-only AST analysis; other languages are inventoried, not parsed.
2. `find_callers` finds definition sites and in-repo importers — it does not resolve
   aliased or dynamic call sites.
3. Risk scoring is pattern-based and will produce false positives and negatives.
4. Concurrent subtask execution exists and is tested, but the evaluation runs
   sequentially for reproducibility.
5. The end-to-end value of repository intelligence, memory and model routing is
   **unmeasured** — they are invisible to a scripted-model harness.
6. No live-model benchmark has been run; no public-benchmark number is claimed.

---

# Phase 23/24 additions

## Dynamic multi-agent dispatch (`agents/specialized.py`)

Nine agent classes, dispatched from the validated plan rather than a fixed node.
`subtask.agent` selects the concrete class; different plans genuinely execute
different code. Measured distinctness (verified, not asserted):

| Role | Class | Structured output | Model role | Tools granted |
|---|---|---|---|---|
| `test_agent` | `TestAgent` | `TestChanges` | `test_authoring` | `find_related_tests`, `find_relevant_files` |
| `backend_agent` | `BackendAgent` | `BackendChanges` | `implementation` | `find_dependencies`, `find_callers`, `find_relevant_files` |
| `frontend_agent` | `FrontendAgent` | `FrontendChanges` | `implementation` | `find_relevant_files` |
| `database_agent` | `DatabaseAgent` | `MigrationChanges` | `implementation` | `find_dependencies`, `find_callers` |
| `documentation_agent` | `DocumentationAgent` | `DocChanges` | `documentation` | `find_relevant_files` |
| `security_agent` | `SecurityAgent` | `SecurityAssessment` | `security_analysis` | `security_scan`, `calculate_change_risk` |
| `implementation_agent` | `ImplementationAgent` | `ImplementationOutput` | `implementation` | `find_relevant_files`, `find_dependencies` |
| reviewer | `IndependentReviewer` | `ReviewResult` | `review` | (git diff via graph) |
| recovery | `Diagnostician` | `RepairOutput` | `recovery` | — |

6/6 distinct system prompts, 6/6 distinct output models, 5 distinct tool grants,
4 distinct model roles. Two design choices worth defending:

* **`SecurityAgent` returns findings, never edits.** An agent that both flags and
  silently fixes a security issue removes the human's chance to see it.
* **`DatabaseAgent` must declare reversibility and destructive operations.**
  Under-reporting there would defeat the risk gate downstream.

## Real agent tool-calling (`agents/tool_loop.py`)

`ToolCallingLoop` implements `bind_tools` → `AIMessage.tool_calls` → `ToolMessage`
→ model continuation → structured output. Bounded by `max_iterations` and by the
execution budget.

**Why two tool paths exist, deliberately:**

| Path | Used by | Why |
|---|---|---|
| `runtime.call_tool(...)` | graph nodes | Deterministic steps where the model *must not* choose: verification, risk gate, security scan. Routing must not depend on a model's whim. |
| `bind_tools(...)` | agents | Steps where the model *should* choose which evidence to gather. |

Both are ledgered with node/agent provenance. Only the second is "tool calling",
and the docs never conflate them.

`ScriptedChatModel.bind_tools` replays scripted tool-call rounds through the real
LangChain contract, so this path is exercised in tests rather than merely present.

## Hard execution budgets (`budget.py`)

See [EXECUTION_BUDGETS.md](EXECUTION_BUDGETS.md). Eight limits, all enforced in
Python before the expensive operation, all verified to raise. `budget_exhausted`
is an explicit terminal state. The model cannot see or raise a limit.

## Model retry and fallback (`routing/execution_policy.py`)

Tier *escalation* (between operations) and *fallback* (within one operation) are
different things and are no longer conflated. `ModelExecutionPolicy` retries the
same model on retryable errors, then falls back across tiers to a genuinely
different model. Each attempt is a separate `ModelAttempt` record — a fallback is
never counted as the same call. Non-retryable failures do not spend fallbacks.

## MCP orchestration (`mcp/orchestration.py`)

Upstream owns MCP transport. SWE-Forge owns the decision: `MCPCapabilityRegistry`
(discovery + schemas), `MCPToolSelector` (deterministic, reads explicit external
references in the task — never asks a model whether it feels like browsing), and
`MCPInvocationPolicy` (deny-by-default allowlist, retry, per-run cap, budget).
Reached from the `external_context` graph node.

## Open SWE baseline adapter (`evaluation/baselines/`)

Resolves the genuine `agent.server.get_agent`. Preflight probes modules, upstream
importability, model credentials and sandbox credentials separately, so
"unavailable" names the exact blocker instead of failing vaguely.
