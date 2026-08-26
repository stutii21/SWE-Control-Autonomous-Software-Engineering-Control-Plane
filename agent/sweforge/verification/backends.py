"""Command execution backends for verification.

Security posture
----------------
Verification runs repository code, which is untrusted by definition. The
production path is therefore :class:`OpenSWESandboxBackend`, which delegates to
the Open SWE sandbox infrastructure (Daytona / Modal / E2B / Runloop via
``deepagents`` ``SandboxBackendProtocol``). SWE-Forge does not implement its own
sandbox — reusing mature upstream isolation is the correct call.

:class:`LocalSubprocessBackend` exists for one purpose: running the checked-in
evaluation fixtures, which are code SWE-Forge itself ships. It refuses to run
unless ``SWEFORGE_ALLOW_LOCAL_EXEC=1`` is set explicitly, so it can never
become the accidental default for a real repository.
"""

import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

LOCAL_EXEC_ENV = "SWEFORGE_ALLOW_LOCAL_EXEC"


class LocalExecutionForbidden(RuntimeError):
    """Raised when host execution is attempted without explicit opt-in."""


@dataclass
class ExecResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def combined_output(self) -> str:
        return f"{self.stdout}\n{self.stderr}".strip()


@runtime_checkable
class ExecutionBackend(Protocol):
    """Minimal contract the verifier needs from any execution environment."""

    name: str

    def run(self, command: str, *, timeout: int = 300) -> ExecResult: ...

    def write_file(self, path: str, content: str) -> None: ...

    def read_file(self, path: str) -> str: ...


class LocalSubprocessBackend:
    """Runs commands on the host. Gated; intended for shipped fixtures only."""

    name = "local-subprocess"

    def __init__(self, root: str | Path, *, env: dict[str, str] | None = None) -> None:
        environ = env if env is not None else dict(os.environ)
        if environ.get(LOCAL_EXEC_ENV) != "1":
            raise LocalExecutionForbidden(
                "Refusing to execute repository code on the host. "
                f"Set {LOCAL_EXEC_ENV}=1 only for SWE-Forge's own evaluation fixtures; "
                "use OpenSWESandboxBackend for real repositories."
            )
        self.root = Path(root).resolve()
        self._env = environ

    def _resolve(self, path: str) -> Path:
        target = (self.root / path).resolve()
        if not str(target).startswith(str(self.root)):
            raise ValueError(f"path escapes fixture root: {path!r}")
        return target

    def run(self, command: str, *, timeout: int = 300) -> ExecResult:
        started = time.perf_counter()
        try:
            completed = subprocess.run(  # noqa: S603 - gated, fixture-only
                shlex.split(command),
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**self._env, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            return ExecResult(
                command=command,
                exit_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                duration_seconds=time.perf_counter() - started,
            )
        except subprocess.TimeoutExpired as exc:
            return ExecResult(
                command=command,
                exit_code=124,
                stdout=(exc.stdout or b"").decode()
                if isinstance(exc.stdout, bytes)
                else (exc.stdout or ""),
                stderr=f"command timed out after {timeout}s",
                duration_seconds=time.perf_counter() - started,
                timed_out=True,
            )
        except FileNotFoundError as exc:
            return ExecResult(
                command=command,
                exit_code=127,
                stdout="",
                stderr=f"executable not found: {exc}",
                duration_seconds=time.perf_counter() - started,
            )

    def write_file(self, path: str, content: str) -> None:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def read_file(self, path: str) -> str:
        return self._resolve(path).read_text(encoding="utf-8", errors="replace")


class OpenSWESandboxBackend:
    """Adapter over the upstream Open SWE / deepagents sandbox backend.

    Upstream owns sandbox lifecycle, provisioning and the GitHub proxy. This
    class only translates the SWE-Forge verifier contract onto whatever
    ``SandboxBackendProtocol`` implementation the host thread already has, so
    no isolation logic is duplicated or weakened.
    """

    name = "open-swe-sandbox"

    def __init__(self, sandbox: Any, *, workdir: str = ".") -> None:
        self._sandbox = sandbox
        self.workdir = workdir

    @classmethod
    async def for_thread(cls, thread_id: str, **kwargs: Any) -> "OpenSWESandboxBackend":
        """Attach to the sandbox Open SWE already provisioned for a thread."""
        from agent.runtime.sandbox import ensure_sandbox_for_thread

        sandbox = await ensure_sandbox_for_thread(thread_id, **kwargs)
        return cls(sandbox)

    def _exec(self, command: str, timeout: int) -> Any:
        """Call whichever execute method the upstream backend exposes."""
        for attr in ("execute", "exec", "run_command", "run"):
            fn = getattr(self._sandbox, attr, None)
            if callable(fn):
                return fn(command, timeout=timeout) if attr != "run" else fn(command)
        raise AttributeError(
            "sandbox backend exposes no known execute method (tried execute/exec/run_command/run)"
        )

    def run(self, command: str, *, timeout: int = 300) -> ExecResult:
        started = time.perf_counter()
        raw = self._exec(command, timeout)
        exit_code = getattr(raw, "exit_code", getattr(raw, "returncode", 0)) or 0
        stdout = getattr(raw, "stdout", "") or ""
        stderr = getattr(raw, "stderr", "") or ""
        if not stdout and isinstance(raw, str):
            stdout = raw
        return ExecResult(
            command=command,
            exit_code=int(exit_code),
            stdout=str(stdout),
            stderr=str(stderr),
            duration_seconds=time.perf_counter() - started,
        )

    def write_file(self, path: str, content: str) -> None:
        for attr in ("write_file", "write", "put_file"):
            fn = getattr(self._sandbox, attr, None)
            if callable(fn):
                fn(path, content)
                return
        raise AttributeError("sandbox backend exposes no write_file method")

    def read_file(self, path: str) -> str:
        for attr in ("read_file", "read", "get_file"):
            fn = getattr(self._sandbox, attr, None)
            if callable(fn):
                return str(fn(path))
        raise AttributeError("sandbox backend exposes no read_file method")
