# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Resolve a Connect AI Agent trace event to an AgentCore Memory actor_id.

The actor_id is the Customer Profiles ``profileId``. Trace events do not carry it, so it
is resolved with the chain below (see ``docs/architecture.md``, "Identity"):

    trace event ──> session_id + assistant_id     (present on every trace event)
      └─ qconnect.GetSession        ──> session.name        == Connect contactId
         └─ connect.GetContactAttributes ──> attributes["customerProfileId"] == actor_id

The ``customerProfileId`` attribute is trusted as the customer's identity. The contact
flow must set it only from a verified Customer Profiles match and clear it on every other
path (docs/connect-flow-instructions.md, section 1).

No profile resolved => return None. The caller MUST skip the memory write entirely rather
than fall back to a synthetic key: a synthetic actor_id creates a per-contact memory silo
that can never produce cross-contact recall, with records that cannot be attributed to a
real customer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

logger = logging.getLogger(__name__)

# User-defined contact attribute the flow sets from $.Customer.ProfileId after a verified
# Get profile match, and clears on every other path.
PROFILE_ID_ATTRIBUTE = "customerProfileId"

# AgentCore actor_id pattern is [a-zA-Z0-9][a-zA-Z0-9-_/]*(?::[a-zA-Z0-9-_/]+)*[a-zA-Z0-9-_/]*
# A Customer Profiles profileId is 32 lowercase hex chars, which satisfies it. We validate
# defensively so a malformed attribute value fails here rather than at CreateEvent time.
_PROFILE_ID_LEN = 32


class SkipReason:
    """Why a trace event produced no actor_id. Emit as a metric dimension."""

    NO_SESSION_ID = "no_session_id"
    NO_ASSISTANT_ID = "no_assistant_id"
    SESSION_NOT_FOUND = "session_not_found"
    NO_CONTACT_ID_ON_SESSION = "no_contact_id_on_session"
    CONTACT_NOT_FOUND = "contact_not_found"
    NO_PROFILE_ATTRIBUTE = "no_profile_attribute"
    MALFORMED_PROFILE_ID = "malformed_profile_id"


@dataclass(frozen=True)
class ResolvedIdentity:
    """A trace event mapped onto the AgentCore Memory identity axes."""

    actor_id: str
    """Customer Profiles profileId. Becomes AgentCore actorId."""

    session_id: str
    """Connect contactId. Becomes AgentCore sessionId (NOT the AI agent session id)."""

    qic_session_id: str
    """AI agent session id, kept for correlation back to the trace stream."""


class QConnectClient(Protocol):
    def get_session(self, *, assistantId: str, sessionId: str) -> Mapping[str, Any]: ...


class ConnectClient(Protocol):
    def get_contact_attributes(
        self, *, InstanceId: str, InitialContactId: str
    ) -> Mapping[str, Any]: ...


# Value the flow sets at flow start to clear the attribute (a flow cannot set an empty
# value). Treated exactly like a missing attribute.
UNBOUND_PROFILE_VALUE = "none"


def is_unbound_profile_id(value: str | None) -> bool:
    """True when no customer is bound: missing, empty, or the ``UNBOUND_PROFILE_VALUE``."""
    return not value or value.strip().lower() == UNBOUND_PROFILE_VALUE


def is_valid_profile_id(value: str | None) -> bool:
    """True when the value looks like a Customer Profiles profileId (32 lowercase hex)."""
    if not value or len(value) != _PROFILE_ID_LEN:
        return False
    return all(c in "0123456789abcdef" for c in value)


def contact_id_from_trace_event(event: Mapping[str, Any]) -> str | None:
    """Return the Connect contactId if this specific trace event already carries it.

    Only TRANSCRIPT_CREATE_SESSION events carry ``session_name``, so this is an
    optimisation, not a resolution path. Everything else goes via GetSession.
    """
    name = event.get("session_name")
    return name if isinstance(name, str) and name else None


def resolve_actor_id(
    event: Mapping[str, Any],
    *,
    qconnect: QConnectClient,
    connect: ConnectClient,
    connect_instance_id: str,
) -> tuple[ResolvedIdentity | None, str | None]:
    """Resolve a trace event to a ResolvedIdentity.

    Returns ``(identity, None)`` on success or ``(None, skip_reason)`` when the event cannot
    be attributed to a real customer profile. Never invents an actor_id.
    """
    qic_session_id = event.get("session_id")
    if not qic_session_id:
        return None, SkipReason.NO_SESSION_ID

    contact_id = contact_id_from_trace_event(event)

    if contact_id is None:
        assistant_id = event.get("assistant_id")
        if not assistant_id:
            return None, SkipReason.NO_ASSISTANT_ID
        try:
            session = qconnect.get_session(
                assistantId=assistant_id, sessionId=qic_session_id
            ).get("session", {})
        except Exception as exc:  # noqa: BLE001 - any failure means "cannot attribute"
            logger.warning(
                "GetSession failed for qic_session=%s: %s: %s",
                qic_session_id, type(exc).__name__, exc,
            )
            return None, SkipReason.SESSION_NOT_FOUND
        contact_id = session.get("name")
        if not contact_id:
            return None, SkipReason.NO_CONTACT_ID_ON_SESSION

    try:
        attributes = connect.get_contact_attributes(
            InstanceId=connect_instance_id, InitialContactId=contact_id
        ).get("Attributes", {})
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "GetContactAttributes failed for contact=%s: %s: %s",
            contact_id, type(exc).__name__, exc,
        )
        return None, SkipReason.CONTACT_NOT_FOUND

    profile_id = attributes.get(PROFILE_ID_ATTRIBUTE)
    if is_unbound_profile_id(profile_id):
        logger.info(
            "contact=%s has no %s attribute - skipping memory write",
            contact_id, PROFILE_ID_ATTRIBUTE,
        )
        return None, SkipReason.NO_PROFILE_ATTRIBUTE

    if not is_valid_profile_id(profile_id):
        logger.warning(
            "contact=%s has a malformed %s value - skipping memory write",
            contact_id, PROFILE_ID_ATTRIBUTE,
        )
        return None, SkipReason.MALFORMED_PROFILE_ID

    return (
        ResolvedIdentity(
            actor_id=profile_id,
            session_id=contact_id,
            qic_session_id=qic_session_id,
        ),
        None,
    )


def semantic_namespace(actor_id: str) -> str:
    """Cross-contact durable-fact namespace. Deliberately excludes the contact id."""
    return f"connect/profile/{actor_id}/semantic"


def recap_namespace(actor_id: str) -> str:
    """Contact-recap namespace (CUSTOM strategy). Flat, so it is retrievable without
    knowing any prior contact id."""
    return f"connect/profile/{actor_id}/recap"


def summary_namespace(actor_id: str, session_id: str) -> str:
    """Built-in SUMMARIZATION namespace.

    The namespace contains the contact id (``{sessionId}``). At contact start the reader
    does not know earlier contact ids, so it reads ``recap_namespace`` instead. The
    deletion script removes these records with the rest of the customer's hierarchy.
    """
    return f"connect/profile/{actor_id}/summary/{session_id}"


def read_namespaces(actor_id: str) -> tuple[str, ...]:
    """Namespaces the contact-start read path queries.

    The read path issues one RetrieveMemoryRecords call per exact namespace name, in
    parallel, and does not rely on prefix or wildcard namespace matching.
    """
    return (semantic_namespace(actor_id), recap_namespace(actor_id))
