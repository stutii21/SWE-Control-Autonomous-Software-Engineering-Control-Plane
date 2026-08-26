"""Real LangChain tool-calling loop.

Phase 23 remediation of audit row 5. Previously the graph called
``StructuredTool.invoke`` from nodes and described that as agents using tools.
That is graph-owned tool invocation, which is legitimate for deterministic
steps, but it is not tool *calling*: the model never chose a tool, never saw a
result, and never revised.

This module implements the genuine semantics:

    model.bind_tools(tools)  ->  AIMessage.tool_calls  ->  ToolMessage  ->  model

and then a final ``with_structured_output`` pass to produce the agent's typed
result. Both phases are budget-checked and error-classified.

Design notes worth defending:

* **Bounded by construction.** ``max_iterations`` caps the loop, and
  :class:`~agent.sweforge.budget.ExecutionBudget` can halt it earlier. A
  tool-calling agent that cannot be stopped is a liability.
* **Errors go back to the model, not up the stack.** A tool failure becomes a
  ``ToolMessage`` the model can read and route around, except where
  :class:`~agent.sweforge.tools.errors.ToolErrorPolicy` says the class of error
  is not worth retrying.
* **Models without tool support degrade.** If ``bind_tools`` is unavailable the
  loop skips straight to structured output rather than failing, so a scripted or
  minimal model still works.
"""

from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from pydantic import BaseModel

from agent.sweforge.budget import BudgetExceeded, ExecutionBudget
from agent.sweforge.routing.model_router import ModelSpec
from agent.sweforge.tools.errors import ToolErrorAction, ToolErrorPolicy

MAX_TOOL_ITERATIONS = 4


class ToolCallingLoop:
    """Runs a bind_tools conversation, then extracts a structured result."""

    def __init__(
        self,
        *,
        router: Any,
        node_name: str,
        agent_role: str = "unknown",
        budget: ExecutionBudget | None = None,
        error_policy: ToolErrorPolicy | None = None,
        max_iterations: int = MAX_TOOL_ITERATIONS,
    ) -> None:
        self.router = router
        self.node_name = node_name
        self.agent_role = agent_role
        self.budget = budget
        self.error_policy = error_policy or ToolErrorPolicy()
        self.max_iterations = max_iterations
        self.tool_invocations: list[dict[str, Any]] = []
        self.iterations_used = 0
        self.tool_phase_ran = False

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _bind(model: Any, tools: list[Any]) -> Any | None:
        """Bind tools, or return None when the model cannot do tool calling.

        ``BaseChatModel.bind_tools`` *exists* on every chat model but raises
        ``NotImplementedError`` unless the subclass implements it. Probing for
        the attribute is therefore not a capability check — it has to be
        called. Models without support (including the deterministic scripted
        model used in evaluation) skip the tool phase and go straight to
        structured output.
        """
        binder = getattr(model, "bind_tools", None)
        if not callable(binder):
            return None
        try:
            return binder(tools)
        except (NotImplementedError, TypeError, AttributeError):
            return None

    def _record_usage(self, model: Any, usage: dict[str, Any]) -> None:
        last = getattr(model, "last_usage", None)
        if isinstance(last, dict):
            usage.update(last)

    def _run_tool(self, tool: Any, args: dict[str, Any]) -> tuple[str, bool]:
        """Invoke one tool, applying the error policy. Returns (content, ok)."""
        attempts = 0
        while True:
            attempts += 1
            try:
                result = tool.invoke(args)
            except Exception as exc:
                classification = self.error_policy.classify_exception(exc)
                decision = self.error_policy.decide(classification, attempts)
                self.tool_invocations.append(
                    {
                        "tool": getattr(tool, "name", "unknown"),
                        "status": classification.category,
                        "attempt": attempts,
                        "action": decision.action,
                        "node": self.node_name,
                        "agent": self.agent_role,
                    }
                )
                if decision.action == ToolErrorAction.RETRY:
                    continue
                return f"TOOL ERROR ({classification.category}): {classification.message}", False

            # A structured {"ok": false} payload is also a failure signal.
            ok = not (isinstance(result, dict) and result.get("ok") is False)
            if not ok:
                classification = self.error_policy.classify_payload(result)
                decision = self.error_policy.decide(classification, attempts)
                self.tool_invocations.append(
                    {
                        "tool": getattr(tool, "name", "unknown"),
                        "status": classification.category,
                        "attempt": attempts,
                        "action": decision.action,
                        "node": self.node_name,
                        "agent": self.agent_role,
                    }
                )
                if decision.action == ToolErrorAction.RETRY:
                    continue
                return str(result), False

            self.tool_invocations.append(
                {
                    "tool": getattr(tool, "name", "unknown"),
                    "status": "ok",
                    "attempt": attempts,
                    "action": ToolErrorAction.NONE,
                    "node": self.node_name,
                    "agent": self.agent_role,
                }
            )
            return str(result)[:4000], True

    # -- main --------------------------------------------------------------
    def run(
        self,
        *,
        spec: ModelSpec,
        messages: list[BaseMessage],
        tools: list[Any],
        output_model: type[BaseModel],
    ) -> Any:
        """Execute the tool-calling phase, then return a validated output."""
        model = self.router.build_model(spec)
        conversation: list[BaseMessage] = list(messages)

        bound = self._bind(model, tools) if tools else None
        self.tool_phase_ran = bound is not None
        if bound is not None:
            by_name = {getattr(t, "name", ""): t for t in tools}

            for _ in range(self.max_iterations):
                if self.budget is not None:
                    try:
                        self.budget.check_model_call()
                    except BudgetExceeded:
                        break  # stop calling tools; still produce a result below

                with self.router.track(self.node_name, spec) as usage:
                    response = bound.invoke(conversation)
                    self._record_usage(model, usage)
                self.iterations_used += 1
                if self.budget is not None:
                    self.budget.consume_model_call()

                calls = list(getattr(response, "tool_calls", []) or [])
                if not calls:
                    if isinstance(response, AIMessage):
                        conversation.append(response)
                    break

                conversation.append(response)
                for call in calls:
                    name = call.get("name", "")
                    # Attribute agent tool calls to this agent in the trace.
                    for candidate in by_name.values():
                        ctx = getattr(candidate, "_sweforge_context", None)
                        if ctx is not None:
                            ctx.current_agent = self.agent_role
                            ctx.current_node = self.node_name
                            break
                    tool = by_name.get(name)
                    call_id = call.get("id") or name
                    if tool is None:
                        conversation.append(
                            ToolMessage(
                                content=f"TOOL ERROR (not_found): no tool named {name!r}",
                                tool_call_id=call_id,
                            )
                        )
                        continue
                    if self.budget is not None:
                        try:
                            self.budget.check_tool_call()
                        except BudgetExceeded as exc:
                            conversation.append(
                                ToolMessage(
                                    content=f"TOOL BUDGET EXHAUSTED: {exc}",
                                    tool_call_id=call_id,
                                )
                            )
                            break
                        self.budget.consume_tool_call()
                    content, _ok = self._run_tool(tool, call.get("args") or {})
                    conversation.append(ToolMessage(content=content, tool_call_id=call_id))

        # Final structured extraction.
        if self.budget is not None:
            self.budget.check_model_call()
        with self.router.track(self.node_name, spec) as usage:
            result = model.with_structured_output(output_model).invoke(conversation)
            self._record_usage(model, usage)
        if self.budget is not None:
            self.budget.consume_model_call()
        return result
