# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for ``connect_memory.writer`` - the memory-writer orchestration logic.

All AWS dependencies (qconnect, connect, bedrock-agentcore, the dedup store) are fakes.
Fixtures reuse real (redacted) trace events from tests/fixtures/trace_events.json,
same convention as tests/test_identity.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from connect_memory.identity import PROFILE_ID_ATTRIBUTE, SkipReason as IdentitySkipReason
from connect_memory.parser import Turn, build_turns
from connect_memory.writer import (
    MemoryWriter,
    MemoryWriterConfig,
    SkipReason,
    client_token_for,
    dedup_keys_for_session,
    group_turns_by_qic_session,
    to_conversational_payload,
)

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "trace_events.json").read_text()
)

CONTACT_ID = "c0a7ac71-0000-4000-8000-000000000001"
QIC_SESSION_ID = "5e551011-0000-4000-8000-000000000001"
PROFILE_ID = "0123456789abcdef0123456789abcdef"
INSTANCE_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

TURNS = build_turns(FIXTURES)  # [CREATE_SESSION -> no turn, UTTERANCE -> no turn, ORCH_MSG -> 1 turn]


# --- fakes -----------------------------------------------------------------------------


class FakeQConnect:
    def __init__(self, session_name: str | None = CONTACT_ID):
        self._session_name = session_name
        self.calls: list[dict] = []

    def get_session(self, *, assistantId: str, sessionId: str):
        self.calls.append({"assistantId": assistantId, "sessionId": sessionId})
        session: dict = {"sessionId": sessionId}
        if self._session_name is not None:
            session["name"] = self._session_name
        return {"session": session}


class FakeConnect:
    def __init__(self, attributes: dict | None = None):
        self._attributes = {PROFILE_ID_ATTRIBUTE: PROFILE_ID} if attributes is None else attributes
        self.calls: list[dict] = []

    def get_contact_attributes(self, *, InstanceId: str, InitialContactId: str):
        self.calls.append({"InstanceId": InstanceId, "InitialContactId": InitialContactId})
        return {"Attributes": self._attributes}


class FakeMemory:
    """Fake bedrock-agentcore data-plane client. Records every create_event call."""

    def __init__(self, raises: Exception | None = None):
        self._raises = raises
        self.calls: list[dict] = []
        self._next_event_id = 0

    def create_event(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises:
            raise self._raises
        self._next_event_id += 1
        return {"event": {"eventId": f"evt-{self._next_event_id}"}}


class FakeDedupStore:
    """In-memory dedup store. ``already_seen`` seeds keys as if a prior invocation wrote them."""

    def __init__(self, already_seen: set[str] | None = None, mark_raises: Exception | None = None):
        self._seen: set[str] = set(already_seen or ())
        self._mark_raises = mark_raises
        self.checked: list[str] = []
        self.marked: list[str] = []

    def is_written(self, key: str) -> bool:
        self.checked.append(key)
        return key in self._seen

    def mark_written(self, key: str) -> None:
        if self._mark_raises:
            raise self._mark_raises
        self.marked.append(key)
        self._seen.add(key)


def _writer(qconnect=None, connect=None, memory=None, dedup=None) -> MemoryWriter:
    return MemoryWriter(
        config=MemoryWriterConfig(memory_id="mem-123", connect_instance_id=INSTANCE_ID),
        qconnect=qconnect or FakeQConnect(),
        connect=connect or FakeConnect(),
        memory=memory or FakeMemory(),
        dedup=dedup or FakeDedupStore(),
    )


def _customer_turn(orchestration_id="orch-1", session_id=QIC_SESSION_ID, text="hi") -> Turn:
    return Turn(
        role="customer.message",
        session_id=session_id,
        event_timestamp_ms=1000,
        input_value=text,
        orchestration_id=orchestration_id,
        raw={"session_id": session_id, "assistant_id": "asst-1"},
    )


def _bot_turn(orchestration_id="orch-1", session_id=QIC_SESSION_ID, text="hello") -> Turn:
    return Turn(
        role="bot.response",
        session_id=session_id,
        event_timestamp_ms=1500,
        output_value=text,
        orchestration_id=orchestration_id,
        raw={"session_id": session_id, "assistant_id": "asst-1"},
    )


def _tool_turn(session_id=QIC_SESSION_ID) -> Turn:
    return Turn(
        role="tool.call",
        session_id=session_id,
        event_timestamp_ms=1200,
        input_value="{}",
        raw={"session_id": session_id, "assistant_id": "asst-1"},
    )


# --- to_conversational_payload ----------------------------------------------------------


def test_customer_and_bot_turns_map_to_user_and_assistant():
    payload = to_conversational_payload([_customer_turn(text="hi there"), _bot_turn(text="hello!")])
    assert payload == [
        {"conversational": {"role": "USER", "content": {"text": "hi there"}}},
        {"conversational": {"role": "ASSISTANT", "content": {"text": "hello!"}}},
    ]


def test_non_conversational_roles_are_dropped():
    turns = [_customer_turn(), _tool_turn(), _bot_turn()]
    payload = to_conversational_payload(turns)
    assert len(payload) == 2
    assert all(entry["conversational"]["role"] in ("USER", "ASSISTANT") for entry in payload)


def test_turns_with_empty_content_are_dropped():
    empty = Turn(role="customer.message", session_id=QIC_SESSION_ID, event_timestamp_ms=1000, raw={})
    assert to_conversational_payload([empty]) == []


def test_payload_preserves_turn_order():
    turns = [_customer_turn(text="1"), _bot_turn(text="2"), _customer_turn(text="3")]
    payload = to_conversational_payload(turns)
    assert [entry["conversational"]["content"]["text"] for entry in payload] == ["1", "2", "3"]


def test_real_fixture_turn_is_conversational():
    # The one ORCHESTRATION_MESSAGE turn in the shared fixture is a customer message.
    assert len(TURNS) == 1
    payload = to_conversational_payload(TURNS)
    assert payload == [
        {
            "conversational": {
                "role": "USER",
                "content": {
                    "text": "do you have the details about my booking? "
                    "booking reference: EX-BK-2026-4471"
                },
            }
        }
    ]


# --- group_turns_by_qic_session ---------------------------------------------------------


def test_groups_by_session_id_preserving_order():
    turns = [
        _customer_turn(session_id="s1", text="a"),
        _customer_turn(session_id="s2", text="b"),
        _bot_turn(session_id="s1", text="c"),
    ]
    grouped = group_turns_by_qic_session(turns)
    assert list(grouped.keys()) == ["s1", "s2"]
    assert [t.content for t in grouped["s1"]] == ["a", "c"]
    assert [t.content for t in grouped["s2"]] == ["b"]


def test_empty_input_groups_to_empty_dict():
    assert group_turns_by_qic_session([]) == {}


# --- dedup_keys_for_session ---------------------------------------------------------------


def test_dedup_keys_deduplicated_and_order_preserved():
    turns = [
        _customer_turn(orchestration_id="orch-1"),
        _bot_turn(orchestration_id="orch-1"),
        _customer_turn(orchestration_id="orch-2"),
    ]
    assert dedup_keys_for_session(turns, QIC_SESSION_ID) == ["orch-1", "orch-2"]


def test_dedup_key_falls_back_to_qic_session_id_when_no_orchestration_id():
    turn = Turn(role="customer.message", session_id=QIC_SESSION_ID, event_timestamp_ms=1, raw={})
    assert dedup_keys_for_session([turn], QIC_SESSION_ID) == [QIC_SESSION_ID]


def test_dedup_keys_for_empty_turns_falls_back_to_session_id():
    assert dedup_keys_for_session([], QIC_SESSION_ID) == [QIC_SESSION_ID]


# --- MemoryWriter.write_session: happy path -----------------------------------------------


def test_write_session_resolves_identity_dedupes_and_calls_create_event():
    memory = FakeMemory()
    dedup = FakeDedupStore()
    writer = _writer(memory=memory, dedup=dedup)

    outcome = writer.write_session(QIC_SESSION_ID, [_customer_turn(), _bot_turn()])

    assert outcome.written is True
    assert outcome.actor_id == PROFILE_ID
    assert outcome.contact_id == CONTACT_ID
    assert outcome.event_id == "evt-1"
    assert outcome.skip_reason is None
    assert outcome.error is None

    assert len(memory.calls) == 1
    call = memory.calls[0]
    assert call["memoryId"] == "mem-123"
    assert call["actorId"] == PROFILE_ID
    assert call["sessionId"] == CONTACT_ID
    assert call["payload"] == to_conversational_payload([_customer_turn(), _bot_turn()])
    assert call["clientToken"] == client_token_for(["orch-1"])

    assert dedup.checked == ["orch-1"]
    assert dedup.marked == ["orch-1"]


def test_write_session_against_real_fixture_turn():
    writer = _writer()
    outcome = writer.write_session(QIC_SESSION_ID, TURNS)
    assert outcome.written is True
    assert outcome.actor_id == PROFILE_ID
    assert outcome.contact_id == CONTACT_ID


def test_create_event_call_includes_a_non_null_event_timestamp():
    """CreateEvent requires eventTimestamp. FakeMemory has no botocore-style
    required-parameter validation, so this test asserts on the call kwargs directly."""
    memory = FakeMemory()
    writer = _writer(memory=memory)

    writer.write_session(QIC_SESSION_ID, [_customer_turn(), _bot_turn()])

    assert len(memory.calls) == 1
    call = memory.calls[0]
    assert "eventTimestamp" in call
    assert call["eventTimestamp"] is not None


# --- MemoryWriter.write_session: skip paths -----------------------------------------------


def test_no_conversational_turns_skips_before_any_identity_resolution():
    qconnect = FakeQConnect()
    writer = _writer(qconnect=qconnect)
    outcome = writer.write_session(QIC_SESSION_ID, [_tool_turn()])
    assert outcome.written is False
    assert outcome.skip_reason == SkipReason.NO_CONVERSATIONAL_TURNS
    assert qconnect.calls == [], "should not attempt identity resolution with nothing to write"


def test_unresolvable_identity_skips_and_surfaces_identity_skip_reason():
    connect = FakeConnect(attributes={"sentiment": "frustrated"})  # no customerProfileId
    memory = FakeMemory()
    writer = _writer(connect=connect, memory=memory)

    outcome = writer.write_session(QIC_SESSION_ID, [_customer_turn(), _bot_turn()])

    assert outcome.written is False
    assert outcome.skip_reason == IdentitySkipReason.NO_PROFILE_ATTRIBUTE
    assert outcome.actor_id is None
    assert memory.calls == [], "must never call CreateEvent when no actor_id resolved"


def test_unresolvable_identity_never_invents_a_synthetic_actor_id():
    # Regression: even though the caller has qic_session_id and contact_id available,
    # neither may be used as a stand-in actor_id. See docs/architecture.md's identity
    # resolution section.
    connect = FakeConnect(attributes={})
    memory = FakeMemory()
    writer = _writer(connect=connect, memory=memory)
    outcome = writer.write_session(QIC_SESSION_ID, [_customer_turn()])
    assert outcome.actor_id is None
    assert memory.calls == []


def test_duplicate_dedup_key_skips_without_calling_create_event():
    dedup = FakeDedupStore(already_seen={"orch-1"})
    memory = FakeMemory()
    writer = _writer(memory=memory, dedup=dedup)

    outcome = writer.write_session(QIC_SESSION_ID, [_customer_turn(orchestration_id="orch-1")])

    assert outcome.written is False
    assert outcome.skip_reason == SkipReason.DUPLICATE
    assert outcome.actor_id == PROFILE_ID, "identity is still resolved/reported even when skipped"
    assert memory.calls == []


def test_partial_duplicate_within_a_session_still_writes_the_whole_payload():
    # One orchestration_id in this session was already written (partial redelivery), the
    # other is new - the whole payload still goes out in one CreateEvent call.
    dedup = FakeDedupStore(already_seen={"orch-1"})
    memory = FakeMemory()
    writer = _writer(memory=memory, dedup=dedup)

    turns = [
        _customer_turn(orchestration_id="orch-1", text="first exchange"),
        _customer_turn(orchestration_id="orch-2", text="second exchange"),
    ]
    outcome = writer.write_session(QIC_SESSION_ID, turns)

    assert outcome.written is True
    assert len(memory.calls) == 1
    assert len(memory.calls[0]["payload"]) == 2, "both turns still written, not just the new one"


def test_dedup_is_checked_before_create_event_is_called():
    # If dedup finds a duplicate, CreateEvent must never be reached - order matters for a
    # correctness reason (avoiding a real duplicate write), not just efficiency.
    dedup = FakeDedupStore(already_seen={QIC_SESSION_ID})
    memory = FakeMemory()
    writer = _writer(memory=memory, dedup=dedup)
    # Turn with no orchestration_id -> dedup key falls back to qic_session_id, seeded above.
    turn = _customer_turn()
    turn = Turn(**{**turn.__dict__, "orchestration_id": ""})
    writer.write_session(QIC_SESSION_ID, [turn])
    assert memory.calls == []


# --- MemoryWriter.write_session: failure path ---------------------------------------------


def test_create_event_failure_is_reported_not_raised():
    memory = FakeMemory(raises=RuntimeError("ThrottlingException"))
    writer = _writer(memory=memory)

    outcome = writer.write_session(QIC_SESSION_ID, [_customer_turn(), _bot_turn()])

    assert outcome.written is False
    assert outcome.error == "RuntimeError: ThrottlingException"
    assert outcome.actor_id == PROFILE_ID, "identity resolution result still reported on failure"
    assert outcome.skip_reason is None, "a CreateEvent failure is not a skip"


def test_failed_create_event_leaves_keys_unmarked_so_the_retry_writes():
    """A failed CreateEvent must not mark the dedup keys: the Lambda retry has to write the
    session again instead of skipping it as a duplicate (and, once retries are exhausted,
    the batch has to reach the DLQ)."""
    dedup = FakeDedupStore()
    failing = FakeMemory(raises=RuntimeError("ThrottlingException"))
    turns = [_customer_turn(), _bot_turn()]

    first = _writer(memory=failing, dedup=dedup).write_session(QIC_SESSION_ID, turns)
    assert first.error is not None
    assert dedup.marked == []

    memory = FakeMemory()
    retry = _writer(memory=memory, dedup=dedup).write_session(QIC_SESSION_ID, turns)

    assert retry.written is True
    assert retry.skip_reason is None
    assert len(memory.calls) == 1
    assert dedup.marked == ["orch-1"]


def test_dedup_mark_failure_after_a_successful_write_still_reports_written():
    dedup = FakeDedupStore(mark_raises=RuntimeError("table unavailable"))
    memory = FakeMemory()

    outcome = _writer(memory=memory, dedup=dedup).write_session(
        QIC_SESSION_ID, [_customer_turn(), _bot_turn()]
    )

    assert outcome.written is True
    assert len(memory.calls) == 1


def test_partial_redelivery_uses_a_new_client_token():
    """The token must not repeat the earlier request's token when new turns are added,
    otherwise the service ignores the call and the new turns are lost."""
    memory = FakeMemory()
    dedup = FakeDedupStore()
    first = [_customer_turn(orchestration_id="orch-1")]
    _writer(memory=memory, dedup=dedup).write_session(QIC_SESSION_ID, first)

    both = first + [_customer_turn(orchestration_id="orch-2", text="second exchange")]
    _writer(memory=memory, dedup=dedup).write_session(QIC_SESSION_ID, both)

    tokens = [c["clientToken"] for c in memory.calls]
    assert len(tokens) == 2 and tokens[0] != tokens[1]
    assert tokens[1] == client_token_for(["orch-2"])


def test_client_token_is_stable_for_the_same_keys():
    token = client_token_for(["b", "a"])
    assert token == client_token_for(["a", "b"])
    assert token.startswith("cm-") and len(token) == 63
