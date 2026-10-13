"""SQLAlchemy adapter for standalone two-party Custody Transfers."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    and_,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.engine import Result
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.base import Executable

from ams.domain.assets import AssetCondition, Custody, CustodyStatus
from ams.domain.transfers import (
    CustodyTransfer,
    ITransferRepository,
    ReconciliationTransferReference,
    TransferEvidence,
    TransferEvidenceSide,
    TransferPersistenceError,
    TransferStatus,
)
from ams.infrastructure.asset_persistence import (
    AssetPersistenceBase,
    CustodyModel,
    SqlAlchemyCustodyRepository,
)
from ams.infrastructure.reconciliation_persistence import (
    ReconciliationItemModel,
    ReconciliationSessionModel,
)

TRANSFER_STATUS_ENUM = ENUM(TransferStatus, name="custody_transfer_status", create_type=False)
TRANSFER_EVIDENCE_SIDE_ENUM = ENUM(
    TransferEvidenceSide,
    name="custody_transfer_evidence_side",
    create_type=False,
)


class CustodyTransferModel(AssetPersistenceBase):
    """Technical mapping for ``custody_transfers``."""

    __tablename__ = "custody_transfers"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    source_custody_id: Mapped[UUID] = mapped_column(ForeignKey("custodies.id"))
    asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"))
    current_custodian_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    target_custodian_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    condition_snapshot: Mapped[str] = mapped_column(String(16))
    reconciliation_item_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("reconciliation_items.id")
    )
    status: Mapped[TransferStatus] = mapped_column(TRANSFER_STATUS_ENUM)
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    current_confirmed_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    current_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    target_confirmed_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    target_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    target_custody_id: Mapped[UUID | None] = mapped_column(ForeignKey("custodies.id"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TransferEvidenceModel(AssetPersistenceBase):
    """Technical mapping for immutable side confirmation evidence."""

    __tablename__ = "custody_transfer_evidence"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    transfer_id: Mapped[UUID] = mapped_column(ForeignKey("custody_transfers.id"))
    side: Mapped[TransferEvidenceSide] = mapped_column(TRANSFER_EVIDENCE_SIDE_ENUM)
    uploaded_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    object_key: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str] = mapped_column(String(32))
    content_sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    note: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SqlAlchemyTransferRepository(ITransferRepository):
    """Persist Transfers and lock source/target Custodies deterministically."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, transfer: CustodyTransfer) -> None:
        """Insert one reserved pending Transfer without committing."""

        try:
            self._session.add(self._to_model(transfer))
            await self._session.flush()
        except SQLAlchemyError as exception:
            raise TransferPersistenceError(
                "Transfer creation persistence failed."
            ) from exception

    async def get_for_update(self, transfer_id: UUID) -> CustodyTransfer | None:
        """Lock and load one Transfer aggregate."""

        result = await self._execute(
            select(CustodyTransferModel)
            .where(CustodyTransferModel.id == transfer_id)
            .with_for_update()
        )
        model = result.scalar_one_or_none()
        return None if model is None else self._to_entity(model)

    async def list_visible(
        self,
        actor_id: UUID,
        *,
        include_all: bool,
        status: TransferStatus | None,
        offset: int,
        limit: int,
    ) -> tuple[tuple[CustodyTransfer, ...], int]:
        """Return an HR-wide or participant-owned deterministic page."""

        filters = []
        if not include_all:
            filters.append(
                or_(
                    CustodyTransferModel.current_custodian_user_id == actor_id,
                    CustodyTransferModel.target_custodian_user_id == actor_id,
                )
            )
        if status is not None:
            filters.append(CustodyTransferModel.status == status)
        count_result = await self._execute(
            select(func.count()).select_from(CustodyTransferModel).where(*filters)
        )
        result = await self._execute(
            select(CustodyTransferModel)
            .where(*filters)
            .order_by(CustodyTransferModel.created_at.desc(), CustodyTransferModel.id)
            .offset(offset)
            .limit(limit)
        )
        return tuple(self._to_entity(model) for model in result.scalars()), int(
            count_result.scalar_one()
        )

    async def save(self, transfer: CustodyTransfer, *, updated_at: datetime) -> None:
        """Stage confirmations and exactly-once completion fields."""

        await self._execute(
            update(CustodyTransferModel)
            .where(CustodyTransferModel.id == transfer.id)
            .values(
                status=transfer.status,
                current_confirmed_by_user_id=transfer.current_confirmed_by_user_id,
                current_confirmed_at=transfer.current_confirmed_at,
                target_confirmed_by_user_id=transfer.target_confirmed_by_user_id,
                target_confirmed_at=transfer.target_confirmed_at,
                target_custody_id=transfer.target_custody_id,
                completed_at=transfer.completed_at,
                updated_at=updated_at,
            )
        )

    async def add_evidence(self, evidence: TransferEvidence) -> None:
        """Stage and flush one immutable evidence row."""

        try:
            self._session.add(
                TransferEvidenceModel(
                    id=evidence.id,
                    transfer_id=evidence.transfer_id,
                    side=evidence.side,
                    uploaded_by_user_id=evidence.uploaded_by_user_id,
                    object_key=evidence.object_key,
                    content_type=evidence.content_type,
                    content_sha256=evidence.content_sha256,
                    size_bytes=evidence.size_bytes,
                    note=evidence.note,
                    created_at=evidence.created_at,
                )
            )
            await self._session.flush()
        except SQLAlchemyError as exception:
            raise TransferPersistenceError(
                "Transfer evidence persistence failed."
            ) from exception

    async def list_evidence(
        self, transfer_id: UUID, side: TransferEvidenceSide
    ) -> tuple[TransferEvidence, ...]:
        """Return one side's evidence in stable creation order."""

        result = await self._execute(
            select(TransferEvidenceModel)
            .where(
                TransferEvidenceModel.transfer_id == transfer_id,
                TransferEvidenceModel.side == side,
            )
            .order_by(TransferEvidenceModel.created_at, TransferEvidenceModel.id)
        )
        return tuple(
            TransferEvidence(
                model.id,
                model.transfer_id,
                model.side,
                model.uploaded_by_user_id,
                model.object_key,
                model.content_type,
                model.content_sha256,
                model.size_bytes,
                model.created_at,
                model.note,
            )
            for model in result.scalars()
        )

    async def lock_completion_custodies(
        self, transfer: CustodyTransfer
    ) -> tuple[Custody, Custody | None]:
        """Lock source and target current rows in ascending UUID order."""

        result = await self._execute(
            select(CustodyModel.id)
            .where(
                or_(
                    CustodyModel.id == transfer.source_custody_id,
                    and_(
                        CustodyModel.asset_id == transfer.asset_id,
                        CustodyModel.custodian_user_id == transfer.target_custodian_user_id,
                        CustodyModel.status == CustodyStatus.ACTIVE.value,
                    ),
                )
            )
            .order_by(CustodyModel.id)
            .with_for_update()
        )
        ids = tuple(result.scalars())
        custody_repository = SqlAlchemyCustodyRepository(self._session)
        values = {
            custody_id: await custody_repository.get_by_id(custody_id) for custody_id in ids
        }
        source = values.get(transfer.source_custody_id)
        if source is None:
            raise TransferPersistenceError("Locked source Custody is unavailable.")
        target = next(
            (
                custody
                for custody_id, custody in values.items()
                if custody_id != transfer.source_custody_id and custody is not None
            ),
            None,
        )
        return source, target

    async def get_reconciliation_reference(
        self, item_id: UUID
    ) -> ReconciliationTransferReference | None:
        """Load linked item source and parent session-open state."""

        result = await self._execute(
            select(
                ReconciliationItemModel.id,
                ReconciliationItemModel.custody_id,
                ReconciliationSessionModel.status,
            )
            .join(
                ReconciliationSessionModel,
                ReconciliationSessionModel.id == ReconciliationItemModel.reconciliation_id,
            )
            .where(ReconciliationItemModel.id == item_id)
        )
        row = result.one_or_none()
        if row is None:
            return None
        return ReconciliationTransferReference(
            row.id,
            row.custody_id,
            str(row.status) == "OPEN" or getattr(row.status, "value", None) == "OPEN",
        )

    async def _execute(self, statement: Executable) -> Result[Any]:
        try:
            return await self._session.execute(statement)
        except SQLAlchemyError as exception:
            raise TransferPersistenceError(
                "Transfer persistence operation failed."
            ) from exception

    @staticmethod
    def _to_model(transfer: CustodyTransfer) -> CustodyTransferModel:
        return CustodyTransferModel(
            id=transfer.id,
            source_custody_id=transfer.source_custody_id,
            asset_id=transfer.asset_id,
            current_custodian_user_id=transfer.current_custodian_user_id,
            target_custodian_user_id=transfer.target_custodian_user_id,
            quantity=transfer.quantity,
            condition_snapshot=transfer.condition_snapshot.value,
            reconciliation_item_id=transfer.reconciliation_item_id,
            status=transfer.status,
            created_by_user_id=transfer.created_by_user_id,
            created_at=transfer.created_at,
            current_confirmed_by_user_id=transfer.current_confirmed_by_user_id,
            current_confirmed_at=transfer.current_confirmed_at,
            target_confirmed_by_user_id=transfer.target_confirmed_by_user_id,
            target_confirmed_at=transfer.target_confirmed_at,
            target_custody_id=transfer.target_custody_id,
            completed_at=transfer.completed_at,
            updated_at=transfer.created_at,
        )

    @staticmethod
    def _to_entity(model: CustodyTransferModel) -> CustodyTransfer:
        return CustodyTransfer(
            id=model.id,
            source_custody_id=model.source_custody_id,
            asset_id=model.asset_id,
            current_custodian_user_id=model.current_custodian_user_id,
            target_custodian_user_id=model.target_custodian_user_id,
            quantity=model.quantity,
            condition_snapshot=AssetCondition(model.condition_snapshot),
            created_by_user_id=model.created_by_user_id,
            created_at=model.created_at,
            reconciliation_item_id=model.reconciliation_item_id,
            status=model.status,
            current_confirmed_by_user_id=model.current_confirmed_by_user_id,
            current_confirmed_at=model.current_confirmed_at,
            target_confirmed_by_user_id=model.target_confirmed_by_user_id,
            target_confirmed_at=model.target_confirmed_at,
            target_custody_id=model.target_custody_id,
            completed_at=model.completed_at,
        )
