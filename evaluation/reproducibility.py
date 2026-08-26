"""Run manifests and reproducibility metadata (Phase 25, Parts 5-6, 21).

Every result must be attributable to a specific code revision. Without that, a
number in a report is an anecdote: you cannot tell whether two runs differ
because the architecture changed or because the fixtures did.

A manifest records the commit SHA, package versions, benchmark version, seed and
budget configuration. It deliberately records **no secrets** — only whether a
credential was present, never its value.
"""

import json
import os
import platform
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ARTIFACT_ROOT = Path(__file__).parent / "artifacts"
BENCHMARK_VERSION = "1.0.0"
SCHEMA_VERSION = "1"

TRACKED_PACKAGES = ("langgraph", "langchain", "langchain-core", "pydantic", "langsmith", "pytest")


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=Path(__file__).parent.parent,
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None


def git_commit() -> str | None:
    return _git("rev-parse", "HEAD")


def git_dirty() -> bool | None:
    status = _git("status", "--porcelain")
    if status is None:
        return None
    # Untracked SWE-Forge files are expected in overlay mode; tracked
    # modifications are what would invalidate reproducibility.
    return any(not line.startswith("??") for line in status.splitlines())


def package_versions() -> dict[str, str]:
    import importlib.metadata as md

    out: dict[str, str] = {}
    for name in TRACKED_PACKAGES:
        try:
            out[name] = md.version(name)
        except Exception:
            out[name] = "not-installed"
    return out


def upstream_openswe_version() -> dict[str, Any]:
    """Identify the upstream Open SWE checkout this overlay sits in."""
    import importlib.util as util

    return {
        "commit": git_commit(),
        "deepagents_installed": util.find_spec("deepagents") is not None,
        "fastapi_installed": util.find_spec("fastapi") is not None,
    }


def credential_presence() -> dict[str, bool]:
    """Presence only. Never values."""
    names = (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "LANGSMITH_API_KEY",
        "DAYTONA_API_KEY",
        "E2B_API_KEY",
    )
    return {name: bool(os.environ.get(name)) for name in names}


@dataclass
class RunManifest:
    """Everything needed to reproduce or invalidate a run."""

    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: str = field(
        default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    )
    schema_version: str = SCHEMA_VERSION
    benchmark_version: str = BENCHMARK_VERSION
    experiment: str = "unknown"
    variant: str | None = None
    seed: int | None = None
    model_mode: str = "scripted-deterministic"
    model: str | None = None
    deterministic: bool = True
    git_commit: str | None = field(default_factory=git_commit)
    git_dirty: bool | None = field(default_factory=git_dirty)
    python_version: str = field(default_factory=lambda: sys.version.split()[0])
    platform: str = field(default_factory=platform.platform)
    packages: dict[str, str] = field(default_factory=package_versions)
    upstream: dict[str, Any] = field(default_factory=upstream_openswe_version)
    budget: dict[str, Any] = field(default_factory=dict)
    scenarios: list[str] = field(default_factory=list)
    credentials_present: dict[str, bool] = field(default_factory=credential_presence)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunArtifacts:
    """Per-run output directory: manifest, results, metrics, traces, summary."""

    manifest: RunManifest
    root: Path

    @classmethod
    def create(cls, manifest: RunManifest, base: str | Path | None = None) -> "RunArtifacts":
        root = Path(base or ARTIFACT_ROOT) / manifest.run_id
        root.mkdir(parents=True, exist_ok=True)
        (root / "manifest.json").write_text(
            json.dumps(manifest.to_dict(), indent=2), encoding="utf-8"
        )
        return cls(manifest=manifest, root=root)

    def write_json(self, name: str, payload: Any) -> Path:
        path = self.root / name
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return path

    def write_text(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def write_csv(self, name: str, rows: list[dict[str, Any]]) -> Path:
        import csv

        path = self.root / name
        if not rows:
            path.write_text("", encoding="utf-8")
            return path
        fields: list[str] = []
        for row in rows:
            for key in row:
                if key not in fields:
                    fields.append(key)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        return path

    def append_traces(self, jsonl: str) -> Path:
        path = self.root / "traces.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            if jsonl.strip():
                handle.write(jsonl.rstrip("\n") + "\n")
        return path
