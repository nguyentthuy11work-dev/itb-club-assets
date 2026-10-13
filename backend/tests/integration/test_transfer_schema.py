"""PostgreSQL constraints and indexes for F10 Custody Transfers."""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime
from uuid import UUID

import asyncpg

HR_ID = UUID("00000000-0000-4000-8000-000000011101")
CURRENT_ID = UUID("00000000-0000-4000-8000-000000011102")
TARGET_ID = UUID("00000000-0000-4000-8000-000000011103")
CATEGORY_ID = UUID("00000000-0000-4000-8000-000000011104")
ASSET_ID = UUID("00000000-0000-4000-8000-000000011105")
CUSTODY_ID = UUID("00000000-0000-4000-8000-000000011106")
TRANSFER_ID = UUID("00000000-0000-4000-8000-000000011107")
NOW = datetime(2030, 2, 1, 3, 0, tzinfo=UTC)


def _database_url() -> str | None:
    value = os.getenv("AMS_DATABASE_URL")
    return None if value is None else value.replace("postgresql+asyncpg://", "postgresql://", 1)


@unittest.skipUnless(_database_url(), "AMS_DATABASE_URL is required for PostgreSQL tests")
class TransferSchemaTests(unittest.IsolatedAsyncioTestCase):
    """Prove snapshot, state, active-source, evidence, and access protections."""

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
                ($1, 'transfer.schema.hr', 'transfer.schema.hr@example.test', 'HR', 'hash'),
                ($2, 'ts.current', 'ts.current@example.test', 'Current', 'hash'),
                ($3, 'ts.target', 'ts.target@example.test', 'Target', 'hash')
            """,
            HR_ID,
            CURRENT_ID,
            TARGET_ID,
        )
        await self.connection.execute(
            """
            INSERT INTO asset_categories (id, code, name)
            VALUES ($1, 'TRANSFER_SCHEMA', 'Transfer schema')
            """,
            CATEGORY_ID,
        )
        await self.connection.execute(
            """
            INSERT INTO assets (
                id, category_id, name, source, total_quantity,
                available_quantity, created_by_user_id
            ) VALUES ($1, $2, 'Camera', 'PURCHASE', 2, 1, $3)
            """,
            ASSET_ID,
            CATEGORY_ID,
            HR_ID,
        )
        await self.connection.execute(
            """
            INSERT INTO custodies (
                id, asset_id, custodian_user_id, assigned_by_user_id, status,
                on_hand_quantity, reserved_quantity, condition, assigned_at,
                confirmed_at, confirmed_by_user_id
            ) VALUES ($1, $2, $3, $4, 'ACTIVE', 2, 1, 'GOOD', $5, $5, $3)
            """,
            CUSTODY_ID,
            ASSET_ID,
            CURRENT_ID,
            HR_ID,
            NOW,
        )
        await self.connection.execute(
            """
            INSERT INTO custody_transfers (
                id, source_custody_id, asset_id, current_custodian_user_id,
                target_custodian_user_id, quantity, condition_snapshot,
                created_by_user_id, created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, 1, 'GOOD', $6, $7, $7)
            """,
            TRANSFER_ID,
            CUSTODY_ID,
            ASSET_ID,
            CURRENT_ID,
            TARGET_ID,
            HR_ID,
            NOW,
        )

    async def asyncTearDown(self) -> None:
        await self.transaction.rollback()
        await self.connection.close()

    async def test_identity_quantity_and_confirmation_state_are_enforced(self) -> None:
        """Reject same-party, non-positive, and status/confirmation mismatches."""

        for columns in (
            "target_custodian_user_id = current_custodian_user_id",
            "quantity = 0",
            "status = 'PENDING_TARGET_CONFIRMATION'",
        ):
            with self.assertRaises(asyncpg.CheckViolationError):
                async with self.connection.transaction():
                    await self.connection.execute(
                        f"UPDATE custody_transfers SET {columns} WHERE id = $1",  # noqa: S608
                        TRANSFER_ID,
                    )

    async def test_source_snapshot_and_active_until_completion_are_enforced(self) -> None:
        """Reject mismatched inserts and closure while a Transfer remains pending."""

        with self.assertRaises(asyncpg.CheckViolationError):
            async with self.connection.transaction():
                await self.connection.execute(
                    """
                    INSERT INTO custody_transfers (
                        source_custody_id, asset_id, current_custodian_user_id,
                        target_custodian_user_id, quantity, condition_snapshot,
                        created_by_user_id
                    ) VALUES ($1, $2, $3, $4, 1, 'GOOD', $5)
                    """,
                    CUSTODY_ID,
                    ASSET_ID,
                    TARGET_ID,
                    CURRENT_ID,
                    HR_ID,
                )
        with self.assertRaises(asyncpg.CheckViolationError):
            async with self.connection.transaction():
                await self.connection.execute(
                    """
                    UPDATE custodies
                    SET status = 'CLOSED', closed_at = $1
                    WHERE id = $2
                    """,
                    NOW,
                    CUSTODY_ID,
                )
                await self.connection.execute("SET CONSTRAINTS ALL IMMEDIATE")

    async def test_required_transfer_indexes_exist(self) -> None:
        """Verify participant inbox, source history, link, and evidence paths."""

        rows = await self.connection.fetch(
            """
            SELECT indexname FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename IN ('custody_transfers', 'custody_transfer_evidence')
            """
        )
        names = {str(row["indexname"]) for row in rows}
        self.assertTrue(
            {
                "ix_custody_transfers_current_status",
                "ix_custody_transfers_target_status",
                "ix_custody_transfers_source_history",
                "ix_custody_transfers_reconciliation_item",
                "ix_custody_transfer_evidence_transfer_side",
            }.issubset(names)
        )
