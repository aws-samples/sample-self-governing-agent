"""
Policy and gateway setup.

Creates the policy engine, attaches Cedar policies, and wires both the policy
engine and the budget interceptor to the gateway.

TWO-PHASE DEPLOY IS MANDATORY. Cedar rejects wildcard resources, so every policy
must name the gateway ARN literally — and that ARN does not exist until the
gateway is created. So:

    phase 1  create gateway (no policy engine)      -> gateway ARN
    phase 2  substitute ARN into Cedar, create      -> policy engine ARN
             engine + policies, attach to gateway

START IN LOG_ONLY. `mode="LOG_ONLY"` evaluates every policy and logs the
decision without blocking, which is how you find out that a claim your IdP
silently dropped is turning every tag-gated permit into a default-deny. Flip to
ENFORCE once CloudWatch shows the decisions you expect.

Run: python -m src.policy_setup --gateway-arn <arn> [--enforce]
"""

import argparse
import pathlib
import time

import boto3

_POLICY_FILE = pathlib.Path(__file__).resolve().parent.parent / "policies" / "governance_authority.cedar"

control = boto3.client("bedrock-agentcore-control")


def create_policy_engine(name: str = "self-governance-policy-engine") -> dict:
    """Create the policy engine that will hold the Cedar policies."""
    response = control.create_policy_engine(
        name=name,
        description="Tier-bound tool authority for the self-governing agent",
    )
    print(f"policy engine: {response['policyEngineId']} ({response['status']})")
    return response


def create_authority_policy(policy_engine_id: str, gateway_arn: str) -> dict:
    """
    Load the Cedar file, substitute the real gateway ARN, and create the policy.

    validationMode is set explicitly to FAIL_ON_ANY_FINDINGS (the alternative is
    IGNORE_ALL_FINDINGS), which validates the policy against the schema the
    gateway generates from its own tool definitions. A failure here means the
    policy references a tool, action, or context field the gateway does not
    expose — fix the policy rather than suppressing the finding, because a
    misspelled action name that passes validation becomes a permit that never
    matches, and therefore a silent default-deny at runtime.

    Note the two independent mode controls, which are easy to confuse:
      - enforcementMode, here, is a property of the POLICY (ACTIVE | LOG_ONLY).
      - policyEngineConfiguration.mode, in attach_to_gateway, is a property of
        the GATEWAY's attachment (LOG_ONLY | ENFORCE).
    The staged rollout below is done at the gateway, so the policy stays ACTIVE
    and only the attachment changes.
    """
    statement = _POLICY_FILE.read_text().replace("<GATEWAY_ARN>", gateway_arn)

    response = control.create_policy(
        name="tier-bound-tool-authority",
        policyEngineId=policy_engine_id,
        definition={"cedar": {"statement": statement}},
        description="Grants tool authority per complexity tier from session claims",
        validationMode="FAIL_ON_ANY_FINDINGS",
        enforcementMode="ACTIVE",
    )
    print(f"policy: {response['policyId']} ({response['status']})")
    return response


def attach_to_gateway(
    gateway_id: str,
    policy_engine_arn: str,
    interceptor_lambda_arn: str,
    mode: str = "LOG_ONLY",
) -> dict:
    """
    Attach the policy engine and budget interceptor to the gateway.

    Both are set on the gateway itself, so they apply to every tool call
    regardless of which agent or framework made it. update_gateway requires the
    full desired state, so existing values must be read and re-sent — omitting a
    field clears it.
    """
    current = control.get_gateway(gatewayIdentifier=gateway_id)

    params = {
        "gatewayIdentifier": gateway_id,
        "name": current["name"],
        "roleArn": current["roleArn"],
        "authorizerType": current["authorizerType"],
        "policyEngineConfiguration": {"arn": policy_engine_arn, "mode": mode},
        "interceptorConfigurations": [
            {
                "interceptor": {"lambda": {"arn": interceptor_lambda_arn}},
                # REQUEST: run before the tool executes, so the call can be
                # blocked rather than merely recorded.
                "interceptionPoints": ["REQUEST"],
                "inputConfiguration": {"passRequestHeaders": True},
            }
        ],
    }
    for optional in ("protocolType", "protocolConfiguration", "authorizerConfiguration",
                     "description", "kmsKeyArn", "exceptionLevel"):
        if optional in current:
            params[optional] = current[optional]

    response = control.update_gateway(**params)
    print(f"gateway {gateway_id}: policy engine attached in {mode} mode")
    return response


TERMINAL_FAILED = ("CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED")


def wait_for_engine(policy_engine_id: str, timeout: int = 120) -> str:
    """
    Poll until the engine reaches ACTIVE.

    A failed engine is raised rather than returned. Treating CREATE_FAILED as
    "no longer creating, therefore ready" would attach a broken engine to the
    gateway, and in ENFORCE mode that denies every tool call.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        engine = control.get_policy_engine(policyEngineId=policy_engine_id)
        status = engine["status"]
        if status in TERMINAL_FAILED:
            raise RuntimeError(
                f"policy engine {policy_engine_id} is {status}: "
                f"{engine.get('statusReasons')}"
            )
        if status not in ("CREATING", "UPDATING"):
            return status
        time.sleep(5)
    raise TimeoutError(f"policy engine {policy_engine_id} not ready in {timeout}s")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-id", required=True, help="Gateway identifier")
    parser.add_argument("--gateway-arn", required=True,
                        help="Gateway ARN, from `agentcore status` (phase 1 output)")
    parser.add_argument("--interceptor-arn", required=True,
                        help="ARN of the deployed budget interceptor Lambda")
    parser.add_argument("--enforce", action="store_true",
                        help="Attach in ENFORCE mode. Omit to start in LOG_ONLY.")
    parser.add_argument("--policy-engine-arn",
                        help="Reattach this existing engine instead of creating "
                             "one. Required with --enforce, so promoting from "
                             "LOG_ONLY changes the mode rather than creating a "
                             "second engine and a duplicate policy.")
    args = parser.parse_args()

    if args.enforce and not args.policy_engine_arn:
        parser.error(
            "--enforce requires --policy-engine-arn (the engine printed by the "
            "LOG_ONLY run). Without it this would create a second policy engine "
            "and leave the first one attached to nothing."
        )

    if args.policy_engine_arn:
        engine_arn = args.policy_engine_arn
    else:
        engine = create_policy_engine()
        wait_for_engine(engine["policyEngineId"])
        create_authority_policy(engine["policyEngineId"], args.gateway_arn)
        engine_arn = engine["policyEngineArn"]

    attach_to_gateway(
        gateway_id=args.gateway_id,
        policy_engine_arn=engine_arn,
        interceptor_lambda_arn=args.interceptor_arn,
        mode="ENFORCE" if args.enforce else "LOG_ONLY",
    )

    if not args.enforce:
        print(
            f"\nAttached in LOG_ONLY. Verify in CloudWatch that decisions match "
            f"expectations and that task_tier appears in the token claims, then "
            f"promote with:\n\n"
            f"  python -m src.policy_setup --gateway-id {args.gateway_id} "
            f"--gateway-arn {args.gateway_arn} "
            f"--interceptor-arn {args.interceptor_arn} \\\n"
            f"      --policy-engine-arn {engine_arn} --enforce\n"
        )


if __name__ == "__main__":
    main()
