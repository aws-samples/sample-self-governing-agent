"""
Teardown for the governance layers attached to a gateway.

Exists as a script rather than as CLI commands in a README because deletion here
is order-dependent and the ordering is not obvious:

  1. DETACH the policy engine and interceptor from the gateway. update_gateway
     takes the full desired state, so "detach" means re-sending the gateway
     without policyEngineConfiguration or interceptorConfigurations. There is no
     dedicated detach call, and deleting an engine that a gateway still
     references fails.
  2. DELETE each policy. delete_policy needs BOTH policyEngineId and policyId —
     a policy ID alone is not unique across engines.
  3. DELETE the engine, which is only possible once it holds no policies and no
     gateway points at it.

Order matters in the other direction too: if you delete the gateway first (via
`agentcore destroy`), step 1 becomes unnecessary, but the engine and policies are
NOT removed with it and will keep accruing nothing visible in the console while
still counting against service quotas. This script covers the case where the
gateway outlives the governance configuration.

Run:
    python -m src.policy_teardown --gateway-id <id> --policy-engine-id <id>
    python -m src.policy_teardown --gateway-id <id> --policy-engine-id <id> --dry-run
"""

import argparse

import boto3

control = boto3.client("bedrock-agentcore-control")

# Fields update_gateway requires on every call. Because the API replaces state
# rather than patching it, anything not re-sent is cleared — which is the
# mechanism used to detach below, and an easy way to clear something by accident.
_REQUIRED = ("name", "roleArn", "authorizerType")
_PRESERVED = ("protocolType", "protocolConfiguration", "authorizerConfiguration",
              "description", "kmsKeyArn", "exceptionLevel",
              "customTransformConfiguration")


def detach_from_gateway(gateway_id: str, dry_run: bool = False) -> None:
    """Re-send the gateway without its policy engine or interceptor."""
    current = control.get_gateway(gatewayIdentifier=gateway_id)

    params = {"gatewayIdentifier": gateway_id}
    for field in _REQUIRED:
        params[field] = current[field]
    for field in _PRESERVED:
        if field in current:
            params[field] = current[field]

    had_engine = "policyEngineConfiguration" in current
    had_interceptor = bool(current.get("interceptorConfigurations"))
    print(f"gateway {gateway_id}: policy_engine={had_engine} "
          f"interceptor={had_interceptor}")

    if dry_run:
        print(f"  [dry-run] would update_gateway with keys: {sorted(params)}")
        return

    control.update_gateway(**params)
    print("  detached policy engine and interceptor")


def delete_policies(policy_engine_id: str, dry_run: bool = False) -> None:
    """Delete every policy in the engine. Paginated: engines can hold many."""
    token = None
    while True:
        kwargs = {"policyEngineId": policy_engine_id}
        if token:
            kwargs["nextToken"] = token
        page = control.list_policies(**kwargs)
        for policy in page.get("policies", []):
            policy_id = policy["policyId"]
            if dry_run:
                print(f"  [dry-run] would delete policy {policy_id} "
                      f"({policy.get('name')})")
                continue
            control.delete_policy(
                policyEngineId=policy_engine_id, policyId=policy_id
            )
            print(f"  deleted policy {policy_id} ({policy.get('name')})")
        token = page.get("nextToken")
        if not token:
            return


def delete_engine(policy_engine_id: str, dry_run: bool = False) -> None:
    if dry_run:
        print(f"  [dry-run] would delete policy engine {policy_engine_id}")
        return
    control.delete_policy_engine(policyEngineId=policy_engine_id)
    print(f"  deleted policy engine {policy_engine_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-id",
                        help="Detach from this gateway first. Omit if the "
                             "gateway is already deleted.")
    parser.add_argument("--policy-engine-id", required=True)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be deleted and change nothing.")
    args = parser.parse_args()

    if args.gateway_id:
        detach_from_gateway(args.gateway_id, args.dry_run)
    delete_policies(args.policy_engine_id, args.dry_run)
    delete_engine(args.policy_engine_id, args.dry_run)
    print("\ndone" if not args.dry_run else "\ndry-run complete, nothing changed")


if __name__ == "__main__":
    main()
