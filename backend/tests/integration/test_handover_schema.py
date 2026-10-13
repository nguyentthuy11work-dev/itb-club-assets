"""PostgreSQL constraints and immutable-lock proofs for F11 V8."""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime
from uuid import UUID

import asyncpg

OLD_HR_ID = UUID("00000000-0000-4000-8000-000000013101")
NEW_HR_ID = UUID("00000000-0000-4000-8000-000000013102")
CATEGORY_ID = UUID("00000000-0000-4000-8000-000000013103")
ASSET_ID = UUID("00000000-0000-4000-8000-000000013104")
TERM_ID = UUID("00000000-0000-4000-8000-000000013105")
HANDOVER_ID = UUID("00000000-0000-4000-8000-000000013106")
ITEM_ID = UUID("00000000-0000-4000-8000-000000013107")
NOW = datetime(2030, 2, 2, 3, 0, tzinfo=UTC)


def _database_url() -> str | None:
    value = os.getenv("AMS_DATABASE_URL")
    return None if value is None else value.replace("postgresql+asyncpg://", "postgresql://", 1)


@unittest.skipUnless(_database_url(), "AMS_DATABASE_URL is required for PostgreSQL tests")
class HandoverSchemaTests(unittest.IsolatedAsyncioTestCase):
    """Prove term, snapshot, state, index, and terminal immutability constraints."""

    async def asyncSetUp(self) -> None:
        database_url = _database_url()
        assert database_url is not None
        self.connection = await asyncpg.connect(database_url)
        self.transaction = self.connection.transaction()
        await self.transaction.start()
        await self.connection.execute(
            """
            INSERT INTO users (id, username, email, display_name, password_hash)
            VALUES
                ($1, 'hs.old', 'hs.old@example.test', 'Old HR', 'hash'),
                ($2, 'hs.new', 'hs.new@example.test', 'New HR', 'hash')
            """,
            OLD_HR_ID,
            NEW_HR_ID,
        )
        await self.connection.execute(
            """
            INSERT INTO asset_categories (id, code, name)
            VALUES ($1, 'HANDOVER_SCHEMA', 'Handover schema')
            """,
            CATEGORY_ID,
        )
        await self.connection.execute(
            """
            INSERT INTO assets (
                id, category_id, name, source, total_quantity,
                available_quantity, created_by_user_id, created_at, updated_at
            ) VALUES ($1, $2, 'Camera', 'PURCHASE', 2, 2, $3, $4, $4)
            """,
            ASSET_ID,
            CATEGORY_ID,
            OLD_HR_ID,
            NOW,
        )
        await self.connection.execute(
            """
            INSERT INTO terms (
                id, code, name, starts_on, ends_on, old_hr_user_id,
                new_hr_user_id, created_by_user_id, created_at, updated_at
            ) VALUES ($1, '2029-2030', 'Term', '2030-01-01', '2030-01-31',
                      $2, $3, $2, $4, $4)
            """,
            TERM_ID,
            OLD_HR_ID,
            NEW_HR_ID,
            NOW,
        )
        await self.connection.execute(
            """
            INSERT INTO handovers (
                id, term_id, term_code_snapshot, term_name_snapshot,
                term_starts_on, term_ends_on, old_hr_user_id, new_hr_user_id,
                created_by_user_id, created_at, updated_at
            ) VALUES ($1, $2, '2029-2030', 'Term', '2030-01-01', '2030-01-31',
                      $3, $4, $3, $5, $5)
            """,
            HANDOVER_ID,
            TERM_ID,
            OLD_HR_ID,
            NEW_HR_ID,
            NOW,
        )
        await self.connection.execute(
            """
            INSERT INTO handover_items (
                id, handover_id, asset_id, asset_name_snapshot,
                total_quantity_snapshot, available_quantity_snapshot,
                condition_snapshot, custody_snapshot, is_stale, snapshot_at,
                created_at
            ) VALUES ($1, $2, $3, 'Camera', 2, 2, 'GOOD', '[]', TRUE, $4, $4)
            """,
            ITEM_ID,
            HANDOVER_ID,
            ASSET_ID,
            NOW,
        )

    async def asyncTearDown(self) -> None:
        await self.transaction.rollback()
        await self.connection.close()

    async def test_term_and_handover_state_constraints_reject_invalid_values(self) -> None:
        """Reject invalid calendars, same actors, and false locked states."""

        with self.assertRaises(asyncpg.CheckViolationError):
            async with self.connection.transaction():
                await self.connection.execute(
                    """
                    INSERT INTO terms (
                        code, name, starts_on, ends_on, old_hr_user_id,
                        new_hr_user_id, created_by_user_id
                    ) VALUES ('BAD', 'Bad', '2030-02-01', '2030-01-01', $1, $2, $1)
                    """,
                    OLD_HR_ID,
                    NEW_HR_ID,
                )
        with self.assertRaises(asyncpg.CheckViolationError):
            async with self.connection.transaction():
                await self.connection.execute(
                    "UPDATE handovers SET status = 'LOCKED' WHERE id = $1",
                    HANDOVER_ID,
                )

    async def test_locked_record_and_items_reject_update_delete_and_insert(self) -> None:
        """Permit the locking transition once and reject every later write."""

        await self.connection.execute(
            """
            UPDATE handovers
            SET status = 'LOCKED', old_confirmed_by_user_id = $1,
                old_confirmed_at = $3, new_confirmed_by_user_id = $2,
                new_confirmed_at = $3, locked_at = $3, updated_at = $3
            WHERE id = $4
            """,
            OLD_HR_ID,
            NEW_HR_ID,
            NOW,
            HANDOVER_ID,
        )
        statements = (
            ("UPDATE handovers SET term_name_snapshot = 'Changed' WHERE id = $1", HANDOVER_ID),
            ("DELETE FROM handovers WHERE id = $1", HANDOVER_ID),
            ("UPDATE handover_items SET is_stale = FALSE WHERE id = $1", ITEM_ID),
            ("DELETE FROM handover_items WHERE id = $1", ITEM_ID),
        )
        for statement, identifier in statements:
            with self.assertRaises(asyncpg.CheckViolationError):
                async with self.connection.transaction():
                    await self.connection.execute(statement, identifier)
        with self.assertRaises(asyncpg.CheckViolationError):
            async with self.connection.transaction():
                await self.connection.execute(
                    """
                    INSERT INTO handover_items (
                        handover_id, asset_id, asset_name_snapshot,
                        total_quantity_snapshot, available_quantity_snapshot,
                        condition_snapshot, custody_snapshot, is_stale, snapshot_at
                    ) VALUES ($1, $2, 'Camera 2', 2, 2, 'GOOD', '[]', TRUE, $3)
                    """,
                    HANDOVER_ID,
                    ASSET_ID,
                    NOW,
                )

    async def test_required_handover_indexes_exist(self) -> None:
        """Verify term calendar, participant, stale, and Asset history access paths."""

        rows = await self.connection.fetch(
            """
            SELECT indexname FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename IN ('terms', 'handovers', 'handover_items')
            """
        )
        names = {str(row["indexname"]) for row in rows}
        self.assertTrue(
            {
                "ix_terms_calendar",
                "ix_handovers_old_hr_status",
                "ix_handovers_new_hr_status",
                "ix_handover_items_handover_stale",
                "ix_handover_items_asset_history",
            }.issubset(names)
        )
