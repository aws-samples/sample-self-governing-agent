"""
Adaptive Limit Engine

Computes per-tier resource budgets from historical telemetry.

TWO CORRECTIONS FROM THE NAIVE VERSION
--------------------------------------
1. Storage. Telemetry is time-series data, so it lives in DynamoDB, not
   AgentCore Memory. Memory extracts conversational facts for agent recall via
   configured memoryStrategies; writing JSON blobs as conversational events and
   expecting semantic search to return them as parseable records does not work.

2. Censoring. This is the substantive fix. The naive formula is
       budget = P90(historical usage) * 1.2
   but historical usage is bounded by the budget that produced it, so usage
   above the cap is never observed. Runs truncated by the cap are recorded as
   failures and then excluded by a "successful only" filter — so the surviving
   sample is exactly the population that fit inside the current budget. P90
   drifts down, more runs get truncated, and the budget collapses. There is no
   upward pressure anywhere in that loop.

   The fix has three parts:
     a. Record DEMAND (what the run wanted), not just what it was allowed, and
        flag right-censored runs explicitly.
     b. Estimate over censored data with a Kaplan-Meier survival curve, which
        uses truncated runs as evidence of "needed at least this much" instead
        of discarding them.
     c. Explore: a small fraction of invocations get a raised cap, so the system
        keeps observing the tail it would otherwise never see. Floor the budget
        so it cannot collapse regardless.

Computation runs in-process. The original spun up a Code Interpreter session per
invocation to compute one percentile — seconds of latency and a sandbox charge
on the critical path of a system whose selling point is efficiency. Worse, it
interpolated stored strings into a Python literal, which is arbitrary code
execution if any record contains a triple quote. Code Interpreter is the right
tool for untrusted or model-authored code; a percentile over your own telemetry
is neither.
"""

import os
from dataclasses import dataclass

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

_TABLE_NAME = os.environ.get("PROFILE_TABLE", "agent-execution-profiles")

# Cold-start budgets, used until MIN_SAMPLES observations exist for a tier.
COLD_START = {
    "SIMPLE": {"max_tool_calls": 3, "max_model_calls": 3, "max_input_tokens": 30_000},
    "MEDIUM": {"max_tool_calls": 10, "max_model_calls": 8, "max_input_tokens": 120_000},
    "COMPLEX": {"max_tool_calls": 25, "max_model_calls": 20, "max_input_tokens": 400_000},
}

# Hard ceilings. Must match TIER_CEILINGS in the interceptor — the interceptor is
# authoritative; this copy only avoids proposing budgets that would be rejected.
CEILINGS = {
    "SIMPLE": {"max_tool_calls": 5, "max_model_calls": 5, "max_input_tokens": 60_000},
    "MEDIUM": {"max_tool_calls": 15, "max_model_calls": 12, "max_input_tokens": 200_000},
    "COMPLEX": {"max_tool_calls": 40, "max_model_calls": 30, "max_input_tokens": 600_000},
}

# Floors: the budget may never adapt below these, which is what stops the
# downward ratchet even if the estimator is fed bad data.
FLOORS = {
    "SIMPLE": {"max_tool_calls": 2, "max_model_calls": 2, "max_input_tokens": 10_000},
    "MEDIUM": {"max_tool_calls": 5, "max_model_calls": 4, "max_input_tokens": 40_000},
    "COMPLEX": {"max_tool_calls": 12, "max_model_calls": 8, "max_input_tokens": 100_000},
}

MIN_SAMPLES = 30          # below this, cold-start defaults win
TARGET_QUANTILE = 0.90    # budget to cover this share of demand
HEADROOM = 1.20
EXPLORATION_RATE = 0.05   # 5% of invocations probe above the current budget
EXPLORATION_FACTOR = 1.5

_table = None


def _profile_table():
    """Lazily resolve the table so importing this module needs no AWS call."""
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(_TABLE_NAME)
    return _table


@dataclass
class Observation:
    """
    One historical run's demand for a single resource.

    value:    units consumed
    censored: True if the run was cut off by the budget, meaning true demand was
              >= value rather than == value.
    """

    value: float
    censored: bool


def km_quantile(observations: list[Observation], q: float) -> float:
    """
    Estimate the q-th quantile of demand from right-censored observations
    using the Kaplan-Meier estimator.

    A censored run at value v contributes "demand exceeded v" without claiming
    demand equalled v. That is the information the naive percentile throws away.

    Returns the smallest observed value where the survival function S(t) — the
    estimated fraction of runs demanding more than t — drops to or below 1-q.
    """
    if not observations:
        return 0.0

    ordered = sorted(observations, key=lambda o: o.value)
    n_at_risk = len(ordered)
    survival = 1.0
    threshold = 1.0 - q

    # Group by distinct value so ties are handled in one step.
    i = 0
    while i < len(ordered):
        v = ordered[i].value
        events = 0   # uncensored runs completing exactly at v
        total = 0    # all runs leaving the risk set at v
        while i < len(ordered) and ordered[i].value == v:
            total += 1
            if not ordered[i].censored:
                events += 1
            i += 1

        if events and n_at_risk > 0:
            survival *= 1.0 - events / n_at_risk
            if survival <= threshold:
                return float(v)

        n_at_risk -= total

    # Survival never fell to the threshold: demand is concentrated above
    # everything observed, so return the largest value seen. Combined with
    # HEADROOM and the exploration path, this pushes the budget UP over time —
    # the upward pressure the naive loop lacked.
    return float(ordered[-1].value)


def load_observations(tier: str, limit: int = 500) -> dict[str, list[Observation]]:
    """
    Load recent telemetry for a tier from DynamoDB.

    Table layout:
        PK tier (S), SK ts (S)  -- newest last
        attrs: tool_calls, model_calls, input_tokens (N),
               exhausted (S|null) identifying which budget truncated the run
    """
    try:
        response = _profile_table().query(
            KeyConditionExpression=Key("tier").eq(tier),
            ScanIndexForward=False,   # newest first
            Limit=limit,
        )
    except ClientError as exc:
        # No history available (table missing, throttled, denied) is not fatal:
        # returning nothing makes compute_adaptive_limits fall back to
        # cold-start defaults. Budgets must degrade safely, not take the
        # invocation down — and the defaults are conservative, so degrading
        # cannot widen a budget.
        print(
            f"[governance] profile history unavailable "
            f"({exc.response['Error']['Code']}); using cold-start defaults"
        )
        return {
            "max_tool_calls": [],
            "max_model_calls": [],
            "max_input_tokens": [],
        }

    series: dict[str, list[Observation]] = {
        "max_tool_calls": [],
        "max_model_calls": [],
        "max_input_tokens": [],
    }
    field_map = {
        "max_tool_calls": ("tool_calls", "tool_calls"),
        "max_model_calls": ("model_calls", "model_calls"),
        "max_input_tokens": ("input_tokens", "input_tokens"),
    }

    for item in response.get("Items", []):
        exhausted = item.get("exhausted")
        for budget_key, (attr, censor_reason) in field_map.items():
            if attr not in item:
                continue
            series[budget_key].append(
                Observation(
                    value=float(item[attr]),
                    # Censored only for the specific resource that ran out.
                    censored=(exhausted == censor_reason),
                )
            )
    return series


def compute_adaptive_limits(
    tier: str,
    observations: dict[str, list[Observation]] | None = None,
    explore: bool = False,
) -> dict:
    """
    Compute the budget for one invocation of `tier`.

    Args:
        tier: SIMPLE | MEDIUM | COMPLEX.
        observations: Pre-loaded history; loaded from DynamoDB if omitted.
        explore: If True, raise the budget by EXPLORATION_FACTOR to sample the
            tail. Callers should set this on ~EXPLORATION_RATE of invocations.

    Returns:
        dict of budget values plus a `governance` block explaining the decision.
    """
    tier = tier if tier in COLD_START else "MEDIUM"
    ceilings, floors = CEILINGS[tier], FLOORS[tier]

    if observations is None:
        observations = load_observations(tier)

    budget: dict = {}
    detail: dict = {}

    for key, cold_value in COLD_START[tier].items():
        series = observations.get(key, [])

        if len(series) < MIN_SAMPLES:
            proposed = cold_value
            basis = f"cold_start (n={len(series)}<{MIN_SAMPLES})"
        else:
            estimate = km_quantile(series, TARGET_QUANTILE)
            proposed = estimate * HEADROOM
            basis = (
                f"km_q{int(TARGET_QUANTILE * 100)}={estimate:.0f}"
                f"*{HEADROOM} (n={len(series)}, "
                f"censored={sum(o.censored for o in series)})"
            )

        if explore:
            proposed *= EXPLORATION_FACTOR
            basis += f" *explore{EXPLORATION_FACTOR}"

        # Clamp: floor stops collapse, ceiling stops runaway.
        final = int(max(floors[key], min(round(proposed), ceilings[key])))
        budget[key] = final
        detail[key] = {
            "value": final,
            "basis": basis,
            "floor": floors[key],
            "ceiling": ceilings[key],
            "clamped": final != int(round(proposed)),
        }

    budget["governance"] = {
        "tier": tier,
        "exploring": explore,
        "per_resource": detail,
    }
    return budget


def should_explore(counter: int, rate: float = EXPLORATION_RATE) -> bool:
    """
    Deterministic exploration schedule: every Nth invocation probes upward.

    Deterministic rather than random so behaviour is reproducible in tests and
    the exploration rate is exact rather than approximate.
    """
    if rate <= 0:
        return False
    interval = max(1, round(1 / rate))
    return counter % interval == 0
