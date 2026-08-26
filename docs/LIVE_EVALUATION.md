# Live-Model Evaluation (Experiment C)

## Status in this environment: UNAVAILABLE

No model provider credential is configured, so **no live-model result exists and
none is reported**. This document describes the implemented track and how to run
it; it does not present numbers.

```bash
$ python -c "from evaluation.live import describe_live_availability as d; print(d())"
{'track': 'C_live_model', 'available': False,
 'reason': 'SWEFORGE_EVAL_MODEL is not set; ANTHROPIC_API_KEY is not set', ...}
```

## Why a second track exists

The scripted track (`evaluation/runner.py`) pins model behaviour so that graph
topology is the only variable. That is the right way to measure *orchestration*,
and it is why the ablation numbers are trustworthy. But it is structurally blind
to anything whose value flows through the *content* of a model decision:

- repository intelligence (planner evidence is pinned)
- experience memory (planner prompt is pinned)
- model routing (tier choice cannot change a pinned output)

`docs/EVALUATION.md` reports the resulting negative result honestly: variants A
and B are identical. Only a live model can settle those questions.

## Configuration

Entirely environmental. No key is hard-coded, and no key value is written to any
artefact — `LiveEvalConfig.to_dict()` reports `credential_present: true/false`.

```bash
export SWEFORGE_EVAL_PROVIDER=anthropic          # anthropic|openai|google|fireworks
export SWEFORGE_EVAL_MODEL=claude-sonnet-4-5
export ANTHROPIC_API_KEY=...                     # provider key, never committed
export SWEFORGE_EVAL_MAX_COST_USD=2.00           # hard ceiling for the run
export SWEFORGE_EVAL_TIMEOUT_SECONDS=600
```

The cost ceiling becomes a real `BudgetLimits`
(`LiveEvalConfig.budget_limits()`), so a live benchmark cannot overspend: the
run terminates in `budget_exhausted` rather than continuing.

## What it would measure

Against the same task set, the same repository, and the same model on both
sides: task success, tests passed, first-attempt success, recovery success,
model calls, tool calls, latency, input/output tokens, estimated cost, human
interventions, security interventions.

## Relationship to the other experiments

| Track | Question | Baseline | Status |
|---|---|---|---|
| **A — Architectural ablation** | Does each SWE-Forge component help? | Stripped SWE-Forge graph | **Complete** (30 runs) |
| **B — System baseline** | Does SWE-Forge improve on actual Open SWE? | Real upstream agent | **UNAVAILABLE** (upstream cannot execute here) |
| **C — Live model** | How does it behave with a real model deciding? | Same model both sides | **UNAVAILABLE** (no credential) |

## Running it

```bash
python -c "from evaluation.live import describe_live_availability as d; print(d())"
python -m evaluation.experiment_b --model "$SWEFORGE_EVAL_MODEL"
```

Experiment B drives both systems with the same model. With credentials absent it
reports `comparable_pairs: 0` and states explicitly that no head-to-head
conclusion may be drawn.

## Rule

If credentials are unavailable, the run is marked **unavailable**. A fabricated
live result would be worse than no result, because it would invalidate every
other number in the project.
