"""PostgreSQL constraint and snapshot proofs for the F09.01 V6 migration."""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime
from uuid import UUID

import asyncpg

HR_ID = UUID("00000000-0000-4000-8000-000000009201")
CUSTODIAN_ID = UUID("00000000-0000-4000-8000-000000009202")
CATEGORY_ID = UUID("00000000-0000-4000-8000-000000009203")
ASSET_ID = UUID("00000000-0000-4000-8000-000000009204")
CUSTODY_ID = UUID("00000000-0000-4000-8000-000000009205")
SESSION_ID = UUID("00000000-0000-4000-8000-000000009206")
ITEM_ID = UUID("00000000-0000-4000-8000-000000009207")
NOW = datetime(2030, 1, 2, 3, 0, tzinfo=UTC)


def _database_url() -> str | None:
    value = os.getenv("AMS_DATABASE_URL")
    return None if value is None else value.replace("postgresql+asyncpg://", "postgresql://", 1)


@unittest.skipUnless(_database_url(), "AMS_DATABASE_URL is required for PostgreSQL tests")
class ReconciliationSchemaTests(unittest.IsolatedAsyncioTestCase):
    """Prove open-session, state, snapshot, and access-index protections."""

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
                ($1, 'recon.hr', 'recon.hr@example.test', 'Recon HR', 'hash'),
                ($2, 'recon.custodian', 'recon.custodian@example.test', 'Custodian', 'hash')
            """,
            HR_ID,
            CUSTODIAN_ID,
        )
        await self.connection.execute(
            """
            INSERT INTO asset_categories (id, code, name)
            VALUES ($1, 'RECON_SCHEMA', 'Reconciliation')
            """,
            CATEGORY_ID,
        )
        await self.connection.execute(
            """
            INSERT INTO assets (
                id, category_id, name, source, total_quantity,
                available_quantity, created_by_user_id
            ) VALUES ($1, $2, 'Camera', 'PURCHASE', 2, 2, $3)
            """,
            ASSET_ID,
            CATEGORY_ID,
            HR_ID,
        )
        await self.connection.execute(
            """
            INSERT INTO custodies (
                id, asset_id, custodian_user_id, assigned_by_user_id, status,
                on_hand_quantity, condition, assigned_at, confirmed_at,
                confirmed_by_user_id
            ) VALUES ($1, $2, $3, $4, 'ACTIVE', 2, 'GOOD', $5, $5, $3)
            """,
            CUSTODY_ID,
            ASSET_ID,
            CUSTODIAN_ID,
            HR_ID,
            NOW,
        )
        await self.connection.execute(
            """
            INSERT INTO reconciliation_sessions (
                id, reconciliation_date, opened_by_user_id, opened_at, updated_at
            ) VALUES ($1, '2030-01-03', $2, $3, $3)
            """,
            SESSION_ID,
            HR_ID,
            NOW,
        )
        await self.connection.execute(
            """
            INSERT INTO reconciliation_items (
                id, reconciliation_id, custody_id, asset_id, custodian_user_id,
                asset_name_snapshot, expected_quantity, expected_condition,
                snapshot_at, created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, 'Camera', 2, 'GOOD', $6, $6, $6)
            """,
            ITEM_ID,
            SESSION_ID,
            CUSTODY_ID,
            ASSET_ID,
            CUSTODIAN_ID,
            NOW,
        )

    async def asyncTearDown(self) -> None:
        await self.transaction.rollback()
        await self.connection.close()

    async def test_one_open_session_and_discrepancy_reason_are_enforced(self) -> None:
        """Reject a second open session and an unexplained discrepancy."""

        with self.assertRaises(asyncpg.UniqueViolationError):
            async with self.connection.transaction():
                await self.connection.execute(
                    """
                    INSERT INTO reconciliation_sessions (
                        reconciliation_date, opened_by_user_id
                    ) VALUES ('2030-02-01', $1)
                    """,
                    HR_ID,
                )
        with self.assertRaises(asyncpg.CheckViolationError):
            async with self.connection.transaction():
                await self.connection.execute(
                    """
                    UPDATE reconciliation_items
                    SET status = 'DISCREPANCY', actual_quantity = 1,
                        actual_condition = 'GOOD', confirmed_by_user_id = $1,
                        confirmed_at = $2, updated_at = $2
                    WHERE id = $3
                    """,
                    CUSTODIAN_ID,
                    NOW,
                    ITEM_ID,
                )

    async def test_later_custody_change_does_not_mutate_snapshot(self) -> None:
        """Keep expected values stable after source Custody changes."""

        await self.connection.execute(
            """
            UPDATE custodies
            SET on_hand_quantity = 1, condition = 'DAMAGED', updated_at = $1
            WHERE id = $2
            """,
            NOW,
            CUSTODY_ID,
        )
        row = await self.connection.fetchrow(
            """
            SELECT expected_quantity, expected_condition
            FROM reconciliation_items
            WHERE id = $1
            """,
            ITEM_ID,
        )
        assert row is not None
        self.assertEqual(row["expected_quantity"], 2)
        self.assertEqual(row["expected_condition"], "GOOD")

    async def test_required_reconciliation_indexes_exist(self) -> None:
        """Verify session, work-queue, history, and evidence access paths."""

        rows = await self.connection.fetch(
            """
            SELECT indexname FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename IN (
                  'reconciliation_sessions',
                  'reconciliation_items',
                  'reconciliation_item_evidence'
              )
            """
        )
        names = {str(row["indexname"]) for row in rows}
        self.assertTrue(
            {
                "uq_reconciliation_sessions_single_open",
                "ix_reconciliation_sessions_date",
                "ix_reconciliation_items_session_status",
                "ix_reconciliation_items_custodian_status",
                "ix_reconciliation_items_asset_history",
                "ix_reconciliation_item_evidence_item",
            }.issubset(names)
        )
