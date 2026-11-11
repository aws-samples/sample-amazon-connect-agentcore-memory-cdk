# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for ``connect_memory.parser``.

Fixtures in ``tests/fixtures/cw_log_events.json`` follow the shape of Connect AI agent
trace events (``values`` as a JSON string, ``span`` as a string, tool_use_id matching)
with synthetic identifiers and content.
"""

from __future__ import annotations

import base64
import gzip
import json
from pathlib import Path

import pytest

from connect_memory.parser import (
    Turn,
    build_orchestration_error_turn,
    build_orchestration_message_turn,
    build_turns,
    compute_duration_ms,
    decode_subscription_payload,
    parse_log_events,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture()
def cw_log_events() -> list[dict]:
    return json.loads((FIXTURES_DIR / "cw_log_events.json").read_text())


def _make_subscription_payload(events: list[dict]) -> dict:
    """Wrap raw Connect trace events as a decoded CloudWatch Logs subscription payload
    (the shape ``decode_subscription_payload`` returns, and ``parse_log_events`` consumes)."""
    return {
        "messageType": "DATA_MESSAGE",
        "owner": "123456789012",
        "logGroup": "/aws/connect/ai-agent/example-connect-instance",
        "logStream": "QiCAssistantTranscript",
        "subscriptionFilters": ["memory-writer"],
        "logEvents": [
            {"id": str(i), "timestamp": e.get("event_timestamp", 0), "message": json.dumps(e)}
            for i, e in enumerate(events)
        ],
    }


def _encode_awslogs_data(payload: dict) -> str:
    """Inverse of ``decode_subscription_payload``: JSON -> gzip -> base64, matching how
    CloudWatch actually delivers ``event["awslogs"]["data"]`` to the Lambda."""
    raw = json.dumps(payload).encode("utf-8")
    return base64.b64encode(gzip.compress(raw)).decode("ascii")


# --- decode_subscription_payload / parse_log_events --------------------------------------


def test_decode_subscription_payload_round_trips(cw_log_events):
    payload = _make_subscription_payload(cw_log_events)
    encoded = _encode_awslogs_data(payload)

    decoded = decode_subscription_payload(encoded)

    assert decoded["logGroup"] == "/aws/connect/ai-agent/example-connect-instance"
    assert len(decoded["logEvents"]) == len(cw_log_events)


def test_parse_log_events_sorts_by_event_timestamp(cw_log_events):
    payload = _make_subscription_payload(cw_log_events)

    events = parse_log_events(payload)

    timestamps = [e.get("event_timestamp", 0) for e in events]
    assert timestamps == sorted(timestamps)
    assert len(events) == len(cw_log_events)


def test_parse_log_events_skips_malformed_messages():
    payload = {
        "logEvents": [
            {"message": "{not valid json"},
            {"message": json.dumps({"event_type": "TRANSCRIPT_ORCHESTRATION_ERROR", "event_timestamp": 5})},
            {"message": None},
        ]
    }

    events = parse_log_events(payload)

    assert len(events) == 1
    assert events[0]["event_type"] == "TRANSCRIPT_ORCHESTRATION_ERROR"


# --- build_turns: classification & unknown event types ------------------------------------


def test_build_turns_skips_unrecognised_event_types():
    events = [
        {"event_type": "TRANSCRIPT_CREATE_SESSION", "event_timestamp": 1, "session_id": "s1"},
        {"event_type": "TRANSCRIPT_UTTERANCE", "event_timestamp": 2, "session_id": "s1"},
        {"event_type": "TRANSCRIPT_AGENTIC_MESSAGE", "event_timestamp": 3, "session_id": "s1"},
        {
            "event_type": "TRANSCRIPT_ORCHESTRATION_ERROR",
            "event_timestamp": 4,
            "session_id": "s1",
            "orchestration_error": json.dumps({"errorMessage": "boom"}),
        },
    ]

    turns = build_turns(events)

    assert len(turns) == 1
    assert turns[0].role == "orchestration.error"


def test_build_turns_over_real_fixture_produces_expected_roles(cw_log_events):
    turns = build_turns(cw_log_events)

    roles = [t.role for t in turns]
    assert roles == [
        "customer.message",
        "bot.reasoning",
        "bot.response",
        "tool.call",
        "tool.result",
        "bot.response",
        "orchestration.error",
    ]


# --- ORCHESTRATION_MESSAGE: per-role field extraction --------------------------------------


def test_customer_message_extraction():
    msg = {
        "event_type": "TRANSCRIPT_ORCHESTRATION_MESSAGE",
        "session_id": "s1",
        "event_timestamp": 100,
        "participant": "CUSTOMER",
        "orchestration_id": "orch-1",
        "orchestration_iteration": 0,
        "session_message_id": "m1",
        "values": json.dumps([{"type": "text", "value": "hello there"}]),
    }

    turn = build_orchestration_message_turn(msg, duration_ms=500)

    assert turn.role == "customer.message"
    assert turn.span_name == "customer.message"
    assert turn.input_value == "hello there"
    assert turn.output_value == ""
    assert turn.participant == "CUSTOMER"
    assert turn.orchestration_id == "orch-1"
    assert turn.session_id == "s1"
    assert turn.event_timestamp_ms == 100
    assert turn.duration_ms == 500


def test_bot_response_extraction():
    msg = {
        "event_type": "TRANSCRIPT_ORCHESTRATION_MESSAGE",
        "session_id": "s1",
        "event_timestamp": 100,
        "participant": "BOT",
        "values": json.dumps([{"type": "text", "value": "here is your answer"}]),
    }

    turn = build_orchestration_message_turn(msg, duration_ms=500)

    assert turn.role == "bot.response"
    assert turn.span_name == "bot.response"
    assert turn.output_value == "here is your answer"
    assert turn.input_value == ""


def test_bot_reasoning_extraction():
    msg = {
        "event_type": "TRANSCRIPT_ORCHESTRATION_MESSAGE",
        "session_id": "s1",
        "event_timestamp": 100,
        "participant": "BOT",
        "values": json.dumps([{"type": "reasoning", "value": "thinking about tools"}]),
    }

    turn = build_orchestration_message_turn(msg, duration_ms=500)

    assert turn.role == "bot.reasoning"
    assert turn.span_name == "bot.reasoning"
    assert turn.output_value == "thinking about tools"


def test_tool_call_extraction():
    msg = {
        "event_type": "TRANSCRIPT_ORCHESTRATION_MESSAGE",
        "session_id": "s1",
        "event_timestamp": 100,
        "participant": "BOT",
        "values": json.dumps(
            [
                {
                    "type": "tool_use",
                    "tool_use_id": "tooluse_abc123",
                    "name": "get_customer_booking",
                    "arguments": {"booking_ref": "REF-1"},
                }
            ]
        ),
    }

    turn = build_orchestration_message_turn(msg, duration_ms=1000)

    assert turn.role == "tool.call"
    assert turn.span_name == "tool.call.get_customer_booking"
    assert turn.span_kind_oi == "TOOL"
    assert turn.tool_name == "get_customer_booking"
    assert turn.tool_use_id == "tooluse_abc123"
    assert json.loads(turn.input_value) == {"booking_ref": "REF-1"}
    assert turn.tool_params == turn.input_value
    assert turn.output_value == ""


def test_tool_result_extraction():
    msg = {
        "event_type": "TRANSCRIPT_ORCHESTRATION_MESSAGE",
        "session_id": "s1",
        "event_timestamp": 100,
        "participant": "BOT",
        "values": json.dumps(
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "tooluse_abc123",
                    "name": "get_customer_booking",
                    "values": [{"type": "text", "value": '{"status": "success"}'}],
                }
            ]
        ),
    }

    turn = build_orchestration_message_turn(msg, duration_ms=500)

    assert turn.role == "tool.result"
    assert turn.span_name == "tool.result.get_customer_booking"
    assert turn.span_kind_oi == "TOOL"
    assert turn.tool_use_id == "tooluse_abc123"
    assert turn.output_value == '{"status": "success"}'
    assert turn.input_value == ""


def test_tool_result_error_extraction():
    msg = {
        "event_type": "TRANSCRIPT_ORCHESTRATION_MESSAGE",
        "session_id": "s1",
        "event_timestamp": 100,
        "participant": "BOT",
        "values": json.dumps(
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "tooluse_abc123",
                    "name": "get_customer_booking",
                    "error": "booking not found",
                }
            ]
        ),
    }

    turn = build_orchestration_message_turn(msg, duration_ms=500)

    assert turn.role == "tool.result"
    assert turn.output_value == "ERROR: booking not found"


def test_text_and_tool_result_truncation_limits():
    msg_text = {
        "event_type": "TRANSCRIPT_ORCHESTRATION_MESSAGE",
        "session_id": "s1",
        "event_timestamp": 100,
        "participant": "BOT",
        "values": json.dumps([{"type": "text", "value": "x" * 5000}]),
    }
    turn = build_orchestration_message_turn(msg_text, duration_ms=1)
    assert len(turn.output_value) == 4000

    msg_tool_use = {
        "event_type": "TRANSCRIPT_ORCHESTRATION_MESSAGE",
        "session_id": "s1",
        "event_timestamp": 100,
        "participant": "BOT",
        "values": json.dumps(
            [{"type": "tool_use", "name": "t", "tool_use_id": "id1", "arguments": {"a": "y" * 5000}}]
        ),
    }
    turn = build_orchestration_message_turn(msg_tool_use, duration_ms=1)
    assert len(turn.input_value) == 2000

    msg_tool_result = {
        "event_type": "TRANSCRIPT_ORCHESTRATION_MESSAGE",
        "session_id": "s1",
        "event_timestamp": 100,
        "participant": "BOT",
        "values": json.dumps(
            [{"type": "tool_result", "name": "t", "tool_use_id": "id1", "values": [{"type": "text", "value": "z" * 5000}]}]
        ),
    }
    turn = build_orchestration_message_turn(msg_tool_result, duration_ms=1)
    assert len(turn.output_value) == 4000


def test_multiple_values_accumulate_independently_not_reset_between_branches():
    """Pins the live code's exact (non-obvious) accumulation semantics: each of
    span_name/role/input_value/output_value/tool_name is set by whichever branch visits it
    LAST, but a field a later branch never touches keeps whatever an EARLIER branch left it
    at - it does not reset. Concretely: reasoning sets output_value, then a following
    tool_use sets input_value/tool_name/span_kind_oi/span_name/role - but output_value
    (never touched by the tool_use branch) still carries the reasoning text, not "".

    Events usually carry a single ``values`` entry; this documents the intended behaviour
    when an event carries more than one.
    """
    msg = {
        "event_type": "TRANSCRIPT_ORCHESTRATION_MESSAGE",
        "session_id": "s1",
        "event_timestamp": 100,
        "participant": "BOT",
        "values": json.dumps(
            [
                {"type": "reasoning", "value": "thinking first"},
                {"type": "tool_use", "tool_use_id": "abc", "name": "get_thing", "arguments": {"x": 1}},
            ]
        ),
    }

    turn = build_orchestration_message_turn(msg, duration_ms=1)

    assert turn.role == "tool.call"
    assert turn.span_name == "tool.call.get_thing"
    assert turn.span_kind_oi == "TOOL"
    assert turn.output_value == "thinking first"  # left over from the reasoning branch
    assert turn.input_value == '{"x": 1}'
    assert turn.tool_name == "get_thing"


# --- compute_duration_ms: tool_use/tool_result matching + fallbacks ------------------------


def test_duration_matches_tool_use_to_tool_result_by_id_across_sessions():
    events = [
        {
            "event_type": "TRANSCRIPT_ORCHESTRATION_MESSAGE",
            "session_id": "s1",
            "event_timestamp": 1000,
            "values": json.dumps([{"type": "tool_use", "tool_use_id": "abc", "name": "t"}]),
        },
        {
            # Different session - must be ignored even though it matches tool_use_id.
            "event_type": "TRANSCRIPT_ORCHESTRATION_MESSAGE",
            "session_id": "OTHER",
            "event_timestamp": 1100,
            "values": json.dumps([{"type": "tool_result", "tool_use_id": "abc", "name": "t"}]),
        },
        {
            "event_type": "TRANSCRIPT_ORCHESTRATION_MESSAGE",
            "session_id": "s1",
            "event_timestamp": 3400,
            "values": json.dumps([{"type": "tool_result", "tool_use_id": "abc", "name": "t"}]),
        },
    ]

    duration = compute_duration_ms(events, 0)

    assert duration == 2400  # 3400 - 1000, not the same-session-only 1100 - 1000


def test_duration_clamps_tool_use_result_gap_to_30000_ceiling_and_100_floor():
    events = [
        {
            "event_type": "TRANSCRIPT_ORCHESTRATION_MESSAGE",
            "session_id": "s1",
            "event_timestamp": 0,
            "values": json.dumps([{"type": "tool_use", "tool_use_id": "abc", "name": "t"}]),
        },
        {
            "event_type": "TRANSCRIPT_ORCHESTRATION_MESSAGE",
            "session_id": "s1",
            "event_timestamp": 999999,
            "values": json.dumps([{"type": "tool_result", "tool_use_id": "abc", "name": "t"}]),
        },
    ]
    assert compute_duration_ms(events, 0) == 30000

    events_fast = [
        {
            "event_type": "TRANSCRIPT_ORCHESTRATION_MESSAGE",
            "session_id": "s1",
            "event_timestamp": 0,
            "values": json.dumps([{"type": "tool_use", "tool_use_id": "abc", "name": "t"}]),
        },
        {
            "event_type": "TRANSCRIPT_ORCHESTRATION_MESSAGE",
            "session_id": "s1",
            "event_timestamp": 5,
            "values": json.dumps([{"type": "tool_result", "tool_use_id": "abc", "name": "t"}]),
        },
    ]
    assert compute_duration_ms(events_fast, 0) == 100


def test_duration_falls_back_to_next_event_gap_skipping_same_millisecond():
    events = [
        {
            "event_type": "TRANSCRIPT_ORCHESTRATION_MESSAGE",
            "session_id": "s1",
            "event_timestamp": 1000,
            "values": json.dumps([{"type": "text", "value": "hi"}]),
        },
        {
            # Same millisecond - logging artifact, must be skipped (delta must be > 10).
            "event_type": "TRANSCRIPT_ORCHESTRATION_MESSAGE",
            "session_id": "s1",
            "event_timestamp": 1005,
            "values": json.dumps([{"type": "text", "value": "artifact"}]),
        },
        {
            "event_type": "TRANSCRIPT_ORCHESTRATION_MESSAGE",
            "session_id": "s1",
            "event_timestamp": 1800,
            "values": json.dumps([{"type": "text", "value": "next real event"}]),
        },
    ]

    assert compute_duration_ms(events, 0) == 800


def test_duration_defaults_to_1000ms_when_no_next_event_in_session():
    events = [
        {
            "event_type": "TRANSCRIPT_ORCHESTRATION_MESSAGE",
            "session_id": "s1",
            "event_timestamp": 1000,
            "values": json.dumps([{"type": "text", "value": "last event"}]),
        }
    ]

    assert compute_duration_ms(events, 0) == 1000


# --- ORCHESTRATION_ERROR --------------------------------------------------------------------


def test_orchestration_error_extraction():
    msg = {
        "event_type": "TRANSCRIPT_ORCHESTRATION_ERROR",
        "session_id": "s1",
        "event_timestamp": 100,
        "orchestration_id": "orch-1",
        "orchestration_iteration": 2,
        "orchestration_error": json.dumps({"errorMessage": "Bedrock throttled"}),
    }

    turn = build_orchestration_error_turn(msg)

    assert turn.role == "orchestration.error"
    assert turn.output_value == "Bedrock throttled"
    assert turn.duration_ms == 100
    assert turn.orchestration_iteration == 2


def test_orchestration_error_defaults_message_when_missing():
    msg = {
        "event_type": "TRANSCRIPT_ORCHESTRATION_ERROR",
        "session_id": "s1",
        "event_timestamp": 100,
        "orchestration_error": "{}",
    }

    turn = build_orchestration_error_turn(msg)

    assert turn.output_value == "Unknown error"


# --- AI_AGENT_TRACE ------------------------------------------------------------------------


def test_ai_agent_trace_events_are_skipped(cw_log_events):
    trace_events = [e for e in cw_log_events if e.get("event_type") == "TRANSCRIPT_AI_AGENT_TRACE"]
    assert trace_events, "fixture must contain AI_AGENT_TRACE events"
    assert build_turns(trace_events) == []


def test_build_turns_over_a_batch_with_trace_events_keeps_conversation_turns(cw_log_events):
    with_trace = build_turns(cw_log_events)
    without_trace = build_turns(
        [e for e in cw_log_events if e.get("event_type") != "TRANSCRIPT_AI_AGENT_TRACE"]
    )
    assert [t.role for t in with_trace] == [t.role for t in without_trace]


# --- Turn dataclass sanity -------------------------------------------------------------------


def test_turn_is_frozen_and_carries_raw_event():
    raw_event = {"event_type": "TRANSCRIPT_ORCHESTRATION_ERROR", "foo": "bar"}
    turn = Turn(
        role="orchestration.error",
        session_id="s1",
        event_timestamp_ms=1,
        output_value="x",
        raw=raw_event,
    )

    assert turn.raw is raw_event
    assert turn.content == "x"
    with pytest.raises(AttributeError):
        turn.role = "bot.response"  # type: ignore[misc]


def test_turn_content_property_prefers_output_then_input():
    assert Turn(role="bot.response", session_id="s", event_timestamp_ms=0, output_value="out").content == "out"
    assert Turn(role="customer.message", session_id="s", event_timestamp_ms=0, input_value="in").content == "in"
    assert Turn(role="bot.response", session_id="s", event_timestamp_ms=0).content == ""
