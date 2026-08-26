# SWE-Forge Architecture Diagrams

Two diagrams: what runs, and how it is evaluated. Both distinguish
**SWE-Forge-owned** components from **upstream Open SWE** infrastructure.
Nothing upstream is claimed as a SWE-Forge implementation.

---

## 1. System architecture

```
                              USER TASK
                                  │
╔═════════════════════════════════▼══════════════════════════════════════════╗
║  SWE-FORGE  (agent/sweforge/) — OWNED BY THIS PROJECT                      ║
║                                                                            ║
║   ┌──────────────────────────────────────────────────────────────────┐    ║
║   │  LANGGRAPH StateGraph   graph/workflow.py                        │    ║
║   │  17 domain nodes · 5 conditional routers · 4 terminal nodes       │    ║
║   │  registered in langgraph.json as `sweforge`                       │    ║
║   └──────────────────────────────────────────────────────────────────┘    ║
║                                  │                                         ║
║   task_intake ───────────────────┤                                         ║
║        │                          │                                        ║
║        ▼                          │   ┌───────────────────────────────┐    ║
║   repository_analysis ◄───────────┼───┤ REPOSITORY INTELLIGENCE       │    ║
║        │                          │   │ repository/analyzer.py (AST)  │    ║
║        ▼                          │   │ repository/graph_index.py     │    ║
║   external_context ◄──────────────┼───┤ MCP  mcp/orchestration.py     │    ║
║        │                          │   │  (deny-by-default allowlist)  │    ║
║        ▼                          │   └───────────────────────────────┘    ║
║   task_complexity_analysis ◄──────┼───┤ MEMORY  memory/store.py (BM25)│    ║
║        │                          │                                        ║
║        ▼                          │   ┌───────────────────────────────┐    ║
║   planning ───────────────────────┼───┤ structured TaskPlan (Pydantic)│    ║
║        │                          │   │ planning/planner.py           │    ║
║        ▼                          │   └───────────────────────────────┘    ║
║   dynamic_agent_selection         │                                        ║
║        │                          │   ┌───────────────────────────────┐    ║
║        ▼                          │   │ 9 AGENT CLASSES               │    ║
║   implementation ◄────────────────┼───┤ agents/specialized.py         │    ║
║        │   (plan-driven dispatch) │   │ backend · test · frontend ·   │    ║
║        │                          │   │ database · docs · security    │    ║
║        │                          │   │ + implementer/reviewer/diag.  │    ║
║        │                          │   └───────────────┬───────────────┘    ║
║        │                          │                   │ bind_tools         ║
║        │                          │   ┌───────────────▼───────────────┐    ║
║        │                          │   │ 12 LANGCHAIN TOOLS            │    ║
║        │                          │   │ tools/registry.py             │    ║
║        │                          │   │ agents/tool_loop.py           │    ║
║        │                          │   │ (tool_calls → ToolMessage)    │    ║
║        │                          │   └───────────────────────────────┘    ║
║        ▼                                                                   ║
║   verification ──────────┐                                                 ║
║        │                 │        ┌───────────────────────────────────┐    ║
║   (FAIL)                 │        │ EXECUTION BUDGETS  budget.py      │    ║
║        ▼                 │        │ 8 hard limits, checked BEFORE     │    ║
║   failure_analysis ──────┼────────┤ every expensive operation         │    ║
║        │ (10-category    │        │ → budget_exhausted (terminal)     │    ║
║        │  classifier)    │        └───────────────────────────────────┘    ║
║        ▼                 │                                                 ║
║   recovery ──────────────┘        ┌───────────────────────────────────┐    ║
║    (bounded loop)                 │ MODEL ROUTING  routing/            │    ║
║        │                          │ per-role tiers + retry→fallback    │    ║
║   (PASS)                          └───────────────────────────────────┘    ║
║        ▼                                                                   ║
║   independent_review  (never sees the implementer's reasoning)             ║
║        │                                                                   ║
║        ▼                                                                   ║
║   security_analysis → risk_gate   security/risk.py (deterministic)         ║
║        │                                                                   ║
║   ┌────┴─────────────────┬────────────────┬──────────────────┐            ║
║   ▼                      ▼                ▼                  ▼            ║
║ finalization      human_approval     escalation      budget_exhausted     ║
║  (LOW/MED)          (HIGH risk)     (loop bound)      (limit hit)         ║
║                                                                            ║
║   LOCAL TRACE  observability/trace.py → traces.jsonl (always on)          ║
║   LangSmith = OPTIONAL additional sink                                    ║
╚════════════════════════════════════╤═══════════════════════════════════════╝
                                     │ reuses, never reimplements
╔════════════════════════════════════▼═══════════════════════════════════════╗
║  UPSTREAM OPEN SWE  (unmodified)                                           ║
║                                                                            ║
║   Sandbox isolation ....... Daytona · Modal · E2B · Runloop                ║
║   Middleware (24) ......... fallback · timeout · provider sanitisation     ║
║   Model construction ...... agent/utils/model.py                          ║
║   Deep Agents ............. create_deep_agent (the upstream agent loop)   ║
║   GitHub App · PR creation · Slack · Linear                               ║
║   MCP transport ........... langchain-mcp-adapters, integration clients   ║
║   PR-review product · dashboard · 267 upstream tests                      ║
╚════════════════════════════════════════════════════════════════════════════╝
```

**The architectural claim in one line:** upstream drives its SWE loop through a
single `create_deep_agent` ReAct loop — across 420 upstream Python files the only
`StateGraph` is a cron scheduler — so control flow is emergent from a prompt.
SWE-Forge keeps the reasoning in the LLM and makes the control flow explicit.

---

## 2. Evaluation architecture

```
                              SAME TASK
                                  │
              ┌───────────────────┴───────────────────┐
              ▼                                       ▼
        ┌───────────┐                          ┌─────────────┐
        │ Open SWE  │                          │  SWE-Forge  │
        │ upstream  │                          │   full (E)  │
        └─────┬─────┘                          └──────┬──────┘
              │      same repo · commit · model       │
              │      · timeout · sandbox              │
              └───────────────────┬───────────────────┘
                                  ▼
                          PAIRED BY task_id
                                  │
            ┌─────────────────────┼─────────────────────┐
            ▼                     ▼                     ▼
      RELIABILITY               COST                 SAFETY
   task success            model calls          security findings
   verification            tool calls           high-risk blocked
   first-attempt           tokens               human approvals
   recovery success        estimated $          unsafe changes stopped
            │                     │                     │
            └─────────────────────┼─────────────────────┘
                                  ▼
                        STATISTICAL TEST
              McNemar (discordant pairs) + bootstrap 95% CI
                                  │
                                  ▼
                  ┌───────────────────────────────┐
                  │  < 20 paired tasks            │
                  │  → INSUFFICIENT_SAMPLE        │
                  │  → NO verdict is emitted      │
                  └───────────────────────────────┘

  ┌──────────────────────────────────────────────────────────────────────┐
  │  STATUS: IMPLEMENTED BUT UNAVAILABLE                                 │
  │                                                                      │
  │  The adapter resolves the genuine `agent.server.get_agent`, and the  │
  │  scorer (McNemar + bootstrap) is implemented and tested. Execution   │
  │  requires model AND sandbox credentials, which are absent here.      │
  │                                                                      │
  │  Current measured value:  comparable_pairs = 0                       │
  │  Therefore: NO Open SWE vs SWE-Forge result exists in this repo.     │
  └──────────────────────────────────────────────────────────────────────┘
```

### What *is* available: Experiment A

```
   6 deterministic architectural fixtures  ×  5 cumulative variants  =  30 runs

   A_baseline ──► B_repo_intel ──► C_recovery ──► D_reviewer ──► E_full
   (stripped)      (+AST graph)     (+repair)      (+review)     (+risk gate)

   ALL FIVE VARIANTS ARE SWE-FORGE.  This is a component ablation,
   NOT a comparison against Open SWE.
```
