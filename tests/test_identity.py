# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the trace-event -> actor_id resolution chain.

Fixtures in tests/fixtures/trace_events.json are real events captured from
/aws/connect/ai-agent/example-connect-instance.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from connect_memory.identity import (
    PROFILE_ID_ATTRIBUTE,
    ResolvedIdentity,
    SkipReason,
    contact_id_from_trace_event,
    is_valid_profile_id,
    read_namespaces,
    recap_namespace,
    resolve_actor_id,
    semantic_namespace,
    summary_namespace,
)

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "trace_events.json").read_text())
BY_TYPE = {e["event_type"]: e for e in FIXTURES}

CONTACT_ID = "c0a7ac71-0000-4000-8000-000000000001"
QIC_SESSION_ID = "5e551011-0000-4000-8000-000000000001"
PROFILE_ID = "0123456789abcdef0123456789abcdef"
INSTANCE_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class FakeQConnect:
    def __init__(self, session_name: str | None = CONTACT_ID, raises: Exception | None = None):
        self._session_name = session_name
        self._raises = raises
        self.calls: list[dict] = []

    def get_session(self, *, assistantId: str, sessionId: str):
        self.calls.append({"assistantId": assistantId, "sessionId": sessionId})
        if self._raises:
            raise self._raises
        session: dict = {"sessionId": sessionId}
        if self._session_name is not None:
            session["name"] = self._session_name
        return {"session": session}


class FakeConnect:
    def __init__(self, attributes: dict | None = None, raises: Exception | None = None):
        self._attributes = {PROFILE_ID_ATTRIBUTE: PROFILE_ID} if attributes is None else attributes
        self._raises = raises
        self.calls: list[dict] = []

    def get_contact_attributes(self, *, InstanceId: str, InitialContactId: str):
        self.calls.append({"InstanceId": InstanceId, "InitialContactId": InitialContactId})
        if self._raises:
            raise self._raises
        return {"Attributes": self._attributes}


def _resolve(event, qconnect=None, connect=None):
    return resolve_actor_id(
        event,
        qconnect=qconnect or FakeQConnect(),
        connect=connect or FakeConnect(),
        connect_instance_id=INSTANCE_ID,
    )


# --- fixture sanity -------------------------------------------------------------------

def test_fixtures_cover_the_three_event_shapes():
    assert set(BY_TYPE) == {
        "TRANSCRIPT_CREATE_SESSION",
        "TRANSCRIPT_UTTERANCE",
        "TRANSCRIPT_ORCHESTRATION_MESSAGE",
    }


def test_every_fixture_event_carries_session_and_assistant_id():
    # This is the invariant the whole resolution chain rests on.
    for event in FIXTURES:
        assert event.get("session_id"), event["event_type"]
        assert event.get("assistant_id"), event["event_type"]


def test_only_create_session_carries_the_contact_id_inline():
    assert contact_id_from_trace_event(BY_TYPE["TRANSCRIPT_CREATE_SESSION"]) == CONTACT_ID
    assert contact_id_from_trace_event(BY_TYPE["TRANSCRIPT_UTTERANCE"]) is None
    assert contact_id_from_trace_event(BY_TYPE["TRANSCRIPT_ORCHESTRATION_MESSAGE"]) is None


# --- happy path ----------------------------------------------------------------------

def test_create_session_resolves_without_calling_get_session():
    qconnect = FakeQConnect()
    identity, reason = _resolve(BY_TYPE["TRANSCRIPT_CREATE_SESSION"], qconnect=qconnect)
    assert reason is None
    assert identity == ResolvedIdentity(
        actor_id=PROFILE_ID, session_id=CONTACT_ID, qic_session_id=QIC_SESSION_ID
    )
    assert qconnect.calls == [], "session_name was inline; GetSession should be skipped"


@pytest.mark.parametrize(
    "event_type", ["TRANSCRIPT_UTTERANCE", "TRANSCRIPT_ORCHESTRATION_MESSAGE"]
)
def test_other_events_resolve_via_get_session(event_type):
    qconnect, connect = FakeQConnect(), FakeConnect()
    identity, reason = _resolve(BY_TYPE[event_type], qconnect=qconnect, connect=connect)
    assert reason is None
    assert identity.actor_id == PROFILE_ID
    assert identity.session_id == CONTACT_ID
    assert qconnect.calls == [
        {"assistantId": BY_TYPE[event_type]["assistant_id"], "sessionId": QIC_SESSION_ID}
    ]
    assert connect.calls == [{"InstanceId": INSTANCE_ID, "InitialContactId": CONTACT_ID}]


def test_session_id_is_the_contact_id_not_the_qic_session_id():
    # Getting this backwards would silo memory per AI agent session and break
    # the summary namespace, so pin it explicitly.
    identity, _ = _resolve(BY_TYPE["TRANSCRIPT_UTTERANCE"])
    assert identity.session_id == CONTACT_ID
    assert identity.session_id != identity.qic_session_id


# --- skip paths: must never invent an actor_id ----------------------------------------

def test_missing_session_id_skips():
    identity, reason = _resolve({"event_type": "X", "assistant_id": "a"})
    assert identity is None and reason == SkipReason.NO_SESSION_ID


def test_missing_assistant_id_skips():
    identity, reason = _resolve({"event_type": "X", "session_id": QIC_SESSION_ID})
    assert identity is None and reason == SkipReason.NO_ASSISTANT_ID


def test_get_session_failure_skips():
    identity, reason = _resolve(
        BY_TYPE["TRANSCRIPT_UTTERANCE"],
        qconnect=FakeQConnect(raises=RuntimeError("ResourceNotFoundException")),
    )
    assert identity is None and reason == SkipReason.SESSION_NOT_FOUND


def test_session_without_name_skips():
    identity, reason = _resolve(
        BY_TYPE["TRANSCRIPT_UTTERANCE"], qconnect=FakeQConnect(session_name=None)
    )
    assert identity is None and reason == SkipReason.NO_CONTACT_ID_ON_SESSION


def test_get_contact_attributes_failure_skips():
    identity, reason = _resolve(
        BY_TYPE["TRANSCRIPT_UTTERANCE"],
        connect=FakeConnect(raises=RuntimeError("ResourceNotFoundException")),
    )
    assert identity is None and reason == SkipReason.CONTACT_NOT_FOUND


def test_contact_without_profile_attribute_skips():
    identity, reason = _resolve(
        BY_TYPE["TRANSCRIPT_UTTERANCE"],
        connect=FakeConnect(attributes={"sentiment": "frustrated"}),
    )
    assert identity is None and reason == SkipReason.NO_PROFILE_ATTRIBUTE


@pytest.mark.parametrize("bad", ["", "not-a-profile-id", "23fb8d62bb5e41e0970fda639a8440",
                                 "0123456789abcdef0123456789abcdef0", "ZZfb8d62bb5e41e0970fda639a8440f0"])
def test_malformed_profile_id_skips(bad):
    identity, reason = _resolve(
        BY_TYPE["TRANSCRIPT_UTTERANCE"],
        connect=FakeConnect(attributes={PROFILE_ID_ATTRIBUTE: bad}),
    )
    assert identity is None
    assert reason in (SkipReason.MALFORMED_PROFILE_ID, SkipReason.NO_PROFILE_ATTRIBUTE)


# --- profile id validation ------------------------------------------------------------

def test_real_profile_id_is_valid():
    assert is_valid_profile_id(PROFILE_ID)


@pytest.mark.parametrize("bad", [None, "", "abc", PROFILE_ID + "0", PROFILE_ID.replace("2", "g", 1)])
def test_invalid_profile_ids_rejected(bad):
    assert not is_valid_profile_id(bad)


# --- namespaces -----------------------------------------------------------------------

def test_semantic_namespace_excludes_contact_id():
    ns = semantic_namespace(PROFILE_ID)
    assert ns == f"connect/profile/{PROFILE_ID}/semantic"
    assert CONTACT_ID not in ns, "cross-contact recall requires no contact id in the namespace"


def test_recap_namespace_is_flat():
    ns = recap_namespace(PROFILE_ID)
    assert ns == f"connect/profile/{PROFILE_ID}/recap"
    assert CONTACT_ID not in ns, "recap must be retrievable without knowing a prior contact id"


def test_summary_namespace_includes_contact_id():
    # {sessionId} is mandatory for SUMMARIZATION strategies - the service rejects a flat one.
    assert summary_namespace(PROFILE_ID, CONTACT_ID) == (
        f"connect/profile/{PROFILE_ID}/summary/{CONTACT_ID}"
    )


def test_read_namespaces_are_exact_and_contact_independent():
    # Guards the finding that prefix/wildcard namespaces silently return zero records:
    # every namespace the read path uses must be fully resolved from the actor id alone.
    namespaces = read_namespaces(PROFILE_ID)
    assert namespaces == (semantic_namespace(PROFILE_ID), recap_namespace(PROFILE_ID))
    for ns in namespaces:
        assert "*" not in ns, "wildcards silently match nothing"
        assert not ns.endswith("/"), "a trailing slash is a prefix, which matches nothing"


def test_read_namespaces_excludes_summary():
    # Built-in summary records need a known contactId, so they are off the read path.
    assert summary_namespace(PROFILE_ID, CONTACT_ID) not in read_namespaces(PROFILE_ID)


@pytest.mark.parametrize("value", ["none", "NONE", " none ", ""])
def test_cleared_profile_attribute_is_skipped_as_missing(value):
    from connect_memory.identity import resolve_actor_id, SkipReason as _SR

    class _QC:
        def get_session(self, **_):
            return {"session": {"name": CONTACT_ID}}

    class _C:
        def get_contact_attributes(self, **_):
            return {"Attributes": {PROFILE_ID_ATTRIBUTE: value}}

    identity, reason = resolve_actor_id(
        {"session_id": QIC_SESSION_ID, "assistant_id": "asst"},
        qconnect=_QC(), connect=_C(), connect_instance_id="i",
    )
    assert identity is None
    assert reason == _SR.NO_PROFILE_ATTRIBUTE


def test_upper_case_profile_id_is_rejected():
    from connect_memory.identity import is_valid_profile_id

    assert is_valid_profile_id("0123456789abcdef0123456789abcdef")
    assert not is_valid_profile_id("0123456789ABCDEF0123456789ABCDEF")
