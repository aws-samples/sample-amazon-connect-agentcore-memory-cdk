#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CDK app entry point.

Reads deployment-specific configuration from ``cdk.json`` context (see that file's
``context`` block) rather than hardcoding any account/region/instance values in source -
every environment-specific identifier lives in ``cdk.json`` (or
in a ``-c key=value`` CLI override), never in a stack or shared module.

All validation below is offline (string shape checks only, no AWS calls) so this module -
and therefore ``cdk synth`` - stays credential-free and runs cleanly in CI. Anything that
needs a live AWS call to verify (log group existence, subscription-filter slot
availability, memory resource ACTIVE status, AI agent assistant resolution, CDK bootstrap)
belongs in ``scripts/preflight.py``, not here.

Required context keys (see cdk.json for placeholder values and README.md for how to
override them per-deployment):
    connectInstanceId      Amazon Connect instance id (bare id, not ARN - a UUID)
    connectInstanceAlias   Instance alias, used to derive the default AI Agent log group
    aiAgentLogGroupName    CloudWatch Logs group the Connect AI Agent already writes to
    memoryResourceArn      ARN of the AgentCore Memory resource (see scripts/provision_memory.sh)
    wisdomAssistantArn     ARN (or bare id) of the AI agent assistant (Wisdom API)
    wisdomAssistantId      Bare AI agent assistant id (used by the reader Lambda's env config)
    accountId / region     Target AWS account and region for both stacks
"""
import os
import re

import aws_cdk as cdk
import cdk_nag

from stacks.memory_reader_stack import MemoryReaderStack
from stacks.memory_writer_stack import MemoryWriterStack

app = cdk.App()

context = app.node.try_get_context

# A cdk.json context value that still equals its own placeholder (e.g.
# "REPLACE_WITH_ACCOUNT_ID") is exactly as absent as a missing key for this app's purposes -
# both mean "this deployment was never configured" and must fail synth the same way.
_PLACEHOLDER_PREFIX = "REPLACE_WITH_"

_CONNECT_INSTANCE_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_MEMORY_RESOURCE_ARN_PATTERN = re.compile(
    r"^arn:aws[a-zA-Z-]*:bedrock-agentcore:[a-z0-9-]+:\d{12}:memory/\S+$"
)
_ACCOUNT_ID_PATTERN = re.compile(r"^\d{12}$")


def _require(key: str) -> str:
    """Fetch a required context key, rejecting both a missing value and an
    un-overridden ``REPLACE_WITH_...`` placeholder still left in cdk.json."""
    value = context(key)
    if not value or str(value).startswith(_PLACEHOLDER_PREFIX):
        raise ValueError(
            f"Missing or unconfigured required CDK context key '{key}' (value: {value!r}). "
            f"Set it in cdk.json's `context` block or pass -c {key}=<value> on the CLI. "
            "See README.md."
        )
    return str(value)


def _require_matching(key: str, pattern: re.Pattern[str], description: str) -> str:
    """Like ``_require``, but also rejects a value that is present and non-placeholder yet
    does not look like the shape this app expects - catching a config mistake (e.g. an ARN
    pasted into ``connectInstanceId``) at synth time rather than as an opaque CloudFormation
    validation failure or, worse, a resource scoped to the wrong ARN."""
    value = _require(key)
    if not pattern.match(value):
        raise ValueError(
            f"CDK context key '{key}' is set to {value!r}, which does not look like "
            f"{description}. See README.md."
        )
    return value


connect_instance_id = _require_matching(
    "connectInstanceId", _CONNECT_INSTANCE_ID_PATTERN, "an Amazon Connect instance id (a UUID)"
)
ai_agent_log_group_name = _require("aiAgentLogGroupName")
memory_resource_arn = _require_matching(
    "memoryResourceArn",
    _MEMORY_RESOURCE_ARN_PATTERN,
    "an AgentCore Memory resource ARN "
    "(arn:aws:bedrock-agentcore:<region>:<account>:memory/<id>)",
)
wisdom_assistant_arn = _require("wisdomAssistantArn")

account_id = context("accountId")
if account_id and str(account_id).startswith(_PLACEHOLDER_PREFIX):
    account_id = None
if account_id and not _ACCOUNT_ID_PATTERN.match(str(account_id)):
    raise ValueError(
        f"CDK context key 'accountId' is set to {account_id!r}, which does not look like "
        "a 12-digit AWS account id. See README.md."
    )
account_id = account_id or os.getenv("CDK_DEFAULT_ACCOUNT")

region = context("region")
if region and str(region).startswith(_PLACEHOLDER_PREFIX):
    region = None
region = region or os.getenv("CDK_DEFAULT_REGION")

env = cdk.Environment(account=account_id, region=region) if account_id and region else None

MemoryWriterStack(
    app,
    "MemoryWriterStack",
    connect_instance_id=connect_instance_id,
    ai_agent_log_group_name=ai_agent_log_group_name,
    memory_resource_arn=memory_resource_arn,
    wisdom_assistant_arn=wisdom_assistant_arn,
    env=env,
)

MemoryReaderStack(
    app,
    "MemoryReaderStack",
    connect_instance_id=connect_instance_id,
    memory_resource_arn=memory_resource_arn,
    wisdom_assistant_arn=wisdom_assistant_arn,
    env=env,
)

cdk.Aspects.of(app).add(cdk_nag.AwsSolutionsChecks())

app.synth()
