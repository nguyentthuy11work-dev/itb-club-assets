"""Unit tests for authorization-aware operational query services."""

from __future__ import annotations

import unittest
from datetime import UTC, date, datetime
from uuid import UUID

from ams.application.operational_queries import (
    InvalidTransactionRangeError,
    OperationalReadNotFoundError,
    OperationalReadService,
)
from ams.application.reliability import IClock
from ams.domain.identity import AuthorizationError, CurrentActor, OperationalRole
from ams.domain.operational_views import (
    AssetTransactionKind,
    AssetTransactionView,
    DashboardAction,
    DashboardReferenceType,
    DashboardTodoKind,
    DashboardTodoView,
    HrDashboardStats,
    HrDashboardView,
    IOperationalReadRepository,
    MemberDashboardStats,
    MemberDashboardView,
)

ACTOR_ID = UUID("00000000-0000-4000-8000-000000006001")
ASSET_ID = UUID("00000000-0000-4000-8000-000000006002")
TEAM_ID = UUID("00000000-0000-4000-8000-000000006003")
NOW = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)


class FixedClock(IClock):
    """Return one deterministic instant."""

    def now(self) -> datetime:
        """Return the fixed instant."""

        return NOW


class FakeOperationalRepository(IOperationalReadRepository):
    """Capture query arguments and return deterministic projections."""

    def __init__(self) -> None:
        self.asset_scope: tuple[bool, UUID | None] = (True, TEAM_ID)
        self.transaction_args: dict[str, object] = {}
        todo = DashboardTodoView(
            "todo",
            DashboardTodoKind.TICKET_APPROVAL,
            DashboardAction.REVIEW_TICKET,
            DashboardReferenceType.TICKET,
            ASSET_ID,
        )
        self.member = MemberDashboardView(MemberDashboardStats(1, 2, 3, 4, 5, 6), (todo,))
        self.hr = HrDashboardView(HrDashboardStats(7, 8, 9, 10, 11, 12), (todo,))

    async def get_asset_scope(self, asset_id: UUID) -> tuple[bool, UUID | None]:
        """Return configured scope."""

        return self.asset_scope

    async def list_transactions(
        self,
        *,
        asset_id: UUID | None,
        from_time: datetime | None,
        to_time: datetime | None,
        offset: int,
        limit: int,
    ) -> tuple[tuple[AssetTransactionView, ...], int]:
        """Capture filters and return one row."""

        self.transaction_args = {
            "asset_id": asset_id,
            "from_time": from_time,
            "to_time": to_time,
            "offset": offset,
            "limit": limit,
        }
        row = AssetTransactionView(
            "event",
            ASSET_ID,
            "Camera",
            AssetTransactionKind.ASSET_CREATED,
            "ASSET_CREATED",
            2,
            NOW,
            ACTOR_ID,
            "Actor",
        )
        return (row,), 1

    async def get_member_dashboard(self, actor_id: UUID, *, today: date) -> MemberDashboardView:
        """Return the member projection."""

        self.member_args = (actor_id, today)
        return self.member

    async def get_hr_dashboard(
        self, *, today: date, custody_overdue_before: datetime
    ) -> HrDashboardView:
        """Return the HR projection."""

        self.hr_args = (today, custody_overdue_before)
        return self.hr


class OperationalReadServiceTests(unittest.IsolatedAsyncioTestCase):
    """Verify scope, time validation, paging, and dashboard clocks."""

    async def test_asset_history_allows_hr_and_explicit_team_lead_scope(self) -> None:
        repository = FakeOperationalRepository()
        service = OperationalReadService(repository, FixedClock())
        aware = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
        page = await service.list_asset_transactions(
            _actor(OperationalRole.HR),
            ASSET_ID,
            from_time=aware,
            to_time=NOW,
            offset=2,
            limit=5,
        )
        self.assertEqual((page.total, page.offset, page.limit), (1, 2, 5))
        self.assertEqual(repository.transaction_args["from_time"], aware)
        lead = _actor(OperationalRole.TEAM_LEAD_ASSET, teams=frozenset({TEAM_ID}))
        await service.list_asset_transactions(
            lead, ASSET_ID, from_time=None, to_time=None, offset=0, limit=20
        )

    async def test_asset_history_rejects_missing_unscoped_and_inactive_actor(self) -> None:
        repository = FakeOperationalRepository()
        service = OperationalReadService(repository, FixedClock())
        repository.asset_scope = (False, None)
        with self.assertRaises(OperationalReadNotFoundError):
            await service.list_asset_transactions(
                _actor(OperationalRole.HR),
                ASSET_ID,
                from_time=None,
                to_time=None,
                offset=0,
                limit=20,
            )
        repository.asset_scope = (True, None)
        for actor in (
            _actor(OperationalRole.TEAM_LEAD_ASSET, teams=frozenset({TEAM_ID})),
            _actor(),
        ):
            with self.assertRaises(AuthorizationError):
                await service.list_asset_transactions(
                    actor,
                    ASSET_ID,
                    from_time=None,
                    to_time=None,
                    offset=0,
                    limit=20,
                )
        with self.assertRaises(AuthorizationError):
            await service.list_asset_transactions(
                _actor(OperationalRole.HR, active=False),
                ASSET_ID,
                from_time=None,
                to_time=None,
                offset=0,
                limit=20,
            )

    async def test_hr_history_validates_role_and_timezone_range(self) -> None:
        repository = FakeOperationalRepository()
        service = OperationalReadService(repository, FixedClock())
        page = await service.list_hr_transactions(
            _actor(OperationalRole.HR),
            asset_id=None,
            from_time=None,
            to_time=None,
            offset=0,
            limit=10,
        )
        self.assertEqual(page.total, 1)
        with self.assertRaises(AuthorizationError):
            await service.list_hr_transactions(
                _actor(),
                asset_id=None,
                from_time=None,
                to_time=None,
                offset=0,
                limit=10,
            )
        for start, end in (
            (datetime(2026, 10, 1), NOW),
            (NOW, datetime(2026, 10, 1, tzinfo=UTC)),
        ):
            with self.assertRaises(InvalidTransactionRangeError):
                await service.list_hr_transactions(
                    _actor(OperationalRole.HR),
                    asset_id=None,
                    from_time=start,
                    to_time=end,
                    offset=0,
                    limit=10,
                )

    async def test_dashboards_use_local_date_and_hr_cutoff(self) -> None:
        repository = FakeOperationalRepository()
        service = OperationalReadService(repository, FixedClock())
        member = await service.get_member_dashboard(_actor())
        hr = await service.get_hr_dashboard(_actor(OperationalRole.HR))
        self.assertIs(member, repository.member)
        self.assertIs(hr, repository.hr)
        self.assertEqual(repository.member_args, (ACTOR_ID, date(2026, 10, 5)))
        self.assertEqual(repository.hr_args, (date(2026, 10, 5), NOW.replace(day=4)))
        with self.assertRaises(AuthorizationError):
            await service.get_member_dashboard(_actor(active=False))
        with self.assertRaises(AuthorizationError):
            await service.get_hr_dashboard(_actor())


def _actor(
    *roles: OperationalRole,
    teams: frozenset[UUID] = frozenset(),
    active: bool = True,
) -> CurrentActor:
    return CurrentActor(ACTOR_ID, frozenset(roles), teams, active, "Actor")
