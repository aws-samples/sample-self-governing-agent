// ============================================================================
// Self-Governing Agent: Cedar policies for tier-bound tool authority
// ============================================================================
// WHAT CEDAR DOES HERE: governs *which tools* a session may reach, based on the
// complexity tier carried in the session's identity claims.
//
// WHAT CEDAR DOES NOT DO HERE: count iterations or tool calls. AgentCore Policy
// evaluates each tool call independently and exposes only `context.input` (the
// tool's arguments) plus principal attributes. It holds no cross-call counters,
// so a running total cannot be expressed in Cedar. Volume limits are enforced
// by the gateway interceptor (see src/interceptor/budget_interceptor.py) and by
// in-process Strands hooks (see src/governed_agent.py).
//
// HOW THE TIER GETS HERE: the agent classifies the request, then mints a session
// token whose `task_tier` claim holds the result. For an OAuth gateway, JWT
// claims become tags on AgentCore::OAuthUser, readable via principal.getTag().
// The agent never asserts its own tier to the policy engine — it receives a
// credential and the gateway reads the claim from it.
//
// DEPLOYMENT: replace <GATEWAY_ARN> with the ARN from `agentcore status`.
// Cedar rejects wildcard resources, so the ARN must be literal. This forces a
// two-phase deploy: create the gateway, read its ARN, then add policies.
//
// SEMANTICS: the policy engine is default-deny with forbid-wins. Anything not
// explicitly permitted below is denied.
// ============================================================================

// ---------------------------------------------------------------------------
// Read-only tools: available to every authenticated tier.
// ---------------------------------------------------------------------------
permit(
    principal is AgentCore::OAuthUser,
    action in [
        AgentCore::Action::"CostAPI___get_cost_summary",
        AgentCore::Action::"CostAPI___list_resources",
        AgentCore::Action::"CostAPI___describe_resource"
    ],
    resource == AgentCore::Gateway::"<GATEWAY_ARN>"
);

// ---------------------------------------------------------------------------
// Analysis tools: MEDIUM and COMPLEX only.
// A SIMPLE-tier session cannot reach these at all, so a misclassification (or a
// prompt-injected attempt to act broadly) is contained by the gateway rather
// than by the agent's own restraint.
// ---------------------------------------------------------------------------
permit(
    principal is AgentCore::OAuthUser,
    action in [
        AgentCore::Action::"CostAPI___analyze_utilization",
        AgentCore::Action::"CostAPI___run_projection"
    ],
    resource == AgentCore::Gateway::"<GATEWAY_ARN>"
)
when {
    principal.hasTag("task_tier") &&
    (principal.getTag("task_tier") == "MEDIUM" ||
     principal.getTag("task_tier") == "COMPLEX")
};

// ---------------------------------------------------------------------------
// Report generation: COMPLEX only.
// ---------------------------------------------------------------------------
permit(
    principal is AgentCore::OAuthUser,
    action == AgentCore::Action::"CostAPI___generate_report",
    resource == AgentCore::Gateway::"<GATEWAY_ARN>"
)
when {
    principal.hasTag("task_tier") &&
    principal.getTag("task_tier") == "COMPLEX"
};

// ---------------------------------------------------------------------------
// Mutating tools: COMPLEX tier, human-approved, and bounded by blast radius.
// Four independent conditions, all enforced outside agent code: the tier, the
// presence of an approval reference, the presence of the arguments needed to
// judge blast radius, and a bound on that radius. `context.input` holds the
// arguments of this specific tool call, so per-call caps on magnitude ARE
// expressible in Cedar even though running totals are not.
// ---------------------------------------------------------------------------
permit(
    principal is AgentCore::OAuthUser,
    action == AgentCore::Action::"CostAPI___terminate_idle_resource",
    resource == AgentCore::Gateway::"<GATEWAY_ARN>"
)
when {
    principal.hasTag("task_tier") &&
    principal.getTag("task_tier") == "COMPLEX" &&
    principal.hasTag("approval_ref") &&
    context.input has resourceId &&
    context.input has estimatedMonthlyCost &&
    context.input.estimatedMonthlyCost < 500
};

// ---------------------------------------------------------------------------
// Production is never touched by an agent, at any tier. Forbid-wins means this
// overrides every permit above, including the COMPLEX + approval path.
// ---------------------------------------------------------------------------
forbid(
    principal is AgentCore::OAuthUser,
    action == AgentCore::Action::"CostAPI___terminate_idle_resource",
    resource == AgentCore::Gateway::"<GATEWAY_ARN>"
)
when {
    context.input has environment &&
    context.input.environment like "*prod*"
};

// ---------------------------------------------------------------------------
// A degraded classification (classifier unavailable) is marked on the token and
// loses all write authority, regardless of the tier it fell back to.
// ---------------------------------------------------------------------------
forbid(
    principal is AgentCore::OAuthUser,
    action == AgentCore::Action::"CostAPI___terminate_idle_resource",
    resource == AgentCore::Gateway::"<GATEWAY_ARN>"
)
when {
    principal.hasTag("classification_degraded") &&
    principal.getTag("classification_degraded") == "true"
};
