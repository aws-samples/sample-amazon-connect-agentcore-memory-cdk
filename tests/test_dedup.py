# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for ``connect_memory.dedup.DynamoDbDedupStore``. Fakes the DynamoDB Table
resource, no live AWS calls."""

from __future__ import annotations

import time

import pytest

from connect_memory.dedup import DynamoDbDedupStore


class _ClientErrorLike(Exception):
    """Mimics botocore.exceptions.ClientError's .response shape without importing
    botocore - DynamoDbDedupStore checks the error code by attribute, not isinstance."""

    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeTable:
    def __init__(self, raises: Exception | None = None, items: set[str] | None = None):
        self._raises = raises
        self._items = set(items or ())
        self.put_item_calls: list[dict] = []
        self.get_item_calls: list[dict] = []

    def get_item(self, **kwargs):
        self.get_item_calls.append(kwargs)
        key = kwargs["Key"]["orchestration_id"]
        return {"Item": {"orchestration_id": key}} if key in self._items else {}

    def put_item(self, **kwargs):
        self.put_item_calls.append(kwargs)
        if self._raises:
            raise self._raises
        self._items.add(kwargs["Item"]["orchestration_id"])
        return {}


def test_is_written_false_for_unknown_key_and_uses_consistent_read():
    table = FakeTable()
    store = DynamoDbDedupStore(table)

    assert store.is_written("orch-1") is False
    assert table.get_item_calls[0]["ConsistentRead"] is True
    assert table.put_item_calls == [], "checking a key must not mark it"


def test_is_written_true_after_mark_written():
    table = FakeTable()
    store = DynamoDbDedupStore(table)

    store.mark_written("orch-1")

    assert store.is_written("orch-1") is True
    call = table.put_item_calls[0]
    assert call["Item"]["orchestration_id"] == "orch-1"
    assert call["ConditionExpression"] == "attribute_not_exists(orchestration_id)"


def test_mark_written_tolerates_a_key_marked_concurrently():
    table = FakeTable(raises=_ClientErrorLike("ConditionalCheckFailedException"))
    store = DynamoDbDedupStore(table)

    store.mark_written("orch-1")  # must not raise


def test_mark_written_reraises_unrelated_errors():
    table = FakeTable(raises=_ClientErrorLike("ProvisionedThroughputExceededException"))
    store = DynamoDbDedupStore(table)

    with pytest.raises(_ClientErrorLike):
        store.mark_written("orch-1")


def test_mark_written_reraises_non_client_error_exceptions():
    table = FakeTable(raises=RuntimeError("network blip"))
    store = DynamoDbDedupStore(table)

    with pytest.raises(RuntimeError):
        store.mark_written("orch-1")


def test_item_carries_ttl_matching_configured_expiry():
    table = FakeTable()
    store = DynamoDbDedupStore(table, ttl_seconds=3600)

    before = int(time.time())
    store.mark_written("orch-1")
    after = int(time.time())

    item = table.put_item_calls[0]["Item"]
    assert item["expires_at"] - item["written_at"] == 3600
    assert before <= item["written_at"] <= after


def test_default_ttl_matches_90_day_event_expiry():
    table = FakeTable()
    store = DynamoDbDedupStore(table)
    store.mark_written("orch-1")
    item = table.put_item_calls[0]["Item"]
    assert item["expires_at"] - item["written_at"] == 90 * 24 * 60 * 60
