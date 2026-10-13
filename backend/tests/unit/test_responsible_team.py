"""Unit tests for Responsible Team classification administration."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from types import TracebackType
from typing import cast
from uuid import UUID

from ams.application.assets import IAssetUnitOfWork
from ams.application.responsible_team import (
    ResponsibleTeamAssetNotFoundError,
    ResponsibleTeamNotFoundError,
    ResponsibleTeamService,
)
from ams.domain.assets import Asset
from ams.domain.identity import AuthorizationError, CurrentActor, OperationalRole
from ams.domain.repositories import IAssetRepository, ICustodyRepository

HR_ID = UUID("00000000-0000-4000-8000-000000004201")
ASSET_ID = UUID("00000000-0000-4000-8000-000000004202")
CATEGORY_ID = UUID("00000000-0000-4000-8000-000000004203")
TEAM_ID = UUID("00000000-0000-4000-8000-000000004204")
NOW = datetime(2026, 10, 4, 22, 0, tzinfo=UTC)


class FakeAssetRepository(IAssetRepository):
    """Expose one lockable Asset and configurable active Team state."""

    def __init__(self, asset: Asset | None) -> None:
        self.asset = asset
        self.team_exists = True
        self.saved: list[Asset] = []

    async def get_by_id(self, asset_id: UUID) -> Asset | None:
        """Reject unlocked access in this command."""

        raise AssertionError("not used")

    async def get_for_update(self, asset_id: UUID) -> Asset | None:
        """Return the configured locked Asset projection."""

        return self.asset if asset_id == ASSET_ID else None

    async def add(self, asset: Asset) -> None:
        """Reject inserts in a classification-only command."""

        raise AssertionError("not used")

    async def save(self, asset: Asset) -> None:
        """Capture the aggregate staged for update."""

        self.saved.append(asset)

    async def active_category_exists(self, category_id: UUID) -> bool:
        """Reject category reads in this command."""

        raise AssertionError("not used")

    async def active_team_exists(self, team_id: UUID) -> bool:
        """Return configured active Team state."""

        return self.team_exists and team_id == TEAM_ID


class FakeAssetUnitOfWork(IAssetUnitOfWork):
    """Capture classification transaction completion without Custody access."""

    def __init__(self, asset: Asset | None) -> None:
        self.assets: FakeAssetRepository = FakeAssetRepository(asset)
        self.custodies: ICustodyRepository = cast(ICustodyRepository, object())
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self) -> FakeAssetUnitOfWork:
        """Open the fake transaction."""

        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the fake transaction."""

    async def commit(self) -> None:
        """Record successful transaction completion."""

        self.commits += 1

    async def rollback(self) -> None:
        """Record explicit rollback requests."""

        self.rollbacks += 1


class ResponsibleTeamServiceTests(unittest.IsolatedAsyncioTestCase):
    """Verify HR authorization, references, and classification-only mutation."""

    async def test_hr_assigns_and_clears_responsible_team(self) -> None:
        """Both approved nullable classifications save and commit the Asset only."""

        asset = _asset()
        unit_of_work = FakeAssetUnitOfWork(asset)
        service = ResponsibleTeamService(lambda: unit_of_work)

        assigned = await service.update(ASSET_ID, TEAM_ID, _hr())
        cleared = await service.update(ASSET_ID, None, _hr())

        self.assertEqual((assigned.asset_id, assigned.responsible_team_id), (ASSET_ID, TEAM_ID))
        self.assertEqual(cleared.responsible_team_id, None)
        self.assertEqual(unit_of_work.assets.saved, [asset, asset])
        self.assertEqual(unit_of_work.commits, 2)
        self.assertEqual(asset.total_quantity, 3)
        self.assertEqual(asset.available_quantity, 2)

    async def test_missing_asset_or_team_is_rejected_without_save(self) -> None:
        """Unknown references cannot stage or commit classification changes."""

        missing_asset = FakeAssetUnitOfWork(None)
        with self.assertRaises(ResponsibleTeamAssetNotFoundError):
            await ResponsibleTeamService(lambda: missing_asset).update(ASSET_ID, TEAM_ID, _hr())
        self.assertEqual(missing_asset.commits, 0)

        missing_team = FakeAssetUnitOfWork(_asset())
        missing_team.assets.team_exists = False
        with self.assertRaises(ResponsibleTeamNotFoundError):
            await ResponsibleTeamService(lambda: missing_team).update(ASSET_ID, TEAM_ID, _hr())
        self.assertEqual(missing_team.assets.saved, [])
        self.assertEqual(missing_team.commits, 0)

    async def test_non_hr_and_inactive_hr_are_rejected_before_transaction(self) -> None:
        """Application authorization protects callers outside the HTTP route."""

        factory_calls = 0

        def factory() -> FakeAssetUnitOfWork:
            nonlocal factory_calls
            factory_calls += 1
            return FakeAssetUnitOfWork(_asset())

        service = ResponsibleTeamService(factory)
        for actor in (
            CurrentActor(HR_ID, frozenset(), is_active=True),
            CurrentActor(HR_ID, frozenset({OperationalRole.HR}), is_active=False),
        ):
            with self.subTest(actor=actor), self.assertRaises(AuthorizationError):
                await service.update(ASSET_ID, TEAM_ID, actor)
        self.assertEqual(factory_calls, 0)


def _asset() -> Asset:
    return Asset(
        id=ASSET_ID,
        category_id=CATEGORY_ID,
        responsible_team_id=None,
        name="Classification Camera",
        source="PURCHASE",
        total_quantity=3,
        available_quantity=2,
        created_by_user_id=HR_ID,
        created_at=NOW,
    )


def _hr() -> CurrentActor:
    return CurrentActor(HR_ID, frozenset({OperationalRole.HR}), is_active=True)
