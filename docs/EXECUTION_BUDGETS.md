# Execution Budgets

Phase 23 addition. Before this, SWE-Forge *measured* cost and tokens after the
fact and bounded only the recovery and review loops. Nothing stopped a run from
making a hundred model calls.

## Why enforcement, not accounting

An autonomous system that edits code and calls paid APIs needs a ceiling that
holds regardless of what the model decides. The distinction that matters:

| | Accounting (before) | Enforcement (now) |
|---|---|---|
| When | after the call | **before** the call |
| On breach | reported in metrics | raises `BudgetExceeded` |
| Bypassable by a prompt | n/a | **no** — enforced in Python |
| Terminal state | none | `budget_exhausted` |

The limits are invisible to the model. There is no tool to raise them, no field
in any structured output that influences them, and no prompt text that can talk
past them. That is deliberate: a limit an agent can negotiate is not a limit.

## Limits

`agent/sweforge/budget.py` → `BudgetLimits`

| Limit | Default | Enforced by |
|---|---|---|
| `max_model_calls` | 40 | `check_model_call()` |
| `max_tool_calls` | 60 | `check_tool_call()` |
| `max_input_tokens` | 2,000,000 | `check_tokens()` |
| `max_output_tokens` | 400,000 | `check_tokens()` |
| `max_estimated_cost_usd` | 5.00 | `check_cost()` |
| `max_wall_time_seconds` | 900 | `check_wall_time()` |
| `max_recovery_attempts` | 3 | `check_recovery()` + routing function |
| `max_review_cycles` | 2 | `_review_budget_left()` |

`None` disables an individual limit. `BudgetLimits.generous()` raises them for
long-running work while keeping every one finite.

## Usage contract

```python
budget.check_model_call()          # raises BudgetExceeded if this call would breach
response = model.invoke(prompt)    # only reached when within budget
budget.consume_model_call(input_tokens=..., output_tokens=..., cost_usd=...)
```

`ExecutionBudget.sync_from_ledger(router.ledger)` adopts the authoritative
token and cost totals from the model usage ledger, so a cost ceiling reflects
what was actually recorded rather than a separate estimate.

## Routing

Budget state is consulted at branch points, so exhaustion produces an explicit
terminal state rather than a crash:

```python
def route(state) -> Literal["recovery", "escalation", "budget_exhausted"]:
    if budget is not None and (budget.is_exhausted or budget.would_exceed("model")):
        return "budget_exhausted"
    ...
```

`would_exceed()` is the non-raising predicate used for routing; the `check_*`
methods raise and are used at call sites.

One deliberate asymmetry: a run whose verification is already **green** is not
diverted to `budget_exhausted`. Reaching the review and risk gates on work that
already passes costs little and is more useful than discarding a finished
change. Asserted by `test_passing_run_not_diverted_by_budget`.

## Interaction with tools and MCP

* `SWEForgeRuntime.call_tool` consults the tool budget and returns a structured
  `{"ok": false, "error_category": "budget"}` rather than raising, so a graph
  node degrades instead of dying.
* `ToolCallingLoop` stops requesting tools when the budget is spent but still
  produces a structured result from what it already has.
* `MCPInvocationPolicy` enforces both the execution budget and its own
  per-run cap (`max_calls_per_run`, default 3).

## Tests

`tests_sweforge/test_phase23.py::TestExecutionBudget` — 12 tests covering model,
tool, cost, wall-time, token and recovery exhaustion; `None` disabling; the
non-raising predicate; snapshot headroom; ledger sync; routing to the terminal
state; and the green-run asymmetry.

## Limitation

Cost enforcement is only as accurate as the price table
(`SWEFORGE_PRICE_<TIER>`). It is an estimate derived from reported token counts,
not a provider invoice. Under scripted models the token counts are synthetic, so
the cost ceiling is exercised but the dollar figures are not real spend.
