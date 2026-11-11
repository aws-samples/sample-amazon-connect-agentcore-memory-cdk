# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""DynamoDB-backed implementation of ``connect_memory.writer.DedupStore``.

Kept in its own module (rather than in ``writer.py``) so the orchestration logic in
``writer.py`` has no boto3 import and every branch there is testable with a plain
in-memory fake.

Table shape (created by ``stacks/memory_writer_stack.py``):

    partition key: ``orchestration_id`` (S)
    TTL attribute: ``expires_at`` (N, epoch seconds). Matches the memory resource's 90-day
        event expiry, since a dedup entry has no reason to outlive the memory event it
        guarded.
"""

from __future__ import annotations

import time
from typing import Any, Mapping, Protocol


class DynamoDbTable(Protocol):
    """The subset of a boto3 DynamoDB Table resource this store calls."""

    def get_item(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def put_item(self, **kwargs: Any) -> Any: ...


class DynamoDbDedupStore:
    """Records which ``orchestration_id`` values already produced a successful CreateEvent.

    ``is_written`` is checked before the write and ``mark_written`` is called only after
    CreateEvent succeeds, so a failed write is retried instead of being skipped as a
    duplicate. Two invocations racing on the same redelivered batch can both see "not
    written"; both then send the same CreateEvent ``clientToken`` (derived from the keys),
    and CreateEvent treats a request with a token it has already seen as a repeat.
    """

    def __init__(self, table: DynamoDbTable, *, ttl_seconds: int = 90 * 24 * 60 * 60) -> None:
        self._table = table
        self._ttl_seconds = ttl_seconds

    def is_written(self, key: str) -> bool:
        response = self._table.get_item(
            Key={"orchestration_id": key},
            ConsistentRead=True,
            ProjectionExpression="orchestration_id",
        )
        return "Item" in response

    def mark_written(self, key: str) -> None:
        now = int(time.time())
        try:
            self._table.put_item(
                Item={
                    "orchestration_id": key,
                    "written_at": now,
                    "expires_at": now + self._ttl_seconds,
                },
                ConditionExpression="attribute_not_exists(orchestration_id)",
            )
        except Exception as exc:  # noqa: BLE001
            # boto3 raises a botocore.exceptions.ClientError whose .response['Error']['Code']
            # is 'ConditionalCheckFailedException' when the key already exists (a concurrent
            # invocation marked it first). Checked by name (not isinstance) so this module
            # has no botocore import.
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                return
            raise
