# Demo Guide

## Setup (about a minute)

```bash
git clone <this-repo> && cd open-swe
python -m venv .venv && source .venv/bin/activate
pip install "langgraph>=1.0" "langchain>=1.0" "pydantic>=2" pytest ruff
```

SWE-Forge's core needs only LangGraph, LangChain, Pydantic. **No API key is required**
for the tests, the repository analyzer, the demo, or the evaluation suite.

```bash
export SWEFORGE_ALLOW_LOCAL_EXEC=1   # required: demos run real pytest on shipped fixtures
```

## 1. Repository intelligence (no model calls)

Point the analyzer at any Python repository — including this one:

```bash
python -m agent.sweforge.cli analyze --repo . \
  --task "model fallback middleware retry on provider error"
```

Real output against the upstream Open SWE codebase:

```
{ "file_count": 907, "symbol_count": 7429, "analysis_seconds": 1.47 }
graph: { "import_edges": 1036, "test_files": 278 }

  28.10  agent/middleware/model_fallback.py
         path matches ['fallback', 'middleware', 'model']
         defines ['error', 'fallback', 'middleware', 'model', 'provider']
  21.00  tests/middleware/test_model_fallback_middleware.py
  20.05  agent/middleware/model_call_timeout.py
```

The top hit is the correct file, and every ranking shows *why* it ranked.

Inspect blast radius or a symbol:

```bash
python -m agent.sweforge.cli analyze --repo . --file agent/utils/model.py
python -m agent.sweforge.cli analyze --repo . --symbol make_model
```

## 2. The full workflow — recovery

```bash
python -m agent.sweforge.cli demo --scenario inventory_boundary_recovery
```

The first implementation attempt is deliberately wrong (off-by-one). Real `pytest` fails,
the failure is classified, a repair is applied, and re-verification passes:

```
 1. -> task_intake
 2. -> repository_analysis
 3. -> task_complexity_analysis
 4. -> planning
 5. -> dynamic_agent_selection
 6. -> implementation(1 subtasks, 1 files)
 7. -> verification(FAIL)
 8. -> failure_analysis(test_assertion)
 9. -> recovery(attempt 1, 1 files)
10. -> verification(PASS)
11. -> independent_review(APPROVED)
12. -> security_analysis(0 findings)
13. -> risk_gate(LOW, score=0)
14. -> finalization
```

## 3. The risk gate blocking a credential — the best 60 seconds

```bash
python -m agent.sweforge.cli demo --scenario pipeline_secret_risk_gate
```

Tests pass. The reviewer approves. The change still does not ship:

```
5. VERIFICATION      result : PASSED     summary : tests 1/1
7. INDEPENDENT REVIEW verdict : APPROVED
8. SECURITY ANALYSIS
   [blocker] github_token deploy.py:5 — Possible GitHub token committed
9. RISK GATE
   score : 90/100 (HIGH)
     + 60 security_blocker: 1 blocker finding(s): github_token
     + 30 sensitive_ci_workflow: modified 1 ci_workflow file(s): .github/workflows/ci.yml
10. OUTCOME
   final status : awaiting_human_approval
```

Every other workflow variant shipped this change. Compare directly:

```bash
python -m agent.sweforge.cli demo --scenario pipeline_secret_risk_gate --baseline
# → completed
```

## 4. The bounded recovery loop

```bash
python -m agent.sweforge.cli demo --scenario inventory_recovery_exhausted
```

The scripted repair is always wrong. The graph stops after exactly 3 attempts:

```
attempt 1: category=test_assertion   strategy: reapply the same wrong comparison
attempt 2: category=test_assertion   strategy: reapply the same wrong comparison
attempt 3: category=test_assertion   strategy: reapply the same wrong comparison
final status : escalated_recovery_exhausted
```

The bound is enforced by a routing function, so it is a property of the graph topology
rather than a prompt instruction.

## 5. The review gate catching green-but-wrong work

```bash
python -m agent.sweforge.cli demo --scenario billing_review_rejection
```

Verification passes, but the reviewer records a `major` finding (only one of three
required validations was implemented), which routes back through recovery.

## All scenarios

```bash
python -m agent.sweforge.cli demo --list
```

| Scenario | Demonstrates |
|---|---|
| `billing_validation_first_try` | Clean first-attempt success |
| `inventory_boundary_recovery` | Assertion failure → diagnosis → repair |
| `textutil_syntax_recovery` | Syntax error → repair |
| `inventory_recovery_exhausted` | Bounded loop → escalation |
| `billing_review_rejection` | Review gate → recovery → approval |
| `pipeline_secret_risk_gate` | Risk gate → human approval |

## 6. Tests

```bash
python -m pytest -c pytest-sweforge.ini      # 478 tests, no API key, no network
```

## 7. Evaluation and ablation

```bash
python -m evaluation.runner        # 30 runs (6 scenarios x 5 variants), ~37s
python -m evaluation.evaluator     # writes Markdown + CSV + JSON
```

Subsets:

```bash
python -m evaluation.runner --variants A_baseline E_full
python -m evaluation.runner --scenarios inventory_boundary_recovery
```

Generated artefacts land in `evaluation/results/` and `evaluation/reports/`.

## 8. Configuration check

```bash
python -m agent.sweforge.cli doctor
```

Shows resolved model tiers, which provider credentials are present (names only, never
values), LangSmith status and execution-backend safety.

## 9. Running a real task (needs credentials)

```bash
cp .env.example .env    # then add a provider key
python -m agent.sweforge.cli run --repo /path/to/repo --task "Fix the off-by-one in ..."
```

For any repository you do not fully trust, use the Open SWE sandbox backend rather than
`--backend local`. See `docs/SECURITY.md`.

## What the demo does and does not prove

**Does:** the graph routes correctly, the recovery bound holds, the gates fire, real
`pytest` runs against real fixture code, and the metrics are genuine measurements.

**Does not:** say anything about frontier-model capability. Demo model outputs are pinned
by `evaluation/scenarios.py` so runs are reproducible. See `docs/EVALUATION.md` §1 for
the full scope boundary.

---

# Phase 23/24 additions

## Inspect the specialized agents

```bash
python -c "
from agent.sweforge.agents.specialized import AGENT_CLASSES
for role, cls in sorted(AGENT_CLASSES.items()):
    print(f'{role:22s} {cls.__name__:20s} {cls.output_model.__name__:20s} {cls.model_role}')
"
```

## Verify budgets actually stop execution

```bash
python -m pytest -c pytest-sweforge.ini tests_sweforge/test_phase23.py -k ExecutionBudget -v
```

## Verify model retry then fallback

```bash
python -m pytest -c pytest-sweforge.ini tests_sweforge/test_phase23.py -k ModelExecutionPolicy -v
```

## Verify MCP orchestration (deterministic fixtures)

```bash
python -m pytest -c pytest-sweforge.ini tests_sweforge/test_phase23.py -k MCP -v
```

## Check the Open SWE baseline status

```bash
python -c "from evaluation.baselines import describe_baseline_availability as d; import json; print(json.dumps(d(), indent=2))"
python -m evaluation.experiment_b            # reports comparable_pairs, never fabricates
```

## Check live-model availability

```bash
python -c "from evaluation.live import describe_live_availability as d; print(d())"
```

Both report **UNAVAILABLE** in this environment, with the exact missing
dependency or credential named.
