"""Authorization-aware transaction history and dashboard query services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from ams.application.reliability import IClock
from ams.domain.identity import AuthorizationError, CurrentActor, OperationalRole
from ams.domain.operational_views import (
    AssetTransactionView,
    HrDashboardView,
    IOperationalReadRepository,
    MemberDashboardView,
)

LOCAL_TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")


class OperationalReadError(Exception):
    """Base class for expected operational query failures."""


class OperationalReadNotFoundError(OperationalReadError):
    """Requested Asset history target does not exist."""

    code = "ASSET_NOT_FOUND"


class OperationalReadPersistenceError(OperationalReadError):
    """Infrastructure could not load an operational projection."""

    code = "OPERATIONAL_READ_FAILED"


class InvalidTransactionRangeError(OperationalReadError):
    """Transaction time range is ambiguous or reversed."""

    code = "INVALID_TRANSACTION_RANGE"


@dataclass(frozen=True, slots=True)
class AssetTransactionPage:
    """Stable page of immutable Asset transactions."""

    items: tuple[AssetTransactionView, ...]
    offset: int
    limit: int
    total: int


class OperationalReadService:
    """Serve operational projections through one injected read repository."""

    def __init__(self, repository: IOperationalReadRepository, clock: IClock) -> None:
        self._repository = repository
        self._clock = clock

    async def list_asset_transactions(
        self,
        actor: CurrentActor,
        asset_id: UUID,
        *,
        from_time: datetime | None,
        to_time: datetime | None,
        offset: int,
        limit: int,
    ) -> AssetTransactionPage:
        """Return Asset history to HR or its explicitly scoped Asset Team Lead."""

        self._ensure_active(actor)
        exists, team_id = await self._repository.get_asset_scope(asset_id)
        if not exists:
            raise OperationalReadNotFoundError("Asset is not available.")
        is_hr = actor.has_role(OperationalRole.HR)
        is_scoped_lead = (
            actor.has_role(OperationalRole.TEAM_LEAD_ASSET)
            and team_id is not None
            and team_id in actor.team_ids
        )
        if not is_hr and not is_scoped_lead:
            raise AuthorizationError("Actor cannot read this Asset transaction history.")
        return await self._page(
            asset_id=asset_id,
            from_time=from_time,
            to_time=to_time,
            offset=offset,
            limit=limit,
        )

    async def list_hr_transactions(
        self,
        actor: CurrentActor,
        *,
        asset_id: UUID | None,
        from_time: datetime | None,
        to_time: datetime | None,
        offset: int,
        limit: int,
    ) -> AssetTransactionPage:
        """Return globally filtered Asset history to HR only."""

        self._require_hr(actor)
        return await self._page(
            asset_id=asset_id,
            from_time=from_time,
            to_time=to_time,
            offset=offset,
            limit=limit,
        )

    async def get_member_dashboard(self, actor: CurrentActor) -> MemberDashboardView:
        """Return the authenticated actor's current counts and typed work queue."""

        self._ensure_active(actor)
        today = self._clock.now().astimezone(LOCAL_TIMEZONE).date()
        return await self._repository.get_member_dashboard(actor.id, today=today)

    async def get_hr_dashboard(self, actor: CurrentActor) -> HrDashboardView:
        """Return HR's club-wide current counts and intervention queue."""

        self._require_hr(actor)
        now = self._clock.now()
        return await self._repository.get_hr_dashboard(
            today=now.astimezone(LOCAL_TIMEZONE).date(),
            custody_overdue_before=now - timedelta(hours=24),
        )

    async def _page(
        self,
        *,
        asset_id: UUID | None,
        from_time: datetime | None,
        to_time: datetime | None,
        offset: int,
        limit: int,
    ) -> AssetTransactionPage:
        from_time = self._validated_time(from_time)
        to_time = self._validated_time(to_time)
        if from_time is not None and to_time is not None and from_time > to_time:
            raise InvalidTransactionRangeError("Transaction time range is reversed.")
        items, total = await self._repository.list_transactions(
            asset_id=asset_id,
            from_time=from_time,
            to_time=to_time,
            offset=offset,
            limit=limit,
        )
        return AssetTransactionPage(items, offset, limit, total)

    @staticmethod
    def _validated_time(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.utcoffset() is None:
            raise InvalidTransactionRangeError("Transaction times must include a timezone.")
        return value.astimezone(UTC)

    @staticmethod
    def _ensure_active(actor: CurrentActor) -> None:
        if not actor.is_active:
            raise AuthorizationError("Inactive accounts cannot read operational data.")

    @classmethod
    def _require_hr(cls, actor: CurrentActor) -> None:
        cls._ensure_active(actor)
        if not actor.has_role(OperationalRole.HR):
            raise AuthorizationError("HR role is required for this operational view.")
