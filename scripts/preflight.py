#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pre-deploy validation for the AgentCore Memory CDK stacks.

Run this BEFORE ``cdk deploy`` (and ideally before ``cdk synth``, since a failing check
here almost always means the deploy would fail or - worse - silently misbehave). Every
check prints a clear PASS/FAIL line with an actionable message; the script exits non-zero
if any check fails.

Checks:
    1. AI Agent log group exists (``/aws/connect/ai-agent/<instance-alias>``)
    2. CloudWatch subscription filter slot is available (queries account's actual
       Service Quotas limit, falls back to default of 2; prints what already
       occupies existing slot(s))
    3. AgentCore Memory resource exists and is ACTIVE
    4. AI agent assistant ARN/id resolves
    5. CDK is bootstrapped in the target account/region

Reads the same ``cdk.json`` context keys ``app.py`` requires, so a config mistake here
surfaces before any CloudFormation call rather than as a deploy-time stack failure.

Usage:
    python3 scripts/preflight.py [--profile PROFILE]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError, ProfileNotFound

REPO_ROOT = Path(__file__).resolve().parent.parent


class CheckResult:
    def __init__(self, name: str, passed: bool, message: str) -> None:
        self.name = name
        self.passed = passed
        self.message = message

    def print(self) -> None:
        status = "PASS" if self.passed else "FAIL"
        print(f"[{status}] {self.name}: {self.message}")


def load_context() -> dict[str, Any]:
    cdk_json = REPO_ROOT / "cdk.json"
    if not cdk_json.exists():
        print(f"FATAL: {cdk_json} not found - run this script from the repo root.", file=sys.stderr)
        sys.exit(2)
    data = json.loads(cdk_json.read_text())
    return data.get("context", {})


def _require_context(context: dict[str, Any], key: str) -> str | None:
    value = context.get(key)
    if not value or str(value).startswith("REPLACE_WITH_"):
        return None
    return str(value)


def check_ai_agent_log_group(logs_client, log_group_name: str | None) -> CheckResult:
    name = "AI Agent log group exists"
    if not log_group_name:
        return CheckResult(name, False, "cdk.json context 'aiAgentLogGroupName' is not set")
    try:
        resp = logs_client.describe_log_groups(logGroupNamePrefix=log_group_name)
    except ClientError as exc:
        return CheckResult(name, False, f"describe_log_groups failed: {exc}")
    groups = [g for g in resp.get("logGroups", []) if g.get("logGroupName") == log_group_name]
    if not groups:
        return CheckResult(
            name, False,
            f"log group '{log_group_name}' not found - verify aiAgentLogGroupName/"
            f"connectInstanceAlias in cdk.json match the live Connect instance.",
        )
    return CheckResult(name, True, f"found '{log_group_name}'")


def check_subscription_filter_slot(logs_client, log_group_name: str | None, quota_limit: int = 2) -> CheckResult:
    name = "Subscription filter slot available"
    if not log_group_name:
        return CheckResult(name, False, "cdk.json context 'aiAgentLogGroupName' is not set")
    try:
        resp = logs_client.describe_subscription_filters(logGroupName=log_group_name)
    except ClientError as exc:
        return CheckResult(name, False, f"describe_subscription_filters failed: {exc}")
    filters = resp.get("subscriptionFilters", [])
    if len(filters) >= quota_limit:
        occupants = ", ".join(
            f"{f.get('filterName')} -> {f.get('destinationArn')}" for f in filters
        )
        return CheckResult(
            name, False,
            f"log group already has {len(filters)}/{quota_limit} subscription filters ({occupants}) - "
            f"MemoryWriterStack needs a free slot. Remove or consolidate an existing "
            f"filter before deploying.",
        )
    occupants = (
        ", ".join(f"{f.get('filterName')} -> {f.get('destinationArn')}" for f in filters)
        or "none"
    )
    return CheckResult(name, True, f"{len(filters)}/{quota_limit} slots used (existing: {occupants})")


def check_memory_resource_active(agentcore_control_client, memory_resource_arn: str | None) -> CheckResult:
    name = "AgentCore Memory resource is ACTIVE"
    if not memory_resource_arn:
        return CheckResult(name, False, "cdk.json context 'memoryResourceArn' is not set")
    memory_id = memory_resource_arn.rsplit("/", 1)[-1]
    try:
        resp = agentcore_control_client.get_memory(memoryId=memory_id)
    except ClientError as exc:
        return CheckResult(
            name, False,
            f"get_memory failed for '{memory_id}': {exc}. Has "
            f"scripts/provision_memory.sh been run against this account/region?",
        )
    status = resp.get("memory", {}).get("status")
    if status != "ACTIVE":
        return CheckResult(name, False, f"memory '{memory_id}' status is '{status}', not ACTIVE")
    strategy_statuses = [s.get("status") for s in resp.get("memory", {}).get("strategies", [])]
    if any(s != "ACTIVE" for s in strategy_statuses):
        return CheckResult(
            name, False,
            f"memory '{memory_id}' is ACTIVE but has non-ACTIVE strategies: {strategy_statuses}",
        )
    return CheckResult(name, True, f"memory '{memory_id}' is ACTIVE with {len(strategy_statuses)} strategy(ies)")


def check_wisdom_assistant_resolves(qconnect_client, wisdom_assistant_arn: str | None) -> CheckResult:
    name = "AI agent assistant ARN resolves"
    if not wisdom_assistant_arn:
        return CheckResult(name, False, "cdk.json context 'wisdomAssistantArn' is not set")
    try:
        qconnect_client.get_assistant(assistantId=wisdom_assistant_arn)
    except ClientError as exc:
        return CheckResult(
            name, False, f"get_assistant failed for '{wisdom_assistant_arn}': {exc}"
        )
    return CheckResult(name, True, f"assistant '{wisdom_assistant_arn}' resolved")


def check_cdk_bootstrap(cfn_client) -> CheckResult:
    name = "CDK bootstrap present in target account/region"
    try:
        resp = cfn_client.describe_stacks(StackName="CDKToolkit")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("ValidationError",):
            return CheckResult(
                name, False,
                "stack 'CDKToolkit' not found - run `cdk bootstrap` against this "
                "account/region before deploying.",
            )
        return CheckResult(name, False, f"describe_stacks failed: {exc}")
    stacks = resp.get("Stacks", [])
    if not stacks or stacks[0].get("StackStatus") not in (
        "CREATE_COMPLETE", "UPDATE_COMPLETE",
    ):
        status = stacks[0].get("StackStatus") if stacks else "not found"
        return CheckResult(name, False, f"CDKToolkit stack status is '{status}', not complete")
    return CheckResult(name, True, "CDKToolkit stack present and complete")


def _get_subscription_filter_quota(session: boto3.Session) -> int:
    """Query the actual subscription filter quota for the account/region.

    Falls back to the default of 2 if the quota lookup fails (the Service
    Quotas API may not return a result for every account).
    """
    default_limit = 2
    try:
        sq_client = session.client("service-quotas")
        resp = sq_client.get_service_quota(ServiceCode="logs", QuotaCode="L-87E7D306")
        value = resp.get("Quota", {}).get("Value")
        if value is not None:
            return int(value)
    except (ClientError, Exception):
        pass
    return default_limit


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default=None, help="AWS profile to use (optional)")
    parser.add_argument("--region", default=None, help="AWS region override (optional)")
    args = parser.parse_args()

    context = load_context()
    region = args.region or _require_context(context, "region")

    try:
        session = boto3.Session(profile_name=args.profile, region_name=region)
    except ProfileNotFound as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    log_group_name = _require_context(context, "aiAgentLogGroupName")
    memory_resource_arn = _require_context(context, "memoryResourceArn")
    wisdom_assistant_arn = _require_context(context, "wisdomAssistantArn")

    results = [
        check_ai_agent_log_group(session.client("logs"), log_group_name),
        check_subscription_filter_slot(
            session.client("logs"), log_group_name,
            quota_limit=_get_subscription_filter_quota(session),
        ),
        check_memory_resource_active(
            session.client("bedrock-agentcore-control"), memory_resource_arn
        ),
        check_wisdom_assistant_resolves(session.client("qconnect"), wisdom_assistant_arn),
        check_cdk_bootstrap(session.client("cloudformation")),
    ]

    print("=" * 70)
    print("Preflight checks")
    print("=" * 70)
    for result in results:
        result.print()
    print("=" * 70)

    failed = [r for r in results if not r.passed]
    if failed:
        print(f"{len(failed)}/{len(results)} check(s) FAILED - resolve before deploying.")
        return 1

    print(f"All {len(results)} checks passed - safe to run `cdk deploy`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
