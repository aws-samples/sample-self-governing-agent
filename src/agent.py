"""
Self-Governing Agent — orchestrator.

Composes the four governance layers. Each does the one job it is actually
capable of, which is the core correction to the original design:

  1. CLASSIFY   (this module)      what kind of task is this?
  2. AUTHORITY  (Cedar, at gateway) which tools may this session reach?
  3. VOLUME     (interceptor)       how many calls, in aggregate?
  4. FAIL FAST  (Strands hooks)     stop cleanly in-process, with a usable signal

Layers 2 and 3 run outside the agent process and are the security boundary.
Layer 4 runs inside it and is an efficiency and UX measure, not a guarantee: an
in-process check cannot defend against a compromised agent loop. The original
design collapsed 2, 3, and 4 into a single in-process dict and called it a hard
ceiling; that is the claim this structure makes true.

Tier reaches Cedar as an identity claim on the session credential, not as a
value the agent passes to the policy engine. The agent cannot widen its own
authority, because it never asserts its own tier.
"""

import time
import uuid

from src.adaptive_limit_engine import compute_adaptive_limits, should_explore
from src.complexity_classifier import classify_with_fallback
from src.feedback_writer import emit_governance_metrics, record_execution_profile
from src.governed_agent import BudgetEnforcementHook, BudgetState


class SelfGoverningAgent:
    """
    Wraps a Strands agent factory with per-invocation governance.

    Args:
        agent_factory: Callable taking (hooks, session_token) and returning a
            configured Strands Agent. A factory rather than an instance because
            the tools are reached through a gateway whose credential encodes the
            tier, which is not known until the request is classified.
        session_counter: Monotonic counter driving the exploration schedule.
    """

    def __init__(self, agent_factory, session_counter: int = 0):
        self.agent_factory = agent_factory
        self.session_counter = session_counter

    def invoke(self, user_request: str, telemetry: bool = True) -> dict:
        session_id = str(uuid.uuid4())
        start = time.time()

        # -- Layer 1: classify -------------------------------------------------
        governance_start = time.time()
        classification = classify_with_fallback(user_request)
        tier = classification["tier"]
        degraded = classification.get("degraded", False)

        # -- Layer 3 input: resolve the volume budget --------------------------
        self.session_counter += 1
        explore = should_explore(self.session_counter)
        budget_spec = compute_adaptive_limits(tier, explore=explore)
        governance_overhead = time.time() - governance_start

        budget = BudgetState(
            max_tool_calls=budget_spec["max_tool_calls"],
            max_model_calls=budget_spec["max_model_calls"],
            max_input_tokens=budget_spec["max_input_tokens"],
        )

        print(
            f"[governance] tier={tier} "
            f"tools<={budget.max_tool_calls} models<={budget.max_model_calls} "
            f"tokens<={budget.max_input_tokens} "
            f"explore={explore} degraded={degraded} "
            f"overhead={governance_overhead:.2f}s"
        )

        # -- Layer 2: mint a credential carrying the tier ----------------------
        # The gateway reads task_tier from this token's claims; Cedar then gates
        # tool authority on it. See session_token.py.
        session_token = self._mint_token(tier, session_id, degraded)

        # -- Layer 4: run under in-process hooks -------------------------------
        hook = BudgetEnforcementHook(budget)
        agent = self.agent_factory(hooks=[hook], session_token=session_token)

        output, error = "", None
        try:
            result = agent(user_request)
            output = str(result)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"

        duration = time.time() - start
        usage = {
            "tool_calls": budget.tool_calls,
            "model_calls": budget.model_calls,
            "input_tokens": budget.input_tokens,
        }

        if telemetry:
            self._record(
                tier, budget_spec, usage, budget.exhausted, duration,
                session_id, explore, degraded, governance_overhead,
            )

        return {
            "output": output,
            "error": error,
            # Truncated by a budget is NOT success, and is recorded as censored
            # so the adaptive engine can use it as evidence of unmet demand.
            "completed": budget.exhausted is None and error is None,
            "truncated_by": budget.exhausted,
            "tier": tier,
            "degraded_classification": degraded,
            "usage": usage,
            "budget": {
                k: v for k, v in budget_spec.items() if k != "governance"
            },
            "utilization": budget.utilization(),
            "tools_invoked": budget.tools_invoked,
            "duration_seconds": round(duration, 2),
            "governance_overhead_seconds": round(governance_overhead, 2),
            "session_id": session_id,
        }

    def _mint_token(self, tier: str, session_id: str, degraded: bool):
        """
        Obtain a session credential whose claims carry the tier.

        Overridden in tests. In production this calls your IdP / AgentCore
        Identity to issue a short-lived token with task_tier (and
        classification_degraded) as claims. See src/session_token.py.
        """
        from src.session_token import mint_session_token

        return mint_session_token(
            tier=tier, session_id=session_id, degraded=degraded
        )

    @staticmethod
    def _record(tier, budget_spec, usage, exhausted, duration,
                session_id, explore, degraded, overhead):
        """Persist telemetry, never letting a telemetry failure break the run."""
        try:
            record_execution_profile(
                tier=tier,
                budget=budget_spec,
                usage=usage,
                exhausted=exhausted,
                duration_seconds=duration,
                session_id=session_id,
                exploring=explore,
                degraded_classification=degraded,
            )
            emit_governance_metrics(
                tier=tier,
                budget=budget_spec,
                usage=usage,
                exhausted=exhausted,
                duration_seconds=duration,
                governance_overhead_seconds=overhead,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[governance] telemetry write failed: {exc}")
