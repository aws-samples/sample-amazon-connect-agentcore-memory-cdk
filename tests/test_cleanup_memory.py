# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for ``scripts/cleanup_memory.py`` against an in-memory fake of the
bedrock-agentcore data-plane client. No live AWS calls."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "cleanup_memory.py"
_spec = importlib.util.spec_from_file_location("cleanup_memory", SCRIPT)
cleanup = importlib.util.module_from_spec(_spec)
sys.modules["cleanup_memory"] = cleanup
_spec.loader.exec_module(cleanup)

MEMORY_ID = "example_memory-AbCdEf1234"
PROFILE = "0123456789abcdef0123456789abcdef"
OTHER = "fedcba9876543210fedcba9876543210"


def _record(rid: str, profile: str, kind: str) -> dict:
    return {"memoryRecordId": rid, "namespaces": [f"/connect/profile/{profile}/{kind}/"]}


class _Paginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        return self._pages(**kwargs)


class FakeMemory:
    """Holds events per (actor, session) and records; pages of two to exercise pagination."""

    def __init__(self, *, events=None, records=None, fail_record_ids=(), refill=False,
                 extra_records=()):
        self.events: dict[tuple[str, str], list[str]] = {k: list(v) for k, v in (events or {}).items()}
        self.records: list[dict] = list(records or [])
        self.fail_record_ids = set(fail_record_ids)
        self.refill = refill
        self.extra_records = list(extra_records)
        self.deleted_events: list[tuple[str, str, str]] = []
        self.batch_calls: list[list[str]] = []
        self.list_record_calls: list[dict] = []

    @staticmethod
    def _pages_of(items, key):
        items = list(items)
        if not items:
            return [{key: []}]
        return [{key: items[i:i + 2]} for i in range(0, len(items), 2)]

    def get_paginator(self, operation):
        if operation == "list_actors":
            actors = sorted({a for a, _ in self.events})
            return _Paginator(lambda **k: self._pages_of(
                [{"actorId": a} for a in actors], "actorSummaries"))
        if operation == "list_sessions":
            return _Paginator(lambda **k: self._pages_of(
                [{"sessionId": s} for a, s in self.events if a == k["actorId"]],
                "sessionSummaries"))
        if operation == "list_events":
            return _Paginator(lambda **k: self._pages_of(
                [{"eventId": e} for e in self.events.get((k["actorId"], k["sessionId"]), [])],
                "events"))
        if operation == "list_memory_records":
            def pages(**k):
                self.list_record_calls.append(k)
                prefix = "/" + k["namespacePath"].strip("/") + "/"
                hits = [r for r in self.records if r["namespaces"][0].startswith(prefix)]
                return self._pages_of(hits + self.extra_records, "memoryRecordSummaries")
            return _Paginator(pages)
        raise AssertionError(f"unexpected paginator {operation}")

    def delete_event(self, *, memoryId, actorId, sessionId, eventId):
        self.events[(actorId, sessionId)].remove(eventId)
        self.deleted_events.append((actorId, sessionId, eventId))
        return {}

    def batch_delete_memory_records(self, *, memoryId, records):
        ids = [r["memoryRecordId"] for r in records]
        self.batch_calls.append(ids)
        ok, failed = [], []
        for rid in ids:
            if rid in self.fail_record_ids:
                failed.append({"memoryRecordId": rid, "status": "FAILED", "errorCode": 500,
                               "errorMessage": "boom"})
            else:
                ok.append({"memoryRecordId": rid, "status": "SUCCEEDED"})
                self.records = [r for r in self.records if r["memoryRecordId"] != rid]
        if self.refill:
            self.records.append(_record("mem-late", PROFILE, "semantic"))
            self.refill = False
        return {"successfulRecords": ok, "failedRecords": failed}


def _populated() -> FakeMemory:
    return FakeMemory(
        events={
            (PROFILE, "contact-1"): ["e1", "e2", "e3"],
            (PROFILE, "contact-2"): ["e4"],
            (OTHER, "contact-9"): ["e9"],
        },
        records=[
            _record("mem-1", PROFILE, "semantic"),
            _record("mem-2", PROFILE, "recap"),
            _record("mem-3", PROFILE, "summary/contact-1"),
            _record("mem-9", OTHER, "semantic"),
        ],
    )


def test_deletes_events_and_records_of_one_customer_only():
    memory = _populated()

    inventory = cleanup.delete_profile_memory(memory, MEMORY_ID, PROFILE, log=lambda _m: None)

    assert inventory.event_count == 4
    assert {r["memoryRecordId"] for r in inventory.records} == {"mem-1", "mem-2", "mem-3"}
    assert all(actor == PROFILE for actor, _s, _e in memory.deleted_events)
    assert memory.events[(OTHER, "contact-9")] == ["e9"]
    assert [r["memoryRecordId"] for r in memory.records] == ["mem-9"]
    assert memory.list_record_calls[0]["namespacePath"] == f"connect/profile/{PROFILE}"


def test_includes_per_contact_summary_records():
    memory = _populated()
    cleanup.delete_profile_memory(memory, MEMORY_ID, PROFILE, log=lambda _m: None)
    assert "mem-3" in memory.batch_calls[0]


def test_record_deletions_are_batched_at_the_service_limit():
    records = [_record(f"mem-{i:03d}", PROFILE, "semantic") for i in range(205)]
    memory = FakeMemory(records=records)

    cleanup.delete_profile_memory(memory, MEMORY_ID, PROFILE, log=lambda _m: None)

    assert [len(c) for c in memory.batch_calls] == [100, 100, 5]


def test_failed_record_deletion_raises_instead_of_reporting_success():
    memory = FakeMemory(records=[_record("mem-1", PROFILE, "semantic")], fail_record_ids={"mem-1"})

    with pytest.raises(cleanup.CleanupError, match="1 record deletion"):
        cleanup.delete_profile_memory(memory, MEMORY_ID, PROFILE, log=lambda _m: None)


def test_leftover_records_after_deletion_raise_once_the_wait_is_over():
    memory = FakeMemory(records=[_record("mem-1", PROFILE, "semantic")], refill=True)

    with pytest.raises(cleanup.CleanupError, match="still present"):
        cleanup.delete_profile_memory(memory, MEMORY_ID, PROFILE, log=lambda _m: None,
                                      verify_timeout_seconds=0, sleep=lambda _s: None)


def test_verification_waits_for_deletions_to_become_visible():
    class Lagging(FakeMemory):
        """List calls keep returning deleted records for the first two inventories."""

        def __init__(self, **kw):
            super().__init__(**kw)
            self._lag = 0
            self._ghosts = []

        def batch_delete_memory_records(self, *, memoryId, records):
            self._ghosts = [r for r in self.records]
            self._lag = 2
            return super().batch_delete_memory_records(memoryId=memoryId, records=records)

        def get_paginator(self, operation):
            if operation == "list_memory_records" and self._lag:
                self._lag -= 1
                ghosts = self._ghosts
                return _Paginator(lambda **k: [{"memoryRecordSummaries": ghosts}])
            return super().get_paginator(operation)

    memory = Lagging(records=[_record("mem-1", PROFILE, "semantic")])
    sleeps = []
    t = iter(range(1000))
    cleanup.delete_profile_memory(memory, MEMORY_ID, PROFILE, log=lambda _m: None,
                                  verify_timeout_seconds=60, sleep=sleeps.append,
                                  clock=lambda: next(t))
    assert len(sleeps) == 2


def test_record_outside_the_customer_hierarchy_stops_before_any_delete():
    memory = FakeMemory(
        events={(PROFILE, "contact-1"): ["e1"]},
        records=[_record("mem-1", PROFILE, "semantic")],
        extra_records=[_record("mem-9", OTHER, "semantic")],
    )

    with pytest.raises(cleanup.CleanupError, match="refusing to delete"):
        cleanup.delete_profile_memory(memory, MEMORY_ID, PROFILE, log=lambda _m: None)
    assert memory.deleted_events == []
    assert memory.batch_calls == []


def test_aws_errors_propagate():
    class Broken(FakeMemory):
        def delete_event(self, **kwargs):
            raise RuntimeError("AccessDeniedException")

    memory = Broken(events={(PROFILE, "contact-1"): ["e1"]})
    with pytest.raises(RuntimeError, match="AccessDenied"):
        cleanup.delete_profile_memory(memory, MEMORY_ID, PROFILE, log=lambda _m: None)


def test_main_rejects_a_malformed_profile_id():
    rc = cleanup.main(["--memory-id", MEMORY_ID, "--region", "us-east-1",
                       "--profile-id", "not-a-profile", "--yes"], client=FakeMemory())
    assert rc == 2


def test_main_dry_run_deletes_nothing(capsys):
    memory = _populated()
    rc = cleanup.main(["--memory-id", MEMORY_ID, "--region", "us-east-1",
                       "--profile-id", PROFILE, "--dry-run"], client=memory)
    assert rc == 0
    assert memory.deleted_events == [] and memory.batch_calls == []
    assert "4 raw event(s)" in capsys.readouterr().out


def test_main_returns_non_zero_when_deletion_is_incomplete():
    memory = FakeMemory(records=[_record("mem-1", PROFILE, "semantic")], fail_record_ids={"mem-1"})
    rc = cleanup.main(["--memory-id", MEMORY_ID, "--region", "us-east-1",
                       "--profile-id", PROFILE, "--yes"], client=memory)
    assert rc == 1


def test_main_list_actors(capsys):
    rc = cleanup.main(["--memory-id", MEMORY_ID, "--region", "us-east-1", "--list-actors"],
                      client=_populated())
    assert rc == 0
    assert capsys.readouterr().out.split() == sorted([PROFILE, OTHER])


def test_main_returns_non_zero_on_an_aws_error(capsys):
    class Broken(FakeMemory):
        def get_paginator(self, operation):
            raise RuntimeError("ExpiredTokenException")

    rc = cleanup.main(["--memory-id", MEMORY_ID, "--region", "us-east-1",
                       "--profile-id", PROFILE, "--yes"], client=Broken())
    assert rc == 1
    assert "ExpiredTokenException" in capsys.readouterr().err
