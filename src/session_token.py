"""
Session token minting — binds the classified tier to the session's identity.

This is the hinge of the whole design. Cedar cannot read a value the agent
merely holds in a variable; it reads principal tags, which come from the JWT
claims on the credential the gateway authenticates. So the tier has to travel as
a *claim*, minted once per session, before any tool call happens.

Consequences, and the reason this beats an in-process check:

  - The agent cannot widen its own authority. It receives a credential; it does
    not assert a tier to the policy engine. Prompt injection can influence what
    the agent tries to do, but not what the gateway permits.
  - Enforcement survives agent bugs. A runaway loop, a malformed tool arg, or a
    compromised system prompt cannot alter what has already been signed.
  - Escalation becomes an explicit, auditable act: re-minting a token, not
    flipping a variable.

Two implementations:
  - `mint_session_token`      Cognito, for an OAuth (JWT) gateway.
  - `assume_tier_role`        STS, for an AWS_IAM gateway. Cedar matches on
                              principal.id (the assumed-role ARN) instead of
                              tags, since IAM principals do not carry tags.

Tokens are deliberately short-lived: the tier is valid for one task, and a
long-lived credential carrying COMPLEX authority is exactly what this design
exists to avoid.
"""

import os
from dataclasses import dataclass

import boto3

SESSION_DURATION_SECONDS = int(os.environ.get("SESSION_DURATION_SECONDS", "900"))

# One IAM role per tier, for AWS_IAM gateways. Cedar policies reference these
# by their assumed-role ARN.
TIER_ROLE_ARNS = {
    "SIMPLE": os.environ.get("SIMPLE_TIER_ROLE_ARN", ""),
    "MEDIUM": os.environ.get("MEDIUM_TIER_ROLE_ARN", ""),
    "COMPLEX": os.environ.get("COMPLEX_TIER_ROLE_ARN", ""),
}


@dataclass
class SessionToken:
    """A credential plus the claims the gateway will see."""

    access_token: str
    claims: dict
    expires_in: int

    def as_authorization_header(self) -> str:
        return f"Bearer {self.access_token}"


def mint_session_token(
    tier: str,
    session_id: str,
    degraded: bool = False,
    approval_ref: str | None = None,
    client=None,
) -> SessionToken:
    """
    Mint a short-lived OAuth token whose claims carry the governance tier.

    The claims below are what the Cedar policies in
    policies/governance_authority.cedar read via principal.getTag(...):

        task_tier                 SIMPLE | MEDIUM | COMPLEX
        classification_degraded   "true" when the classifier fell back
        approval_ref              present only when a human approved a
                                  destructive action for this session

    `approval_ref` is threaded through rather than inferred: the Cedar policy for
    terminate_idle_resource requires the claim to exist, so a session can only
    gain write authority when something outside the agent put it there.

    IMPLEMENTATION NOTE: custom claims are attached by a Cognito pre-token
    generation Lambda trigger (or the equivalent in your IdP) reading the
    client metadata passed here. Verify the claim actually lands in the issued
    token — a claim your IdP silently drops means every tag-gated permit falls
    through to default-deny, which fails closed but looks like a policy bug.
    """
    if tier not in ("SIMPLE", "MEDIUM", "COMPLEX"):
        raise ValueError(f"Unknown tier {tier!r}")

    claims = {
        "task_tier": tier,
        "session_id": session_id,
        "classification_degraded": "true" if degraded else "false",
    }
    if approval_ref:
        claims["approval_ref"] = approval_ref

    idp = client or boto3.client("cognito-idp")
    response = idp.initiate_auth(
        ClientId=os.environ["COGNITO_CLIENT_ID"],
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={
            "USERNAME": os.environ["AGENT_SERVICE_USER"],
            "PASSWORD": os.environ["AGENT_SERVICE_SECRET"],
        },
        # Read by the pre-token generation trigger to populate custom claims.
        ClientMetadata={k: str(v) for k, v in claims.items()},
    )
    auth = response["AuthenticationResult"]
    return SessionToken(
        access_token=auth["AccessToken"],
        claims=claims,
        expires_in=auth.get("ExpiresIn", SESSION_DURATION_SECONDS),
    )


def assume_tier_role(tier: str, session_id: str, client=None) -> dict:
    """
    Alternative for AWS_IAM gateways: assume a per-tier IAM role.

    Cedar then matches on principal.id, e.g.

        permit(
          principal == AgentCore::IamEntity::"arn:aws:sts::111122223333:assumed-role/AgentTier-COMPLEX",
          action == AgentCore::Action::"CostAPI___generate_report",
          resource == AgentCore::Gateway::"<arn>"
        );

    Simpler to operate than custom JWT claims (no IdP trigger), at the cost of
    per-call input conditions being the only fine-grained lever, since IAM
    principals carry no tags.
    """
    role_arn = TIER_ROLE_ARNS.get(tier)
    if not role_arn:
        raise ValueError(f"No role configured for tier {tier!r}")

    sts = client or boto3.client("sts")
    response = sts.assume_role(
        RoleArn=role_arn,
        # Session name is truncated to STS's 64-char limit.
        RoleSessionName=f"agent-{tier.lower()}-{session_id}"[:64],
        DurationSeconds=SESSION_DURATION_SECONDS,
    )
    return response["Credentials"]
