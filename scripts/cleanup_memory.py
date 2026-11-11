#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Delete everything AgentCore Memory holds for one customer, or list the customers it holds.

Per-customer deletion removes, for one Customer Profiles ``profileId`` (the memory
``actorId``):

1. the raw conversation events of every session (contact) of that actor, and
2. every long-term memory record under ``connect/profile/<profileId>/`` (semantic facts,
   contact recaps and the per-contact summaries).

Records are found with ``ListMemoryRecords`` and ``namespacePath``, which returns every
record in the namespace hierarchy below that path. Each record is checked against the
expected prefix before it is deleted. After deleting, the script lists again and exits
non-zero if anything is still listed after it has re-listed for up to 60 seconds. Every AWS
error stops the script with a non-zero exit code. Session ids can stay listed after all
their events are deleted.

Run it after the customer's contacts have ended: a conversation that is still in progress
can write new events after the script finishes.

Usage:
    python3 scripts/cleanup_memory.py --memory-id <memory-id> --region <region> \\
        --profile-id <customer-profile-id> [--aws-profile <profile>] [--dry-run] [--yes]

    python3 scripts/cleanup_memory.py --memory-id <memory-id> --region <region> \\
        --list-actors [--aws-profile <profile>]

Required permissions on the memory resource: bedrock-agentcore:ListActors, ListSessions,
ListEvents, DeleteEvent, ListMemoryRecords and BatchDeleteMemoryRecords.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator

PROFILE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
BATCH_DELETE_LIMIT = 100
VERIFY_TIMEOUT_SECONDS = 60
VERIFY_POLL_SECONDS = 5


class CleanupError(RuntimeError):
    """A delete did not complete. The message says what is left."""


def profile_namespace_path(profile_id: str) -> str:
    """Parent namespace of every record the memory strategies write for one customer."""
    return f"connect/profile/{profile_id}"


def _normalise(namespace: str) -> str:
    return "/" + namespace.strip("/") + "/"


def record_belongs_to_profile(record: dict[str, Any], profile_id: str) -> bool:
    """True when every namespace of the record is inside the customer's hierarchy."""
    expected = _normalise(profile_namespace_path(profile_id))
    namespaces = record.get("namespaces") or []
    return bool(namespaces) and all(_normalise(ns).startswith(expected) for ns in namespaces)


def _paginate(client: Any, operation: str, result_key: str, **kwargs: Any) -> Iterator[dict]:
    paginator = client.get_paginator(operation)
    for page in paginator.paginate(**kwargs):
        yield from page.get(result_key, [])


def list_actor_ids(client: Any, memory_id: str) -> list[str]:
    return [a["actorId"] for a in _paginate(client, "list_actors", "actorSummaries", memoryId=memory_id)]


def list_session_ids(client: Any, memory_id: str, profile_id: str) -> list[str]:
    return [
        s["sessionId"]
        for s in _paginate(
            client, "list_sessions", "sessionSummaries", memoryId=memory_id, actorId=profile_id
        )
    ]


def list_event_ids(client: Any, memory_id: str, profile_id: str, session_id: str) -> list[str]:
    return [
        e["eventId"]
        for e in _paginate(
            client,
            "list_events",
            "events",
            memoryId=memory_id,
            actorId=profile_id,
            sessionId=session_id,
            includePayloads=False,
        )
    ]


def list_profile_records(client: Any, memory_id: str, profile_id: str) -> list[dict[str, Any]]:
    """Long-term records of one customer. Raises if the service returns a record outside
    the customer's namespace hierarchy, so nothing outside it is ever deleted."""
    records = list(
        _paginate(
            client,
            "list_memory_records",
            "memoryRecordSummaries",
            memoryId=memory_id,
            namespacePath=profile_namespace_path(profile_id),
        )
    )
    foreign = [r["memoryRecordId"] for r in records if not record_belongs_to_profile(r, profile_id)]
    if foreign:
        raise CleanupError(
            f"ListMemoryRecords returned {len(foreign)} record(s) outside "
            f"{profile_namespace_path(profile_id)}/; refusing to delete: {foreign[:5]}"
        )
    return records


@dataclass
class Inventory:
    events: dict[str, list[str]] = field(default_factory=dict)
    """session id -> event ids"""
    records: list[dict[str, Any]] = field(default_factory=list)

    @property
    def event_count(self) -> int:
        return sum(len(ids) for ids in self.events.values())


def take_inventory(client: Any, memory_id: str, profile_id: str) -> Inventory:
    events = {
        session_id: list_event_ids(client, memory_id, profile_id, session_id)
        for session_id in list_session_ids(client, memory_id, profile_id)
    }
    records = list_profile_records(client, memory_id, profile_id)
    return Inventory(events=events, records=records)


def _chunks(items: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def delete_profile_memory(
    client: Any,
    memory_id: str,
    profile_id: str,
    *,
    log: Callable[[str], None] = print,
    verify_timeout_seconds: float = VERIFY_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Inventory:
    """Delete one customer's raw events and long-term records, then verify none are left.

    Returns the inventory that was deleted. Raises ``CleanupError`` when the service
    reports a failed record deletion or when anything is left afterwards. Any other AWS
    error propagates unchanged.
    """
    inventory = take_inventory(client, memory_id, profile_id)
    log(f"found {inventory.event_count} raw event(s) in {len(inventory.events)} session(s) "
        f"and {len(inventory.records)} long-term record(s)")

    # Raw events first, so no new extraction can start from them while records are deleted.
    for session_id, event_ids in inventory.events.items():
        for event_id in event_ids:
            client.delete_event(
                memoryId=memory_id, actorId=profile_id, sessionId=session_id, eventId=event_id
            )
    log(f"deleted {inventory.event_count} raw event(s)")

    deleted_records = 0
    for chunk in _chunks(inventory.records, BATCH_DELETE_LIMIT):
        response = client.batch_delete_memory_records(
            memoryId=memory_id,
            records=[{"memoryRecordId": r["memoryRecordId"]} for r in chunk],
        )
        failed = response.get("failedRecords") or []
        if failed:
            details = "; ".join(
                f"{f.get('memoryRecordId')}: {f.get('errorCode')} {f.get('errorMessage')}"
                for f in failed[:5]
            )
            raise CleanupError(f"{len(failed)} record deletion(s) failed: {details}")
        deleted_records += len(response.get("successfulRecords") or [])
    log(f"deleted {deleted_records} long-term record(s)")

    # Re-list for up to verify_timeout_seconds before reporting leftovers.
    deadline = clock() + verify_timeout_seconds
    while True:
        remaining = take_inventory(client, memory_id, profile_id)
        if not (remaining.event_count or remaining.records) or clock() >= deadline:
            break
        sleep(VERIFY_POLL_SECONDS)
    if remaining.event_count or remaining.records:
        raise CleanupError(
            f"still present after deletion: {remaining.event_count} raw event(s), "
            f"{len(remaining.records)} long-term record(s). A contact may still be in "
            "progress or extraction may still be running; run the script again later."
        )
    log("verified: no raw events and no long-term records left for this customer")
    return inventory


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--memory-id", required=True, help="AgentCore Memory resource id")
    parser.add_argument("--region", required=True, help="AWS Region of the memory resource")
    parser.add_argument("--aws-profile", default=None, help="AWS CLI profile (optional)")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--profile-id", help="Customer Profiles profileId (memory actorId)")
    mode.add_argument("--list-actors", action="store_true", help="List actor ids, delete nothing")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *, client: Any = None) -> int:
    args = _parse_args(argv)

    try:
        return _run(args, client)
    except CleanupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - any AWS error stops the script
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def _run(args: argparse.Namespace, client: Any) -> int:
    if client is None:
        import boto3

        session = boto3.Session(profile_name=args.aws_profile, region_name=args.region)
        client = session.client("bedrock-agentcore")

    if args.list_actors:
        actors = list_actor_ids(client, args.memory_id)
        for actor_id in actors:
            print(actor_id)
        print(f"{len(actors)} actor(s)", file=sys.stderr)
        return 0

    profile_id = args.profile_id.strip().lower()
    if not PROFILE_ID_PATTERN.match(profile_id):
        print("--profile-id must be a Customer Profiles profileId (32 hex characters)",
              file=sys.stderr)
        return 2

    if args.dry_run:
        inventory = take_inventory(client, args.memory_id, profile_id)
        print(f"[dry run] would delete {inventory.event_count} raw event(s) in "
              f"{len(inventory.events)} session(s) and {len(inventory.records)} long-term "
              f"record(s) for {profile_id}")
        return 0

    if not args.yes:
        answer = input(f"Permanently delete all memory for {profile_id}? Type 'delete': ")
        if answer.strip() != "delete":
            print("aborted", file=sys.stderr)
            return 1

    delete_profile_memory(client, args.memory_id, profile_id)
    print(f"all memory for {profile_id} deleted; the Customer Profiles record is unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
