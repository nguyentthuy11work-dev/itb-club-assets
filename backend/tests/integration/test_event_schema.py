"""PostgreSQL constraint and index proofs for the F07.01 V5 migration."""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime
from uuid import UUID

import asyncpg

USER_ID = UUID("00000000-0000-4000-8000-000000007201")
CATEGORY_ID = UUID("00000000-0000-4000-8000-000000007202")
ASSET_ID = UUID("00000000-0000-4000-8000-000000007203")
EVENT_ID = UUID("00000000-0000-4000-8000-000000007204")
SHIFT_ID = UUID("00000000-0000-4000-8000-000000007205")


def _database_url() -> str | None:
    value = os.getenv("AMS_DATABASE_URL")
    return None if value is None else value.replace("postgresql+asyncpg://", "postgresql://", 1)


@unittest.skipUnless(_database_url(), "AMS_DATABASE_URL is required for PostgreSQL tests")
class EventSchemaTests(unittest.IsolatedAsyncioTestCase):
    """Prove V5 chronology, uniqueness, snapshot, and access-index protections."""

    async def asyncSetUp(self) -> None:
        database_url = _database_url()
        assert database_url is not None
        self.connection = await asyncpg.connect(database_url)
        self.transaction = self.connection.transaction()
        await self.transaction.start()
        await self.connection.execute(
            """
            INSERT INTO users (id, username, email, display_name, password_hash)
            VALUES ($1, 'event.schema', 'event.schema@example.test', 'Event Schema', 'hash')
            """,
            USER_ID,
        )
        await self.connection.execute(
            """
            INSERT INTO asset_categories (id, code, name)
            VALUES ($1, 'EVENT_SCHEMA', 'Event')
            """,
            CATEGORY_ID,
        )
        await self.connection.execute(
            """
            INSERT INTO assets (
                id, category_id, name, source, total_quantity,
                available_quantity, created_by_user_id
            ) VALUES ($1, $2, 'Event Asset', 'PURCHASE', 10, 10, $3)
            """,
            ASSET_ID,
            CATEGORY_ID,
            USER_ID,
        )
        await self.connection.execute(
            """
            INSERT INTO event_days (id, name, event_date, created_by_user_id)
            VALUES ($1, 'Event Day', '2030-01-02', $2)
            """,
            EVENT_ID,
            USER_ID,
        )

    async def asyncTearDown(self) -> None:
        await self.transaction.rollback()
        await self.connection.close()

    async def test_schedule_and_snapshot_constraints_are_enforced(self) -> None:
        """Invalid chronology, duplicate order, and formula drift are rejected by PostgreSQL."""

        await self._insert_shift(SHIFT_ID, 1)
        with self.assertRaises(asyncpg.UniqueViolationError):
            async with self.connection.transaction():
                await self._insert_shift(UUID(int=7206), 1)
        with self.assertRaises(asyncpg.CheckViolationError):
            async with self.connection.transaction():
                await self._insert_shift(UUID(int=7207), 2, reversed_time=True)
        with self.assertRaises(asyncpg.CheckViolationError):
            async with self.connection.transaction():
                await self.connection.execute(
                    """
                    INSERT INTO shift_assets (
                        shift_id, asset_id, quota_quantity,
                        carry_over_quantity, expected_open_quantity
                    ) VALUES ($1, $2, 2, 1, 2)
                    """,
                    SHIFT_ID,
                    ASSET_ID,
                )

    async def test_required_event_access_indexes_exist(self) -> None:
        """Date, assignee, ordered schedule, Asset, and history access paths are indexed."""

        rows = await self.connection.fetch(
            """
            SELECT indexname FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename IN ('event_days', 'shifts', 'shift_assets', 'shift_events')
            """
        )
        names = {str(row["indexname"]) for row in rows}
        self.assertTrue(
            {
                "ix_event_days_date",
                "ix_event_days_creator",
                "ix_shifts_assignee_schedule",
                "ix_shifts_event_schedule",
                "ix_shift_assets_asset",
                "ix_shift_events_history",
            }.issubset(names)
        )

    async def _insert_shift(
        self, shift_id: UUID, sequence: int, *, reversed_time: bool = False
    ) -> None:
        starts_at = datetime(2030, 1, 2, 4, 0, tzinfo=UTC)
        ends_at = datetime(2030, 1, 2, 3 if reversed_time else 5, 0, tzinfo=UTC)
        await self.connection.execute(
            """
            INSERT INTO shifts (
                id, event_day_id, sequence, name, starts_at,
                ends_at, shift_lead_user_id
            ) VALUES ($1, $2, $3, 'Shift', $4, $5, $6)
            """,
            shift_id,
            EVENT_ID,
            sequence,
            starts_at,
            ends_at,
            USER_ID,
        )
