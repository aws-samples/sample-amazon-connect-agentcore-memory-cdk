# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""MemoryWriterStack: CloudWatch subscription filter -> writer Lambda -> AgentCore Memory.

Provisions everything the write path needs EXCEPT the AgentCore Memory resource itself and
its extraction execution role - those are provisioned separately by
``scripts/provision_memory.sh`` (the memory resource holds customer data and must survive a
``cdk destroy``, so it is intentionally kept out of this stack's lifecycle).

Resources created here:
    - A DynamoDB table for ``orchestration_id`` dedup (protects against CloudWatch Logs'
      at-least-once subscription-filter delivery writing duplicate memory events).
    - An SQS dead-letter queue for the writer Lambda's failed invocations.
    - An IAM execution role scoped to exactly what the writer needs: writing to the
      configured AgentCore Memory resource, resolving identity via the AI agent session and Connect, and
      dedup table + DLQ access.
    - The writer Lambda function itself (Python 3.12, boto3/botocore vendored via
      ``scripts/bundle_lambdas.sh`` - NOT a Docker-bundled ``PythonFunction``, because the
      managed runtime's built-in boto3 lags newer service APIs such as
      ``bedrock-agentcore``).
    - A CloudWatch Logs subscription filter on the EXISTING AI Agent log group, forwarding
      every log event to the writer Lambda. This is deliberately a separate, independent
      subscription filter alongside any other filter that may already occupy
      the log group's other slot (CloudWatch caps a log group at 2 subscription filters) -
      see ``scripts/preflight.py``'s slot-count check, which callers MUST run before
      deploying this stack.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_lambda_destinations as lambda_destinations,
    aws_logs as logs,
    aws_logs_destinations as logs_destinations,
    aws_sqs as sqs,
)
from constructs import Construct
import cdk_nag

# Bundled Lambda deployment package produced by scripts/bundle_lambdas.sh - contains
# lambdas/writer/lambda_function.py, a copy of src/connect_memory, and boto3/botocore
# vendored via `pip install -t` (see that script's docstring for the full rationale).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_WRITER_BUNDLE_DIR = _REPO_ROOT / "dist" / "writer"


class MemoryWriterStack(Stack):
    """Writer Lambda + subscription filter + DynamoDB dedup table + SQS DLQ."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        connect_instance_id: str,
        ai_agent_log_group_name: str,
        memory_resource_arn: str,
        wisdom_assistant_arn: str,
        **kwargs,
    ) -> None:
        """
        Args:
            connect_instance_id: Amazon Connect instance id (NOT the ARN) - used both to
                scope the identity-resolution IAM permissions and to build the
                ``CONNECT_INSTANCE_ID`` Lambda environment variable
                (``connect_memory.identity.resolve_actor_id`` needs the bare id, matching
                boto3's ``connect.get_contact_attributes(InstanceId=...)`` parameter shape).
            ai_agent_log_group_name: Name of the EXISTING CloudWatch Logs group the
                Connect AI Agent already writes to (e.g.
                ``/aws/connect/ai-agent/<instance-alias>``). This stack imports it rather
                than creating it - it is a pre-existing, shared resource that other
                consumers may already subscribe to.
            memory_resource_arn: ARN of the AgentCore Memory resource, created separately
                by ``scripts/provision_memory.sh`` (see module docstring).
            wisdom_assistant_arn: ARN (or bare assistant id - both forms are accepted;
                mirrors ``MemoryReaderStack``'s identically-named parameter) of the
                AI agent assistant (Wisdom API) this deployment's AI agent runs on. Scopes
                the writer's ``wisdom:GetSession`` permission (used by
                ``connect_memory.identity.resolve_actor_id`` to chase a trace event's
                AI agent session back to a Connect contactId) to sessions under this
                one assistant, rather than every assistant in the account.
        """
        super().__init__(scope, construct_id, **kwargs)

        if not _WRITER_BUNDLE_DIR.exists():
            raise RuntimeError(
                f"Writer Lambda bundle not found at {_WRITER_BUNDLE_DIR}. Run "
                "`scripts/bundle_lambdas.sh` before `cdk synth`/`cdk deploy` - see that "
                "script's docstring for what it does and why (boto3/botocore vendoring)."
            )

        # --- Dedup table -------------------------------------------------------------
        # Partition key only (no sort key) - one item per orchestration_id, matching
        # connect_memory.dedup.DynamoDbDedupStore's conditional PutItem contract.
        dedup_table = dynamodb.Table(
            self,
            "DedupTable",
            partition_key=dynamodb.Attribute(
                name="orchestration_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",
            # RETAIN: a dedup table accidentally deleted mid-migration could cause a
            # burst of duplicate memory writes, not data loss of anything irreplaceable -
            # DESTROY would be defensible too, but RETAIN costs nothing extra and matches
            # this stack's general bias toward not silently losing state on teardown.
            removal_policy=RemovalPolicy.RETAIN,
        )

        # --- Dead-letter queue ---------------------------------------------------------
        dlq = sqs.Queue(
            self,
            "WriterDlq",
            retention_period=Duration.days(14),  # max retention - survives investigation time
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
        )

        # --- Execution role --------------------------------------------------------------
        # Not the AWSLambdaBasicExecutionRole managed policy: its CloudWatch Logs statement
        # uses ``Resource: "*"`` (every log group in the account and Region). The role gets
        # write access to this function's own log group only (see WriterLogGroup below).
        execution_role = iam.Role(
            self,
            "WriterExecutionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Execution role for the AgentCore Memory writer Lambda",
        )
        execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="AllowMemoryWrite",
                actions=["bedrock-agentcore:CreateEvent"],
                resources=[memory_resource_arn],
            )
        )
        # Scope GetSession to sessions under this one configured AI agent assistant, mirroring
        # MemoryReaderStack's identically-scoped wisdom:UpdateSessionData permission - the
        # wildcard covers "any session", not "any assistant".
        execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="AllowIdentityResolutionGetSession",
                actions=["wisdom:GetSession"],
                resources=[_wisdom_session_wildcard_resource(self, wisdom_assistant_arn)],
            )
        )
        execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="AllowIdentityResolutionContactAttributes",
                actions=["connect:GetContactAttributes"],
                resources=[
                    self.format_arn(
                        service="connect",
                        resource="instance",
                        resource_name=f"{connect_instance_id}/contact/*",
                    )
                ],
            )
        )
        dedup_table.grant(execution_role, "dynamodb:PutItem", "dynamodb:GetItem")
        dlq.grant_send_messages(execution_role)

        # The function's own log group, created with a stack-derived name before the
        # function and passed in through ``log_group``. Deriving the name from the
        # function instead would create a Role -> LogGroup -> Function -> Role dependency
        # cycle in CloudFormation.
        writer_log_group = logs.LogGroup(
            self,
            "WriterLogGroup",
            log_group_name=f"/aws/lambda/{self.stack_name}-writer",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        writer_log_group.grant_write(execution_role)

        # --- Writer Lambda -----------------------------------------------------------
        writer_fn = _lambda.Function(
            self,
            "WriterFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="lambda_function.lambda_handler",
            code=_lambda.Code.from_asset(str(_WRITER_BUNDLE_DIR)),
            role=execution_role,
            timeout=Duration.seconds(60),
            memory_size=256,
            log_group=writer_log_group,
            environment={
                "MEMORY_ID": _memory_id_from_arn(memory_resource_arn),
                "CONNECT_INSTANCE_ID": connect_instance_id,
                "DEDUP_TABLE_NAME": dedup_table.table_name,
            },
        )

        # Route failed asynchronous invocations to the DLQ. CloudWatch Logs subscription
        # filters invoke Lambda asynchronously, so an on-failure destination is the right
        # mechanism (an event-source-mapping DLQ applies only to poll-based sources).
        writer_fn.configure_async_invoke(on_failure=lambda_destinations.SqsDestination(dlq))

        # --- Subscription filter on the EXISTING AI Agent log group --------------------
        ai_agent_log_group = logs.LogGroup.from_log_group_name(
            self, "AiAgentLogGroup", ai_agent_log_group_name
        )

        # Grant invoke to the Region-specific CloudWatch Logs principal, scoped to the AI
        # agent log group, instead of LambdaDestination's default grant to the global
        # ``logs.amazonaws.com`` principal.
        writer_fn.add_permission(
            "AllowCloudWatchLogsInvoke",
            principal=iam.ServicePrincipal(f"logs.{self.region}.amazonaws.com"),
            source_arn=ai_agent_log_group.log_group_arn,
        )
        logs.SubscriptionFilter(
            self,
            "WriterSubscriptionFilter",
            log_group=ai_agent_log_group,
            destination=logs_destinations.LambdaDestination(
                writer_fn, add_permissions=False
            ),
            filter_pattern=logs.FilterPattern.all_events(),
            filter_name="memory-writer",
        )

        CfnOutput(
            self,
            "WriterFunctionArn",
            value=writer_fn.function_arn,
            description="ARN of the AgentCore Memory writer Lambda",
        )
        CfnOutput(
            self,
            "WriterDlqUrl",
            value=dlq.queue_url,
            description="URL of the writer Lambda's async-invoke failure DLQ",
        )
        CfnOutput(
            self,
            "DedupTableName",
            value=dedup_table.table_name,
            description="Name of the orchestration_id dedup DynamoDB table",
        )

        self.writer_function = writer_fn
        self.dedup_table = dedup_table
        self.dlq = dlq

        # --- CDK Nag suppressions (justified) ----------------------------------------
        cdk_nag.NagSuppressions.add_resource_suppressions(
            dlq,
            [cdk_nag.NagPackSuppression(
                id="AwsSolutions-SQS3",
                reason="This is the dead-letter queue: it is the terminal failure "
                       "destination for the writer Lambda's async invocation failures. "
                       "Adding a DLQ to the DLQ would create an unbounded chain.",
            )],
        )
        cdk_nag.NagSuppressions.add_resource_suppressions(
            dedup_table,
            [cdk_nag.NagPackSuppression(
                id="AwsSolutions-DDB3",
                reason="Dedup table holds ephemeral orchestration_id entries with TTL "
                       "expiry. Data is reconstructable from CloudWatch Logs if needed. "
                       "PITR adds cost without meaningful recovery benefit.",
            )],
        )
        cdk_nag.NagSuppressions.add_resource_suppressions(
            writer_fn,
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
                reason="Wildcards are scoped to specific resource paths: "
                       "session/<assistantId>/* covers any session under one assistant, "
                       "and instance/<id>/contact/* covers any contact on one instance. "
                       "These are the narrowest grants possible for identity resolution "
                       "at runtime (the writer resolves arbitrary contact ids).",
            )],
            apply_to_children=True,
        )


def _wisdom_session_wildcard_resource(stack: Stack, wisdom_assistant_arn: str) -> str:
    """Build the ``session/<assistantId>/*`` resource ARN for the configured assistant,
    accepting either a bare assistant id or a full assistant ARN as input - identical
    helper to ``MemoryReaderStack``'s method of the same name (kept as a free function here
    since it is needed before the stack's execution role construction, ahead of any
    instance method call site)."""
    assistant_id = wisdom_assistant_arn.rsplit("/", 1)[-1]
    return stack.format_arn(
        service="wisdom",
        resource="session",
        resource_name=f"{assistant_id}/*",
    )


def _memory_id_from_arn(memory_resource_arn: str) -> str:
    """Extract the bare memory id from an AgentCore Memory ARN
    (``arn:aws:bedrock-agentcore:<region>:<account>:memory/<memory-id>``).

    The writer/reader Lambdas' ``MEMORY_ID`` env var takes the bare id (matching
    ``connect_memory.writer.MemoryWriterConfig.memory_id`` and
    ``connect_memory.reader.retrieve_memory_context``'s ``memory_id`` parameter, both of
    which are passed straight into boto3 calls that expect the bare id, not the ARN).
    """
    return memory_resource_arn.rsplit("/", 1)[-1]
