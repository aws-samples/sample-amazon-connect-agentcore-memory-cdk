# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Connect AI agent -> AgentCore Memory writer Lambda.

Consumes the ``/aws/connect/ai-agent/<instance-alias>`` CloudWatch Logs group through its
own subscription filter, so its retry and DLQ behaviour is independent of any other
consumer of the same log group.

Uses ``connect_memory.parser`` for payload decoding and turn classification and
``connect_memory.writer`` for identity resolution, dedup and CreateEvent. The bundle
contains a copy of ``connect_memory``; no Lambda layer is used.

Batch failure semantics: the handler writes every session in the batch it can resolve,
and re-raises after all sessions were attempted if at least one CreateEvent call failed.
The re-raise makes Lambda retry the asynchronous invocation; when retries are exhausted
the batch goes to the DLQ configured as the function's on-failure destination. One
failing session therefore does not stop the other sessions in the batch, and a failure is
never dropped silently.

Required environment variables:
    MEMORY_ID              AgentCore Memory resource id
    CONNECT_INSTANCE_ID    Amazon Connect instance id
    DEDUP_TABLE_NAME       DynamoDB table for orchestration_id dedup
"""

from __future__ import annotations

import logging
import os

import boto3

from connect_memory.dedup import DynamoDbDedupStore
from connect_memory.parser import build_turns, decode_subscription_payload, parse_log_events
from connect_memory.writer import (
    MemoryWriter,
    MemoryWriterConfig,
    WriteOutcome,
    group_turns_by_qic_session,
)

# The Lambda runtime installs its own root handler, so basicConfig() would be a no-op;
# set the level on the root logger instead so INFO lines reach CloudWatch Logs.
logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

MEMORY_ID = os.environ["MEMORY_ID"]
CONNECT_INSTANCE_ID = os.environ["CONNECT_INSTANCE_ID"]
DEDUP_TABLE_NAME = os.environ["DEDUP_TABLE_NAME"]

_config = MemoryWriterConfig(memory_id=MEMORY_ID, connect_instance_id=CONNECT_INSTANCE_ID)

# Constructed once per container so warm invocations reuse connections.
_qconnect = boto3.client("qconnect")
_connect = boto3.client("connect")
_memory = boto3.client("bedrock-agentcore")
_dedup_table = boto3.resource("dynamodb").Table(DEDUP_TABLE_NAME)
_dedup_store = DynamoDbDedupStore(_dedup_table)

_writer = MemoryWriter(
    config=_config,
    qconnect=_qconnect,
    connect=_connect,
    memory=_memory,
    dedup=_dedup_store,
)


def lambda_handler(event, context):
    if "awslogs" not in event:
        return {"statusCode": 200, "body": "Only CloudWatch Logs subscription events supported"}

    log_data = decode_subscription_payload(event["awslogs"]["data"])
    events = parse_log_events(log_data)

    turns = build_turns(events)
    sessions = group_turns_by_qic_session(turns)

    outcomes: list[WriteOutcome] = []
    for qic_session_id, session_turns in sessions.items():
        outcomes.append(_writer.write_session(qic_session_id, session_turns))

    written = sum(1 for o in outcomes if o.written)
    skipped = sum(1 for o in outcomes if not o.written and o.error is None)
    failed = [o for o in outcomes if o.error is not None]

    logger.info(
        "batch complete: %d session(s), %d written, %d skipped, %d failed",
        len(outcomes), written, skipped, len(failed),
    )

    if failed:
        # Re-raise after every session has been attempted (see module docstring).
        details = "; ".join(f"{o.qic_session_id}: {o.error}" for o in failed)
        raise RuntimeError(
            f"{len(failed)}/{len(outcomes)} session(s) failed CreateEvent: {details}"
        )

    return {
        "statusCode": 200,
        "body": f"{written} written, {skipped} skipped, 0 failed ({len(outcomes)} sessions)",
    }
