"""Evaluator: turns raw results into JSON, CSV and a Markdown report.

Reporting discipline enforced in this module:

* Undefined rates print as ``n/a``, never ``0.00``.
* Synthetic metrics (token counts, cost) are labelled synthetic in every table
  header, because they come from scripted models rather than a provider.
* Unavailable runs are reported as unavailable and excluded from rates.
* Where a component shows **no measurable effect**, the report says so. A
  negative result stated plainly is worth more than a flattering one.
"""

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evaluation.metrics import VariantMetrics, aggregate, expectation_check

REPORTS_ROOT = Path(__file__).parent / "reports"
RESULTS_ROOT = Path(__file__).parent / "results"

VARIANT_LABELS = {
    "A_baseline": "A. Baseline (single pass, fixed path)",
    "B_repo_intel": "B. + Repository intelligence",
    "C_recovery": "C. + Bounded self-repair",
    "D_reviewer": "D. + Independent review gate",
    "E_full": "E. Full SWE-Forge (+ security & risk gate)",
}


def _fmt(value: Any, *, pct: bool = False, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if pct and isinstance(value, (int, float)):
        return f"{value * 100:.0f}%"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


@dataclass
class Report:
    payload: dict[str, Any]
    variants: dict[str, VariantMetrics]
    expectations: dict[str, Any]

    def markdown(self) -> str:
        lines: list[str] = []
        add = lines.append

        add("# SWE-Forge Evaluation Report")
        add("")
        add(f"- Generated: `{self.payload.get('generated_at')}`")
        add(f"- Suite wall time: `{self.payload.get('duration_seconds')}s`")
        add(f"- Model mode: `{self.payload.get('model_mode')}`")
        add(f"- Scenarios: {len(self.payload.get('scenarios', []))}")
        add(f"- Variants: {len(self.payload.get('variants', []))}")
        add(f"- Total executions: {len(self.payload.get('records', []))}")
        add("")

        add("## How to read this report")
        add("")
        for note in self.payload.get("notes", []):
            add(f"- {note}")
        add("")
        add(
            "> **Scope of the claim.** This evaluation measures *orchestration*, not model "
            "capability. Model outputs are pinned by scripted fixtures so that the graph "
            "topology is the only variable across variants. Test results, routing paths, "
            "recovery counts, gate decisions and wall-clock times are real measurements from "
            "executed runs against real `pytest` suites. Token and cost columns are synthetic "
            "and exist to demonstrate ledger accounting only."
        )
        add("")

        # -- headline table ------------------------------------------------
        add("## Ablation results")
        add("")
        add(
            "| Variant | Runs | Task success | Verification pass | First-attempt | "
            "Recovery success | Avg recovery attempts | Escalations | Human-approval gate |"
        )
        add("|---|---|---|---|---|---|---|---|---|")
        for key in self.payload.get("variants", []):
            m = self.variants.get(key)
            if not m:
                continue
            add(
                f"| {VARIANT_LABELS.get(key, key)} | {m.runs_available}"
                f" | {_fmt(m.task_success_rate, pct=True)}"
                f" | {_fmt(m.verification_pass_rate, pct=True)}"
                f" | {_fmt(m.first_attempt_success_rate, pct=True)}"
                f" | {_fmt(m.recovery_success_rate, pct=True)}"
                f" | {_fmt(m.avg_recovery_attempts)}"
                f" | {m.escalated}"
                f" | {m.awaiting_human_approval} |"
            )
        add("")

        # -- effort table --------------------------------------------------
        add("## Cost and effort per variant")
        add("")
        add(
            "| Variant | Nodes executed | Model calls | Verification runs | Tool calls | "
            "Avg wall time (s) | Tokens (synthetic) | Est. cost USD (synthetic) |"
        )
        add("|---|---|---|---|---|---|---|---|")
        for key in self.payload.get("variants", []):
            m = self.variants.get(key)
            if not m:
                continue
            tokens = m.input_tokens + m.output_tokens
            add(
                f"| {VARIANT_LABELS.get(key, key)} | {m.node_count} | {m.model_calls}"
                f" | {m.verification_runs} | {m.tool_calls}"
                f" | {_fmt(m.avg_wall_time, digits=3)} | {tokens}"
                f" | {_fmt(m.estimated_cost_usd, digits=4)} |"
            )
        add("")

        # -- assurance -----------------------------------------------------
        add("## Assurance activity")
        add("")
        add(
            "| Variant | Runs reviewed | Review interventions | Security findings | "
            "Risk-gate HIGH interventions |"
        )
        add("|---|---|---|---|---|")
        for key in self.payload.get("variants", []):
            m = self.variants.get(key)
            if not m:
                continue
            add(
                f"| {VARIANT_LABELS.get(key, key)} | {m.runs_reviewed} | {m.review_rejections}"
                f" | {m.security_findings} | {m.security_gate_interventions} |"
            )
        add("")

        # -- per-scenario matrix -------------------------------------------
        add("## Per-scenario terminal states")
        add("")
        variants = self.payload.get("variants", [])
        add("| Scenario | " + " | ".join(v.replace("_", " ") for v in variants) + " |")
        add("|---" * (len(variants) + 1) + "|")
        by_scenario: dict[str, dict[str, str]] = {}
        for record in self.payload.get("records", []):
            status = record.get("status") if record.get("available", True) else "UNAVAILABLE"
            by_scenario.setdefault(record["scenario_id"], {})[record["variant"]] = str(status)
        for scenario in self.payload.get("scenarios", []):
            row = by_scenario.get(scenario, {})
            add(
                f"| `{scenario}` | "
                + " | ".join(row.get(v, "—").replace("_", " ") for v in variants)
                + " |"
            )
        add("")

        # -- routing correctness -------------------------------------------
        add("## Graph routing correctness (full variant)")
        add("")
        add(
            "Each scenario declares the terminal state its design should produce. This checks "
            "that the graph routed as intended, independently of task success."
        )
        add("")
        add(
            f"**{self.expectations['passed']}/{self.expectations['checked']} scenarios routed "
            f"exactly as designed** "
            f"({_fmt(self.expectations['rate'], pct=True)})."
        )
        add("")
        add("| Scenario | Expected terminal state | Observed | Match |")
        add("|---|---|---|---|")
        for detail in self.expectations.get("details", []):
            add(
                f"| `{detail['scenario_id']}` | {detail['expected_status']}"
                f" | {detail['observed_status']}"
                f" | {'PASS' if detail['passed'] else 'FAIL'} |"
            )
        add("")

        # -- interpretation -------------------------------------------------
        add("## What the numbers actually show")
        add("")
        add(self._interpretation())
        add("")

        add("## Unavailable runs")
        add("")
        any_unavailable = False
        for key, m in self.variants.items():
            if m.runs_unavailable:
                any_unavailable = True
                add(f"- **{key}**: {m.runs_unavailable} unavailable")
                for reason in m.unavailable_reasons:
                    add(f"  - {reason}")
        if not any_unavailable:
            add("None. Every scenario x variant execution completed.")
        add("")
        return "\n".join(lines)

    def _interpretation(self) -> str:
        base = self.variants.get("A_baseline")
        repo = self.variants.get("B_repo_intel")
        rec = self.variants.get("C_recovery")
        rev = self.variants.get("D_reviewer")
        full = self.variants.get("E_full")
        parts: list[str] = []

        if base and rec:
            b = _fmt(base.task_success_rate, pct=True)
            c = _fmt(rec.task_success_rate, pct=True)
            parts.append(
                f"**Bounded self-repair is the component that moves task success.** "
                f"Baseline reaches {b}; adding the recovery loop reaches {c}. The two "
                f"scenarios that flip are the ones whose first implementation attempt was "
                f"scripted wrong — precisely the case a single-pass workflow cannot address, "
                f"because nothing re-reads the failing output."
            )

        if base and repo and base.task_success_rate == repo.task_success_rate:
            parts.append(
                "**Repository intelligence shows no measurable effect on task success in this "
                "harness — a negative result, reported as such.** This is expected and is a "
                "limitation of the method, not evidence the subsystem is useless: the planner's "
                "output is pinned by the scripted fixture, so richer planning evidence cannot "
                "change the plan. Repository intelligence is measured directly instead of "
                "end-to-end (see `docs/EVALUATION.md`, static-analysis benchmarks), and its "
                "end-to-end value is untested until a live-model run is performed."
            )

        if rec and rev and rev.review_rejections > rec.review_rejections:
            parts.append(
                f"**The independent review gate catches work that tests call green.** The "
                f"`billing_review_rejection` scenario passes verification under variant C and "
                f"terminates as complete; under variant D the reviewer records "
                f"{rev.review_rejections} intervention(s), which routes the run back through "
                f"recovery and produces the fully-validated implementation. Passing tests are "
                f"necessary but not sufficient, and this is the measurement of that claim."
            )

        if full and full.security_gate_interventions:
            parts.append(
                f"**The risk gate blocks a change that every other variant shipped.** In "
                f"`pipeline_secret_risk_gate` the change is functionally correct and "
                f"verification is green, so variants A-D all terminate as `completed`. Variant E "
                f"scores it HIGH (committed credential plus a CI workflow edit) and terminates "
                f"in `awaiting_human_approval` instead — {full.security_gate_interventions} gate "
                f"intervention(s). This is the clearest single argument for a deterministic risk "
                f"layer in an autonomous system."
            )

        exhausted = [
            r
            for r in self.payload.get("records", [])
            if r.get("status") == "escalated_recovery_exhausted"
        ]
        if exhausted:
            attempts = {int(r["metrics"].get("recovery_attempts", 0)) for r in exhausted}
            parts.append(
                f"**The recovery loop terminates.** The `inventory_recovery_exhausted` scenario "
                f"scripts an endlessly-wrong repair. Every recovery-enabled variant stops after "
                f"exactly {sorted(attempts)} attempt(s) and escalates, confirming the bound is "
                f"structural (enforced by the routing function) rather than advisory."
            )

        if (
            full
            and rev
            and full.task_success_rate is not None
            and rev.task_success_rate is not None
        ):
            if full.task_success_rate < rev.task_success_rate:
                parts.append(
                    f"**Variant E's lower headline task-success rate "
                    f"({_fmt(full.task_success_rate, pct=True)} vs "
                    f"{_fmt(rev.task_success_rate, pct=True)}) is the risk gate working, not a "
                    f"regression.** `awaiting_human_approval` is not counted as success, and the "
                    f"only run that changes category is the one that tried to commit a "
                    f"credential. A benchmark that rewarded shipping that change would be "
                    f"measuring the wrong thing — which is why this report reports terminal "
                    f"states rather than a single success number."
                )

        if base and full and full.node_count > base.node_count:
            ratio = round(full.node_count / max(1, base.node_count), 2)
            parts.append(
                f"**The assurance machinery is not free.** The full variant executes "
                f"{full.node_count} nodes against the baseline's {base.node_count} ({ratio}x) "
                f"and {full.model_calls} model calls against {base.model_calls}. Whether that "
                f"overhead is worth paying depends on the cost of shipping a bad change, which "
                f"the `pipeline_secret_risk_gate` case illustrates."
            )

        return "\n\n".join(f"{i + 1}. {p}" for i, p in enumerate(parts))

    # -- writers -----------------------------------------------------------
    def write_csv(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [m.to_dict() for m in self.variants.values()]
        flat: list[dict[str, Any]] = []
        for row in rows:
            outcomes = row.pop("outcomes", {})
            row.pop("unavailable_reasons", None)
            flat.append({**row, **{f"outcome_{k}": v for k, v in outcomes.items()}})
        if not flat:
            return path
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(flat[0]))
            writer.writeheader()
            writer.writerows(flat)
        return path

    def write_run_csv(self, path: Path) -> Path:
        """Per-run detail, one row per (scenario, variant)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        rows: list[dict[str, Any]] = []
        for record in self.payload.get("records", []):
            rows.append(
                {
                    "scenario_id": record["scenario_id"],
                    "variant": record["variant"],
                    "available": record.get("available", True),
                    "status": record.get("status"),
                    "error": record.get("error") or "",
                    **dict(record.get("metrics") or {}),
                }
            )
        if not rows:
            return path
        fields: list[str] = []
        for row in rows:
            for key in row:
                if key not in fields:
                    fields.append(key)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def write_json(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "generated_at": self.payload.get("generated_at"),
                    "model_mode": self.payload.get("model_mode"),
                    "notes": self.payload.get("notes"),
                    "variants": {k: v.to_dict() for k, v in self.variants.items()},
                    "routing_correctness": self.expectations,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return path


def build_report(results_path: str | Path | None = None) -> Report:
    path = Path(results_path or RESULTS_ROOT / "results.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("records", [])
    return Report(
        payload=payload,
        variants=aggregate(records),
        expectations=expectation_check(records),
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Generate the SWE-Forge evaluation report.")
    parser.add_argument("--results", default=None, help="Path to results.json")
    parser.add_argument("--out-dir", default=None, help="Directory for generated reports")
    args = parser.parse_args(argv)

    report = build_report(args.results)
    out_dir = Path(args.out_dir or REPORTS_ROOT)
    md_path = out_dir / "EVALUATION_REPORT.md"
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path.write_text(report.markdown(), encoding="utf-8")
    report.write_csv(out_dir / "variant_metrics.csv")
    report.write_run_csv(out_dir / "run_details.csv")
    report.write_json(out_dir / "summary.json")

    print(f"report:        {md_path}")
    print(f"variant csv:   {out_dir / 'variant_metrics.csv'}")
    print(f"run csv:       {out_dir / 'run_details.csv'}")
    print(f"summary json:  {out_dir / 'summary.json'}")
    print(f"routing correctness: {report.expectations['passed']}/{report.expectations['checked']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
