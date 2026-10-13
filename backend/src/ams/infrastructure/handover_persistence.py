"""SQLAlchemy adapter for configurable terms and immutable Handovers."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.engine import Result, RowMapping
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.base import Executable

from ams.domain.assets import AssetCondition, CustodyStatus
from ams.domain.handovers import (
    AssetHandoverSnapshot,
    Handover,
    HandoverCustodySnapshot,
    HandoverItem,
    HandoverPersistenceError,
    HandoverStatus,
    IHandoverRepository,
    Term,
)
from ams.infrastructure.asset_persistence import AssetPersistenceBase, CustodyModel

HANDOVER_STATUS_ENUM = ENUM(HandoverStatus, name="handover_status", create_type=False)


class TermModel(AssetPersistenceBase):
    """Technical mapping for preconfigured organizational terms."""

    __tablename__ = "terms"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date] = mapped_column(Date)
    old_hr_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    new_hr_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class HandoverModel(AssetPersistenceBase):
    """Technical mapping for one term-owned Handover root."""

    __tablename__ = "handovers"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    term_id: Mapped[UUID] = mapped_column(ForeignKey("terms.id"))
    term_code_snapshot: Mapped[str] = mapped_column(String(64))
    term_name_snapshot: Mapped[str] = mapped_column(String(200))
    term_starts_on: Mapped[date] = mapped_column(Date)
    term_ends_on: Mapped[date] = mapped_column(Date)
    old_hr_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    new_hr_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    status: Mapped[HandoverStatus] = mapped_column(HANDOVER_STATUS_ENUM)
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    old_confirmed_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    old_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    new_confirmed_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    new_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class HandoverItemModel(AssetPersistenceBase):
    """Technical mapping for immutable Asset/Custody snapshot rows."""

    __tablename__ = "handover_items"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    handover_id: Mapped[UUID] = mapped_column(ForeignKey("handovers.id"))
    asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"))
    asset_name_snapshot: Mapped[str] = mapped_column(String(200))
    total_quantity_snapshot: Mapped[int] = mapped_column(Integer)
    available_quantity_snapshot: Mapped[int] = mapped_column(Integer)
    condition_snapshot: Mapped[str] = mapped_column(String(16))
    custody_snapshot: Mapped[list[dict[str, object]]] = mapped_column(JSONB)
    is_stale: Mapped[bool] = mapped_column(Boolean)
    last_qualifying_transaction_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SqlAlchemyHandoverRepository(IHandoverRepository):
    """Persist Handovers and query approved qualifying term transactions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_term(self, term_id: UUID) -> Term | None:
        """Lock and return one configured term to serialize snapshot creation."""

        result = await self._execute(
            select(TermModel).where(TermModel.id == term_id).with_for_update()
        )
        model = result.scalar_one_or_none()
        return None if model is None else self._term(model)

    async def has_handover_for_term(self, term_id: UUID) -> bool:
        """Return whether a snapshot already exists after locking its term."""

        result = await self._execute(
            select(HandoverModel.id).where(HandoverModel.term_id == term_id).limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def snapshot_assets(self, term: Term) -> tuple[AssetHandoverSnapshot, ...]:
        """Read all Assets, current Custodies, and qualifying in-term activity."""

        asset_result = await self._execute(
            text(
                """
                WITH qualifying_transactions AS (
                    SELECT id AS asset_id, created_at AS occurred_at
                    FROM assets
                    UNION ALL
                    SELECT asset_id, assigned_at
                    FROM custodies
                    UNION ALL
                    SELECT items.asset_id, events.occurred_at
                    FROM ticket_events AS events
                    JOIN ticket_items AS items ON items.ticket_id = events.ticket_id
                    WHERE events.event_type IN ('CONFIRM_RECEIVED', 'CONFIRM_RETURNED')
                    UNION ALL
                    SELECT asset_id, completed_at
                    FROM custody_transfers
                    WHERE status = 'COMPLETED' AND completed_at IS NOT NULL
                ),
                latest AS (
                    SELECT asset_id, max(occurred_at) AS occurred_at
                    FROM qualifying_transactions
                    WHERE (occurred_at AT TIME ZONE 'Asia/Ho_Chi_Minh')::DATE
                          BETWEEN :starts_on AND :ends_on
                    GROUP BY asset_id
                )
                SELECT assets.id, assets.name, assets.total_quantity,
                       assets.available_quantity, latest.occurred_at
                FROM assets
                LEFT JOIN latest ON latest.asset_id = assets.id
                ORDER BY assets.id
                """
            ).bindparams(starts_on=term.starts_on, ends_on=term.ends_on)
        )
        custody_result = await self._execute(
            select(CustodyModel)
            .where(CustodyModel.status.in_(["PENDING", "ACTIVE"]))
            .order_by(CustodyModel.asset_id, CustodyModel.id)
        )
        by_asset: dict[UUID, list[HandoverCustodySnapshot]] = defaultdict(list)
        for model in custody_result.scalars():
            by_asset[model.asset_id].append(self._custody(model))
        return tuple(
            self._asset_snapshot(row, tuple(by_asset[UUID(str(row["id"]))]))
            for row in asset_result.mappings()
        )

    async def add(self, handover: Handover) -> None:
        """Stage and flush one root followed by all immutable snapshot items."""

        try:
            self._session.add(self._handover_model(handover))
            await self._session.flush()
            self._session.add_all(self._item_model(value) for value in handover.items)
            await self._session.flush()
        except SQLAlchemyError as exception:
            raise HandoverPersistenceError(
                "Handover creation persistence failed."
            ) from exception

    async def get(self, handover_id: UUID) -> Handover | None:
        """Return one complete Handover without taking a write lock."""

        return await self._get(handover_id, for_update=False)

    async def get_for_update(self, handover_id: UUID) -> Handover | None:
        """Lock the Handover root before either confirmation transition."""

        return await self._get(handover_id, for_update=True)

    async def save(self, handover: Handover, *, updated_at: datetime) -> None:
        """Persist confirmation and terminal lock fields without touching items."""

        await self._execute(
            update(HandoverModel)
            .where(HandoverModel.id == handover.id)
            .values(
                status=handover.status,
                old_confirmed_by_user_id=handover.old_confirmed_by_user_id,
                old_confirmed_at=handover.old_confirmed_at,
                new_confirmed_by_user_id=handover.new_confirmed_by_user_id,
                new_confirmed_at=handover.new_confirmed_at,
                locked_at=handover.locked_at,
                updated_at=updated_at,
            )
        )

    async def _get(self, handover_id: UUID, *, for_update: bool) -> Handover | None:
        statement = select(HandoverModel).where(HandoverModel.id == handover_id)
        if for_update:
            statement = statement.with_for_update()
        result = await self._execute(statement)
        model = result.scalar_one_or_none()
        if model is None:
            return None
        item_result = await self._execute(
            select(HandoverItemModel)
            .where(HandoverItemModel.handover_id == handover_id)
            .order_by(HandoverItemModel.asset_name_snapshot, HandoverItemModel.id)
        )
        return self._entity(model, tuple(item_result.scalars()))

    async def _execute(self, statement: Executable) -> Result[Any]:
        try:
            return await self._session.execute(statement)
        except SQLAlchemyError as exception:
            raise HandoverPersistenceError(
                "Handover persistence operation failed."
            ) from exception

    @staticmethod
    def _term(model: TermModel) -> Term:
        return Term(
            model.id,
            model.code,
            model.name,
            model.starts_on,
            model.ends_on,
            model.old_hr_user_id,
            model.new_hr_user_id,
        )

    @staticmethod
    def _custody(model: CustodyModel) -> HandoverCustodySnapshot:
        return HandoverCustodySnapshot(
            model.id,
            model.custodian_user_id,
            CustodyStatus(model.status),
            model.on_hand_quantity,
            model.reserved_quantity,
            model.temporarily_loaned_quantity,
            AssetCondition(model.condition),
            model.condition_note,
        )

    @staticmethod
    def _asset_snapshot(
        row: RowMapping, custodies: tuple[HandoverCustodySnapshot, ...]
    ) -> AssetHandoverSnapshot:
        condition = AssetCondition.GOOD
        if any(value.condition is AssetCondition.LOST for value in custodies):
            condition = AssetCondition.LOST
        elif any(value.condition is AssetCondition.DAMAGED for value in custodies):
            condition = AssetCondition.DAMAGED
        return AssetHandoverSnapshot(
            asset_id=UUID(str(row["id"])),
            asset_name=str(row["name"]),
            total_quantity=int(row["total_quantity"]),
            available_quantity=int(row["available_quantity"]),
            condition=condition,
            custodies=custodies,
            last_qualifying_transaction_at=row["occurred_at"],
        )

    @classmethod
    def _handover_model(cls, value: Handover) -> HandoverModel:
        return HandoverModel(
            id=value.id,
            term_id=value.term_id,
            term_code_snapshot=value.term_code,
            term_name_snapshot=value.term_name,
            term_starts_on=value.term_starts_on,
            term_ends_on=value.term_ends_on,
            old_hr_user_id=value.old_hr_user_id,
            new_hr_user_id=value.new_hr_user_id,
            status=value.status,
            created_by_user_id=value.created_by_user_id,
            created_at=value.created_at,
            old_confirmed_by_user_id=value.old_confirmed_by_user_id,
            old_confirmed_at=value.old_confirmed_at,
            new_confirmed_by_user_id=value.new_confirmed_by_user_id,
            new_confirmed_at=value.new_confirmed_at,
            locked_at=value.locked_at,
            updated_at=value.created_at,
        )

    @classmethod
    def _item_model(cls, value: HandoverItem) -> HandoverItemModel:
        return HandoverItemModel(
            id=value.id,
            handover_id=value.handover_id,
            asset_id=value.asset_id,
            asset_name_snapshot=value.asset_name,
            total_quantity_snapshot=value.total_quantity,
            available_quantity_snapshot=value.available_quantity,
            condition_snapshot=value.condition.value,
            custody_snapshot=[cls._custody_json(item) for item in value.custodies],
            is_stale=value.is_stale,
            last_qualifying_transaction_at=value.last_qualifying_transaction_at,
            snapshot_at=value.snapshot_at,
            created_at=value.snapshot_at,
        )

    @staticmethod
    def _custody_json(value: HandoverCustodySnapshot) -> dict[str, object]:
        return {
            "custodyId": str(value.custody_id),
            "custodianUserId": str(value.custodian_user_id),
            "status": value.status.value,
            "onHandQuantity": value.on_hand_quantity,
            "reservedQuantity": value.reserved_quantity,
            "temporarilyLoanedQuantity": value.temporarily_loaned_quantity,
            "condition": value.condition.value,
            "conditionNote": value.condition_note,
        }

    @classmethod
    def _item_entity(cls, model: HandoverItemModel) -> HandoverItem:
        return HandoverItem(
            id=model.id,
            handover_id=model.handover_id,
            asset_id=model.asset_id,
            asset_name=model.asset_name_snapshot,
            total_quantity=model.total_quantity_snapshot,
            available_quantity=model.available_quantity_snapshot,
            condition=AssetCondition(model.condition_snapshot),
            custodies=tuple(cls._custody_entity(item) for item in model.custody_snapshot),
            is_stale=model.is_stale,
            last_qualifying_transaction_at=model.last_qualifying_transaction_at,
            snapshot_at=model.snapshot_at,
        )

    @staticmethod
    def _custody_entity(value: Mapping[str, object]) -> HandoverCustodySnapshot:
        return HandoverCustodySnapshot(
            custody_id=UUID(str(value["custodyId"])),
            custodian_user_id=UUID(str(value["custodianUserId"])),
            status=CustodyStatus(str(value["status"])),
            on_hand_quantity=int(str(value["onHandQuantity"])),
            reserved_quantity=int(str(value["reservedQuantity"])),
            temporarily_loaned_quantity=int(str(value["temporarilyLoanedQuantity"])),
            condition=AssetCondition(str(value["condition"])),
            condition_note=(
                None if value["conditionNote"] is None else str(value["conditionNote"])
            ),
        )

    @classmethod
    def _entity(cls, model: HandoverModel, items: tuple[HandoverItemModel, ...]) -> Handover:
        return Handover(
            id=model.id,
            term_id=model.term_id,
            term_code=model.term_code_snapshot,
            term_name=model.term_name_snapshot,
            term_starts_on=model.term_starts_on,
            term_ends_on=model.term_ends_on,
            old_hr_user_id=model.old_hr_user_id,
            new_hr_user_id=model.new_hr_user_id,
            created_by_user_id=model.created_by_user_id,
            created_at=model.created_at,
            items=tuple(cls._item_entity(item) for item in items),
            status=model.status,
            old_confirmed_by_user_id=model.old_confirmed_by_user_id,
            old_confirmed_at=model.old_confirmed_at,
            new_confirmed_by_user_id=model.new_confirmed_by_user_id,
            new_confirmed_at=model.new_confirmed_at,
            locked_at=model.locked_at,
        )
