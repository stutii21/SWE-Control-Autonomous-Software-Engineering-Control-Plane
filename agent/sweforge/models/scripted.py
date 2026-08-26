"""Deterministic scripted chat model.

Why this exists
---------------
The SWE-Forge contribution is *orchestration*: conditional routing, bounded
recovery loops, an independent review gate, a risk gate. To evaluate
orchestration you must hold the LLM's behaviour constant — otherwise you are
measuring model sampling noise, not architecture.

``ScriptedChatModel`` is a real ``BaseChatModel`` subclass that returns a
pre-declared sequence of validated structured outputs per role. It lets the
evaluation harness construct exact scenarios ("the first implementation
attempt fails an assertion, the second fixes it") and then measure what the
graph *actually did*: how many recovery attempts ran, whether the review gate
fired, which terminal state was reached, how long it took.

Honesty boundary
----------------
Token counts produced here are **synthetic** (derived deterministically from
prompt length), so any cost figure computed from a scripted run is an
accounting demonstration of the ledger, not a real provider bill. Every report
generated from scripted runs labels those columns as synthetic. No claim about
real-world model accuracy is derived from this class.
"""

from collections.abc import Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import BaseModel


class ScriptExhausted(RuntimeError):
    """Raised when a script has no response left and no fallback is allowed."""


class ScriptedChatModel(BaseChatModel):
    """A chat model that replays a fixed list of outputs for one role.

    ``outputs`` may contain Pydantic instances (returned as-is from
    ``with_structured_output``) or plain strings (returned as message content).
    """

    role: str = "unknown"
    outputs: list[Any] = []
    #: Scripted tool-call rounds. Each entry is a list of
    #: ``{"name": ..., "args": {...}}`` dicts the model "decides" to call on
    #: that iteration. An empty list ends the tool phase. This is what makes
    #: the real bind_tools -> tool_calls -> ToolMessage path testable without a
    #: live provider.
    tool_call_script: list[list[dict]] = []
    repeat_last: bool = True
    calls: list[str] = []
    last_usage: dict[str, int] = {}
    _cursor: int = 0

    model_config = {"arbitrary_types_allowed": True}

    @property
    def _llm_type(self) -> str:
        return "sweforge-scripted"

    # -- core ---------------------------------------------------------------
    def _next(self) -> Any:
        if not self.outputs:
            raise ScriptExhausted(f"no scripted outputs for role {self.role!r}")
        if self._cursor < len(self.outputs):
            value = self.outputs[self._cursor]
            self._cursor += 1
            return value
        if self.repeat_last:
            return self.outputs[-1]
        raise ScriptExhausted(
            f"script for role {self.role!r} exhausted after {len(self.outputs)} calls"
        )

    @staticmethod
    def _synthetic_usage(messages: Sequence[BaseMessage], rendered: str) -> dict[str, int]:
        prompt_chars = sum(len(str(getattr(m, "content", ""))) for m in messages)
        return {
            "input_tokens": max(1, prompt_chars // 4),
            "output_tokens": max(1, len(rendered) // 4),
            "total_tokens": max(2, (prompt_chars + len(rendered)) // 4),
        }

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        value = self._next()
        if isinstance(value, BaseModel):
            rendered = value.model_dump_json()
        else:
            rendered = str(value)
        self.calls.append(rendered[:2000])
        message = AIMessage(
            content=rendered,
            usage_metadata=self._synthetic_usage(messages, rendered),  # type: ignore[arg-type]
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    # -- structured output --------------------------------------------------
    def bind_tools(self, tools: Any, **kwargs: Any) -> Runnable[Any, Any]:
        """Replay scripted tool calls through the genuine LangChain contract.

        Returns a runnable that emits ``AIMessage`` objects carrying
        ``tool_calls``, exactly as a tool-capable provider would, so the
        ToolCallingLoop under test executes its real code path.
        """
        known = {getattr(t, "name", "") for t in (tools or [])}
        rounds = list(self.tool_call_script)
        state = {"index": 0}

        def _invoke(prompt: Any) -> AIMessage:
            index = state["index"]
            state["index"] = index + 1
            calls = rounds[index] if index < len(rounds) else []
            emitted = []
            for position, call in enumerate(calls):
                name = call.get("name", "")
                # Unknown names are still emitted: the loop must handle them.
                emitted.append(
                    {
                        "name": name,
                        "args": call.get("args", {}),
                        "id": call.get("id") or f"{self.role}-{index}-{position}",
                        "type": "tool_call",
                    }
                )
            self.calls.append(f"[bind_tools round {index}] {[c['name'] for c in emitted]}")
            _ = known
            return AIMessage(
                content="" if emitted else "no further tools needed",
                tool_calls=emitted,
                usage_metadata={"input_tokens": 8, "output_tokens": 4, "total_tokens": 12},
            )

        return RunnableLambda(_invoke)

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Runnable[Any, Any]:
        """Return validated instances of ``schema`` from the script.

        The scripted value is validated through ``schema`` rather than trusted,
        so a malformed fixture fails the test instead of silently producing an
        object the production code would never see.
        """

        def _invoke(prompt: Any) -> Any:
            messages = prompt if isinstance(prompt, list) else [prompt]
            value = self._next()
            rendered = value.model_dump_json() if isinstance(value, BaseModel) else str(value)
            self.calls.append(rendered[:2000])
            # Record synthetic usage on the model so callers can read it back.
            self.last_usage = self._synthetic_usage(
                [m for m in messages if isinstance(m, BaseMessage)], rendered
            )
            if isinstance(value, schema):
                return value
            if isinstance(value, BaseModel):
                return schema.model_validate(value.model_dump())
            if isinstance(value, dict):
                return schema.model_validate(value)
            return schema.model_validate_json(str(value))

        return RunnableLambda(_invoke)


class ScriptedModelFactory:
    """Router-compatible factory that hands out per-role scripted models.

    Passed to :class:`~agent.sweforge.routing.model_router.ModelRouter` as
    ``model_factory``, so routing logic (tier selection, ledger accounting)
    runs exactly as it would in production while the LLM stays deterministic.
    """

    def __init__(
        self,
        script: dict[str, list[Any]],
        *,
        repeat_last: bool = True,
        tool_calls: dict[str, list[list[dict]]] | None = None,
    ) -> None:
        self.script = script
        self.repeat_last = repeat_last
        self.tool_calls = tool_calls or {}
        self.instances: dict[str, ScriptedChatModel] = {}

    def __call__(self, spec: Any, **kwargs: Any) -> ScriptedChatModel:
        role = getattr(spec, "role", "unknown")
        # One instance per role so a multi-step script advances across nodes.
        if role not in self.instances:
            self.instances[role] = ScriptedChatModel(
                role=role,
                outputs=list(self.script.get(role, [])),
                repeat_last=self.repeat_last,
                tool_call_script=list(self.tool_calls.get(role, [])),
                calls=[],
            )
        return self.instances[role]

    def call_count(self) -> int:
        return sum(len(m.calls) for m in self.instances.values())
