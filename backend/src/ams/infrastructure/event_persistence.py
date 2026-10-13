"""SQLAlchemy adapters for Flyway-owned Event Day configuration records."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, delete, select, update
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from ams.application.event_days import EventPersistenceError
from ams.domain.events import (
    EventDay,
    EventDayStatus,
    IEventDayRepository,
    Shift,
    ShiftAsset,
    ShiftStatus,
)
from ams.infrastructure.asset_persistence import AssetModel, AssetPersistenceBase

EVENT_DAY_STATUS_ENUM = ENUM(EventDayStatus, name="event_day_status", create_type=False)
SHIFT_STATUS_ENUM = ENUM(ShiftStatus, name="shift_status", create_type=False)


class EventDayModel(AssetPersistenceBase):
    """Technical mapping for ``event_days``."""

    __tablename__ = "event_days"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    event_date: Mapped[date] = mapped_column(Date)
    timezone: Mapped[str] = mapped_column(String(64))
    status: Mapped[EventDayStatus] = mapped_column(EVENT_DAY_STATUS_ENUM)
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ShiftModel(AssetPersistenceBase):
    """Technical mapping for ``shifts``."""

    __tablename__ = "shifts"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    event_day_id: Mapped[UUID] = mapped_column(ForeignKey("event_days.id"))
    sequence: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(200))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    shift_lead_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    status: Mapped[ShiftStatus] = mapped_column(SHIFT_STATUS_ENUM)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ShiftAssetModel(AssetPersistenceBase):
    """Technical mapping for configured Shift Asset snapshots."""

    __tablename__ = "shift_assets"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    shift_id: Mapped[UUID] = mapped_column(ForeignKey("shifts.id"))
    asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"))
    quota_quantity: Mapped[int] = mapped_column(Integer)
    carry_over_quantity: Mapped[int] = mapped_column(Integer)
    expected_open_quantity: Mapped[int] = mapped_column(Integer)
    opening_actual_quantity: Mapped[int | None] = mapped_column(Integer)
    closing_actual_quantity: Mapped[int | None] = mapped_column(Integer)
    consumed_quantity: Mapped[int | None] = mapped_column(Integer)
    discrepancy_reason: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SqlAlchemyEventDayRepository(IEventDayRepository):
    """Persist complete Event configuration without owning a commit."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, event_day: EventDay, *, created_at: datetime) -> None:
        """Stage the aggregate and all child snapshots."""

        self._session.add(
            EventDayModel(
                id=event_day.id,
                name=event_day.name,
                event_date=event_day.event_date,
                timezone="Asia/Ho_Chi_Minh",
                status=event_day.status,
                created_by_user_id=event_day.created_by_user_id,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        await self._flush()
        for shift in event_day.shifts:
            self._session.add(self._shift_model(event_day.id, shift, created_at))
        await self._flush()
        for shift in event_day.shifts:
            for item in shift.assets:
                self._session.add(self._asset_model(shift.id, item, created_at))
        await self._flush()

    async def get(self, event_day_id: UUID) -> EventDay | None:
        """Load one aggregate without a write lock."""

        return await self._get(event_day_id, for_update=False)

    async def get_for_update(self, event_day_id: UUID) -> EventDay | None:
        """Lock the aggregate root before loading its configuration children."""

        return await self._get(event_day_id, for_update=True)

    async def save(self, event_day: EventDay, *, updated_at: datetime) -> None:
        """Diff Shift identities and replace only draft configuration rows."""

        try:
            await self._session.execute(
                update(EventDayModel)
                .where(EventDayModel.id == event_day.id)
                .values(
                    name=event_day.name,
                    event_date=event_day.event_date,
                    status=event_day.status,
                    updated_at=updated_at,
                )
            )
            result = await self._session.execute(
                select(ShiftModel)
                .where(ShiftModel.event_day_id == event_day.id)
                .with_for_update()
            )
            existing = {model.id: model for model in result.scalars()}
            desired_ids = {shift.id for shift in event_day.shifts}
            removed_ids = set(existing) - desired_ids
            if removed_ids:
                await self._session.execute(
                    delete(ShiftAssetModel).where(ShiftAssetModel.shift_id.in_(removed_ids))
                )
                await self._session.execute(
                    delete(ShiftModel).where(ShiftModel.id.in_(removed_ids))
                )

            retained_ids = set(existing) & desired_ids
            if retained_ids:
                await self._session.execute(
                    update(ShiftModel)
                    .where(ShiftModel.id.in_(retained_ids))
                    .values(sequence=ShiftModel.sequence + 1_000_000)
                )
                await self._session.execute(
                    delete(ShiftAssetModel).where(ShiftAssetModel.shift_id.in_(retained_ids))
                )
            for shift in event_day.shifts:
                model = existing.get(shift.id)
                if model is None:
                    self._session.add(self._shift_model(event_day.id, shift, updated_at))
                else:
                    model.sequence = shift.sequence
                    model.name = shift.name
                    model.starts_at = shift.starts_at
                    model.ends_at = shift.ends_at
                    model.shift_lead_user_id = shift.shift_lead_user_id
                    model.updated_at = updated_at
            await self._session.flush()
            for shift in event_day.shifts:
                for item in shift.assets:
                    self._session.add(self._asset_model(shift.id, item, updated_at))
            await self._session.flush()
        except SQLAlchemyError as exception:
            raise EventPersistenceError("Event persistence operation failed.") from exception

    async def assets_exist(self, asset_ids: tuple[UUID, ...]) -> bool:
        """Check all selected identities in one set query."""

        if not asset_ids:
            return False
        try:
            result = await self._session.execute(
                select(AssetModel.id).where(AssetModel.id.in_(asset_ids))
            )
        except SQLAlchemyError as exception:
            raise EventPersistenceError("Event reference lookup failed.") from exception
        return set(result.scalars()) == set(asset_ids)

    async def get_shift_for_update(self, shift_id: UUID) -> Shift | None:
        """Lock and load one Shift plus its configured Asset quotas."""

        try:
            result = await self._session.execute(
                select(ShiftModel).where(ShiftModel.id == shift_id).with_for_update()
            )
            model = result.scalar_one_or_none()
            if model is None:
                return None
            asset_result = await self._session.execute(
                select(ShiftAssetModel)
                .where(ShiftAssetModel.shift_id == shift_id)
                .order_by(ShiftAssetModel.asset_id, ShiftAssetModel.id)
            )
            assets = tuple(
                ShiftAsset(
                    id=item.id,
                    asset_id=item.asset_id,
                    quota_quantity=item.quota_quantity,
                    carry_over_quantity=item.carry_over_quantity,
                )
                for item in asset_result.scalars()
            )
        except SQLAlchemyError as exception:
            raise EventPersistenceError("Event persistence operation failed.") from exception
        return Shift(
            id=model.id,
            sequence=model.sequence,
            name=model.name,
            starts_at=model.starts_at,
            ends_at=model.ends_at,
            shift_lead_user_id=model.shift_lead_user_id,
            assets=assets,
            status=ShiftStatus(model.status),
        )

    async def _get(self, event_day_id: UUID, *, for_update: bool) -> EventDay | None:
        statement = select(EventDayModel).where(EventDayModel.id == event_day_id)
        if for_update:
            statement = statement.with_for_update()
        try:
            event_result = await self._session.execute(statement)
            event = event_result.scalar_one_or_none()
            if event is None:
                return None
            shift_result = await self._session.execute(
                select(ShiftModel)
                .where(ShiftModel.event_day_id == event_day_id)
                .order_by(ShiftModel.sequence, ShiftModel.id)
            )
            shifts: list[Shift] = []
            for model in shift_result.scalars():
                asset_result = await self._session.execute(
                    select(ShiftAssetModel)
                    .where(ShiftAssetModel.shift_id == model.id)
                    .order_by(ShiftAssetModel.asset_id, ShiftAssetModel.id)
                )
                assets = tuple(
                    ShiftAsset(
                        id=item.id,
                        asset_id=item.asset_id,
                        quota_quantity=item.quota_quantity,
                        carry_over_quantity=item.carry_over_quantity,
                    )
                    for item in asset_result.scalars()
                )
                shifts.append(
                    Shift(
                        id=model.id,
                        sequence=model.sequence,
                        name=model.name,
                        starts_at=model.starts_at,
                        ends_at=model.ends_at,
                        shift_lead_user_id=model.shift_lead_user_id,
                        assets=assets,
                        status=ShiftStatus(model.status),
                    )
                )
        except SQLAlchemyError as exception:
            raise EventPersistenceError("Event persistence operation failed.") from exception
        return EventDay(
            id=event.id,
            name=event.name,
            event_date=event.event_date,
            created_by_user_id=event.created_by_user_id,
            shifts=tuple(shifts),
            status=event.status,
        )

    @staticmethod
    def _shift_model(event_day_id: UUID, shift: Shift, created_at: datetime) -> ShiftModel:
        return ShiftModel(
            id=shift.id,
            event_day_id=event_day_id,
            sequence=shift.sequence,
            name=shift.name,
            starts_at=shift.starts_at,
            ends_at=shift.ends_at,
            shift_lead_user_id=shift.shift_lead_user_id,
            status=shift.status,
            created_at=created_at,
            updated_at=created_at,
        )

    @staticmethod
    def _asset_model(shift_id: UUID, item: ShiftAsset, timestamp: datetime) -> ShiftAssetModel:
        return ShiftAssetModel(
            id=item.id,
            shift_id=shift_id,
            asset_id=item.asset_id,
            quota_quantity=item.quota_quantity,
            carry_over_quantity=item.carry_over_quantity,
            expected_open_quantity=item.expected_open_quantity,
            opening_actual_quantity=None,
            closing_actual_quantity=None,
            consumed_quantity=None,
            discrepancy_reason=None,
            created_at=timestamp,
            updated_at=timestamp,
        )

    async def _flush(self) -> None:
        try:
            await self._session.flush()
        except SQLAlchemyError as exception:
            raise EventPersistenceError("Event persistence operation failed.") from exception
