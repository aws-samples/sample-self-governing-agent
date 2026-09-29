"""
Governed Agent — real Strands integration.

Replaces the fictional `agent_runtime.step()` interface from the first draft.
Strands owns its event loop; you do not drive it iteration by iteration. You
observe and interrupt it through the hook API:

    BeforeToolCallEvent   -> event.cancel_tool = "<message>"   blocks a tool call
    BeforeModelCallEvent  -> event.cancel = "<message>"        blocks a model call
                             event.projected_input_tokens      pre-call token estimate
    AfterToolCallEvent    -> observe results
    AfterInvocationEvent  -> emit telemetry

Three budgets are enforced here, all verified against strands-agents 1.48:
  - tool calls   (count of BeforeToolCallEvent)
  - model calls  (count of BeforeModelCallEvent; the real "iteration" analogue)
  - input tokens (cumulative projected_input_tokens)

The token budget matters most: iteration caps do not bound cost, because context
grows with every step. A 30-iteration cap on a long conversation can cost far
more than 30x the first call.

This layer is a fast, cooperative stop that gives the model a clean signal. It
is NOT the security boundary — it runs inside the agent process. The gateway
interceptor and Cedar are the boundary.
"""

from dataclasses import dataclass, field

from strands.hooks import (
    AfterInvocationEvent,
    AfterToolCallEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)


@dataclass
class BudgetState:
    """Mutable usage counters for one invocation."""

    max_tool_calls: int
    max_model_calls: int
    max_input_tokens: int

    tool_calls: int = 0
    model_calls: int = 0
    input_tokens: int = 0

    # Which budget stopped the run, if any.
    exhausted: str | None = None
    tools_invoked: list[str] = field(default_factory=list)

    @property
    def stopped_early(self) -> bool:
        return self.exhausted is not None

    def utilization(self) -> dict:
        """Fraction of each budget consumed, for telemetry."""
        return {
            "tool_calls": _ratio(self.tool_calls, self.max_tool_calls),
            "model_calls": _ratio(self.model_calls, self.max_model_calls),
            "input_tokens": _ratio(self.input_tokens, self.max_input_tokens),
        }


def _ratio(used: int, allowed: int) -> float:
    return round(used / allowed, 3) if allowed > 0 else 0.0


class BudgetEnforcementHook(HookProvider):
    """
    Enforces a resource budget on a Strands agent via lifecycle hooks.

    Usage:
        budget = BudgetState(max_tool_calls=15, max_model_calls=10,
                             max_input_tokens=200_000)
        hook = BudgetEnforcementHook(budget)
        agent = Agent(model=..., tools=[...], hooks=[hook])
        result = agent("...")
        if budget.stopped_early:
            ...  # partial result; see budget.exhausted
    """

    def __init__(self, budget: BudgetState, verbose: bool = True):
        self.budget = budget
        self.verbose = verbose

    def register_hooks(self, registry: HookRegistry, **_kwargs) -> None:
        registry.add_callback(BeforeToolCallEvent, self.on_before_tool)
        registry.add_callback(AfterToolCallEvent, self.on_after_tool)
        registry.add_callback(BeforeModelCallEvent, self.on_before_model)
        registry.add_callback(AfterInvocationEvent, self.on_after_invocation)

    # -- tool-call budget ---------------------------------------------------

    def on_before_tool(self, event: BeforeToolCallEvent) -> None:
        """Block the tool call if it would exceed the tool-call budget."""
        b = self.budget
        if b.tool_calls >= b.max_tool_calls:
            b.exhausted = "tool_calls"
            # Setting cancel_tool turns this into an error-status tool result,
            # so the model sees why it was stopped and can wrap up rather than
            # retrying blindly.
            event.cancel_tool = (
                f"Tool-call budget exhausted ({b.tool_calls}/{b.max_tool_calls}). "
                "Do not call further tools. Summarize what you have established "
                "so far and state explicitly what remains unfinished."
            )
            self._log(f"DENY tool (budget {b.tool_calls}/{b.max_tool_calls})")
            return

        b.tool_calls += 1
        name = getattr(event.tool_use, "name", None) or str(event.tool_use)
        b.tools_invoked.append(name)

    def on_after_tool(self, event: AfterToolCallEvent) -> None:
        """Observe completion. Counting happens before the call, not after."""
        if self.verbose and getattr(event, "exception", None):
            self._log(f"tool raised: {event.exception}")

    # -- model-call and token budgets ---------------------------------------

    def on_before_model(self, event: BeforeModelCallEvent) -> None:
        """
        Block the model call if the model-call or token budget is spent.

        Checked before incrementing so a call is never allowed to overshoot,
        which was the off-by-one in the original loop.
        """
        b = self.budget

        if b.model_calls >= b.max_model_calls:
            b.exhausted = "model_calls"
            event.cancel = (
                f"Reasoning-step budget exhausted "
                f"({b.model_calls}/{b.max_model_calls})."
            )
            self._log(f"DENY model call (budget {b.model_calls}/{b.max_model_calls})")
            return

        # projected_input_tokens is None when estimation fails; treat as 0 and
        # rely on the model-call cap in that case rather than guessing.
        projected = getattr(event, "projected_input_tokens", None) or 0
        if projected and b.input_tokens + projected > b.max_input_tokens:
            b.exhausted = "input_tokens"
            event.cancel = (
                f"Token budget exhausted (would reach "
                f"{b.input_tokens + projected}/{b.max_input_tokens} input tokens)."
            )
            self._log(
                f"DENY model call (tokens {b.input_tokens}+{projected}"
                f" > {b.max_input_tokens})"
            )
            return

        b.model_calls += 1
        b.input_tokens += projected

    # -- telemetry ----------------------------------------------------------

    def on_after_invocation(self, _event: AfterInvocationEvent) -> None:
        b = self.budget
        self._log(
            f"done: {b.model_calls} model calls, {b.tool_calls} tool calls, "
            f"~{b.input_tokens} input tokens, "
            f"exhausted={b.exhausted or 'no'}"
        )

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[budget] {message}")
