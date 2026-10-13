"""SQLAlchemy adapter for Shift execution and Event Day summary projections."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import JSON, DateTime, ForeignKey, String, func, select, update
from sqlalchemy.engine import Result
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.base import Executable

from ams.domain.events import EventDayStatus, ShiftStatus
from ams.domain.shift_execution import (
    EventDayAssetSummary,
    EventDayExecutionSummary,
    IShiftExecutionRepository,
    ShiftEvent,
    ShiftExecution,
    ShiftExecutionItem,
    ShiftPersistenceError,
)
from ams.infrastructure.asset_persistence import AssetPersistenceBase
from ams.infrastructure.event_persistence import (
    EventDayModel,
    ShiftAssetModel,
    ShiftModel,
)


class ShiftEventModel(AssetPersistenceBase):
    """Technical mapping for immutable ``shift_events`` audit rows."""

    __tablename__ = "shift_events"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    shift_id: Mapped[UUID] = mapped_column(ForeignKey("shifts.id"))
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    event_type: Mapped[str] = mapped_column(String(64))
    from_status: Mapped[ShiftStatus | None] = mapped_column(ShiftModel.__table__.c.status.type)
    to_status: Mapped[ShiftStatus] = mapped_column(ShiftModel.__table__.c.status.type)
    metadata_json: Mapped[dict[str, object]] = mapped_column("metadata", JSON)
    occurred_at: Mapped[Any] = mapped_column(DateTime(timezone=True))


class SqlAlchemyShiftExecutionRepository(IShiftExecutionRepository):
    """Persist locked Shift transitions and build final Event projections."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_assignee(
        self, actor_id: UUID, *, offset: int, limit: int
    ) -> tuple[tuple[ShiftExecution, ...], int]:
        """Return a deterministic page scoped by assigned Shift Lead."""

        try:
            count_result = await self._session.execute(
                select(func.count())
                .select_from(ShiftModel)
                .where(ShiftModel.shift_lead_user_id == actor_id)
            )
            id_result = await self._session.execute(
                select(ShiftModel.id)
                .where(ShiftModel.shift_lead_user_id == actor_id)
                .order_by(ShiftModel.starts_at, ShiftModel.id)
                .offset(offset)
                .limit(limit)
            )
            loaded_items: list[ShiftExecution] = []
            for shift_id in id_result.scalars():
                shift = await self._get(shift_id, for_update=False)
                if shift is not None:
                    loaded_items.append(shift)
            items = tuple(loaded_items)
        except SQLAlchemyError as exception:
            raise ShiftPersistenceError("Shift read operation failed.") from exception
        return items, int(count_result.scalar_one())

    async def get(self, shift_id: UUID) -> ShiftExecution | None:
        """Load one Shift without a write lock."""

        return await self._get(shift_id, for_update=False)

    async def get_for_update(self, shift_id: UUID) -> ShiftExecution | None:
        """Lock one Shift row before a transition."""

        return await self._get(shift_id, for_update=True)

    async def get_predecessor_for_update(
        self, event_day_id: UUID, sequence: int
    ) -> ShiftExecution | None:
        """Lock and return the immediately preceding Shift."""

        if sequence <= 1:
            return None
        try:
            result = await self._session.execute(
                select(ShiftModel.id)
                .where(
                    ShiftModel.event_day_id == event_day_id,
                    ShiftModel.sequence == sequence - 1,
                )
                .with_for_update()
            )
            shift_id = result.scalar_one_or_none()
        except SQLAlchemyError as exception:
            raise ShiftPersistenceError("Shift predecessor lookup failed.") from exception
        return None if shift_id is None else await self._get(shift_id, for_update=False)

    async def save_transition(
        self,
        shift: ShiftExecution,
        event: ShiftEvent,
        *,
        event_day_status: EventDayStatus,
    ) -> None:
        """Stage the complete transition and immutable event in one transaction."""

        try:
            await self._session.execute(
                update(ShiftModel)
                .where(ShiftModel.id == shift.id)
                .values(status=shift.status, updated_at=event.occurred_at)
            )
            for item in shift.items:
                await self._session.execute(
                    update(ShiftAssetModel)
                    .where(ShiftAssetModel.id == item.id)
                    .values(
                        carry_over_quantity=item.carry_over_quantity,
                        expected_open_quantity=item.expected_open_quantity,
                        opening_actual_quantity=item.opening_actual_quantity,
                        closing_actual_quantity=item.closing_actual_quantity,
                        consumed_quantity=item.consumed_quantity,
                        discrepancy_reason=item.discrepancy_reason,
                        updated_at=event.occurred_at,
                    )
                )
            await self._session.execute(
                update(EventDayModel)
                .where(EventDayModel.id == shift.event_day_id)
                .values(status=event_day_status, updated_at=event.occurred_at)
            )
            self._session.add(
                ShiftEventModel(
                    id=event.id,
                    shift_id=event.shift_id,
                    actor_user_id=event.actor_user_id,
                    event_type=event.event_type,
                    from_status=event.from_status,
                    to_status=event.to_status,
                    metadata_json=event.metadata,
                    occurred_at=event.occurred_at,
                )
            )
            await self._session.flush()
        except SQLAlchemyError as exception:
            raise ShiftPersistenceError("Shift transition persistence failed.") from exception

    async def all_other_shifts_closed(self, event_day_id: UUID, shift_id: UUID) -> bool:
        """Check the completion gate while excluding the in-memory closing Shift."""

        try:
            result = await self._session.execute(
                select(func.count())
                .select_from(ShiftModel)
                .where(
                    ShiftModel.event_day_id == event_day_id,
                    ShiftModel.id != shift_id,
                    ShiftModel.status != ShiftStatus.CLOSED,
                )
            )
        except SQLAlchemyError as exception:
            raise ShiftPersistenceError("Event completion check failed.") from exception
        return int(result.scalar_one()) == 0

    async def get_event_day_summary(
        self, event_day_id: UUID
    ) -> EventDayExecutionSummary | None:
        """Return progress and calculate final totals only past the close gate."""

        try:
            event_result = await self._session.execute(
                select(EventDayModel).where(EventDayModel.id == event_day_id)
            )
            event = event_result.scalar_one_or_none()
            if event is None:
                return None
            count_result = await self._session.execute(
                select(
                    func.count(),
                    func.count().filter(ShiftModel.status == ShiftStatus.CLOSED),
                ).where(ShiftModel.event_day_id == event_day_id)
            )
            total, closed = count_result.one()
            if not total or closed != total:
                return EventDayExecutionSummary(
                    event_day_id, event.status, int(total), int(closed), None
                )
            rows = await self._session.execute(
                select(ShiftModel.sequence, ShiftAssetModel)
                .join(ShiftAssetModel, ShiftAssetModel.shift_id == ShiftModel.id)
                .where(ShiftModel.event_day_id == event_day_id)
                .order_by(ShiftModel.sequence, ShiftAssetModel.asset_id)
            )
        except SQLAlchemyError as exception:
            raise ShiftPersistenceError("Event summary read failed.") from exception

        totals: dict[UUID, dict[str, int]] = {}
        for _, item in rows:
            values = totals.setdefault(
                item.asset_id,
                {"quota": 0, "consumed": 0, "variance": 0, "final": 0},
            )
            values["quota"] += item.quota_quantity
            values["consumed"] += item.consumed_quantity or 0
            values["variance"] += item.opening_actual_quantity - item.expected_open_quantity
            values["final"] = item.closing_actual_quantity or 0
        items = tuple(
            EventDayAssetSummary(
                asset_id=asset_id,
                total_quota_quantity=values["quota"],
                total_consumed_quantity=values["consumed"],
                opening_variance_quantity=values["variance"],
                final_carry_over_quantity=values["final"],
            )
            for asset_id, values in sorted(totals.items(), key=lambda value: str(value[0]))
        )
        return EventDayExecutionSummary(
            event_day_id, EventDayStatus.COMPLETED, int(total), int(closed), items
        )

    async def _get(self, shift_id: UUID, *, for_update: bool) -> ShiftExecution | None:
        statement = (
            select(ShiftModel, EventDayModel.name, EventDayModel.created_by_user_id)
            .join(EventDayModel, EventDayModel.id == ShiftModel.event_day_id)
            .where(ShiftModel.id == shift_id)
        )
        if for_update:
            statement = statement.with_for_update(of=ShiftModel)
        try:
            result = await self._session.execute(statement)
            row = result.one_or_none()
            if row is None:
                return None
            model, event_name, creator_id = row
            item_result = await self._session.execute(
                select(ShiftAssetModel)
                .where(ShiftAssetModel.shift_id == shift_id)
                .order_by(ShiftAssetModel.asset_id, ShiftAssetModel.id)
            )
        except SQLAlchemyError as exception:
            raise ShiftPersistenceError("Shift read operation failed.") from exception
        return ShiftExecution(
            id=model.id,
            event_day_id=model.event_day_id,
            event_day_name=event_name,
            event_day_creator_user_id=creator_id,
            sequence=model.sequence,
            name=model.name,
            starts_at=model.starts_at,
            ends_at=model.ends_at,
            shift_lead_user_id=model.shift_lead_user_id,
            status=model.status,
            items=tuple(self._to_item(item) for item in item_result.scalars()),
        )

    @staticmethod
    def _to_item(model: ShiftAssetModel) -> ShiftExecutionItem:
        return ShiftExecutionItem(
            id=model.id,
            asset_id=model.asset_id,
            quota_quantity=model.quota_quantity,
            carry_over_quantity=model.carry_over_quantity,
            opening_actual_quantity=model.opening_actual_quantity,
            closing_actual_quantity=model.closing_actual_quantity,
            consumed_quantity=model.consumed_quantity,
            discrepancy_reason=model.discrepancy_reason,
        )

    async def _execute(self, statement: Executable) -> Result[Any]:
        try:
            return await self._session.execute(statement)
        except SQLAlchemyError as exception:
            raise ShiftPersistenceError("Shift persistence operation failed.") from exception
