# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""MemoryReaderStack: contact-start read path Lambda + Connect flow-module association.

Provisions the reader Lambda (invoked by Amazon Connect as a Contact Flow module - see
``lambdas/reader/lambda_function.py``'s module docstring for the exact invocation/response
shape) and its IAM execution role, scoped to ``RetrieveMemoryRecords`` on the configured
AgentCore Memory resource and ``UpdateSessionData`` on the configured AI agent assistant.

Also creates an ``AWS::Connect::IntegrationAssociation`` (``LAMBDA_FUNCTION`` type) so the
Lambda is available to be added as an "Invoke AWS Lambda function" block in a Connect
contact flow. Adding that flow block and wiring the AI agent's prompt to the resulting
attribute is a MANUAL, documented post-deploy step (see the project README and
docs/connect-flow-instructions.md) - not something this stack does, since a live flow's
content is out of CDK's blast radius by design here.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_connect as connect,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
)
from constructs import Construct
import cdk_nag

_REPO_ROOT = Path(__file__).resolve().parent.parent
_READER_BUNDLE_DIR = _REPO_ROOT / "dist" / "reader"


class MemoryReaderStack(Stack):
    """Reader Lambda + IAM role + Connect Lambda integration association."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        connect_instance_id: str,
        memory_resource_arn: str,
        wisdom_assistant_arn: str,
        **kwargs,
    ) -> None:
        """
        Args:
            connect_instance_id: Amazon Connect instance id (bare id, not ARN) - the
                ``AWS::Connect::IntegrationAssociation``'s ``InstanceId`` property accepts
                this form (matching ``create-integration-association``'s CLI/boto3
                parameter, see AWS docs).
            memory_resource_arn: ARN of the AgentCore Memory resource (see
                ``MemoryWriterStack``'s docstring for why this is provisioned outside CDK).
            wisdom_assistant_arn: ARN (or bare assistant id - both forms are accepted by
                ``UpdateSessionData``) of the AI agent assistant (Wisdom API) this contact's
                AI agent runs on. Scopes the reader's ``wisdom:UpdateSessionData``
                permission to sessions under this one assistant.
        """
        super().__init__(scope, construct_id, **kwargs)

        if not _READER_BUNDLE_DIR.exists():
            raise RuntimeError(
                f"Reader Lambda bundle not found at {_READER_BUNDLE_DIR}. Run "
                "`scripts/bundle_lambdas.sh` before `cdk synth`/`cdk deploy`."
            )

        # Not the AWSLambdaBasicExecutionRole managed policy: its CloudWatch Logs statement
        # uses ``Resource: "*"``. The role gets write access to this function's own log
        # group only (created before the function; see MemoryWriterStack for why).
        execution_role = iam.Role(
            self,
            "ReaderExecutionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Execution role for the AgentCore Memory reader Lambda",
        )
        execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="RetrieveMemoryRecordsOnly",
                actions=["bedrock-agentcore:RetrieveMemoryRecords"],
                resources=[memory_resource_arn],
            )
        )
        # Scope UpdateSessionData to sessions under the one configured assistant: the
        # wildcard covers any session, not any assistant.
        wisdom_session_resource = (
            self._wisdom_session_wildcard_resource(wisdom_assistant_arn)
        )
        execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="AllowUpdateSessionDataOnConfiguredAssistant",
                actions=["wisdom:UpdateSessionData"],
                resources=[wisdom_session_resource],
            )
        )

        # This function's own log group, created before the function (see MemoryWriterStack
        # for why the order matters).
        reader_log_group = logs.LogGroup(
            self,
            "ReaderLogGroup",
            log_group_name=f"/aws/lambda/{self.stack_name}-reader",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        reader_log_group.grant_write(execution_role)

        reader_fn = _lambda.Function(
            self,
            "ReaderFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="lambda_function.lambda_handler",
            code=_lambda.Code.from_asset(str(_READER_BUNDLE_DIR)),
            role=execution_role,
            # Matches the 8 s maximum of a synchronous flow Lambda block; the reader's own
            # retrieval budget is 3.5 s.
            timeout=Duration.seconds(8),
            memory_size=256,
            log_group=reader_log_group,
            environment={
                "MEMORY_ID": _memory_id_from_arn(memory_resource_arn),
            },
        )

        # Grant the Connect service principal permission to invoke this Lambda from a
        # flow, scoped to the specific Connect instance (matches the resource-based policy
        # a flow's "Invoke AWS Lambda function" block requires).
        reader_fn.add_permission(
            "AllowConnectInvoke",
            principal=iam.ServicePrincipal("connect.amazonaws.com"),
            source_arn=self.format_arn(
                service="connect", resource="instance", resource_name=connect_instance_id
            ),
        )

        # InstanceId takes the instance ARN (arn:aws:connect:<region>:<account>:instance/<id>);
        # format_arn builds it from the bare instance id used elsewhere in this stack.
        connect.CfnIntegrationAssociation(
            self,
            "ReaderIntegrationAssociation",
            instance_id=self.format_arn(
                service="connect", resource="instance", resource_name=connect_instance_id
            ),
            integration_arn=reader_fn.function_arn,
            integration_type="LAMBDA_FUNCTION",
        )

        CfnOutput(
            self,
            "ReaderFunctionArn",
            value=reader_fn.function_arn,
            description="ARN of the AgentCore Memory reader Lambda",
        )

        self.reader_function = reader_fn

        # --- CDK Nag suppressions (justified) ----------------------------------------
        cdk_nag.NagSuppressions.add_resource_suppressions(
            reader_fn,
            [cdk_nag.NagPackSuppression(
                id="AwsSolutions-L1",
                reason="Python 3.12 is intentional: vendored boto3/botocore in the "
                       "Lambda bundle targets this runtime. Upgrading the runtime "
                       "requires re-validating the vendored SDK.",
            )],
        )
        cdk_nag.NagSuppressions.add_resource_suppressions(
            execution_role,
            [cdk_nag.NagPackSuppression(
                id="AwsSolutions-IAM5",
                reason="Wildcard in session/<assistantId>/* is scoped to one AI agent "
                       "assistant, covers any session under that assistant, which is "
                       "the narrowest grant possible for UpdateSessionData at runtime.",
            )],
            apply_to_children=True,
        )

    def _wisdom_session_wildcard_resource(self, wisdom_assistant_arn: str) -> str:
        """Build the ``session/<assistantId>/*`` resource ARN for the configured assistant,
        accepting either a bare assistant id or a full assistant ARN as input (matching
        ``UpdateSessionData``'s own documented flexibility for its ``assistantId``
        parameter - see ``lambdas/reader/lambda_function.py``'s module docstring)."""
        assistant_id = wisdom_assistant_arn.rsplit("/", 1)[-1]
        return self.format_arn(
            service="wisdom",
            resource="session",
            resource_name=f"{assistant_id}/*",
        )


def _memory_id_from_arn(memory_resource_arn: str) -> str:
    """Extract the bare memory id from an AgentCore Memory ARN - see
    ``memory_writer_stack._memory_id_from_arn`` for the identical rationale."""
    return memory_resource_arn.rsplit("/", 1)[-1]
