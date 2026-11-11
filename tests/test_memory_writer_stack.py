# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit/snapshot tests for ``stacks.memory_writer_stack.MemoryWriterStack``.

Uses ``aws_cdk.assertions.Template`` to assert on the synthesized CloudFormation template
rather than a full ``cdk synth`` + string diff - the standard CDK Python testing pattern.
The Lambda bundle directory is provided by the ``minimal_lambda_bundles`` fixture in
``tests/conftest.py``.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from stacks.memory_writer_stack import MemoryWriterStack

CONNECT_INSTANCE_ID = "11111111-1111-1111-1111-111111111111"
AI_AGENT_LOG_GROUP_NAME = "/aws/connect/ai-agent/test-instance-alias"
MEMORY_RESOURCE_ARN = (
    "arn:aws:bedrock-agentcore:us-east-1:123456789012:memory/test_memory-AbCdEf1234"
)
WISDOM_ASSISTANT_ARN = (
    "arn:aws:wisdom:us-east-1:123456789012:assistant/33333333-3333-3333-3333-333333333333"
)


def _synth_template() -> Template:
    app = cdk.App()
    stack = MemoryWriterStack(
        app,
        "TestMemoryWriterStack",
        connect_instance_id=CONNECT_INSTANCE_ID,
        ai_agent_log_group_name=AI_AGENT_LOG_GROUP_NAME,
        memory_resource_arn=MEMORY_RESOURCE_ARN,
        wisdom_assistant_arn=WISDOM_ASSISTANT_ARN,
        env=cdk.Environment(account="123456789012", region="us-east-1"),
    )
    return Template.from_stack(stack)


def test_creates_exactly_one_lambda_function_with_python312_runtime():
    template = _synth_template()
    template.resource_count_is("AWS::Lambda::Function", 1)
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Runtime": "python3.12",
            "Handler": "lambda_function.lambda_handler",
            "Timeout": 60,
            "MemorySize": 256,
        },
    )


def test_lambda_environment_includes_memory_and_instance_config():
    template = _synth_template()
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Environment": {
                "Variables": Match.object_like(
                    {
                        "MEMORY_ID": "test_memory-AbCdEf1234",
                        "CONNECT_INSTANCE_ID": CONNECT_INSTANCE_ID,
                    }
                )
            }
        },
    )


def test_creates_dedup_table_with_ttl_and_pay_per_request_billing():
    template = _synth_template()
    template.resource_count_is("AWS::DynamoDB::Table", 1)
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "KeySchema": [{"AttributeName": "orchestration_id", "KeyType": "HASH"}],
            "BillingMode": "PAY_PER_REQUEST",
            "TimeToLiveSpecification": {"AttributeName": "expires_at", "Enabled": True},
        },
    )


def test_creates_sqs_dlq_with_14_day_retention():
    template = _synth_template()
    template.resource_count_is("AWS::SQS::Queue", 1)
    template.has_resource_properties(
        "AWS::SQS::Queue",
        {"MessageRetentionPeriod": 1209600},  # 14 days in seconds
    )


def test_creates_subscription_filter_on_the_configured_log_group():
    template = _synth_template()
    template.resource_count_is("AWS::Logs::SubscriptionFilter", 1)
    template.has_resource_properties(
        "AWS::Logs::SubscriptionFilter",
        {
            "LogGroupName": AI_AGENT_LOG_GROUP_NAME,
            "FilterName": "memory-writer",
        },
    )


def test_execution_role_is_scoped_to_the_configured_memory_resource_arn():
    template = _synth_template()
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Sid": "AllowMemoryWrite",
                                "Action": "bedrock-agentcore:CreateEvent",
                                "Resource": MEMORY_RESOURCE_ARN,
                            }
                        )
                    ]
                )
            }
        },
    )


def test_execution_role_trusts_only_lambda_service():
    template = _synth_template()
    template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "AssumeRolePolicyDocument": {
                "Statement": [
                    Match.object_like(
                        {
                            "Principal": {"Service": "lambda.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    )
                ]
            }
        },
    )


def test_execution_role_has_no_wildcard_resource_managed_policies():
    """AWSLambdaBasicExecutionRole's CloudWatch Logs statement is scoped to Resource: "*".
    Every IAM policy in this sample is scoped to specific resources, so the execution role
    must not carry that (or any) managed policy at all."""
    template = _synth_template()
    template.has_resource_properties(
        "AWS::IAM::Role",
        {"ManagedPolicyArns": Match.absent()},
    )


def test_execution_role_scopes_get_session_to_configured_assistant():
    template = _synth_template()
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Sid": "AllowIdentityResolutionGetSession",
                                "Action": "wisdom:GetSession",
                                "Resource": _join_ending_with(
                                    r".*session/33333333-3333-3333-3333-333333333333/\*$"
                                ),
                            }
                        )
                    ]
                )
            }
        },
    )


def test_creates_scoped_log_group_and_grants_write_only_on_it():
    template = _synth_template()
    template.resource_count_is("AWS::Logs::LogGroup", 1)
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                                "Resource": Match.object_like(
                                    {"Fn::GetAtt": Match.array_with(["Arn"])}
                                ),
                            }
                        )
                    ]
                )
            }
        },
    )


def test_grants_cloudwatch_logs_region_specific_principal_invoke_permission():
    template = _synth_template()
    template.has_resource_properties(
        "AWS::Lambda::Permission",
        {
            "Action": "lambda:InvokeFunction",
            "Principal": "logs.us-east-1.amazonaws.com",
        },
    )


def test_outputs_writer_arn_dlq_url_and_dedup_table_name():
    template = _synth_template()
    outputs = template.to_json().get("Outputs", {})
    assert "WriterFunctionArn" in outputs
    assert "WriterDlqUrl" in outputs
    assert "DedupTableName" in outputs


def _join_ending_with(regex: str):
    """Matcher for an ``Fn::Join`` resource ARN whose final joined part matches ``regex`` -
    mirrors ``test_memory_reader_stack.py``'s identical helper."""
    return Match.object_like(
        {"Fn::Join": Match.array_with([Match.array_with([Match.string_like_regexp(regex)])])}
    )
