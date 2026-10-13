"""PostgreSQL integration tests for F02.03 repositories and Unit of Work."""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ams.application.assets import AssetPersistenceError
from ams.domain.assets import Asset, Custody, CustodyAllocation
from ams.infrastructure.asset_persistence import (
    SqlAlchemyAssetRepository,
    SqlAlchemyAssetUnitOfWork,
)

HR_ID = UUID("00000000-0000-4000-8000-000000000301")
CUSTODIAN_ID = UUID("00000000-0000-4000-8000-000000000302")
MISSING_USER_ID = UUID("00000000-0000-4000-8000-000000000303")
TEAM_ID = UUID("00000000-0000-4000-8000-000000000304")
CATEGORY_ID = UUID("00000000-0000-4000-8000-000000000305")
ASSET_ID = UUID("00000000-0000-4000-8000-000000000306")
CUSTODY_ID = UUID("00000000-0000-4000-8000-000000000307")
NOW = datetime(2026, 10, 4, 10, 0, tzinfo=UTC)


@unittest.skipUnless(os.getenv("AMS_DATABASE_URL"), "AMS_DATABASE_URL is required")
class AssetRepositoryTests(unittest.IsolatedAsyncioTestCase):
    """Prove mapping, transaction rollback, and row-lock semantics."""

    async def asyncSetUp(self) -> None:
        """Create request-style sessions and committed FK fixtures."""

        database_url = os.environ["AMS_DATABASE_URL"]
        self.engine = create_async_engine(database_url, pool_pre_ping=True)
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)
        await self._cleanup()
        async with self.session_maker() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO users (id, username, email, display_name, password_hash)
                    VALUES
                        (:hr_id, 'repo.hr', 'repo.hr@example.test', 'Repository HR', 'hash'),
                        (:custodian_id, 'repo.custodian', 'repo.custodian@example.test',
                         'Repository Custodian', 'hash')
                    """
                ),
                {"hr_id": HR_ID, "custodian_id": CUSTODIAN_ID},
            )
            await session.execute(
                text(
                    "INSERT INTO teams (id, name, code) VALUES (:id, 'Repo Team', 'REPO_TEAM')"
                ),
                {"id": TEAM_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO asset_categories (id, code, name)
                    VALUES (:id, 'REPO_CATEGORY', 'Repository Category')
                    """
                ),
                {"id": CATEGORY_ID},
            )
            await session.commit()

    async def asyncTearDown(self) -> None:
        """Delete committed fixtures and close the test engine."""

        await self._cleanup()
        await self.engine.dispose()

    async def test_add_commit_and_round_trip_preserve_domain_entities(self) -> None:
        """One commit persists Asset/Custody and both repositories map them back."""

        asset = self._asset()
        custody = self._custody()
        async with SqlAlchemyAssetUnitOfWork(self.session_maker) as unit_of_work:
            await unit_of_work.assets.add(asset)
            await unit_of_work.custodies.add(custody)
            await unit_of_work.commit()

        async with SqlAlchemyAssetUnitOfWork(self.session_maker) as unit_of_work:
            loaded_asset = await unit_of_work.assets.get_by_id(ASSET_ID)
            loaded_custody = await unit_of_work.custodies.get_by_id(CUSTODY_ID)

        self.assertEqual(loaded_asset, asset)
        self.assertEqual(loaded_custody, custody)

    async def test_uncommitted_and_explicitly_rolled_back_work_never_persists(self) -> None:
        """Repository methods only stage; Unit of Work controls transaction outcome."""

        async with SqlAlchemyAssetUnitOfWork(self.session_maker) as unit_of_work:
            await unit_of_work.assets.add(self._asset())

        self.assertFalse(await self._asset_exists())

        async with SqlAlchemyAssetUnitOfWork(self.session_maker) as unit_of_work:
            await unit_of_work.assets.add(self._asset())
            await unit_of_work.rollback()

        self.assertFalse(await self._asset_exists())

    async def test_failed_asset_plus_custody_commit_rolls_back_every_row(self) -> None:
        """A Custody FK failure cannot leave its Asset partially committed."""

        invalid_custody = Custody.create_pending(
            custody_id=CUSTODY_ID,
            asset_id=ASSET_ID,
            custodian_user_id=MISSING_USER_ID,
            assigned_by_user_id=HR_ID,
            quantity=2,
            assigned_at=NOW,
        )

        with self.assertRaises(AssetPersistenceError):
            async with SqlAlchemyAssetUnitOfWork(self.session_maker) as unit_of_work:
                await unit_of_work.assets.add(self._asset())
                await unit_of_work.custodies.add(invalid_custody)
                await unit_of_work.commit()

        self.assertFalse(await self._asset_exists())

    async def test_save_updates_without_repository_commit(self) -> None:
        """Save stages a classification update visible only after Unit of Work commit."""

        await self._persist_asset_and_custody()
        async with SqlAlchemyAssetUnitOfWork(self.session_maker) as unit_of_work:
            asset = await unit_of_work.assets.get_for_update(ASSET_ID)
            assert asset is not None
            asset.responsible_team_id = None
            await unit_of_work.assets.save(asset)

        async with SqlAlchemyAssetUnitOfWork(self.session_maker) as unit_of_work:
            unchanged = await unit_of_work.assets.get_by_id(ASSET_ID)
        assert unchanged is not None
        self.assertEqual(unchanged.responsible_team_id, TEAM_ID)

        async with SqlAlchemyAssetUnitOfWork(self.session_maker) as unit_of_work:
            asset = await unit_of_work.assets.get_for_update(ASSET_ID)
            assert asset is not None
            asset.responsible_team_id = None
            await unit_of_work.assets.save(asset)
            await unit_of_work.commit()

        async with SqlAlchemyAssetUnitOfWork(self.session_maker) as unit_of_work:
            changed = await unit_of_work.assets.get_by_id(ASSET_ID)
        assert changed is not None
        self.assertIsNone(changed.responsible_team_id)

    async def test_get_for_update_holds_postgresql_lock_until_transaction_end(self) -> None:
        """A second writer times out while the first repository owns the Asset row lock."""

        await self._persist_asset_and_custody()
        async with self.session_maker() as first, self.session_maker() as second:
            await first.begin()
            await second.begin()
            first_repository = SqlAlchemyAssetRepository(first)
            second_repository = SqlAlchemyAssetRepository(second)
            locked = await first_repository.get_for_update(ASSET_ID)
            assert locked is not None
            await second.execute(text("SET LOCAL lock_timeout = '100ms'"))

            with self.assertRaises(AssetPersistenceError):
                await second_repository.get_for_update(ASSET_ID)

            await second.rollback()
            await first.rollback()

    async def test_missing_records_return_none_and_inactive_uow_rejects_commit(self) -> None:
        """Absent IDs are explicit and transaction methods require an active context."""

        unit_of_work = SqlAlchemyAssetUnitOfWork(self.session_maker)
        with self.assertRaises(RuntimeError):
            await unit_of_work.commit()

        async with unit_of_work:
            self.assertIsNone(await unit_of_work.assets.get_by_id(ASSET_ID))
            self.assertIsNone(await unit_of_work.custodies.get_for_update(CUSTODY_ID))

    async def _persist_asset_and_custody(self) -> None:
        async with SqlAlchemyAssetUnitOfWork(self.session_maker) as unit_of_work:
            await unit_of_work.assets.add(self._asset())
            await unit_of_work.custodies.add(self._custody())
            await unit_of_work.commit()

    async def _asset_exists(self) -> bool:
        async with self.session_maker() as session:
            result = await session.execute(
                text("SELECT 1 FROM assets WHERE id = :id"), {"id": ASSET_ID}
            )
            return result.scalar_one_or_none() is not None

    async def _cleanup(self) -> None:
        async with self.session_maker() as session:
            await session.execute(
                text("DELETE FROM custody_evidence WHERE custody_id = :custody_id"),
                {"custody_id": CUSTODY_ID},
            )
            await session.execute(
                text("DELETE FROM custodies WHERE id = :custody_id"),
                {"custody_id": CUSTODY_ID},
            )
            await session.execute(
                text("DELETE FROM assets WHERE id = :asset_id"), {"asset_id": ASSET_ID}
            )
            await session.execute(
                text("DELETE FROM asset_categories WHERE id = :category_id"),
                {"category_id": CATEGORY_ID},
            )
            await session.execute(
                text("DELETE FROM teams WHERE id = :team_id"), {"team_id": TEAM_ID}
            )
            await session.execute(
                text("DELETE FROM users WHERE id IN (:hr_id, :custodian_id)"),
                {"hr_id": HR_ID, "custodian_id": CUSTODIAN_ID},
            )
            await session.commit()

    def _asset(self) -> Asset:
        return Asset.create(
            asset_id=ASSET_ID,
            category_id=CATEGORY_ID,
            responsible_team_id=TEAM_ID,
            name="Repository Camera",
            source="DONATION",
            total_quantity=2,
            allocations=(CustodyAllocation(CUSTODIAN_ID, 2),),
            created_by_user_id=HR_ID,
            created_at=NOW,
        )

    def _custody(self) -> Custody:
        return Custody.create_pending(
            custody_id=CUSTODY_ID,
            asset_id=ASSET_ID,
            custodian_user_id=CUSTODIAN_ID,
            assigned_by_user_id=HR_ID,
            quantity=2,
            assigned_at=NOW,
        )
