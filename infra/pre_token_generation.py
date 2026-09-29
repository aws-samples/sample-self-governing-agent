"""
Cognito pre-token-generation Lambda trigger.

This is the component that makes the whole Cedar layer work. Cedar reads
`principal.getTag("task_tier")`, which resolves to a claim on the verified
token — so something has to put the classified tier into that token. This trigger
does, by copying values from the ClientMetadata passed to initiate_auth (see
src/session_token.mint_session_token).

Trigger version matters: `V2_0` is required to add claims to ACCESS tokens.
The V1 event shape only supports ID token claims, and AgentCore gateways
authenticate with the access token, so a V1 trigger produces a token with no
task_tier — every tag-gated permit then falls through to default-deny. Set the
trigger's LambdaVersion to V2_0 in the user pool configuration.

WHY THE ALLOWLIST BELOW MATTERS: ClientMetadata is supplied by the caller. If
this trigger copied it wholesale into claims, any caller who could reach
initiate_auth could mint themselves a COMPLEX token with an arbitrary
approval_ref — which would hand back exactly the self-granted authority this
architecture removes. Only known keys are copied, and each is validated.
"""

import os

VALID_TIERS = ("SIMPLE", "MEDIUM", "COMPLEX")
DEFAULT_TIER = "SIMPLE"  # least authority, for a missing or invalid claim

# Approval references are minted by the approval workflow, not the agent. This
# prefix is a cheap sanity check; the authoritative validation is that the
# approval service is the only thing that can produce a valid reference.
APPROVAL_PREFIX = os.environ.get("APPROVAL_REF_PREFIX", "appr-")


def _tier(metadata: dict) -> str:
    tier = (metadata.get("task_tier") or "").upper()
    return tier if tier in VALID_TIERS else DEFAULT_TIER


def _degraded(metadata: dict, tier_was_valid: bool) -> str:
    """
    Mark the session degraded when the classifier fell back OR when the
    requested tier was unusable. Both mean "we are not confident in this tier",
    and the Cedar policy forbids destructive tools on degraded sessions.
    """
    claimed = (metadata.get("classification_degraded") or "").lower() == "true"
    return "true" if (claimed or not tier_was_valid) else "false"


def handler(event, _context):
    metadata = (event.get("request") or {}).get("clientMetadata") or {}

    raw_tier = (metadata.get("task_tier") or "").upper()
    tier_was_valid = raw_tier in VALID_TIERS
    tier = raw_tier if tier_was_valid else DEFAULT_TIER

    claims: dict[str, str] = {
        "task_tier": tier,
        "classification_degraded": _degraded(metadata, tier_was_valid),
    }

    session_id = metadata.get("session_id")
    if session_id:
        # Carried so the interceptor can key its counter off the verified token
        # rather than the request body. A session id the agent supplies in a
        # payload can be rotated to reset the counter; a signed one cannot.
        claims["session_id"] = str(session_id)[:128]

    approval_ref = metadata.get("approval_ref")
    if approval_ref and str(approval_ref).startswith(APPROVAL_PREFIX):
        claims["approval_ref"] = str(approval_ref)[:128]

    event["response"] = {
        "claimsAndScopeOverrideDetails": {
            "accessTokenGeneration": {
                "claimsToAddOrOverride": claims,
            }
        }
    }
    return event
