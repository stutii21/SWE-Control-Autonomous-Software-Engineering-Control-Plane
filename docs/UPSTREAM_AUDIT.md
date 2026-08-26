# Upstream Audit — Open SWE

> **HISTORICAL DOCUMENT.** This is point-in-time evidence from an earlier phase,
> retained for traceability. Numbers here reflect the repository *at that time*
> and may not match current state. For current, source-verified figures see
> `docs/FINAL_PROJECT_STATUS.md` and `docs/PROJECT_CLAIMS.md`.

This document records what the upstream [Open SWE](https://github.com/langchain-ai/open-swe)
codebase provides **before** any SWE-Forge code exists. It was written by reading the
upstream source, not from assumption, and it is the basis for every ownership claim
made elsewhere in this repository.

**Audit target:** `langchain-ai/open-swe`, `main` branch, shallow clone.
**Method:** static inspection of the repository tree, `langgraph.json`, `pyproject.toml`,
and the `agent/` package.

## Measured shape of the upstream repository

Produced by SWE-Forge's own analyzer (`sweforge analyze --repo .`) against the clone
before adding any SWE-Forge files:

| Metric | Value |
|---|---|
| Indexed files | 812 |
| Python files | 439 |
| TypeScript/TSX files | 310 |
| Extracted symbols (classes/functions) | 6,385 |
| In-repo import edges | 843 |
| Test files | 267 |
| Analysis wall time | ~1.1 s |

## What Open SWE already provides

### 1. Agent construction — `deepagents`, not a hand-built graph

This is the single most important audit finding.

`agent/server.py` (1,804 lines) builds the agent with
`deepagents.create_deep_agent`, wrapped in a large middleware stack:

```python
# agent/server.py (upstream)
from deepagents import create_deep_agent
from deepagents.middleware.subagents import GENERAL_PURPOSE_SUBAGENT, SubAgent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolRetryMiddleware
```

`agent/graphs/agent.py` is a three-line re-export:

```python
from agent.server import get_agent, traced_agent

__all__ = ["get_agent", "traced_agent"]
```

**A repository-wide search for `StateGraph` returns two files: `agent/scheduler.py`
(cron scheduling) and one test.** There is no hand-authored, multi-node `StateGraph`
expressing the software-engineering loop. Control flow — whether the agent verifies
its work, whether it retries after a failure, when it decides to stop — is emergent
behaviour of a ReAct-style loop plus prompt, not an explicit topology.

This is a reasonable design for open-ended interactive work. It is also the gap
SWE-Forge fills, and the reason SWE-Forge is a genuine architectural contribution
rather than a rename.

### 2. Middleware (24 modules, `agent/middleware/`)

Mature, battle-tested cross-cutting concerns. Representative modules:

| Module | Concern |
|---|---|
| `model_fallback.py` | Fall back to a secondary model on provider failure |
| `model_call_timeout.py` | Per-call timeouts |
| `task_retry.py` | Retry semantics |
| `tool_error_handler.py` | Tool error recovery |
| `sandbox_circuit_breaker.py` | Sandbox health / circuit breaking |
| `repair_orphaned_tool_calls.py` | Message-history repair |
| `sanitize_thinking_blocks.py`, `sanitize_openai_responses.py`, `sanitize_fireworks_messages.py` | Provider-specific message normalisation |
| `plan_mode.py`, `pr_creation_guard.py`, `workflow_push_guard.py` | Behavioural guardrails |
| `subdir_agents.py`, `dynamic_tools.py`, `exclude_tools.py` | Tool/agent scoping |

**SWE-Forge must not reimplement any of these.** Provider quirks and message-history
repair are exactly the kind of hard-won infrastructure that should be reused.

### 3. Sandbox abstraction

`agent/runtime/sandbox.py` is a thin façade over `deepagents.backends.protocol.SandboxBackendProtocol`,
delegating to `agent/server.py` for lifecycle management. Concrete providers live in
`agent/integrations/`: `daytona.py`, `modal.py`, `e2b.py`, `runloop.py`, `local.py`.

Provisioning, the GitHub proxy token flow, snapshot handling and git identity
configuration are all upstream concerns.

**SWE-Forge reuses this as the isolation boundary and implements no sandbox of its own.**

### 4. Model abstraction

`agent/utils/model.py` (353 lines) provides `make_model()`, provider-specific reasoning
configuration (`openai_reasoning_for`, `anthropic_thinking_for`,
`fireworks_reasoning_effort_for`, `google_thinking_level_for`), a model cache, and
`fallback_model_id_for()`.

This resolves *one* agent model plus a fallback per run. It does not select different
models for different phases of a task — there are no distinct phases to select for.

### 5. Reviewer

`agent/reviewer.py` (1,449 lines) plus `agent/review/` (findings, diff, grouping,
publishing, style collection) implement a substantial **pull-request review** product:
review of an existing PR diff, posting findings as GitHub comments, deduplication and
reconciliation across runs.

This is PR review as a deliverable, exposed as its own graph entry point
(`reviewer` in `langgraph.json`). It is not an in-loop gate that a self-verifying
implementation workflow must pass before proceeding.

### 6. Integrations and MCP

`agent/integrations/` contains MCP clients (`corridor_mcp.py`, `datadog_mcp.py`,
`notion_mcp.py`), LangSmith tools (`langsmith_tools.py`), browser automation
(`stagehand_browser.py`), and web search (`exa-py`). `langchain-mcp-adapters` is a
declared dependency.

`agent/tools/` contains ~30 tools including Linear issue management, GitHub PR
creation, sandbox file operations and HTTP fetching.

### 7. Graph entry points (`langgraph.json`)

```json
"graphs": {
  "agent":     "agent.graphs.agent:traced_agent",
  "reviewer":  "agent.graphs.reviewer:traced_reviewer_agent",
  "analyzer":  "agent.graphs.analyzer:traced_analyzer",
  "chat":      "agent.graphs.chat:traced_chat_agent",
  "scheduler": "agent.graphs.scheduler:get_scheduler"
}
```

Five entry points. Only `scheduler` is an explicit `StateGraph`, and it schedules
cron-triggered runs rather than orchestrating engineering work.

### 8. Observability, dashboard, tests, evals

- `agent/utils/langsmith.py`, `agent/integrations/langsmith.py` — LangSmith wiring.
- `agent/dashboard/` and `ui/` (310 TS/TSX files) — web dashboard.
- `tests/` — 267 test files. `tests/conftest.py` imports the full server stack
  (`fastapi`, provider SDKs), which is why SWE-Forge's tests live in a sibling
  directory (see `pytest-sweforge.ini`).
- `evals/reviewer/` — a reviewer evaluation harness with golden comments for five
  real repositories (grafana, keycloak, discourse, sentry, cal.com), an LLM judge
  (`judge.py`) and a store reporter.

**Note:** the upstream eval harness evaluates *reviewer comment quality* against
golden data. It does not benchmark end-to-end task completion, recovery behaviour or
orchestration variants, so it does not overlap with SWE-Forge's evaluation.

### 9. Security mechanisms

`SECURITY.md`, `agent/encryption.py`, `agent/local_auth.py`, `agent/utils/auth.py`,
`agent/utils/github_org_membership.py`, `agent/tools/admin_gate.py`, and the
`pr_creation_guard` / `workflow_push_guard` middleware.

These are **access-control** mechanisms: who may run the agent, who may trigger a PR.
There is no static risk assessment of the *content* of a change the agent produced.

## Extension points SWE-Forge uses

| Extension point | Upstream symbol | How SWE-Forge uses it |
|---|---|---|
| Sandbox execution | `agent.runtime.sandbox.ensure_sandbox_for_thread` | Wrapped by `OpenSWESandboxBackend` as the production verification backend |
| Sandbox protocol | `deepagents.backends.protocol.SandboxBackendProtocol` | Duck-typed target of the SWE-Forge execution-backend contract |
| Graph registration | `langgraph.json` `graphs` map | Where the SWE-Forge graph would be registered for deployment |
| Python packaging | `pyproject.toml` | Declares `langchain`, `langgraph`, `langsmith`, `pydantic` — no new core dependency required |
| Repo conventions | `[tool.ruff]` line-length 100, `TID251` bans `from __future__ import annotations` | SWE-Forge code follows both |

## What SWE-Forge adds

Only capabilities that upstream does not have:

| SWE-Forge subsystem | Why it is not duplication |
|---|---|
| Explicit `StateGraph` orchestration (`sweforge/graph/workflow.py`) | Upstream has no domain `StateGraph`; control flow is prompt-emergent |
| Repository intelligence (AST + import graph) | Upstream has no static repository model or relevance ranking |
| Structured-output planner with agent selection | Upstream uses fixed subagents (`GENERAL_PURPOSE_SUBAGENT`, browser); no per-task roster derived from a validated plan |
| Self-verification engine | Upstream can run tests as a tool call; it has no engine that selects targeted tests from the import graph and structures the result |
| Deterministic failure classifier + bounded repair loop | Upstream has retry middleware (transport-level), not failure-taxonomy-driven code repair with an enforced attempt bound |
| In-loop independent review gate | Upstream reviewer is a PR-review product, not a gate an implementation must pass mid-run |
| Change-risk engine + human-approval gate | Upstream security is access control; nothing scores the risk of produced content |
| Adaptive model routing per role | Upstream resolves one model + fallback per run |
| Experience memory | No equivalent upstream |
| Orchestration ablation harness | Upstream evals measure reviewer comment quality, not workflow topology |

## What must NOT be duplicated

Explicit non-goals, to keep the contribution honest and the diff small:

1. **Sandbox providers.** Reuse Daytona/Modal/E2B/Runloop via upstream.
2. **Provider message sanitisation.** The `sanitize_*` middleware encodes real provider
   bugs. Never reimplement.
3. **Model fallback / timeout / retry middleware.** Transport-level concerns already solved.
4. **GitHub App auth, proxy tokens, PR creation.** Upstream owns the GitHub surface.
5. **Slack / Linear integrations.** No SWE-Forge need.
6. **The web dashboard and UI.** SWE-Forge ships a CLI instead; architecture and
   results matter more than a frontend.
7. **The PR-review product.** SWE-Forge's reviewer is an in-loop gate with a different
   contract; the upstream reviewer graph stays untouched.

## Upstream files modified

**One**, additively:

| File | Change | Justification |
|---|---|---|
| `.gitignore` | Appended `!.env.example` and `.sweforge/` | Upstream's `.env.*` rule silently swallowed the placeholder template that must be committed; experience-memory artefacts are machine-local run history, not source |

No upstream Python module, test, config, license or entry point was edited or deleted.
Verified with `git status --porcelain`.
