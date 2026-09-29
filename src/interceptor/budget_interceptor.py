"""
Gateway Budget Interceptor (AWS Lambda)

Enforces per-session VOLUME ceilings that Cedar structurally cannot: running
totals of tool calls. Registered on the AgentCore Gateway via
`interceptorConfigurations` with interceptionPoints=["REQUEST"], so it runs on
every request through the gateway before the target is reached — outside the
agent's process and therefore not bypassable by prompt injection or a
compromised agent loop.

Division of labour:
  - Cedar          -> which tools may be called (per-call authority)
  - This function  -> how many times, in aggregate (cross-call volume)
  - Strands hooks  -> fail fast in-process so the agent gets a clean signal

The counter is a conditional-update DynamoDB item keyed by session, which makes
the increment atomic under concurrent tool calls. The ceiling is read from the
verified `task_tier` claim on the caller's access token, never from the request
body — a caller cannot raise its own ceiling by editing a tool argument or an
extra header, because those are not where the ceiling comes from.

WIRE FORMAT: a REQUEST interceptor is invoked for every request the gateway
routes to an MCP target, not only `tools/call` — `tools/list`, `initialize`,
and similar methods pass through this handler too, and must not be blocked or
counted. The input/output shapes below follow the documented MCP interceptor
contract (interceptorInputVersion/interceptorOutputVersion, `mcp.gatewayRequest`,
`mcp.transformedGatewayRequest` / `mcp.transformedGatewayResponse`). AWS has
changed interceptor payload shapes before, so `_extract_context`, `_allow`, and
`_deny` isolate that shape in one place; confirm it against the interceptor
reference for your API version before deploying, and run in LOG_ONLY first.

The verified access token — including `task_tier` and `session_id` — arrives as
a JWT in the `Authorization` header (present only when the gateway's interceptor
configuration sets `passRequestHeaders: true`; see `src/policy_setup.py`). The
gateway has already authenticated that token before invoking this function, so
`_decode_claims` only *decodes* the claims; it deliberately does not re-verify
the signature, since re-verifying a token the gateway already verified would
duplicate that trust boundary rather than add one.

Table (on-demand billing):
    PK: session_id (S)
    Attributes: tool_calls (N), ttl (N)
"""

import base64
import json
import os
import time

import boto3
from botocore.exceptions import ClientError

_TABLE_NAME = os.environ.get("BUDGET_TABLE", "agent-session-budgets")
_TTL_SECONDS = int(os.environ.get("BUDGET_TTL_SECONDS", "86400"))

# Hard ceilings per tier. These are the outer bound on volume; the adaptive
# engine may compute something lower, never higher.
TIER_CEILINGS = {
    "SIMPLE": {"max_tool_calls": 5},
    "MEDIUM": {"max_tool_calls": 15},
    "COMPLEX": {"max_tool_calls": 40},
}
# Unknown or absent tier gets the most restrictive ceiling.
FALLBACK_TIER = "SIMPLE"

_dynamodb = boto3.resource("dynamodb")
_table = _dynamodb.Table(_TABLE_NAME)


def _decode_claims(authorization_header: str) -> dict:
    """
    Decode (without re-verifying) the JWT claims on the gateway's access token.

    The gateway authenticates the caller before invoking this interceptor, so
    the signature has already been checked upstream. This function reads the
    payload segment only; it must never be used somewhere that boundary does
    not hold.
    """
    if not authorization_header:
        return {}
    token = authorization_header.rsplit(" ", 1)[-1]  # strips a "Bearer " prefix
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1]
    padded = payload + "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, TypeError):
        return {}


def _extract_context(event: dict) -> dict:
    """
    Pull the request id, tool name, session id, and tier out of the
    interceptor event.

    Isolated so the wire-format details live in exactly one place. `tier` and
    `session_id` come from the verified token claims, NOT from the request
    body or any other header a caller could set.
    """
    request = (event.get("mcp") or {}).get("gatewayRequest") or {}
    headers = request.get("headers") or {}
    body = request.get("body") or {}
    params = body.get("params") or {}

    claims = _decode_claims(headers.get("Authorization", ""))

    return {
        "request_id": body.get("id"),
        "tool_name": params.get("name", "<unknown>"),
        "session_id": claims.get("session_id"),
        "tier": str(claims.get("task_tier", "") or "").upper(),
    }


def _allow(request_body: dict) -> dict:
    """Let the request proceed to the target unmodified."""
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {"transformedGatewayRequest": {"body": request_body}},
    }


def _deny(request_id, reason: str, detail: dict) -> dict:
    """
    Short-circuit the call: returning `transformedGatewayResponse` makes the
    gateway respond with this content immediately instead of invoking the
    target. The body is a JSON-RPC error matching the request's id, so callers
    see the same error shape they already handle for tool failures.

    The message is deliberately actionable: the agent should stop retrying and
    either finish with partial results or request escalation.
    """
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayResponse": {
                "statusCode": 429,
                "body": {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32000,
                        "message": reason,
                        "data": {**detail, "retryable": False},
                    },
                },
            }
        },
    }


def handler(event, _context):
    """
    Increment the session's tool-call counter and deny past the tier ceiling.

    Only `tools/call` requests are budgeted. Every other MCP method
    (`tools/list`, `initialize`, ...) passes through untouched, since those
    are not the resource this interceptor governs and blocking them would
    break the protocol handshake rather than enforce a budget.

    Fails CLOSED for tool calls: if the session cannot be identified or the
    counter cannot be written, the call is denied. An unenforceable budget is
    not a budget.
    """
    request = (event.get("mcp") or {}).get("gatewayRequest") or {}
    body = request.get("body") or {}

    if body.get("method") != "tools/call":
        return _allow(body)

    ctx = _extract_context(event)
    session_id = ctx["session_id"]
    request_id = ctx["request_id"]

    if not session_id:
        return _deny(
            request_id,
            "No session identifier on request; cannot enforce budget.",
            {"tool": ctx["tool_name"]},
        )

    tier = ctx["tier"] if ctx["tier"] in TIER_CEILINGS else FALLBACK_TIER
    ceiling = TIER_CEILINGS[tier]["max_tool_calls"]

    try:
        # Atomic increment, conditional on staying at or below the ceiling.
        # if_not_exists seeds the counter on the session's first tool call.
        result = _table.update_item(
            Key={"session_id": session_id},
            UpdateExpression=(
                "SET tool_calls = if_not_exists(tool_calls, :zero) + :one, "
                "#ttl = if_not_exists(#ttl, :ttl), "
                "tier = if_not_exists(tier, :tier)"
            ),
            ConditionExpression="attribute_not_exists(tool_calls) OR tool_calls < :ceiling",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={
                ":zero": 0,
                ":one": 1,
                ":ceiling": ceiling,
                ":ttl": int(time.time()) + _TTL_SECONDS,
                ":tier": tier,
            },
            ReturnValues="UPDATED_NEW",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Ceiling reached. This is the runaway-loop stop.
            return _deny(
                request_id,
                f"Session exhausted its {tier} tool-call budget of {ceiling}. "
                f"Return partial results or request tier escalation; do not retry.",
                {
                    "session_id": session_id,
                    "tier": tier,
                    "ceiling": ceiling,
                    "tool": ctx["tool_name"],
                },
            )
        # Any other DynamoDB failure: fail closed.
        return _deny(
            request_id,
            f"Budget counter unavailable ({exc.response['Error']['Code']}); "
            f"denying to stay within policy.",
            {"session_id": session_id, "tool": ctx["tool_name"]},
        )

    used = int(result["Attributes"]["tool_calls"])
    print(
        f"[budget] session={session_id} tier={tier} "
        f"tool={ctx['tool_name']} used={used}/{ceiling}"
    )
    return _allow(body)
