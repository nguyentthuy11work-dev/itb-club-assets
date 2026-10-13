"""SQLAlchemy adapter for immutable reconciliation snapshots and transitions."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.engine import Result
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.base import Executable

from ams.domain.assets import AssetCondition, CustodyStatus
from ams.domain.reconciliations import (
    CustodySnapshotCandidate,
    IReconciliationRepository,
    ReconciliationEvidence,
    ReconciliationItem,
    ReconciliationItemStatus,
    ReconciliationPersistenceError,
    ReconciliationSession,
    ReconciliationStatus,
    TemporaryLoanReturnTarget,
)
from ams.infrastructure.asset_persistence import (
    AssetModel,
    AssetPersistenceBase,
    CustodyModel,
)

RECONCILIATION_STATUS_ENUM = ENUM(
    ReconciliationStatus, name="reconciliation_status", create_type=False
)
RECONCILIATION_ITEM_STATUS_ENUM = ENUM(
    ReconciliationItemStatus, name="reconciliation_item_status", create_type=False
)


class ReconciliationSessionModel(AssetPersistenceBase):
    """Technical mapping for ``reconciliation_sessions``."""

    __tablename__ = "reconciliation_sessions"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    reconciliation_date: Mapped[date] = mapped_column(Date)
    status: Mapped[ReconciliationStatus] = mapped_column(RECONCILIATION_STATUS_ENUM)
    opened_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    temporary_loan_ticket_count: Mapped[int] = mapped_column(Integer)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    confirmation_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReconciliationItemModel(AssetPersistenceBase):
    """Technical mapping for one immutable expected snapshot."""

    __tablename__ = "reconciliation_items"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    reconciliation_id: Mapped[UUID] = mapped_column(ForeignKey("reconciliation_sessions.id"))
    custody_id: Mapped[UUID] = mapped_column(ForeignKey("custodies.id"))
    asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"))
    custodian_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    asset_name_snapshot: Mapped[str] = mapped_column(String(200))
    expected_quantity: Mapped[int] = mapped_column(Integer)
    expected_condition: Mapped[str] = mapped_column(String(16))
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[ReconciliationItemStatus] = mapped_column(RECONCILIATION_ITEM_STATUS_ENUM)
    actual_quantity: Mapped[int | None] = mapped_column(Integer)
    actual_condition: Mapped[str | None] = mapped_column(String(16))
    condition_note: Mapped[str | None] = mapped_column(String(1000))
    discrepancy_reason: Mapped[str | None] = mapped_column(String(1000))
    confirmed_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_note: Mapped[str | None] = mapped_column(String(1000))
    resolved_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReconciliationEvidenceModel(AssetPersistenceBase):
    """Technical mapping for private item evidence metadata."""

    __tablename__ = "reconciliation_item_evidence"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    reconciliation_item_id: Mapped[UUID] = mapped_column(ForeignKey("reconciliation_items.id"))
    uploaded_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    object_key: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str] = mapped_column(String(32))
    content_sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SqlAlchemyReconciliationRepository(IReconciliationRepository):
    """Persist F09 state while mapping driver failures to a safe boundary."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_open_policy(self) -> None:
        """Acquire a transaction advisory lock for globally serialized opening."""

        await self._execute(
            text("SELECT pg_advisory_xact_lock(hashtext('ams:reconciliation:open'))")
        )

    async def has_open_session(self) -> bool:
        """Check the partial-unique open-session policy."""

        result = await self._execute(
            select(ReconciliationSessionModel.id).where(
                ReconciliationSessionModel.status == ReconciliationStatus.OPEN
            )
        )
        return result.scalar_one_or_none() is not None

    async def list_snapshot_candidates(self) -> tuple[CustodySnapshotCandidate, ...]:
        """Read all active positive Custodies in one stable ordered statement."""

        result = await self._execute(
            select(CustodyModel, AssetModel.name)
            .join(AssetModel, AssetModel.id == CustodyModel.asset_id)
            .where(
                CustodyModel.status == CustodyStatus.ACTIVE.value,
                CustodyModel.on_hand_quantity > 0,
            )
            .order_by(CustodyModel.id)
        )
        return tuple(
            CustodySnapshotCandidate(
                custody_id=custody.id,
                asset_id=custody.asset_id,
                custodian_user_id=custody.custodian_user_id,
                asset_name=asset_name,
                expected_quantity=custody.on_hand_quantity,
                expected_condition=AssetCondition(custody.condition),
            )
            for custody, asset_name in result.all()
        )

    async def list_temporary_loan_return_targets(
        self,
    ) -> tuple[TemporaryLoanReturnTarget, ...]:
        """Project active temporary Tickets and active borrower email addresses."""

        result = await self._execute(
            text(
                """
                SELECT ticket.id, ticket.borrower_user_id, users.email
                FROM borrow_tickets AS ticket
                JOIN users ON users.id = ticket.borrower_user_id
                WHERE ticket.method = 'TEMPORARY'
                  AND ticket.status IN ('APPROVED', 'BORROWING', 'RETURNING')
                  AND users.is_active
                ORDER BY ticket.id
                """
            )
        )
        return tuple(
            TemporaryLoanReturnTarget(
                ticket_id=UUID(str(row.id)),
                borrower_user_id=UUID(str(row.borrower_user_id)),
                borrower_email=str(row.email),
            )
            for row in result
        )

    async def add(self, session: ReconciliationSession) -> None:
        """Insert a parent then all immutable snapshot children without committing."""

        try:
            self._session.add(
                ReconciliationSessionModel(
                    id=session.id,
                    reconciliation_date=session.reconciliation_date,
                    status=session.status,
                    opened_by_user_id=session.opened_by_user_id,
                    temporary_loan_ticket_count=session.temporary_loan_ticket_count,
                    opened_at=session.opened_at,
                    confirmation_requested_at=session.confirmation_requested_at,
                    closed_by_user_id=session.closed_by_user_id,
                    closed_at=session.closed_at,
                    updated_at=session.opened_at,
                )
            )
            await self._session.flush()
            self._session.add_all(
                [self._item_model(item, created_at=session.opened_at) for item in session.items]
            )
            await self._session.flush()
        except SQLAlchemyError as exception:
            raise ReconciliationPersistenceError(
                "Reconciliation snapshot persistence failed."
            ) from exception

    async def get(self, reconciliation_id: UUID) -> ReconciliationSession | None:
        """Load a complete session without a write lock."""

        return await self._get(reconciliation_id, for_update=False)

    async def get_for_update(self, reconciliation_id: UUID) -> ReconciliationSession | None:
        """Lock the session root and load its snapshot items."""

        return await self._get(reconciliation_id, for_update=True)

    async def get_item_session_id(self, item_id: UUID) -> UUID | None:
        """Resolve one item's parent ID without a write lock."""

        result = await self._execute(
            select(ReconciliationItemModel.reconciliation_id).where(
                ReconciliationItemModel.id == item_id
            )
        )
        return result.scalar_one_or_none()

    async def get_item_for_update(self, item_id: UUID) -> ReconciliationItem | None:
        """Lock and load one item after its aggregate root is locked."""

        result = await self._execute(
            select(ReconciliationItemModel)
            .where(ReconciliationItemModel.id == item_id)
            .with_for_update()
        )
        model = result.scalar_one_or_none()
        return None if model is None else self._to_item(model)

    async def list_for_custodian(
        self, custodian_id: UUID, *, offset: int, limit: int
    ) -> tuple[tuple[ReconciliationItem, ...], int]:
        """Return a stable newest-first custodian page and total."""

        count_result = await self._execute(
            select(func.count())
            .select_from(ReconciliationItemModel)
            .where(ReconciliationItemModel.custodian_user_id == custodian_id)
        )
        result = await self._execute(
            select(ReconciliationItemModel)
            .where(ReconciliationItemModel.custodian_user_id == custodian_id)
            .order_by(
                ReconciliationItemModel.snapshot_at.desc(),
                ReconciliationItemModel.id,
            )
            .offset(offset)
            .limit(limit)
        )
        return tuple(self._to_item(model) for model in result.scalars()), int(
            count_result.scalar_one()
        )

    async def count_unresolved_for_update(self, reconciliation_id: UUID) -> int:
        """Lock all parent items in ID order and count close-blocking states."""

        result = await self._execute(
            select(ReconciliationItemModel.status)
            .where(ReconciliationItemModel.reconciliation_id == reconciliation_id)
            .order_by(ReconciliationItemModel.id)
            .with_for_update()
        )
        return sum(
            status
            in {
                ReconciliationItemStatus.PENDING_CONFIRMATION,
                ReconciliationItemStatus.DISCREPANCY,
            }
            for status in result.scalars()
        )

    async def save_session(
        self, session: ReconciliationSession, *, updated_at: datetime
    ) -> None:
        """Stage request and closure fields without committing."""

        await self._execute(
            update(ReconciliationSessionModel)
            .where(ReconciliationSessionModel.id == session.id)
            .values(
                status=session.status,
                confirmation_requested_at=session.confirmation_requested_at,
                closed_by_user_id=session.closed_by_user_id,
                closed_at=session.closed_at,
                updated_at=updated_at,
            )
        )

    async def save_item(self, item: ReconciliationItem, *, updated_at: datetime) -> None:
        """Stage confirmation, resolution, and reminder fields without committing."""

        await self._execute(
            update(ReconciliationItemModel)
            .where(ReconciliationItemModel.id == item.id)
            .values(
                status=item.status,
                actual_quantity=item.actual_quantity,
                actual_condition=(
                    None if item.actual_condition is None else item.actual_condition.value
                ),
                condition_note=item.condition_note,
                discrepancy_reason=item.discrepancy_reason,
                confirmed_by_user_id=item.confirmed_by_user_id,
                confirmed_at=item.confirmed_at,
                resolution_note=item.resolution_note,
                resolved_by_user_id=item.resolved_by_user_id,
                resolved_at=item.resolved_at,
                last_reminded_at=item.last_reminded_at,
                updated_at=updated_at,
            )
        )

    async def add_evidence(self, evidence: ReconciliationEvidence) -> None:
        """Stage private immutable item evidence metadata."""

        try:
            self._session.add(
                ReconciliationEvidenceModel(
                    id=evidence.id,
                    reconciliation_item_id=evidence.reconciliation_item_id,
                    uploaded_by_user_id=evidence.uploaded_by_user_id,
                    object_key=evidence.object_key,
                    content_type=evidence.content_type,
                    content_sha256=evidence.content_sha256,
                    size_bytes=evidence.size_bytes,
                    created_at=evidence.created_at,
                )
            )
            await self._session.flush()
        except SQLAlchemyError as exception:
            raise ReconciliationPersistenceError(
                "Reconciliation evidence persistence failed."
            ) from exception

    async def _get(
        self, reconciliation_id: UUID, *, for_update: bool
    ) -> ReconciliationSession | None:
        statement = select(ReconciliationSessionModel).where(
            ReconciliationSessionModel.id == reconciliation_id
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._execute(statement)
        model = result.scalar_one_or_none()
        if model is None:
            return None
        item_result = await self._execute(
            select(ReconciliationItemModel)
            .where(ReconciliationItemModel.reconciliation_id == reconciliation_id)
            .order_by(ReconciliationItemModel.id)
        )
        return ReconciliationSession(
            id=model.id,
            reconciliation_date=model.reconciliation_date,
            opened_by_user_id=model.opened_by_user_id,
            opened_at=model.opened_at,
            temporary_loan_ticket_count=model.temporary_loan_ticket_count,
            status=model.status,
            confirmation_requested_at=model.confirmation_requested_at,
            closed_by_user_id=model.closed_by_user_id,
            closed_at=model.closed_at,
            items=tuple(self._to_item(item) for item in item_result.scalars()),
        )

    async def _execute(self, statement: Executable) -> Result[Any]:
        try:
            return await self._session.execute(statement)
        except SQLAlchemyError as exception:
            raise ReconciliationPersistenceError(
                "Reconciliation persistence operation failed."
            ) from exception

    @staticmethod
    def _item_model(
        item: ReconciliationItem, *, created_at: datetime
    ) -> ReconciliationItemModel:
        return ReconciliationItemModel(
            id=item.id,
            reconciliation_id=item.reconciliation_id,
            custody_id=item.custody_id,
            asset_id=item.asset_id,
            custodian_user_id=item.custodian_user_id,
            asset_name_snapshot=item.asset_name,
            expected_quantity=item.expected_quantity,
            expected_condition=item.expected_condition.value,
            snapshot_at=item.snapshot_at,
            status=item.status,
            actual_quantity=None,
            actual_condition=None,
            condition_note=None,
            discrepancy_reason=None,
            confirmed_by_user_id=None,
            confirmed_at=None,
            resolution_note=None,
            resolved_by_user_id=None,
            resolved_at=None,
            last_reminded_at=None,
            created_at=created_at,
            updated_at=created_at,
        )

    @staticmethod
    def _to_item(model: ReconciliationItemModel) -> ReconciliationItem:
        return ReconciliationItem(
            id=model.id,
            reconciliation_id=model.reconciliation_id,
            custody_id=model.custody_id,
            asset_id=model.asset_id,
            custodian_user_id=model.custodian_user_id,
            asset_name=model.asset_name_snapshot,
            expected_quantity=model.expected_quantity,
            expected_condition=AssetCondition(model.expected_condition),
            snapshot_at=model.snapshot_at,
            status=model.status,
            actual_quantity=model.actual_quantity,
            actual_condition=(
                None
                if model.actual_condition is None
                else AssetCondition(model.actual_condition)
            ),
            condition_note=model.condition_note,
            discrepancy_reason=model.discrepancy_reason,
            confirmed_by_user_id=model.confirmed_by_user_id,
            confirmed_at=model.confirmed_at,
            resolution_note=model.resolution_note,
            resolved_by_user_id=model.resolved_by_user_id,
            resolved_at=model.resolved_at,
            last_reminded_at=model.last_reminded_at,
        )
