"""PostgreSQL integration tests for the F02.01 Asset/Custody migration."""

from __future__ import annotations

import os
import unittest
from uuid import UUID

import asyncpg

USER_ID = UUID("00000000-0000-4000-8000-000000000101")
OTHER_USER_ID = UUID("00000000-0000-4000-8000-000000000102")
TEAM_ID = UUID("00000000-0000-4000-8000-000000000103")
CATEGORY_ID = UUID("00000000-0000-4000-8000-000000000104")
ASSET_ID = UUID("00000000-0000-4000-8000-000000000105")
CUSTODY_ID = UUID("00000000-0000-4000-8000-000000000106")


def _database_url() -> str | None:
    value = os.getenv("AMS_DATABASE_URL")
    if value is None:
        return None
    return value.replace("postgresql+asyncpg://", "postgresql://", 1)


@unittest.skipUnless(_database_url(), "AMS_DATABASE_URL is required for PostgreSQL tests")
class AssetCustodySchemaTests(unittest.IsolatedAsyncioTestCase):
    """Prove database constraints, ownership, and required access indexes."""

    async def asyncSetUp(self) -> None:
        """Open a rollback-only transaction and insert valid identity fixtures."""

        database_url = _database_url()
        assert database_url is not None
        self.connection = await asyncpg.connect(database_url)
        self.transaction = self.connection.transaction()
        await self.transaction.start()
        await self.connection.executemany(
            """
            INSERT INTO users (id, username, email, display_name, password_hash)
            VALUES ($1, $2, $3, $4, $5)
            """,
            [
                (USER_ID, "schema.hr", "schema.hr@example.test", "Schema HR", "hash"),
                (
                    OTHER_USER_ID,
                    "schema.custodian",
                    "schema.custodian@example.test",
                    "Schema Custodian",
                    "hash",
                ),
            ],
        )
        await self.connection.execute(
            "INSERT INTO teams (id, name, code) VALUES ($1, $2, $3)",
            TEAM_ID,
            "Schema Team",
            "SCHEMA_TEAM",
        )
        await self.connection.execute(
            "INSERT INTO asset_categories (id, code, name) VALUES ($1, $2, $3)",
            CATEGORY_ID,
            "CAMERA",
            "Camera",
        )

    async def asyncTearDown(self) -> None:
        """Remove all test rows regardless of assertion outcome."""

        await self.transaction.rollback()
        await self.connection.close()

    async def test_valid_asset_pending_custody_and_private_evidence_persist(self) -> None:
        """Canonical IDs and evidence metadata produce the expected rows."""

        await self._insert_asset(total=5, available=0)
        await self._insert_pending_custody(on_hand=5, reserved=0, loaned=0)
        await self.connection.execute(
            """
            INSERT INTO custody_evidence (
                id, custody_id, uploaded_by_user_id, evidence_type, object_key,
                content_type, content_sha256, size_bytes
            ) VALUES (
                gen_random_uuid(), $1, $2, 'ASSIGNMENT_CONFIRMATION', $3,
                'image/png', $4, 1024
            )
            """,
            CUSTODY_ID,
            OTHER_USER_ID,
            "opaque/test-object-key",
            "a" * 64,
        )

        row = await self.connection.fetchrow(
            """
            SELECT assets.total_quantity, assets.available_quantity,
                   custodies.status, custodies.on_hand_quantity
            FROM assets
            JOIN custodies ON custodies.asset_id = assets.id
            WHERE assets.id = $1
            """,
            ASSET_ID,
        )
        assert row is not None
        self.assertEqual(
            dict(row),
            {
                "total_quantity": 5,
                "available_quantity": 0,
                "status": "PENDING",
                "on_hand_quantity": 5,
            },
        )

    async def test_asset_quantity_bounds_are_database_enforced(self) -> None:
        """Available quantity cannot be negative or greater than total quantity."""

        for total, available in ((-1, 0), (2, -1), (2, 3)):
            with (
                self.subTest(total=total, available=available),
                self.assertRaises(asyncpg.CheckViolationError),
            ):
                async with self.connection.transaction():
                    await self._insert_asset(total=total, available=available)

    async def test_custody_partition_and_current_uniqueness_are_enforced(self) -> None:
        """Custody partitions stay non-negative and one current logical row exists."""

        await self._insert_asset(total=5, available=0)
        with self.assertRaises(asyncpg.CheckViolationError):
            async with self.connection.transaction():
                await self._insert_pending_custody(on_hand=2, reserved=2, loaned=1)

        await self._insert_pending_custody(on_hand=2, reserved=1, loaned=1)
        with self.assertRaises(asyncpg.UniqueViolationError):
            async with self.connection.transaction():
                await self.connection.execute(
                    """
                    INSERT INTO custodies (
                        asset_id, custodian_user_id, assigned_by_user_id,
                        status, on_hand_quantity
                    ) VALUES ($1, $2, $3, 'PENDING', 1)
                    """,
                    ASSET_ID,
                    OTHER_USER_ID,
                    USER_ID,
                )

    async def test_evidence_policy_and_foreign_keys_are_enforced(self) -> None:
        """Only owned, supported, bounded image metadata can reference a custody."""

        await self._insert_asset(total=1, available=0)
        await self._insert_pending_custody(on_hand=1, reserved=0, loaned=0)

        with self.assertRaises(asyncpg.CheckViolationError):
            async with self.connection.transaction():
                await self._insert_evidence("application/pdf", 100)
        with self.assertRaises(asyncpg.CheckViolationError):
            async with self.connection.transaction():
                await self._insert_evidence("image/jpeg", 10485761)
        with self.assertRaises(asyncpg.ForeignKeyViolationError):
            async with self.connection.transaction():
                await self.connection.execute(
                    """
                    INSERT INTO custody_evidence (
                        custody_id, uploaded_by_user_id, evidence_type, object_key,
                        content_type, content_sha256, size_bytes
                    ) VALUES (
                        gen_random_uuid(), $1, 'ASSIGNMENT_CONFIRMATION',
                        'opaque/missing', 'image/webp', $2, 100
                    )
                    """,
                    OTHER_USER_ID,
                    "b" * 64,
                )

    async def test_required_access_pattern_indexes_exist(self) -> None:
        """Active-custody and pending-reminder scans have named indexes."""

        names = await self.connection.fetch(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = 'public' AND tablename = 'custodies'
            """
        )
        index_names = {str(row["indexname"]) for row in names}
        self.assertTrue(
            {
                "uq_custodies_current_asset_custodian",
                "ix_custodies_active_custodian_asset",
                "ix_custodies_active_asset",
                "ix_custodies_pending_reminder",
            }.issubset(index_names)
        )

    async def _insert_asset(self, total: int, available: int) -> None:
        await self.connection.execute(
            """
            INSERT INTO assets (
                id, category_id, responsible_team_id, name, source,
                total_quantity, available_quantity, created_by_user_id
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            ASSET_ID,
            CATEGORY_ID,
            TEAM_ID,
            "Schema Camera",
            "DONATION",
            total,
            available,
            USER_ID,
        )

    async def _insert_pending_custody(self, on_hand: int, reserved: int, loaned: int) -> None:
        await self.connection.execute(
            """
            INSERT INTO custodies (
                id, asset_id, custodian_user_id, assigned_by_user_id, status,
                on_hand_quantity, reserved_quantity, temporarily_loaned_quantity
            ) VALUES ($1, $2, $3, $4, 'PENDING', $5, $6, $7)
            """,
            CUSTODY_ID,
            ASSET_ID,
            OTHER_USER_ID,
            USER_ID,
            on_hand,
            reserved,
            loaned,
        )

    async def _insert_evidence(self, content_type: str, size_bytes: int) -> None:
        await self.connection.execute(
            """
            INSERT INTO custody_evidence (
                custody_id, uploaded_by_user_id, evidence_type, object_key,
                content_type, content_sha256, size_bytes
            ) VALUES ($1, $2, 'ASSIGNMENT_CONFIRMATION', $3, $4, $5, $6)
            """,
            CUSTODY_ID,
            OTHER_USER_ID,
            f"opaque/{content_type}/{size_bytes}",
            content_type,
            "c" * 64,
            size_bytes,
        )
