# Self-Governing AI Agent with Adaptive Resource Limits

Reference implementation for the blog post *Build a Self-Governing AI Agent with
Adaptive Resource Limits Using Amazon Bedrock AgentCore*.

A Python agent that classifies each request's complexity at runtime, derives a
resource budget from its own execution history, and runs inside boundaries
enforced **outside its own process**.

That last point is the design. An agent whose tier, ceiling, and counter all live
in its own process has no guardrail: whatever compromises the agent compromises
the limits at the same moment. So governance is split into four layers, and only
two of them are enforcement.

## The four layers

| Layer | Question | Where it runs | Boundary? | Code |
|---|---|---|---|---|
| 1. Classify | What kind of task is this? | In the agent | No — an *input* to governance | [complexity_classifier.py](src/complexity_classifier.py) |
| 2. Authority | Which tools may this session reach? | AgentCore Gateway | **Yes** | [governance_authority.cedar](policies/governance_authority.cedar) |
| 3. Volume | How many calls, in aggregate? | Gateway interceptor (Lambda) | **Yes** | [budget_interceptor.py](src/interceptor/budget_interceptor.py) |
| 4. Fail fast | Stop cleanly with a usable signal | In the agent | No — efficiency and UX | [governed_agent.py](src/governed_agent.py) |

The mechanism connecting them: **the tier travels as a signed identity claim, not
as a parameter.** The agent classifies the request, a short-lived credential is
minted carrying `task_tier` as a claim, and the gateway reads the claim from the
credential. The agent never tells the policy engine what tier it is, so it cannot
widen its own authority — a successful prompt injection against the classifier
yields a wider *budget*, never a wider *toolset*.

```
                          ┌──────────────────────────┐
   user request ─────────▶│  1. CLASSIFY             │  Bedrock Converse,
                          │     (in-process)         │  forced tool use
                          └────────────┬─────────────┘
                                       │ tier
                    ┌──────────────────┴──────────────────┐
                    ▼                                     ▼
      ┌──────────────────────────┐           ┌──────────────────────────┐
      │  ADAPTIVE BUDGET         │           │  MINT SESSION CREDENTIAL │
      │  DynamoDB history +      │           │  task_tier as a signed   │
      │  Kaplan-Meier q90        │           │  claim                   │
      └────────────┬─────────────┘           └────────────┬─────────────┘
                   ▼                                      │
      ┌──────────────────────────┐                        │
      │  4. FAIL FAST            │                        │
      │  Strands hooks           │                        │
      └────────────┬─────────────┘                        │
                   │ tool call                            │
   ════════════════▼════════ process boundary ════════════▼═══════════════
      ┌───────────────────────────────────────────────────────────────┐
      │                     AgentCore Gateway                         │
      │   3. VOLUME (interceptor)      2. AUTHORITY (Cedar)           │
      │   conditional DynamoDB         principal tags + context.input │
      │   increment per session                                       │
      └───────────────────────────────┬───────────────────────────────┘
                                      ▼  tools
                          ┌──────────────────────────┐
                          │  5. FEEDBACK             │  records WHICH budget
                          │  DynamoDB + CloudWatch   │  truncated the run
                          └──────────────────────────┘
```

## Project structure

```
self-governing-agent/
├── blog-post-self-governing-agent.md   # the post (AWS blog format)
├── blog-post-longform-engineering-version.md   # long-form draft, code inline
├── requirements.txt
├── infra/
│   ├── governance-tables.yaml          # DynamoDB tables + interceptor Lambda
│   └── pre_token_generation.py         # Cognito trigger: tier -> signed claim
├── iam/
│   ├── execution-role-policy.json      # agent runtime, least privilege
│   └── interceptor-lambda-policy.json  # one action, one table
├── policies/
│   └── governance_authority.cedar      # tier-bound TOOL AUTHORITY (not budgets)
├── src/
│   ├── agent.py                        # orchestrator; composes the four layers
│   ├── complexity_classifier.py        # tier via forced tool use, fails closed
│   ├── session_token.py                # binds tier to identity (Cognito or STS)
│   ├── adaptive_limit_engine.py        # Kaplan-Meier budgets over censored data
│   ├── governed_agent.py               # Strands hooks: tool/model/token budgets
│   ├── feedback_writer.py              # telemetry + governance metrics
│   ├── policy_setup.py                 # two-phase policy/gateway deploy
│   ├── policy_teardown.py              # order-dependent detach + delete
│   └── interceptor/
│       └── budget_interceptor.py       # out-of-process volume enforcement
└── tests/
    ├── test_cedar_authorization.py     # 17 authorization cases (cedarpy, offline)
    ├── test_interceptor.py             # 8 volume cases (stub table, offline)
    ├── test_token_claims.py            # 10 claim-injection cases (offline)
    ├── test_feedback_convergence.py    # reproduces the censoring collapse (offline)
    ├── test_budget_hooks.py            # real Strands agent + real Bedrock
    ├── test_classifier.py              # tier accuracy + injection resistance
    └── verify_e2e.py                   # composed path, real Bedrock
```

## Two deliberate departures from the obvious design

**Telemetry is in DynamoDB, not AgentCore Memory.** Memory extracts facts from
conversational events for agent *recall* via semantic search. Execution telemetry
is append-only time-series data read by exact tier and time range — a DynamoDB
range query. Memory is still right for what the agent should remember across
sessions; it is wrong for governance counters.

**Percentile math runs in-process, not in Code Interpreter.** Code Interpreter is
for model-authored or untrusted code. A percentile over your own telemetry is
neither, and a sandbox session on every invocation adds seconds of latency and a
per-session charge to a system whose pitch is efficiency.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The four offline suites need no AWS credentials. All of the security-relevant
behavior is here, which is deliberate:

```bash
python -m tests.test_cedar_authorization   # 17/17 authorization cases correct
python -m tests.test_interceptor           # 8/8 passed
python -m tests.test_token_claims          # 10/10 passed
python -m tests.test_feedback_convergence  # ALL ASSERTIONS PASSED
```

These three need Bedrock access and model access enabled:

```bash
python -m tests.test_budget_hooks          # ALL TESTS PASSED
python -m tests.test_classifier            # 7/8 accuracy, 3/3 injections resisted
python -m tests.verify_e2e                 # ALL CHECKS PASSED
```

`test_classifier.py` asserts a quality floor and a prompt-level mitigation — not
a boundary. If an injected request did reach `COMPLEX`, the consequence would be a
wider *budget*; Cedar and the interceptor would still hold. The test that can be
flaky is deliberately the one whose failure is survivable.

## Deployment

Cedar rejects wildcard resources, so policies must name the gateway ARN
literally — and that ARN does not exist until the gateway does. Deployment is
therefore **two-phase**.

```bash
# 1. Tables + interceptor Lambda
aws cloudformation deploy --template-file infra/governance-tables.yaml \
    --stack-name agent-governance --capabilities CAPABILITY_IAM

# 2. Agent and gateway (phase 1)
agentcore create --name self-governing-agent --framework strands
agentcore launch
agentcore status                      # note the gateway ARN

# 3. Policies + interceptor wiring, in LOG_ONLY (phase 2)
python -m src.policy_setup \
    --gateway-id  self-governing-agent-gateway \
    --gateway-arn arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/... \
    --interceptor-arn arn:aws:lambda:us-east-1:111122223333:function:agent-budget-interceptor

# 4. Verify in CloudWatch that task_tier is present in claims, then enforce.
#    --policy-engine-arn is REQUIRED here: it promotes the engine created in
#    step 3 rather than creating a second one. Engine names are immutable and
#    unique per account, so the creation path would fail or orphan the first.
python -m src.policy_setup ... --policy-engine-arn <POLICY_ENGINE_ARN> --enforce
```

**Step 4 is not optional.** The most common failure in this architecture is a
claim the IdP silently drops, which turns every tag-gated permit into a
default-deny. In `LOG_ONLY` that is a log line; in `ENFORCE` it is every tool
call denied.

Two other things to know before enforcing. `update_gateway` replaces gateway
state rather than patching it, so any field not re-sent is cleared —
[policy_setup.py](src/policy_setup.py) reads current state and re-sends it for
that reason. And the interceptor Lambda must be deployed from
[budget_interceptor.py](src/interceptor/budget_interceptor.py); the template
ships a `NotImplementedError` placeholder so the enforcement logic has exactly
one source of truth.

Also note: the request/response envelope for gateway interceptors is
version-specific. `_extract_context` and `_deny` isolate that shape; confirm both
against the interceptor reference for your API version before enforcing.

## Teardown

Deletion is order-dependent: an engine still referenced by a gateway cannot be
deleted, and `delete_policy` needs both the engine ID and the policy ID. The
policy operations are also absent from some AWS CLI versions, so teardown is a
script rather than a list of commands.

```bash
python -m src.policy_teardown --gateway-id self-governing-agent-gateway \
    --policy-engine-id <POLICY_ENGINE_ID> --dry-run    # inspect first
python -m src.policy_teardown --gateway-id self-governing-agent-gateway \
    --policy-engine-id <POLICY_ENGINE_ID>
agentcore destroy --name self-governing-agent
aws cloudformation delete-stack --stack-name agent-governance
```

Tear down the policy engine **before** the gateway. A deleted gateway leaves its
engine and policies behind, where they stay invisible in the console and keep
counting against service quotas.

## Configuration

| Variable | Default | Used by |
|---|---|---|
| `PROFILE_TABLE` | `agent-execution-profiles` | adaptive engine, feedback writer |
| `BUDGET_TABLE` | `agent-session-budgets` | interceptor |
| `BUDGET_TTL_SECONDS` | `86400` | interceptor |
| `PROFILE_RETENTION_DAYS` | `90` | feedback writer |
| `METRIC_NAMESPACE` | `SelfGoverningAgent/Governance` | feedback writer |
| `SESSION_DURATION_SECONDS` | `900` | session token |
| `COGNITO_CLIENT_ID`, `AGENT_SERVICE_USER`, `AGENT_SERVICE_SECRET` | — | session token (OAuth path) |
| `SIMPLE_TIER_ROLE_ARN`, `MEDIUM_TIER_ROLE_ARN`, `COMPLEX_TIER_ROLE_ARN` | — | session token (SigV4 path) |

Tier ceilings live in two places by necessity:
[`TIER_CEILINGS`](src/interceptor/budget_interceptor.py) in the interceptor is
**authoritative**; [`CEILINGS`](src/adaptive_limit_engine.py) in the adaptive
engine is a copy that exists only to avoid proposing budgets the interceptor
would deny. Keep them in sync.

## Watch this metric pair

Falling `TaskCompleted` together with rising `BudgetExhaustedByResource` and
utilization near 100% is the signature of the censored-feedback collapse: the
budget censors the very data used to set it, so "P90 of successful runs" ratchets
downward with no upward pressure. It is dangerous precisely because every other
metric improves while it happens. See
[test_feedback_convergence.py](tests/test_feedback_convergence.py), which
reproduces both the collapse and the fix deterministically.

## Known limits

- **Classification is a heuristic.** The design makes being wrong survivable
  rather than making the classifier perfect: a misclassification changes the
  *budget*; only the identity claim changes *authority*.
- **Layer 4 is not a boundary.** The Strands hooks are cooperative. A compromised
  agent loop bypasses them; only the gateway layers hold.
- **Truncation needs product design.** "Budget exhausted" is not an answer to a
  user's question. The cancellation messages ask the model to summarize and name
  what is unfinished — a starting point, not a policy.
- **Multi-tenancy needs a partition key.** These tables key on tier and session.
  A shared platform needs tenant in the partition key and per-tenant ceilings.
- **Escalation is deliberately absent.** Done properly it means minting a new
  credential through an approval path outside the agent, not raising a variable.
  The `approval_ref` claim is already the hook for it.

## Services used

| Service | Purpose |
|---|---|
| Amazon Bedrock AgentCore Runtime | Hosts the agent |
| Amazon Bedrock AgentCore Gateway | Fronts the tools; enforcement point for layers 2 and 3 |
| Amazon Bedrock AgentCore Policy | Per-tool-call Cedar authorization |
| Amazon Cognito | Mints short-lived, tier-bearing session credentials via a pre-token-generation trigger (requires the Essentials feature plan or higher) |
| Amazon Bedrock | Classifier and agent inference |
| Amazon DynamoDB | Execution-profile history; session budget counters |
| AWS Lambda | Gateway interceptor |
| Amazon CloudWatch | Governance metrics and traces |

## License

MIT-0 (see LICENSE)
