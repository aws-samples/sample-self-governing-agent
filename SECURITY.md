# Security Policy

## Reporting a Vulnerability

If you discover a potential security issue in this project, we ask that you
notify AWS Security via our
[vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/)
or directly via email to [aws-security@amazon.com](mailto:aws-security@amazon.com).

**Please do not create a public GitHub issue** for security-sensitive reports.

## Scope and Context

This repository is **sample/illustrative code** that accompanies a blog post. It
demonstrates a governance architecture for AI agents and is not intended to be
deployed to production as-is. It exercises the following AWS services:

- **Amazon Bedrock AgentCore** — agent runtime, gateway, and policy engine (Cedar)
- **Amazon Cognito** — session token minting and the pre-token-generation trigger
- **Amazon DynamoDB** — execution-profile telemetry and session-budget counters
- **AWS Lambda** — the gateway budget interceptor
- **Amazon CloudWatch** — governance metrics

## Before Deploying

Review these items, which are intentionally simplified in the sample:

- **Secrets**: `src/session_token.py` reads a Cognito service credential from an
  environment variable for illustration. Source runtime secrets from AWS Secrets
  Manager or SSM Parameter Store instead.
- **IAM scoping**: The policies in `iam/` use account/region and some resource
  wildcards for portability. Scope them to specific resource ARNs before deploying.
- **Stateful resources**: Add `DeletionPolicy: Retain` to DynamoDB tables that
  hold data you cannot lose.
- **Enforcement mode**: Attach the policy engine in `LOG_ONLY` first (see
  `src/policy_setup.py`) and switch to `ENFORCE` only after verifying decisions
  in CloudWatch.

## Supported Versions

This is sample code provided as-is. It is not versioned for ongoing security
support. Fork it and adapt it to your own security requirements before any
production use.
