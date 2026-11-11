# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contact-start memory read path Lambda.

Invoked synchronously by an Amazon Connect flow ("Invoke AWS Lambda function" block),
placed after the flow has bound ``customerProfileId`` to a verified customer and before
the AI agent block. Connect blocks the flow on the response, so every failure mode in
``connect_memory.reader.retrieve_memory_context`` resolves to an empty result instead of
an exception.

The Lambda does not resolve identity itself. It trusts the ``CustomerProfileId``
parameter, so the flow must set that value only from a verified Customer Profiles match
and clear it on every other path (see docs/connect-flow-instructions.md, section 1).

Input (``event["Details"]["Parameters"]``):
    CustomerProfileId   Customer Profiles profileId bound by the flow. Read from the
                        block's parameters rather than from contact attributes, so the data
                        flow is visible in the flow diagram.
    WisdomAssistantArn  AI agent assistant ARN or id (static parameter value).
    WisdomSessionArn    The contact's AI agent session ARN (``$.Wisdom.SessionArn``),
                        ``arn:aws:wisdom:<region>:<account>:session/<assistantId>/<sessionId>``.

Output (flat string map, as Connect requires):
    memoryAvailable     "true" when at least one memory record was found, else "false"
    memoryRecordCount   number of records retrieved, as a string
    memoryDelivered     "true" when the memory block was written to the AI agent session

The memory text itself is never returned to the flow, so it does not end up in contact
attributes or the contact record. It reaches the AI agent only through
``qconnect:UpdateSessionData``, which writes the delimited block from
``connect_memory.reader.format_memory_context`` into the session's ``Custom`` namespace
under ``MemoryContext``. The AI agent prompt references it as ``{{$.Custom.MemoryContext}}``.

Required environment variables:
    MEMORY_ID              AgentCore Memory resource id

Optional:
    MAX_RESULTS_PER_NAMESPACE  Default 3 (connect_memory.reader.DEFAULT_MAX_RESULTS_PER_NAMESPACE)
    TIMEOUT_SECONDS            Default 3.5 (connect_memory.reader.DEFAULT_TIMEOUT_SECONDS)
"""

from __future__ import annotations

import logging
import os
import re

import boto3

from connect_memory.reader import (
    DEFAULT_MAX_RESULTS_PER_NAMESPACE,
    DEFAULT_TIMEOUT_SECONDS,
    format_memory_context,
    retrieve_memory_context,
)

# The Lambda runtime installs its own root handler, so basicConfig() would be a no-op;
# set the level on the root logger instead so INFO lines reach CloudWatch Logs.
logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

MEMORY_ID = os.environ["MEMORY_ID"]
MAX_RESULTS_PER_NAMESPACE = int(
    os.environ.get("MAX_RESULTS_PER_NAMESPACE", DEFAULT_MAX_RESULTS_PER_NAMESPACE)
)
TIMEOUT_SECONDS = float(os.environ.get("TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))

# The custom session-data key the AI agent's prompt references as
# {{$.Custom.MemoryContext}} (see docs/connect-flow-instructions.md, section 3).
MEMORY_CONTEXT_SESSION_DATA_KEY = "MemoryContext"

# Matches the AI agent session ARN shape:
# arn:aws:wisdom:<region>:<account-id>:session/<assistantId>/<sessionId>
_SESSION_ARN_PATTERN = re.compile(
    r"^arn:aws:wisdom:[a-z0-9-]+:\d{12}:session/[^/]+/(?P<session_id>[^/]+)$"
)

# Constructed once per container so warm invocations reuse connections.
_memory = boto3.client("bedrock-agentcore")
_qconnect = boto3.client("qconnect")


def _parse_session_id(session_arn: str | None) -> str | None:
    """Extract the bare ``sessionId`` from an AI agent session ARN.

    Returns ``None`` (never raises) when ``session_arn`` is missing or does not match the
    expected shape - the caller treats that identically to "nothing to write back".
    """
    if not session_arn:
        return None
    match = _SESSION_ARN_PATTERN.match(session_arn)
    return match.group("session_id") if match else None


def _publish_memory_context(
    *, assistant_id: str | None, session_arn: str | None, formatted_context: str
) -> bool:
    """Write ``formatted_context`` into the AI agent session's ``Custom`` namespace via
    ``qconnect:UpdateSessionData``. Returns True when the call succeeded.

    Never raises. Skips the call when there is nothing to write (an unset custom variable
    interpolates as an empty string in the prompt), when ``assistant_id`` is missing, or
    when ``session_arn`` does not parse to a session id.
    """
    if not formatted_context:
        return False
    if not assistant_id:
        logger.warning("no WisdomAssistantArn parameter - skipping UpdateSessionData")
        return False
    session_id = _parse_session_id(session_arn)
    if session_id is None:
        logger.warning("WisdomSessionArn missing or malformed - skipping UpdateSessionData")
        return False
    try:
        _qconnect.update_session_data(
            assistantId=assistant_id,
            sessionId=session_id,
            data=[
                {
                    "key": MEMORY_CONTEXT_SESSION_DATA_KEY,
                    "value": {"stringValue": formatted_context},
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001 - a session-data write failure must not block the flow
        logger.warning(
            "UpdateSessionData failed for session=%s: %s: %s",
            session_id, type(exc).__name__, exc,
        )
        return False
    return True


def lambda_handler(event, context):
    parameters = event.get("Details", {}).get("Parameters", {})
    profile_id = parameters.get("CustomerProfileId")
    wisdom_assistant_id = parameters.get("WisdomAssistantArn")
    wisdom_session_arn = parameters.get("WisdomSessionArn")

    memory_context = retrieve_memory_context(
        profile_id,
        memory=_memory,
        memory_id=MEMORY_ID,
        max_results_per_namespace=MAX_RESULTS_PER_NAMESPACE,
        timeout_seconds=TIMEOUT_SECONDS,
    )

    formatted_context = format_memory_context(memory_context)
    delivered = _publish_memory_context(
        assistant_id=wisdom_assistant_id,
        session_arn=wisdom_session_arn,
        formatted_context=formatted_context,
    )

    logger.info(
        "contact-start memory lookup: available=%s skip_reason=%s records=%d delivered=%s",
        memory_context.available, memory_context.skip_reason,
        len(memory_context.records), delivered,
    )

    return {
        "memoryAvailable": "true" if memory_context.available else "false",
        "memoryRecordCount": str(len(memory_context.records)),
        "memoryDelivered": "true" if delivered else "false",
    }
