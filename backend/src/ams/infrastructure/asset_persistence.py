"""SQLAlchemy mappings, repositories, and Unit of Work for Asset/Custody."""

from __future__ import annotations

from datetime import datetime
from types import TracebackType
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
    select,
    text,
    update,
)
from sqlalchemy.engine import Result
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql.base import Executable

from ams.application.assets import AssetPersistenceError, IAssetUnitOfWork
from ams.domain.assets import Asset, AssetCondition, Custody, CustodyEvidence, CustodyStatus
from ams.domain.repositories import (
    IAssetRepository,
    ICustodyEvidenceRepository,
    ICustodyRepository,
)


class AssetPersistenceBase(DeclarativeBase):
    """Declarative metadata used only to map the Flyway-owned schema."""


class UserReferenceModel(AssetPersistenceBase):
    """Identity-table reference needed to resolve mapping foreign keys."""

    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True)


class TeamReferenceModel(AssetPersistenceBase):
    """Team-table reference needed to resolve mapping foreign keys."""

    __tablename__ = "teams"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    is_active: Mapped[bool] = mapped_column(Boolean)


class AssetCategoryModel(AssetPersistenceBase):
    """Asset category identity used by Asset foreign-key mapping."""

    __tablename__ = "asset_categories"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    is_active: Mapped[bool] = mapped_column(Boolean)


class AssetModel(AssetPersistenceBase):
    """SQLAlchemy mapping for the Flyway ``assets`` table."""

    __tablename__ = "assets"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    category_id: Mapped[UUID] = mapped_column(ForeignKey("asset_categories.id"))
    responsible_team_id: Mapped[UUID | None] = mapped_column(ForeignKey("teams.id"))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(String(1000))
    source: Mapped[str] = mapped_column(String(255))
    total_quantity: Mapped[int] = mapped_column(Integer)
    available_quantity: Mapped[int] = mapped_column(Integer)
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CustodyModel(AssetPersistenceBase):
    """SQLAlchemy mapping for the Flyway ``custodies`` table."""

    __tablename__ = "custodies"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"))
    custodian_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    assigned_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(16))
    on_hand_quantity: Mapped[int] = mapped_column(Integer)
    reserved_quantity: Mapped[int] = mapped_column(Integer)
    temporarily_loaned_quantity: Mapped[int] = mapped_column(Integer)
    condition: Mapped[str] = mapped_column(String(16))
    condition_note: Mapped[str | None] = mapped_column(String(1000))
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CustodyEvidenceModel(AssetPersistenceBase):
    """Minimal mapping needed to load private evidence references."""

    __tablename__ = "custody_evidence"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    custody_id: Mapped[UUID] = mapped_column(ForeignKey("custodies.id"))
    uploaded_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    evidence_type: Mapped[str] = mapped_column(String(32))
    object_key: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str] = mapped_column(String(32))
    content_sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    note: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SqlAlchemyAssetRepository(IAssetRepository):
    """Map Asset entities to the SQL-first PostgreSQL schema."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, asset_id: UUID) -> Asset | None:
        """Load an Asset without acquiring a write lock."""

        return await self._get(asset_id, for_update=False)

    async def get_for_update(self, asset_id: UUID) -> Asset | None:
        """Lock and load one Asset for quantity-changing work."""

        return await self._get(asset_id, for_update=True)

    async def add(self, asset: Asset) -> None:
        """Stage an Asset insert without flushing or committing."""

        self._session.add(self._to_model(asset))

    async def save(self, asset: Asset) -> None:
        """Stage mutable Asset fields without committing."""

        await self._execute(
            update(AssetModel)
            .where(AssetModel.id == asset.id)
            .values(
                responsible_team_id=asset.responsible_team_id,
                name=asset.name,
                description=asset.description,
                source=asset.source,
                total_quantity=asset.total_quantity,
                available_quantity=asset.available_quantity,
                updated_at=func.now(),
            )
        )

    async def active_category_exists(self, category_id: UUID) -> bool:
        """Return whether the category can classify a new Asset."""

        result = await self._execute(
            select(AssetCategoryModel.id).where(
                AssetCategoryModel.id == category_id,
                AssetCategoryModel.is_active.is_(True),
            )
        )
        return result.scalar_one_or_none() is not None

    async def active_team_exists(self, team_id: UUID) -> bool:
        """Return whether the Team can classify a new Asset."""

        result = await self._execute(
            select(TeamReferenceModel.id).where(
                TeamReferenceModel.id == team_id,
                TeamReferenceModel.is_active.is_(True),
            )
        )
        return result.scalar_one_or_none() is not None

    async def _get(self, asset_id: UUID, *, for_update: bool) -> Asset | None:
        statement = select(AssetModel).where(AssetModel.id == asset_id)
        if for_update:
            statement = statement.with_for_update()
        result = await self._execute(statement)
        model = result.scalar_one_or_none()
        if model is None:
            return None
        active_ids_result = await self._execute(
            select(CustodyModel.id).where(
                CustodyModel.asset_id == asset_id,
                CustodyModel.status == CustodyStatus.ACTIVE.value,
            )
        )
        return Asset(
            id=model.id,
            category_id=model.category_id,
            responsible_team_id=model.responsible_team_id,
            name=model.name,
            description=model.description,
            source=model.source,
            total_quantity=model.total_quantity,
            available_quantity=model.available_quantity,
            created_by_user_id=model.created_by_user_id,
            created_at=model.created_at,
            contributing_custody_ids=frozenset(active_ids_result.scalars()),
        )

    async def _execute(self, statement: Executable) -> Result[Any]:
        try:
            return await self._session.execute(statement)
        except SQLAlchemyError as exception:
            raise AssetPersistenceError("Asset persistence operation failed.") from exception

    @staticmethod
    def _to_model(asset: Asset) -> AssetModel:
        return AssetModel(
            id=asset.id,
            category_id=asset.category_id,
            responsible_team_id=asset.responsible_team_id,
            name=asset.name,
            description=asset.description,
            source=asset.source,
            total_quantity=asset.total_quantity,
            available_quantity=asset.available_quantity,
            created_by_user_id=asset.created_by_user_id,
            created_at=asset.created_at,
            updated_at=asset.created_at,
        )


class SqlAlchemyCustodyRepository(ICustodyRepository):
    """Map Custody entities and evidence references to PostgreSQL."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, custody_id: UUID) -> Custody | None:
        """Load a Custody without acquiring a write lock."""

        return await self._get(custody_id, for_update=False)

    async def get_for_update(self, custody_id: UUID) -> Custody | None:
        """Lock and load one Custody for state or quantity changes."""

        return await self._get(custody_id, for_update=True)

    async def get_many_for_update(self, custody_ids: tuple[UUID, ...]) -> tuple[Custody, ...]:
        """Lock unique source Custodies in deterministic ascending UUID order."""

        ordered_ids = tuple(sorted(set(custody_ids), key=str))
        if not ordered_ids:
            return ()
        result = await self._execute(
            select(CustodyModel)
            .where(CustodyModel.id.in_(ordered_ids))
            .order_by(CustodyModel.id)
            .with_for_update()
        )
        models = tuple(result.scalars())
        evidence_by_custody: dict[UUID, tuple[UUID, ...]] = {}
        evidence_result = await self._execute(
            select(CustodyEvidenceModel.custody_id, CustodyEvidenceModel.id)
            .where(CustodyEvidenceModel.custody_id.in_(ordered_ids))
            .order_by(
                CustodyEvidenceModel.custody_id,
                CustodyEvidenceModel.created_at,
                CustodyEvidenceModel.id,
            )
        )
        mutable_evidence: dict[UUID, list[UUID]] = {}
        for custody_id, evidence_id in evidence_result.all():
            mutable_evidence.setdefault(custody_id, []).append(evidence_id)
        evidence_by_custody = {
            custody_id: tuple(evidence_ids)
            for custody_id, evidence_ids in mutable_evidence.items()
        }
        for model in models:
            if model.id not in evidence_by_custody:
                evidence_by_custody[model.id] = await self._transfer_evidence_ids(model)
        return tuple(
            self._to_entity(model, evidence_by_custody.get(model.id, ())) for model in models
        )

    async def get_current_for_update(
        self, asset_id: UUID, custodian_user_id: UUID
    ) -> Custody | None:
        """Lock an actor's current Custody for one Asset."""

        result = await self._execute(
            select(CustodyModel)
            .where(
                CustodyModel.asset_id == asset_id,
                CustodyModel.custodian_user_id == custodian_user_id,
                CustodyModel.status.in_(
                    (CustodyStatus.PENDING.value, CustodyStatus.ACTIVE.value)
                ),
            )
            .with_for_update()
        )
        model = result.scalar_one_or_none()
        if model is None:
            return None
        evidence_result = await self._execute(
            select(CustodyEvidenceModel.id)
            .where(CustodyEvidenceModel.custody_id == model.id)
            .order_by(CustodyEvidenceModel.created_at, CustodyEvidenceModel.id)
        )
        evidence_ids = tuple(evidence_result.scalars())
        if not evidence_ids:
            evidence_ids = await self._transfer_evidence_ids(model)
        return self._to_entity(model, evidence_ids)

    async def add(self, custody: Custody) -> None:
        """Stage a Custody insert without flushing or committing."""

        self._session.add(self._to_model(custody))

    async def save(self, custody: Custody) -> None:
        """Stage mutable Custody fields without committing."""

        await self._execute(
            update(CustodyModel)
            .where(CustodyModel.id == custody.id)
            .values(
                status=custody.status.value,
                on_hand_quantity=custody.on_hand_quantity,
                reserved_quantity=custody.reserved_quantity,
                temporarily_loaned_quantity=custody.temporarily_loaned_quantity,
                condition=custody.condition.value,
                condition_note=custody.condition_note,
                confirmed_at=custody.confirmed_at,
                confirmed_by_user_id=custody.confirmed_by_user_id,
                closed_at=custody.closed_at,
                updated_at=func.now(),
            )
        )

    async def _get(self, custody_id: UUID, *, for_update: bool) -> Custody | None:
        statement = select(CustodyModel).where(CustodyModel.id == custody_id)
        if for_update:
            statement = statement.with_for_update()
        result = await self._execute(statement)
        model = result.scalar_one_or_none()
        if model is None:
            return None
        evidence_result = await self._execute(
            select(CustodyEvidenceModel.id)
            .where(CustodyEvidenceModel.custody_id == custody_id)
            .order_by(CustodyEvidenceModel.created_at, CustodyEvidenceModel.id)
        )
        evidence_ids = tuple(evidence_result.scalars())
        if not evidence_ids:
            evidence_ids = await self._transfer_evidence_ids(model)
        return self._to_entity(model, evidence_ids)

    async def _transfer_evidence_ids(self, model: CustodyModel) -> tuple[UUID, ...]:
        """Resolve evidence provenance for Custody created by a closed transfer Ticket."""

        result = await self._execute(
            text(
                """
                SELECT evidence.id
                FROM ticket_evidence AS evidence
                JOIN borrow_tickets AS tickets ON tickets.id = evidence.ticket_id
                JOIN ticket_items AS items ON items.ticket_id = tickets.id
                WHERE tickets.method = 'CUSTODY_TRANSFER'
                  AND tickets.status = 'CLOSED'
                  AND tickets.borrower_user_id = :custodian_user_id
                  AND tickets.updated_at = :confirmed_at
                  AND items.asset_id = :asset_id
                  AND evidence.evidence_type = 'PICKUP'
                ORDER BY evidence.created_at, evidence.id
                """
            ).bindparams(
                custodian_user_id=model.custodian_user_id,
                confirmed_at=model.confirmed_at,
                asset_id=model.asset_id,
            )
        )
        return tuple(result.scalars())

    @staticmethod
    def _to_entity(model: CustodyModel, evidence_ids: tuple[UUID, ...]) -> Custody:
        return Custody(
            id=model.id,
            asset_id=model.asset_id,
            custodian_user_id=model.custodian_user_id,
            assigned_by_user_id=model.assigned_by_user_id,
            status=CustodyStatus(model.status),
            on_hand_quantity=model.on_hand_quantity,
            reserved_quantity=model.reserved_quantity,
            temporarily_loaned_quantity=model.temporarily_loaned_quantity,
            condition=AssetCondition(model.condition),
            condition_note=model.condition_note,
            assigned_at=model.assigned_at,
            confirmed_at=model.confirmed_at,
            confirmed_by_user_id=model.confirmed_by_user_id,
            closed_at=model.closed_at,
            evidence_ids=evidence_ids,
        )

    async def _execute(self, statement: Executable) -> Result[Any]:
        try:
            return await self._session.execute(statement)
        except SQLAlchemyError as exception:
            raise AssetPersistenceError("Custody persistence operation failed.") from exception

    @staticmethod
    def _to_model(custody: Custody) -> CustodyModel:
        return CustodyModel(
            id=custody.id,
            asset_id=custody.asset_id,
            custodian_user_id=custody.custodian_user_id,
            assigned_by_user_id=custody.assigned_by_user_id,
            status=custody.status.value,
            on_hand_quantity=custody.on_hand_quantity,
            reserved_quantity=custody.reserved_quantity,
            temporarily_loaned_quantity=custody.temporarily_loaned_quantity,
            condition=custody.condition.value,
            condition_note=custody.condition_note,
            assigned_at=custody.assigned_at,
            confirmed_at=custody.confirmed_at,
            confirmed_by_user_id=custody.confirmed_by_user_id,
            closed_at=custody.closed_at,
            created_at=custody.assigned_at,
            updated_at=custody.confirmed_at or custody.assigned_at,
        )


class SqlAlchemyCustodyEvidenceRepository(ICustodyEvidenceRepository):
    """Stage private Custody evidence metadata in the business transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, evidence: CustodyEvidence) -> None:
        """Stage one assignment-confirmation evidence row without committing."""

        self._session.add(
            CustodyEvidenceModel(
                id=evidence.id,
                custody_id=evidence.custody_id,
                uploaded_by_user_id=evidence.uploaded_by_user_id,
                evidence_type=evidence.evidence_type,
                object_key=evidence.object_key,
                content_type=evidence.content_type,
                content_sha256=evidence.content_sha256,
                size_bytes=evidence.size_bytes,
                note=evidence.note,
                created_at=evidence.created_at,
            )
        )


class SqlAlchemyAssetUnitOfWork(IAssetUnitOfWork):
    """Own one AsyncSession and transaction for an Asset/Custody use case."""

    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self._session_maker = session_maker
        self._session: AsyncSession | None = None
        self._committed = False

    async def __aenter__(self) -> SqlAlchemyAssetUnitOfWork:
        """Open a session and construct repositories sharing its transaction."""

        self._session = self._session_maker()
        self.assets = SqlAlchemyAssetRepository(self._session)
        self.custodies = SqlAlchemyCustodyRepository(self._session)
        self._committed = False
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Roll back unfinished work and always close the owned session."""

        if self._session is None:
            return
        if exception is not None or not self._committed:
            await self._session.rollback()
        await self._session.close()
        self._session = None

    async def commit(self) -> None:
        """Commit all staged repository changes exactly once."""

        session = self._require_session()
        try:
            await session.commit()
            self._committed = True
        except SQLAlchemyError as exception:
            await session.rollback()
            raise AssetPersistenceError(
                "Asset transaction could not be committed."
            ) from exception

    async def rollback(self) -> None:
        """Explicitly roll back all staged repository changes."""

        await self._require_session().rollback()
        self._committed = False

    def _require_session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("Asset Unit of Work is not active.")
        return self._session
