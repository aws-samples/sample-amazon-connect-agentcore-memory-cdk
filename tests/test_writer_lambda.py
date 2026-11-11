# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""End-to-end tests for ``writer/lambda_function.py``.

Loaded via ``importlib.util.spec_from_file_location`` under an explicit module name
(``memory_writer_lambda_function``) rather than a plain ``import lambda_function`` to
avoid module name collisions in ``sys.modules`` when multiple Lambda handler files share
the same ``lambda_function`` filename.

boto3 client/resource construction is monkeypatched out BEFORE the module is loaded (its
module-level code calls ``boto3.client(...)``/``boto3.resource(...)`` eagerly), so no AWS
credentials or network access are needed to import or test this handler.
"""

from __future__ import annotations

import base64
import gzip
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from connect_memory.identity import PROFILE_ID_ATTRIBUTE

FIXTURES_DIR = Path(__file__).parent / "fixtures"
WRITER_DIR = Path(__file__).parent.parent / "lambdas" / "writer"

CONTACT_ID = "c0a7ac71-0000-4000-8000-000000000001"
PROFILE_ID = "0123456789abcdef0123456789abcdef"


class FakeQConnect:
    def get_session(self, *, assistantId: str, sessionId: str):
        return {"session": {"sessionId": sessionId, "name": CONTACT_ID}}


class FakeConnect:
    def get_contact_attributes(self, *, InstanceId: str, InitialContactId: str):
        return {"Attributes": {PROFILE_ID_ATTRIBUTE: PROFILE_ID}}


class FakeMemory:
    def __init__(self):
        self.calls: list[dict] = []
        self._n = 0

    def create_event(self, **kwargs):
        self.calls.append(kwargs)
        self._n += 1
        return {"event": {"eventId": f"evt-{self._n}"}}


class FakeDynamoTable:
    """No-op dedup: every key is treated as new, mirroring a cold DynamoDB table."""

    def get_item(self, **kwargs):
        return {}

    def put_item(self, **kwargs):
        return {}


class FakeDynamoResource:
    def Table(self, name: str):
        return FakeDynamoTable()


@pytest.fixture()
def writer_module(monkeypatch) -> ModuleType:
    """Import writer/lambda_function.py fresh, with boto3 fully faked out."""
    monkeypatch.setenv("MEMORY_ID", "example_memory-AbCdEf1234")
    monkeypatch.setenv("CONNECT_INSTANCE_ID", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    monkeypatch.setenv("DEDUP_TABLE_NAME", "memory-writer-dedup-test")

    import boto3  # the real package; only .client/.resource are monkeypatched below

    fake_clients: dict[str, Any] = {
        "qconnect": FakeQConnect(),
        "connect": FakeConnect(),
        "bedrock-agentcore": FakeMemory(),
    }
    monkeypatch.setattr(boto3, "client", lambda name, *a, **k: fake_clients[name])
    monkeypatch.setattr(boto3, "resource", lambda name, *a, **k: FakeDynamoResource())

    module_name = "memory_writer_lambda_function"
    sys.modules.pop(module_name, None)  # in case a previous test left it cached
    spec = importlib.util.spec_from_file_location(module_name, WRITER_DIR / "lambda_function.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    module._fake_memory = fake_clients["bedrock-agentcore"]  # convenience handle for assertions
    yield module
    sys.modules.pop(module_name, None)


def _make_cw_event(events: list[dict], filter_name: str = "memory-writer") -> dict:
    payload = {
        "messageType": "DATA_MESSAGE",
        "owner": "123456789012",
        "logGroup": "/aws/connect/ai-agent/example-connect-instance",
        "logStream": "test-stream",
        "subscriptionFilters": [filter_name],
        "logEvents": [
            {"id": str(i), "timestamp": e.get("event_timestamp", 0), "message": json.dumps(e)}
            for i, e in enumerate(events)
        ],
    }
    compressed = gzip.compress(json.dumps(payload).encode())
    return {"awslogs": {"data": base64.b64encode(compressed).decode()}}


@pytest.fixture()
def trace_events() -> list[dict]:
    return json.loads((FIXTURES_DIR / "trace_events.json").read_text())


def test_non_cloudwatch_event_is_a_no_op(writer_module):
    result = writer_module.lambda_handler({"not": "awslogs"}, None)
    assert result["statusCode"] == 200
    assert writer_module._fake_memory.calls == []


def test_writes_one_create_event_for_the_fixture_session(writer_module, trace_events):
    event = _make_cw_event(trace_events)
    result = writer_module.lambda_handler(event, None)

    assert result["statusCode"] == 200
    assert "1 written" in result["body"]
    memory = writer_module._fake_memory
    assert len(memory.calls) == 1
    call = memory.calls[0]
    assert call["actorId"] == PROFILE_ID
    assert call["sessionId"] == CONTACT_ID
    assert call["payload"] == [
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


def test_redelivered_batch_is_deduped_on_second_invocation(writer_module, trace_events, monkeypatch):
    # Override the fake table to actually track keys across two calls, simulating
    # at-least-once CloudWatch redelivery of the identical batch.
    seen: set[str] = set()

    class TrackingTable:
        def get_item(self, *, Key, **kwargs):
            return {"Item": Key} if Key["orchestration_id"] in seen else {}

        def put_item(self, *, Item, ConditionExpression):
            key = Item["orchestration_id"]
            if key in seen:
                err = Exception("duplicate")
                err.response = {"Error": {"Code": "ConditionalCheckFailedException"}}
                raise err
            seen.add(key)
            return {}

    import boto3

    monkeypatch.setattr(boto3, "resource", lambda name, *a, **k: type(
        "R", (), {"Table": staticmethod(lambda n: TrackingTable())}
    )())

    # Re-import so the tracking table is picked up at module-load time.
    module_name = "memory_writer_lambda_function_redelivery"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, WRITER_DIR / "lambda_function.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    event = _make_cw_event(trace_events)
    first = module.lambda_handler(event, None)
    second = module.lambda_handler(event, None)

    assert "1 written" in first["body"]
    assert "1 written" not in second["body"]
    assert "1 skipped" in second["body"]

    sys.modules.pop(module_name, None)


def test_unresolvable_identity_skips_without_error(writer_module, trace_events, monkeypatch):
    class NoProfileConnect:
        def get_contact_attributes(self, *, InstanceId, InitialContactId):
            return {"Attributes": {}}

    writer_module._connect = NoProfileConnect()
    writer_module._writer._connect = NoProfileConnect()

    result = writer_module.lambda_handler(_make_cw_event(trace_events), None)

    assert result["statusCode"] == 200
    assert "0 written" in result["body"]
    assert "1 skipped" in result["body"]
    assert writer_module._fake_memory.calls == []


def test_create_event_failure_raises_so_lambda_retries_and_eventually_dlqs(
    writer_module, trace_events
):
    class FailingMemory:
        def create_event(self, **kwargs):
            raise RuntimeError("ServiceUnavailableException")

    writer_module._memory = FailingMemory()
    writer_module._writer._memory = FailingMemory()

    with pytest.raises(RuntimeError, match="1/1 session"):
        writer_module.lambda_handler(_make_cw_event(trace_events), None)


def test_multi_session_batch_processes_every_session_before_raising(writer_module, trace_events):
    """A batch with a good session and a bad session must not let the bad one block the
    good one - see the module docstring's 'process everything, then raise once' contract."""
    good_events = json.loads(json.dumps(trace_events))  # deep copy
    bad_events = json.loads(json.dumps(trace_events))
    other_qic_session = "11111111-1111-1111-1111-111111111111"
    other_contact = "22222222-2222-2222-2222-222222222222"
    for e in bad_events:
        e["session_id"] = other_qic_session
        if e.get("session_name"):
            e["session_name"] = other_contact

    class SessionAwareQConnect:
        """Maps QIC sessionId -> contactId, unlike the module-level FakeQConnect fixture
        (which is single-session). Only the ORCHESTRATION_MESSAGE event lacks
        session_name inline, so both sessions' identity resolution goes through here."""

        def get_session(self, *, assistantId: str, sessionId: str):
            contact = other_contact if sessionId == other_qic_session else CONTACT_ID
            return {"session": {"sessionId": sessionId, "name": contact}}

    class SelectivelyFailingMemory:
        def __init__(self):
            self.calls: list[dict] = []

        def create_event(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs["sessionId"] == other_contact:
                raise RuntimeError("boom")
            return {"event": {"eventId": "evt-good"}}

    failing_memory = SelectivelyFailingMemory()
    writer_module._memory = failing_memory
    writer_module._writer._memory = failing_memory
    writer_module._qconnect = SessionAwareQConnect()
    writer_module._writer._qconnect = SessionAwareQConnect()

    combined = good_events + bad_events
    with pytest.raises(RuntimeError, match="1/2 session"):
        writer_module.lambda_handler(_make_cw_event(combined), None)

    # Both sessions were attempted despite one failing.
    assert len(failing_memory.calls) == 2
    called_sessions = {c["sessionId"] for c in failing_memory.calls}
    assert called_sessions == {CONTACT_ID, other_contact}


def test_ai_agent_trace_event_in_batch_does_not_crash_the_handler(writer_module, trace_events):
    """A TRANSCRIPT_AI_AGENT_TRACE event (``span`` delivered as a string) in the batch is
    skipped and does not affect the conversation turns written for the same session."""
    trace_event = {
        "assistant_id": "11111111-2222-3333-4444-111122223333",
        "event_timestamp": 1781251390800,
        "event_type": "TRANSCRIPT_AI_AGENT_TRACE",
        "session_id": trace_events[0]["session_id"],
        "ai_agent_id": "a1a9e000-0000-4000-8000-000000000001",
        "span": "{span_id=example, span_name=inference}",
    }
    # Interleave it among the fixture's events, matching how CloudWatch delivers real batches
    # (trace events appear alongside orchestration/utterance events for the same session).
    events_with_trace = [trace_events[0], trace_event] + trace_events[1:]

    result = writer_module.lambda_handler(_make_cw_event(events_with_trace), None)

    assert result["statusCode"] == 200
    assert "1 written" in result["body"]
    memory = writer_module._fake_memory
    assert len(memory.calls) == 1
    assert memory.calls[0]["sessionId"] == CONTACT_ID
