# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Parsing of Connect AI agent CloudWatch Logs subscription events.

Classifies the AI agent trace events delivered through the CloudWatch Logs subscription
and structures them into ``Turn`` objects. The writer only uses customer messages and AI
agent responses; the other roles are parsed so they can be filtered explicitly.

Event types used (all others are ignored):

- TRANSCRIPT_ORCHESTRATION_MESSAGE -> customer message, AI agent reasoning, tool call,
  tool result, or AI agent response
- TRANSCRIPT_ORCHESTRATION_ERROR -> orchestration error

TRANSCRIPT_AI_AGENT_TRACE events (LLM span metadata) and the other event types on the
log stream (TRANSCRIPT_CREATE_SESSION, TRANSCRIPT_UTTERANCE, ...) carry no conversation
text the writer needs and are skipped.
"""

from __future__ import annotations

import base64
import gzip
import json
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence

TurnRole = Literal[
    "customer.message",
    "bot.response",
    "bot.reasoning",
    "tool.call",
    "tool.result",
    "orchestration.error",
]


@dataclass(frozen=True)
class Turn:
    """One structured turn extracted from a raw CloudWatch log event.

    Plain data only. Callers (the memory writer) decide how to turn it into their own
    output shape.

    ``span_name``, ``span_kind_oi``, ``input_value``, ``output_value``, ``tool_name`` and
    ``tool_params`` are separate fields because each is set by whichever entry of an
    event's ``values`` array last touched it (they are not reset between entries).
    """

    role: TurnRole
    session_id: str
    event_timestamp_ms: int
    span_name: str = ""
    span_kind_oi: str = "CHAIN"
    input_value: str = ""
    output_value: str = ""
    orchestration_id: str = ""
    orchestration_iteration: int = 0
    tool_use_id: str = ""
    tool_name: str = ""
    tool_params: str = ""
    duration_ms: int = 0
    participant: str = ""
    model_id: str = ""
    guardrail_blocked: bool = False
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    """The original event, for callers that need fields this dataclass does not surface."""

    @property
    def content(self) -> str:
        """Convenience accessor: whichever of input_value/output_value is non-empty.

        Both are typically empty-or-one-set for a single-value event; for a hypothetical
        multi-value event this returns output_value first (matching most single-value
        cases, where output_value is what downstream text/reasoning/tool.result consumers
        care about) - callers that need the full accumulation should read input_value and
        output_value directly instead of this property.
        """
        return self.output_value or self.input_value


def decode_subscription_payload(awslogs_data_b64: str) -> Mapping[str, Any]:
    """Decode the base64+gzip CloudWatch Logs subscription filter payload.

    ``event["awslogs"]["data"]``: base64 decode, gzip decompress, then JSON parse.
    Raises on malformed input.
    """
    compressed = base64.b64decode(awslogs_data_b64)
    return json.loads(gzip.decompress(compressed))


def parse_log_events(log_data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Extract and time-sort the individual Connect trace-event dicts from a decoded
    subscription payload.

    Each ``logEvents[i]["message"]`` is itself a JSON string containing one Connect trace
    event. Events that fail to parse are skipped.
    """
    events: list[dict[str, Any]] = []
    for log_event in log_data.get("logEvents", []):
        try:
            events.append(json.loads(log_event["message"]))
        except (json.JSONDecodeError, TypeError):
            continue
    events.sort(key=lambda e: e.get("event_timestamp", 0))
    return events


def build_turns(events: Sequence[Mapping[str, Any]]) -> list[Turn]:
    """Classify a time-sorted batch of Connect trace events into structured Turns.

    One CloudWatch subscription delivery can contain events from multiple sessions
    interleaved; ``compute_duration_ms`` scopes its forward search to the matching
    ``session_id``. Event types other than ORCHESTRATION_MESSAGE and ORCHESTRATION_ERROR
    (including TRANSCRIPT_AI_AGENT_TRACE) are skipped.
    """
    turns: list[Turn] = []
    for i, msg in enumerate(events):
        event_type = msg.get("event_type", "")

        if event_type == "TRANSCRIPT_ORCHESTRATION_MESSAGE":
            duration_ms = compute_duration_ms(events, i)
            turns.append(build_orchestration_message_turn(msg, duration_ms))
        elif event_type == "TRANSCRIPT_ORCHESTRATION_ERROR":
            turns.append(build_orchestration_error_turn(msg))

    return turns


# --- ORCHESTRATION_MESSAGE ---------------------------------------------------------------


def build_orchestration_message_turn(msg: Mapping[str, Any], duration_ms: int) -> Turn:
    """Structure a TRANSCRIPT_ORCHESTRATION_MESSAGE event: customer message, bot
    reasoning, tool_use, tool_result, or bot response.

    Text values are truncated to 4000 characters and tool parameters to 2000. Fields
    accumulate across a ``values`` array (see the ``Turn`` docstring).
    """
    session_id = msg.get("session_id", "")
    ts_ms = msg.get("event_timestamp", 0)
    participant = msg.get("participant", "BOT")
    orch_id = msg.get("orchestration_id", "")
    iteration = msg.get("orchestration_iteration", 0)
    model_id = msg.get("model_id", "")
    guardrail_blocked = bool(msg.get("guardrail_blocked", False))

    values = _parse_json(msg.get("values", "[]"), [])

    span_kind_oi = "CHAIN"
    span_name = f"orchestration.{participant.lower()}"
    role: TurnRole = "bot.response"
    input_val = ""
    output_val = ""
    tool_use_id = ""
    tool_name = ""
    tool_params = ""

    for v in values:
        vtype = v.get("type", "")
        if vtype == "text":
            text = v.get("value", "")[:4000]
            if participant == "CUSTOMER":
                span_name = "customer.message"
                role = "customer.message"
                input_val = text
            else:
                span_name = "bot.response"
                role = "bot.response"
                output_val = text
        elif vtype == "reasoning":
            span_name = "bot.reasoning"
            role = "bot.reasoning"
            output_val = v.get("value", "")[:4000]
        elif vtype == "tool_use":
            tool_name = v.get("name", "unknown")
            tool_use_id = v.get("tool_use_id") or v.get("toolUseId", "")
            span_name = f"tool.call.{tool_name}"
            span_kind_oi = "TOOL"
            role = "tool.call"
            tool_params = json.dumps(v.get("arguments", {}))[:2000]
            input_val = tool_params
        elif vtype == "tool_result":
            tool_name = v.get("name", "unknown")
            tool_use_id = v.get("tool_use_id") or v.get("toolUseId", "")
            span_name = f"tool.result.{tool_name}"
            span_kind_oi = "TOOL"
            role = "tool.result"
            result_vals = v.get("values", [])
            output_val = result_vals[0].get("value", "")[:4000] if result_vals else ""
            if v.get("error"):
                output_val = f"ERROR: {v['error']}"

    return Turn(
        role=role,
        session_id=session_id,
        event_timestamp_ms=ts_ms,
        span_name=span_name,
        span_kind_oi=span_kind_oi,
        input_value=input_val,
        output_value=output_val,
        orchestration_id=orch_id,
        orchestration_iteration=iteration,
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        tool_params=tool_params,
        duration_ms=duration_ms,
        participant=participant,
        model_id=model_id,
        guardrail_blocked=guardrail_blocked,
        raw=msg,
    )


def compute_duration_ms(events: Sequence[Mapping[str, Any]], idx: int) -> int:
    """Duration heuristic for an ORCHESTRATION_MESSAGE event.

    For a ``tool_use`` value: search forward (same ``session_id`` only) for the matching
    ``tool_result`` by ``tool_use_id``, clamped to [100, 30000] ms. Otherwise: the gap to the
    next event in the same session, skipping events at the same millisecond (logging
    artifacts, gap must be > 10ms), clamped to a 30000ms ceiling. Falls back to 1000ms.
    """
    current = events[idx]
    session_id = current.get("session_id", "")
    ts = current.get("event_timestamp", 0)

    values = _parse_json(current.get("values", "[]"), [])

    for v in values:
        if v.get("type") == "tool_use":
            tool_use_id = v.get("tool_use_id") or v.get("toolUseId", "")
            if tool_use_id:
                for j in range(idx + 1, len(events)):
                    ev = events[j]
                    if ev.get("session_id") != session_id:
                        continue
                    ev_values = _parse_json(ev.get("values", "[]"), [])
                    for rv in ev_values:
                        if rv.get("type") == "tool_result" and (
                            rv.get("tool_use_id") or rv.get("toolUseId", "")
                        ) == tool_use_id:
                            delta = ev.get("event_timestamp", 0) - ts
                            return max(100, min(delta, 30000))

    for j in range(idx + 1, len(events)):
        if events[j].get("session_id") == session_id:
            delta = events[j].get("event_timestamp", 0) - ts
            if delta > 10:
                return min(delta, 30000)

    return 1000


# --- ORCHESTRATION_ERROR ------------------------------------------------------------------


def build_orchestration_error_turn(msg: Mapping[str, Any]) -> Turn:
    """Structure a TRANSCRIPT_ORCHESTRATION_ERROR event. Fixed 100ms duration, error
    message as output_value."""
    session_id = msg.get("session_id", "")
    ts_ms = msg.get("event_timestamp", 0)

    error = _parse_json(msg.get("orchestration_error", "{}"), {})
    error_msg = error.get("errorMessage", "Unknown error")

    return Turn(
        role="orchestration.error",
        session_id=session_id,
        event_timestamp_ms=ts_ms,
        span_name="orchestration.error",
        output_value=error_msg,
        orchestration_id=msg.get("orchestration_id", ""),
        orchestration_iteration=msg.get("orchestration_iteration", 0),
        duration_ms=100,
        raw=msg,
    )


# --- helpers -------------------------------------------------------------------------------


def _parse_json(raw: Any, default: Any) -> Any:
    """Parse a JSON-encoded string, or pass through an already-parsed value."""
    if not raw:
        return default
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default
