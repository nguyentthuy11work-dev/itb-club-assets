"""Unit tests for F03.02 reliability records and transitions."""

from __future__ import annotations

import unittest
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from ams.application.reliability import (
    IdempotencyRecord,
    IdempotencyStatus,
    Notification,
    NotificationStatus,
    OutboxEvent,
    ReliabilityError,
    StagedFile,
)

ACTOR_ID = UUID("00000000-0000-4000-8000-000000000501")
RECORD_ID = UUID("00000000-0000-4000-8000-000000000502")
EVENT_ID = UUID("00000000-0000-4000-8000-000000000503")
AGGREGATE_ID = UUID("00000000-0000-4000-8000-000000000504")
NOW = datetime(2026, 10, 4, 11, 0, tzinfo=UTC)


class ReliabilityRecordTests(unittest.TestCase):
    """Exercise input validation and idempotency response state."""

    def test_valid_event_notification_and_staged_file_preserve_output(self) -> None:
        """Valid records retain normalized reliability metadata."""

        event = OutboxEvent(
            id=EVENT_ID,
            event_type="CUSTODY_ASSIGNED",
            aggregate_type="CUSTODY",
            aggregate_id=AGGREGATE_ID,
            payload={"version": 1},
            scheduled_at=NOW,
            created_at=NOW,
        )
        notification = Notification(
            id=RECORD_ID,
            outbox_event_id=EVENT_ID,
            recipient_id=ACTOR_ID,
            recipient_address="member@example.test",
            created_at=NOW,
        )
        staged = StagedFile("staging/key", "image/png", "a" * 64, 100)

        self.assertEqual(event.payload, {"version": 1})
        self.assertEqual(notification.recipient_address, "member@example.test")
        self.assertEqual(staged.size_bytes, 100)

    def test_event_and_notification_reject_blank_or_non_utc_input(self) -> None:
        """Invalid types, addresses, and timestamps fail before persistence."""

        naive = datetime(2026, 10, 4, 11, 0)
        event_values = (("", "CUSTODY", NOW, NOW), ("EVENT", " ", NOW, NOW))
        for event_type, aggregate_type, scheduled_at, created_at in event_values:
            with self.subTest(event_type=event_type), self.assertRaises(ReliabilityError):
                OutboxEvent(
                    EVENT_ID,
                    event_type,
                    aggregate_type,
                    AGGREGATE_ID,
                    {},
                    scheduled_at,
                    created_at,
                )
        with self.assertRaises(ReliabilityError):
            OutboxEvent(EVENT_ID, "EVENT", "TYPE", AGGREGATE_ID, {}, naive, NOW)
        with self.assertRaises(ReliabilityError):
            OutboxEvent(EVENT_ID, "EVENT", "TYPE", AGGREGATE_ID, {}, NOW, naive)
        with self.assertRaises(ReliabilityError):
            Notification(RECORD_ID, EVENT_ID, ACTOR_ID, " ", NOW)
        with self.assertRaises(ReliabilityError):
            Notification(RECORD_ID, EVENT_ID, ACTOR_ID, "a@example.test", naive)

    def test_notification_records_success_failure_and_terminal_protection(self) -> None:
        """Delivery attempts update one logical audit row with ordered safe metadata."""

        delivered = Notification(RECORD_ID, EVENT_ID, ACTOR_ID, "a@example.test", NOW)
        delivered.mark_delivered(provider_message_id="provider-1", attempted_at=NOW)
        delivered.mark_delivered(provider_message_id="ignored", attempted_at=NOW)
        self.assertEqual(delivered.status, NotificationStatus.DELIVERED)
        self.assertEqual(delivered.attempt_count, 1)
        with self.assertRaises(ReliabilityError):
            delivered.mark_failed(attempted_at=NOW, error="safe")

        failed = Notification(RECORD_ID, EVENT_ID, ACTOR_ID, "a@example.test", NOW)
        failed.mark_failed(attempted_at=NOW, error="provider unavailable")
        self.assertEqual(failed.status, NotificationStatus.FAILED)
        self.assertEqual(failed.last_error, "provider unavailable")

    def test_notification_rejects_invalid_attempt_and_rehydrated_state(self) -> None:
        """Attempts require UTC order and state-complete audit fields."""

        notification = Notification(RECORD_ID, EVENT_ID, ACTOR_ID, "a@example.test", NOW)
        operations: tuple[Callable[[], None], ...] = (
            lambda: notification.mark_delivered(provider_message_id=" ", attempted_at=NOW),
            lambda: notification.mark_delivered(
                provider_message_id="provider",
                attempted_at=datetime(2026, 10, 4, 11, 0),
            ),
            lambda: notification.mark_failed(attempted_at=NOW, error=" "),
            lambda: notification.mark_failed(
                attempted_at=NOW - timedelta(seconds=1), error="safe"
            ),
        )
        for operation in operations:
            with self.assertRaises(ReliabilityError):
                operation()

        with self.assertRaises(ReliabilityError):
            Notification(
                RECORD_ID,
                EVENT_ID,
                ACTOR_ID,
                "a@example.test",
                NOW,
                attempt_count=-1,
            )
        with self.assertRaises(ReliabilityError):
            Notification(
                RECORD_ID,
                EVENT_ID,
                ACTOR_ID,
                "a@example.test",
                NOW,
                status=NotificationStatus.DELIVERED,
            )
        with self.assertRaises(ReliabilityError):
            Notification(
                RECORD_ID,
                EVENT_ID,
                ACTOR_ID,
                "a@example.test",
                NOW,
                status=NotificationStatus.FAILED,
                attempt_count=1,
                last_attempted_at=NOW,
            )
        with self.assertRaises(ReliabilityError):
            Notification(
                RECORD_ID,
                EVENT_ID,
                ACTOR_ID,
                "a@example.test",
                NOW,
                last_attempted_at=datetime(2026, 10, 4, 11, 0),
            )
        with self.assertRaises(ReliabilityError):
            Notification(
                RECORD_ID,
                EVENT_ID,
                ACTOR_ID,
                "a@example.test",
                NOW,
                delivered_at=NOW - timedelta(seconds=1),
            )

    def test_in_progress_idempotency_record_succeeds_once(self) -> None:
        """A canonical 2xx response becomes replayable exactly once."""

        record = self._record()
        record.succeed(
            response_status_code=201,
            response_body={"assetId": "asset-1"},
            completed_at=NOW + timedelta(seconds=1),
        )

        self.assertEqual(record.status, IdempotencyStatus.SUCCEEDED)
        self.assertEqual(record.response_status_code, 201)
        self.assertEqual(record.response_body, {"assetId": "asset-1"})
        with self.assertRaises(ReliabilityError):
            record.succeed(
                response_status_code=201,
                response_body={},
                completed_at=NOW + timedelta(seconds=2),
            )

    def test_idempotency_creation_rejects_invalid_contract_fields(self) -> None:
        """Key, fingerprint, expiry, UTC, and state combinations are strict."""

        factories: tuple[Callable[[], IdempotencyRecord], ...] = (
            lambda: self._record(command_name=" "),
            lambda: self._record(command_name="C" * 161),
            lambda: self._record(idempotency_key=""),
            lambda: self._record(idempotency_key="k" * 256),
            lambda: self._record(request_fingerprint="bad"),
            lambda: self._record(request_fingerprint="G" * 64),
            lambda: self._record(expires_at=NOW),
            lambda: self._record(created_at=datetime(2026, 10, 4, 11, 0)),
            lambda: self._record(expires_at=datetime(2026, 10, 5, 11, 0)),
            lambda: self._record(response_status_code=201),
            lambda: self._record(status=IdempotencyStatus.SUCCEEDED),
        )
        for index, factory in enumerate(factories):
            with self.subTest(case=index), self.assertRaises(ReliabilityError):
                factory()

    def test_idempotency_success_rejects_bad_status_time_and_rehydration(self) -> None:
        """Only ordered UTC 2xx response data can represent success."""

        for status_code in (199, 300):
            with self.subTest(status_code=status_code), self.assertRaises(ReliabilityError):
                self._record().succeed(
                    response_status_code=status_code,
                    response_body={},
                    completed_at=NOW,
                )
        with self.assertRaises(ReliabilityError):
            self._record().succeed(
                response_status_code=200,
                response_body={},
                completed_at=datetime(2026, 10, 4, 11, 0),
            )
        with self.assertRaises(ReliabilityError):
            self._record().succeed(
                response_status_code=200,
                response_body={},
                completed_at=NOW - timedelta(seconds=1),
            )
        with self.assertRaises(ReliabilityError):
            self._record(
                status=IdempotencyStatus.SUCCEEDED,
                response_status_code=200,
                response_body={},
                completed_at=datetime(2026, 10, 4, 11, 0),
            )

        complete = self._record(
            status=IdempotencyStatus.SUCCEEDED,
            response_status_code=200,
            response_body={},
            completed_at=NOW,
        )
        self.assertEqual(complete.status, IdempotencyStatus.SUCCEEDED)

    def _record(
        self,
        *,
        command_name: str = "CREATE_ASSET",
        idempotency_key: str = "request-1",
        request_fingerprint: str = "a" * 64,
        status: IdempotencyStatus = IdempotencyStatus.IN_PROGRESS,
        expires_at: datetime = NOW + timedelta(days=1),
        created_at: datetime = NOW,
        response_status_code: int | None = None,
        response_body: dict[str, object] | None = None,
        completed_at: datetime | None = None,
    ) -> IdempotencyRecord:
        return IdempotencyRecord(
            id=RECORD_ID,
            actor_id=ACTOR_ID,
            command_name=command_name,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            status=status,
            expires_at=expires_at,
            created_at=created_at,
            response_status_code=response_status_code,
            response_body=response_body,
            completed_at=completed_at,
        )
