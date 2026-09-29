"""
Feedback Writer

Records execution telemetry to DynamoDB and emits CloudWatch metrics.

WHY NOT AGENTCORE MEMORY: Memory is built for agent recall — it extracts facts
from conversational events according to configured memoryStrategies and serves
them back by semantic search. Execution telemetry is append-only time-series
data queried by exact tier and time range. DynamoDB does that with a predictable
key schema; semantic retrieval over JSON blobs does not. (Memory remains the
right tool for what the *agent* should remember across sessions — user
preferences, prior findings — just not for governance counters.)

THE CRITICAL FIELD: `exhausted` records WHICH budget truncated the run, or null
if it finished on its own. Without it the adaptive engine cannot tell "used 15
tool calls because that was enough" from "used 15 because that was the cap," and
the feedback loop degenerates. See adaptive_limit_engine.km_quantile.
"""

import os
from datetime import datetime, timezone

import boto3

_TABLE_NAME = os.environ.get("PROFILE_TABLE", "agent-execution-profiles")
_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "SelfGoverningAgent/Governance")
_RETENTION_DAYS = int(os.environ.get("PROFILE_RETENTION_DAYS", "90"))

_table = None


def _profile_table():
    """Lazily resolve the table so importing this module needs no AWS call."""
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(_TABLE_NAME)
    return _table


_cloudwatch = boto3.client("cloudwatch")


def record_execution_profile(
    tier: str,
    budget: dict,
    usage: dict,
    exhausted: str | None,
    duration_seconds: float,
    session_id: str,
    exploring: bool = False,
    degraded_classification: bool = False,
) -> dict:
    """
    Write one execution profile.

    Args:
        tier: Complexity tier assigned.
        budget: Allocated budget (max_tool_calls, max_model_calls, max_input_tokens).
        usage: Actual usage with the same keys minus the max_ prefix.
        exhausted: Which budget ran out ("tool_calls" | "model_calls" |
            "input_tokens"), or None if the task completed within budget.
            This is the censoring indicator.
        duration_seconds: Wall-clock duration.
        session_id: Session identifier.
        exploring: Whether this invocation used an exploration budget.
        degraded_classification: Whether classification fell back to a default.
    """
    now = datetime.now(timezone.utc)
    item = {
        "tier": tier,
        "ts": now.isoformat(),
        "session_id": session_id,
        "tool_calls": int(usage.get("tool_calls", 0)),
        "model_calls": int(usage.get("model_calls", 0)),
        "input_tokens": int(usage.get("input_tokens", 0)),
        "budget_tool_calls": int(budget.get("max_tool_calls", 0)),
        "budget_model_calls": int(budget.get("max_model_calls", 0)),
        "budget_input_tokens": int(budget.get("max_input_tokens", 0)),
        # Stored as integer milliseconds: DynamoDB's Number type round-trips
        # through Decimal, and float(Decimal) conversions on read are an easy
        # place to introduce drift. The field name says ms so nobody has to guess.
        "duration_ms": int(duration_seconds * 1000),
        "completed": exhausted is None,
        "exploring": exploring,
        "degraded_classification": degraded_classification,
        "ttl": int(now.timestamp()) + _RETENTION_DAYS * 86_400,
    }
    # Store `exhausted` only when set; absent means "not censored".
    if exhausted:
        item["exhausted"] = exhausted

    _profile_table().put_item(Item=item)
    return item


def emit_governance_metrics(
    tier: str,
    budget: dict,
    usage: dict,
    exhausted: str | None,
    duration_seconds: float,
    governance_overhead_seconds: float = 0.0,
) -> None:
    """
    Emit governance metrics to CloudWatch.

    Includes GovernanceOverheadSeconds because this architecture spends an LLM
    call plus a history query in order to save resources. If that overhead is not
    measured, the system cannot be shown to be net-positive.
    """
    dimensions = [{"Name": "Tier", "Value": tier}]
    timestamp = datetime.now(timezone.utc)

    def metric(name, value, unit="Count"):
        return {
            "MetricName": name,
            "Value": float(value),
            "Unit": unit,
            "Dimensions": dimensions,
            "Timestamp": timestamp,
        }

    def utilization(used_key, budget_key):
        allowed = budget.get(budget_key, 0)
        return (usage.get(used_key, 0) / allowed * 100) if allowed else 0.0

    metrics = [
        metric("ToolCallUtilization", utilization("tool_calls", "max_tool_calls"), "Percent"),
        metric("ModelCallUtilization", utilization("model_calls", "max_model_calls"), "Percent"),
        metric("TokenUtilization", utilization("input_tokens", "max_input_tokens"), "Percent"),
        metric("BudgetExhausted", 1 if exhausted else 0),
        metric("TaskCompleted", 0 if exhausted else 1),
        metric("InputTokensConsumed", usage.get("input_tokens", 0)),
        metric("InvocationDuration", duration_seconds, "Seconds"),
        metric("GovernanceOverheadSeconds", governance_overhead_seconds, "Seconds"),
    ]
    if exhausted:
        metrics.append(
            {
                "MetricName": "BudgetExhaustedByResource",
                "Value": 1.0,
                "Unit": "Count",
                "Dimensions": dimensions + [{"Name": "Resource", "Value": exhausted}],
                "Timestamp": timestamp,
            }
        )

    _cloudwatch.put_metric_data(Namespace=_NAMESPACE, MetricData=metrics)
