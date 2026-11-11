# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit/snapshot tests for ``stacks.memory_reader_stack.MemoryReaderStack``."""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from stacks.memory_reader_stack import MemoryReaderStack

CONNECT_INSTANCE_ID = "22222222-2222-2222-2222-222222222222"
MEMORY_RESOURCE_ARN = (
    "arn:aws:bedrock-agentcore:us-east-1:123456789012:memory/test_memory-AbCdEf1234"
)
WISDOM_ASSISTANT_ARN = (
    "arn:aws:wisdom:us-east-1:123456789012:assistant/33333333-3333-3333-3333-333333333333"
)


def _synth_template() -> Template:
    app = cdk.App()
    stack = MemoryReaderStack(
        app,
        "TestMemoryReaderStack",
        connect_instance_id=CONNECT_INSTANCE_ID,
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
        },
    )


def test_lambda_environment_includes_memory_id():
    template = _synth_template()
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Environment": {
                "Variables": Match.object_like({"MEMORY_ID": "test_memory-AbCdEf1234"})
            }
        },
    )


def test_creates_connect_integration_association_of_type_lambda_function():
    template = _synth_template()
    template.resource_count_is("AWS::Connect::IntegrationAssociation", 1)
    template.has_resource_properties(
        "AWS::Connect::IntegrationAssociation",
        {
            # CloudFormation validates InstanceId against the full instance ARN shape,
            # not the bare id - see memory_reader_stack.py's comment at the call site.
            "InstanceId": _join_ending_with(f".*instance/{CONNECT_INSTANCE_ID}$"),
            "IntegrationType": "LAMBDA_FUNCTION",
        },
    )


def test_grants_connect_service_permission_to_invoke_the_reader_lambda():
    template = _synth_template()
    template.has_resource_properties(
        "AWS::Lambda::Permission",
        {
            "Action": "lambda:InvokeFunction",
            "Principal": "connect.amazonaws.com",
            "SourceArn": Match.object_like(
                {
                    "Fn::Join": Match.array_with(
                        [
                            Match.array_with(
                                [
                                    Match.string_like_regexp(
                                        f".*instance/{CONNECT_INSTANCE_ID}$"
                                    )
                                ]
                            )
                        ]
                    )
                }
            ),
        },
    )


def test_execution_role_is_scoped_to_retrieve_memory_records_only_on_configured_memory():
    template = _synth_template()
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Sid": "RetrieveMemoryRecordsOnly",
                                "Action": "bedrock-agentcore:RetrieveMemoryRecords",
                                "Resource": MEMORY_RESOURCE_ARN,
                            }
                        )
                    ]
                )
            }
        },
    )


def _join_ending_with(regex: str):
    """Matcher for an ``Fn::Join`` resource ARN whose final joined part matches ``regex`` -
    CDK renders a role-scoped ARN built via ``Stack.format_arn`` as
    ``{"Fn::Join": ["", [..., "<the interesting suffix>"]]}`` rather than a plain string."""
    return Match.object_like(
        {"Fn::Join": Match.array_with([Match.array_with([Match.string_like_regexp(regex)])])}
    )


def test_execution_role_scopes_update_session_data_to_configured_assistant():
    template = _synth_template()
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Sid": "AllowUpdateSessionDataOnConfiguredAssistant",
                                "Action": "wisdom:UpdateSessionData",
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


def test_execution_role_has_no_wildcard_resource_managed_policies():
    """AWSLambdaBasicExecutionRole's CloudWatch Logs statement is scoped to Resource: "*".
    Every IAM policy in this sample is scoped to specific resources, so the execution role
    must not carry that (or any) managed policy at all."""
    template = _synth_template()
    template.has_resource_properties(
        "AWS::IAM::Role",
        {"ManagedPolicyArns": Match.absent()},
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


def test_outputs_reader_function_arn():
    template = _synth_template()
    outputs = template.to_json().get("Outputs", {})
    assert "ReaderFunctionArn" in outputs


def test_accepts_bare_assistant_id_as_well_as_full_arn():
    app = cdk.App()
    stack = MemoryReaderStack(
        app,
        "TestMemoryReaderStackBareId",
        connect_instance_id=CONNECT_INSTANCE_ID,
        memory_resource_arn=MEMORY_RESOURCE_ARN,
        wisdom_assistant_arn="33333333-3333-3333-3333-333333333333",
        env=cdk.Environment(account="123456789012", region="us-east-1"),
    )
    template = Template.from_stack(stack)
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Sid": "AllowUpdateSessionDataOnConfiguredAssistant",
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


def test_reader_timeout_fits_the_synchronous_flow_block_maximum():
    template = _synth_template()
    template.has_resource_properties("AWS::Lambda::Function", {"Timeout": 8})
