# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Memory-writer orchestration: turns -> actor_id resolution -> dedup -> CreateEvent.

This module is pure orchestration logic, deliberately free of any Lambda-handler concerns
(no ``event``/``context``, no CloudWatch payload decoding - that stays in
``connect_memory.parser`` and ``writer/lambda_function.py``). It composes:

- ``connect_memory.parser.Turn`` objects into the ``conversational`` payload shape
  ``bedrock-agentcore``'s ``CreateEvent`` expects.
- ``connect_memory.identity.resolve_actor_id`` to attribute a batch of turns to a
  Customer Profiles ``profileId``, skipping the write entirely when no profile resolves -
  see ``docs/architecture.md``'s identity resolution section for why a synthetic key is
  never substituted.
- A dedup store keyed on ``orchestration_id``, because CloudWatch Logs subscription
  delivery is at-least-once: the same batch of events can be delivered to this Lambda more
  than once, and without a check ``CreateEvent`` would write duplicate memory events for the
  same customer turn.

Namespaces are NOT chosen here. ``CreateEvent`` takes only ``actorId``/``sessionId``/
``payload`` - the memory resource's configured strategies (SEMANTIC, CUSTOM recap,
SUMMARIZATION) are what fan the raw event out into namespaced records asynchronously.
``connect_memory.identity``'s namespace helpers belong to the read path, not this writer.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence

from connect_memory.identity import (
    ConnectClient,
    QConnectClient,
    ResolvedIdentity,
    resolve_actor_id,
)
from connect_memory.parser import Turn

logger = logging.getLogger(__name__)

# Turn roles that are customer-facing conversation content. Excludes bot.reasoning (the AI
# agent's internal reasoning, not a fact about the customer), tool.call/tool.result
# (mechanism, not conversation) and orchestration.error (an operational event).
_CONVERSATIONAL_ROLES: Mapping[str, str] = {
    "customer.message": "USER",
    "bot.response": "ASSISTANT",
}


class SkipReason:
    """Why a session's turns produced no CreateEvent call. Emit as a metric dimension,
    same convention as ``connect_memory.identity.SkipReason``."""

    NO_IDENTITY = "no_identity"
    """No actor_id resolved for this session - see connect_memory.identity.SkipReason for
    the specific sub-reason, logged separately by resolve_actor_id."""

    NO_CONVERSATIONAL_TURNS = "no_conversational_turns"
    """The session's turns were all reasoning/tool/trace/error - nothing worth writing."""

    DUPLICATE = "duplicate"
    """Every turn's orchestration_id (or the session as a whole, if no turn carries one)
    was already written by a previous, at-least-once-redelivered invocation."""


@dataclass(frozen=True)
class WriteOutcome:
    """Result of attempting to write one session's turns to AgentCore Memory."""

    qic_session_id: str
    written: bool
    skip_reason: str | None = None
    event_id: str | None = None
    actor_id: str | None = None
    contact_id: str | None = None
    error: str | None = None
    """Set when written=False due to a CreateEvent failure (not a skip). The caller treats
    this as retryable and lets it propagate to the DLQ (see writer/lambda_function.py)."""


class DedupStore(Protocol):
    """Tracks which orchestration_ids have already produced a successful CreateEvent call.

    Keys are marked only after CreateEvent succeeds, so a failed write is retried rather
    than skipped as a duplicate. ``mark_written`` must tolerate a key that another
    invocation marked concurrently (e.g. a DynamoDB conditional PutItem that treats
    ConditionalCheckFailed as success).
    """

    def is_written(self, key: str) -> bool:
        """Return True if ``key`` was marked by an earlier, successful write."""
        ...

    def mark_written(self, key: str) -> None:
        """Mark ``key`` as written. Called only after CreateEvent succeeded."""
        ...


class MemoryClient(Protocol):
    """The subset of the ``bedrock-agentcore`` data-plane client this module calls."""

    def create_event(self, **kwargs: Any) -> Mapping[str, Any]: ...


def to_conversational_payload(turns: Sequence[Turn]) -> list[dict[str, Any]]:
    """Map customer.message / bot.response turns (in order) to CreateEvent's
    ``conversational`` payload entries.

    Turns are assumed already time-sorted (``build_turns`` sorts its input via
    ``parse_log_events``). Non-conversational turns (reasoning, tool calls/results, trace,
    error) are dropped - see ``_CONVERSATIONAL_ROLES`` docstring for why.
    """
    payload: list[dict[str, Any]] = []
    for turn in turns:
        role = _CONVERSATIONAL_ROLES.get(turn.role)
        if role is None:
            continue
        text = turn.content
        if not text:
            continue
        payload.append({"conversational": {"role": role, "content": {"text": text}}})
    return payload


def dedup_keys_for_session(turns: Sequence[Turn], qic_session_id: str) -> list[str]:
    """Orchestration ids to check/mark before writing this session's turns.

    Most turns in a session share one ``orchestration_id`` per customer/bot exchange; a
    session can have several. Turns with no ``orchestration_id`` (e.g. a session with only
    non-orchestration events) fall back to the AI agent session id itself so a session-level
    duplicate is still caught. Order-preserving, de-duplicated within the list itself.
    """
    keys: list[str] = []
    seen: set[str] = set()
    for turn in turns:
        key = turn.orchestration_id or qic_session_id
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys or [qic_session_id]


def client_token_for(keys: Sequence[str]) -> str:
    """CreateEvent clientToken derived from the dedup keys that are not written yet.

    A redelivery of the same batch produces the same token, so the repeated request is
    ignored by the service. A partial redelivery (some keys already written, new turns
    added) produces a different token, so the new turns are not dropped as a repeat of
    the earlier request.
    """
    digest = hashlib.sha256("\n".join(sorted(keys)).encode("utf-8")).hexdigest()
    return f"cm-{digest[:60]}"


def group_turns_by_qic_session(turns: Sequence[Turn]) -> dict[str, list[Turn]]:
    """Group a time-sorted batch of turns by AI agent ``session_id``.

    One CloudWatch subscription delivery can interleave events from multiple AI agent sessions
    (``connect_memory.parser.build_turns`` handles that at the event-classification level;
    this groups the resulting turns for per-session identity resolution and CreateEvent
    calls). Insertion order is preserved within each group and across groups, matching the
    input's time order.
    """
    grouped: dict[str, list[Turn]] = {}
    for turn in turns:
        grouped.setdefault(turn.session_id, []).append(turn)
    return grouped


@dataclass(frozen=True)
class MemoryWriterConfig:
    memory_id: str
    connect_instance_id: str


class MemoryWriter:
    """Resolves identity, dedupes, and writes one session's turns to AgentCore Memory.

    Constructed once per Lambda invocation (or once per warm container) and reused across
    the sessions in a batch - the qconnect/connect/memory clients and the dedup store are
    all injected so this class has no AWS SDK dependency of its own and every branch can be
    exercised with fakes in unit tests.
    """

    def __init__(
        self,
        *,
        config: MemoryWriterConfig,
        qconnect: QConnectClient,
        connect: ConnectClient,
        memory: MemoryClient,
        dedup: DedupStore,
    ) -> None:
        self._config = config
        self._qconnect = qconnect
        self._connect = connect
        self._memory = memory
        self._dedup = dedup

    def write_session(self, qic_session_id: str, turns: Sequence[Turn]) -> WriteOutcome:
        """Attempt to write one AI agent session's turns as a single CreateEvent call.

        Order: skip before dedup before write. An unresolvable session is skipped
        regardless of dedup state, dedup is checked before CreateEvent so a redelivered
        batch does not reach the API, and dedup keys are marked only after CreateEvent
        succeeds.
        """
        payload = to_conversational_payload(turns)
        if not payload:
            return WriteOutcome(
                qic_session_id=qic_session_id,
                written=False,
                skip_reason=SkipReason.NO_CONVERSATIONAL_TURNS,
            )

        identity, skip_reason = self._resolve(turns[0])
        if identity is None:
            logger.info(
                "qic_session=%s: no actor_id resolved (%s) - skipping memory write",
                qic_session_id, skip_reason,
            )
            return WriteOutcome(
                qic_session_id=qic_session_id,
                written=False,
                skip_reason=skip_reason or SkipReason.NO_IDENTITY,
            )

        dedup_keys = dedup_keys_for_session(turns, qic_session_id)
        new_keys = [k for k in dedup_keys if not self._dedup.is_written(k)]
        if not new_keys:
            logger.info(
                "qic_session=%s actor=%s: all %d dedup key(s) already written - "
                "skipping (at-least-once redelivery)",
                qic_session_id, identity.actor_id, len(dedup_keys),
            )
            return WriteOutcome(
                qic_session_id=qic_session_id,
                written=False,
                skip_reason=SkipReason.DUPLICATE,
                actor_id=identity.actor_id,
                contact_id=identity.session_id,
            )
        if len(new_keys) != len(dedup_keys):
            # Partial redelivery within one batch: some exchanges in this session were
            # already written, others are new. CreateEvent has no per-turn granularity, so
            # the whole payload is written again; a duplicate raw event does not create a
            # duplicate long-term record because extraction consolidates records.
            logger.info(
                "qic_session=%s actor=%s: %d/%d dedup key(s) already written, writing "
                "payload anyway (CreateEvent has no per-turn granularity)",
                qic_session_id, identity.actor_id, len(dedup_keys) - len(new_keys), len(dedup_keys),
            )

        try:
            response = self._memory.create_event(
                memoryId=self._config.memory_id,
                actorId=identity.actor_id,
                sessionId=identity.session_id,
                eventTimestamp=datetime.now(timezone.utc),
                payload=payload,
                clientToken=client_token_for(new_keys),
            )
        except Exception as exc:  # noqa: BLE001 - propagate to the caller as a failure
            logger.error(
                "CreateEvent failed for qic_session=%s actor=%s contact=%s: %s: %s",
                qic_session_id, identity.actor_id, identity.session_id,
                type(exc).__name__, exc,
            )
            # Keys stay unmarked, so the Lambda retry writes this session again and, once
            # retries are exhausted, the batch reaches the DLQ.
            return WriteOutcome(
                qic_session_id=qic_session_id,
                written=False,
                actor_id=identity.actor_id,
                contact_id=identity.session_id,
                error=f"{type(exc).__name__}: {exc}",
            )

        for key in new_keys:
            try:
                self._dedup.mark_written(key)
            except Exception as exc:  # noqa: BLE001 - the write itself succeeded
                # A redelivery of this batch would call CreateEvent again with the same
                # clientToken, which the service ignores.
                logger.warning(
                    "qic_session=%s: could not mark dedup key %s after a successful write: "
                    "%s: %s", qic_session_id, key, type(exc).__name__, exc,
                )

        event_id = response.get("event", {}).get("eventId")
        logger.info(
            "wrote qic_session=%s actor=%s contact=%s eventId=%s (%d payload entries)",
            qic_session_id, identity.actor_id, identity.session_id, event_id, len(payload),
        )
        return WriteOutcome(
            qic_session_id=qic_session_id,
            written=True,
            event_id=event_id,
            actor_id=identity.actor_id,
            contact_id=identity.session_id,
        )

    def _resolve(self, first_turn: Turn) -> tuple[ResolvedIdentity | None, str | None]:
        return resolve_actor_id(
            first_turn.raw,
            qconnect=self._qconnect,
            connect=self._connect,
            connect_instance_id=self._config.connect_instance_id,
        )
