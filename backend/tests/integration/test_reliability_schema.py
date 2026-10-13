"""PostgreSQL integration tests for the F03.01 reliability schema."""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime
from uuid import UUID

import asyncpg

USER_ID = UUID("00000000-0000-4000-8000-000000000401")
OTHER_USER_ID = UUID("00000000-0000-4000-8000-000000000402")
AGGREGATE_ID = UUID("00000000-0000-4000-8000-000000000403")
EVENT_ID = UUID("00000000-0000-4000-8000-000000000404")


def _database_url() -> str | None:
    value = os.getenv("AMS_DATABASE_URL")
    if value is None:
        return None
    return value.replace("postgresql+asyncpg://", "postgresql://", 1)


@unittest.skipUnless(_database_url(), "AMS_DATABASE_URL is required for PostgreSQL tests")
class ReliabilitySchemaTests(unittest.IsolatedAsyncioTestCase):
    """Prove outbox leases, notification dedupe, and idempotency state rules."""

    async def asyncSetUp(self) -> None:
        """Open a rollback-only transaction with two recipient identities."""

        database_url = _database_url()
        assert database_url is not None
        self.connection = await asyncpg.connect(database_url)
        self.transaction = self.connection.transaction()
        await self.transaction.start()
        await self.connection.executemany(
            """
            INSERT INTO users (id, username, email, display_name, password_hash)
            VALUES ($1, $2, $3, $4, 'hash')
            """,
            [
                (USER_ID, "reliability.one", "one@example.test", "Reliability One"),
                (OTHER_USER_ID, "reliability.two", "two@example.test", "Reliability Two"),
            ],
        )

    async def asyncTearDown(self) -> None:
        """Roll back every test record and close the connection."""

        await self.transaction.rollback()
        await self.connection.close()

    async def test_valid_outbox_notification_and_idempotency_records_persist(self) -> None:
        """Canonical event, delivery audit, and completed command satisfy all checks."""

        await self._insert_pending_event()
        await self.connection.execute(
            """
            INSERT INTO notifications (
                outbox_event_id, recipient_id, channel, recipient_address
            ) VALUES ($1, $2, 'EMAIL', 'one@example.test')
            """,
            EVENT_ID,
            USER_ID,
        )
        await self.connection.execute(
            """
            INSERT INTO idempotency_records (
                actor_id, command_name, idempotency_key, request_fingerprint,
                status, response_status_code, response_body, completed_at, expires_at
            ) VALUES (
                $1, 'CREATE_ASSET', 'request-1', $2, 'SUCCEEDED', 201,
                '{"assetId":"result"}'::jsonb, CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP + INTERVAL '1 day'
            )
            """,
            USER_ID,
            "a" * 64,
        )

        row = await self.connection.fetchrow(
            """
            SELECT outbox_events.status AS event_status,
                   notifications.status AS notification_status,
                   idempotency_records.status AS idempotency_status
            FROM outbox_events
            JOIN notifications ON notifications.outbox_event_id = outbox_events.id
            JOIN idempotency_records
              ON idempotency_records.actor_id = notifications.recipient_id
            WHERE outbox_events.id = $1
            """,
            EVENT_ID,
        )
        assert row is not None
        self.assertEqual(
            dict(row),
            {
                "event_status": "PENDING",
                "notification_status": "PENDING",
                "idempotency_status": "SUCCEEDED",
            },
        )

    async def test_outbox_state_requires_consistent_lease_and_completion_metadata(self) -> None:
        """Processing requires a lease while terminal states require completion time."""

        invalid_values = (
            ("PROCESSING", None, None, None),
            ("PENDING", "worker-1", "1 minute", None),
            ("DELIVERED", None, None, None),
        )
        for status, lease_owner, lease_interval, completed_interval in invalid_values:
            with (
                self.subTest(status=status),
                self.assertRaises(asyncpg.CheckViolationError),
            ):
                async with self.connection.transaction():
                    await self.connection.execute(
                        """
                        INSERT INTO outbox_events (
                            event_type, aggregate_type, aggregate_id, payload, status,
                            lease_owner, lease_expires_at, completed_at
                        ) VALUES (
                            'TEST_EVENT', 'TEST', $1, '{}'::jsonb, $2, $3,
                            CASE WHEN $4::text IS NULL THEN NULL
                                 ELSE CURRENT_TIMESTAMP + $4::interval END,
                            CASE WHEN $5::text IS NULL THEN NULL
                                 ELSE CURRENT_TIMESTAMP + $5::interval END
                        )
                        """,
                        AGGREGATE_ID,
                        status,
                        lease_owner,
                        lease_interval,
                        completed_interval,
                    )

    async def test_notification_delivery_is_unique_per_event_recipient_channel(self) -> None:
        """Worker retries cannot create a second logical email-delivery record."""

        await self._insert_pending_event()
        statement = """
            INSERT INTO notifications (
                outbox_event_id, recipient_id, channel, recipient_address
            ) VALUES ($1, $2, 'EMAIL', 'one@example.test')
        """
        await self.connection.execute(statement, EVENT_ID, USER_ID)
        with self.assertRaises(asyncpg.UniqueViolationError):
            async with self.connection.transaction():
                await self.connection.execute(statement, EVENT_ID, USER_ID)

        await self.connection.execute(statement, EVENT_ID, OTHER_USER_ID)
        count = await self.connection.fetchval(
            "SELECT COUNT(*) FROM notifications WHERE outbox_event_id = $1", EVENT_ID
        )
        self.assertEqual(count, 2)

    async def test_notification_status_requires_consistent_delivery_audit(self) -> None:
        """Delivered/failed rows require attempts and matching terminal metadata."""

        await self._insert_pending_event()
        for status, attempts, delivered_at, last_error in (
            ("DELIVERED", 0, datetime.now(UTC), None),
            ("FAILED", 1, None, None),
            ("PENDING", 0, datetime.now(UTC), None),
        ):
            with (
                self.subTest(status=status),
                self.assertRaises(asyncpg.CheckViolationError),
            ):
                async with self.connection.transaction():
                    await self.connection.execute(
                        """
                        INSERT INTO notifications (
                            outbox_event_id, recipient_id, channel, recipient_address,
                            status, attempt_count, delivered_at, last_error
                        ) VALUES (
                            $1, $2, 'EMAIL', 'one@example.test', $3, $4,
                            $5, $6
                        )
                        """,
                        EVENT_ID,
                        USER_ID,
                        status,
                        attempts,
                        delivered_at,
                        last_error,
                    )

    async def test_idempotency_scope_fingerprint_and_response_state_are_enforced(self) -> None:
        """Actor-command-key uniqueness and canonical state fields prevent unsafe replay."""

        statement = """
            INSERT INTO idempotency_records (
                actor_id, command_name, idempotency_key, request_fingerprint,
                status, expires_at
            ) VALUES (
                $1, 'CREATE_ASSET', 'same-key', $2, 'IN_PROGRESS',
                CURRENT_TIMESTAMP + INTERVAL '1 day'
            )
        """
        await self.connection.execute(statement, USER_ID, "b" * 64)
        with self.assertRaises(asyncpg.UniqueViolationError):
            async with self.connection.transaction():
                await self.connection.execute(statement, USER_ID, "b" * 64)

        await self.connection.execute(statement, OTHER_USER_ID, "b" * 64)
        with self.assertRaises(asyncpg.CheckViolationError):
            async with self.connection.transaction():
                await self.connection.execute(
                    """
                    INSERT INTO idempotency_records (
                        actor_id, command_name, idempotency_key, request_fingerprint,
                        status, response_status_code, expires_at
                    ) VALUES (
                        $1, 'OTHER_COMMAND', 'bad-state', $2, 'IN_PROGRESS', 201,
                        CURRENT_TIMESTAMP + INTERVAL '1 day'
                    )
                    """,
                    USER_ID,
                    "c" * 64,
                )
        with self.assertRaises(asyncpg.CheckViolationError):
            async with self.connection.transaction():
                await self.connection.execute(
                    """
                    INSERT INTO idempotency_records (
                        actor_id, command_name, idempotency_key, request_fingerprint,
                        status, expires_at
                    ) VALUES (
                        $1, 'OTHER_COMMAND', 'bad-hash', 'not-a-sha256', 'IN_PROGRESS',
                        CURRENT_TIMESTAMP + INTERVAL '1 day'
                    )
                    """,
                    USER_ID,
                )

    async def test_required_reliability_indexes_exist(self) -> None:
        """Worker scheduling, delivery audit, and cleanup scans have named indexes."""

        rows = await self.connection.fetch(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename IN ('outbox_events', 'notifications', 'idempotency_records')
            """
        )
        names = {str(row["indexname"]) for row in rows}
        self.assertTrue(
            {
                "ix_outbox_pending_schedule",
                "ix_outbox_processing_lease",
                "ix_notifications_recipient_delivery",
                "ix_notifications_pending_delivery",
                "ix_idempotency_in_progress",
                "ix_idempotency_expiry",
            }.issubset(names)
        )

    async def _insert_pending_event(self) -> None:
        await self.connection.execute(
            """
            INSERT INTO outbox_events (
                id, event_type, aggregate_type, aggregate_id, payload
            ) VALUES ($1, 'CUSTODY_ASSIGNED', 'CUSTODY', $2, '{"version":1}'::jsonb)
            """,
            EVENT_ID,
            AGGREGATE_ID,
        )
