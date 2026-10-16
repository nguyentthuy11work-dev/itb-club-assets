"""FastAPI application factory and production dependency wiring."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ams.application.asset_creation import AssetCreationService
from ams.application.asset_imports import AssetImportService
from ams.application.asset_queries import AssetReadService
from ams.application.authentication import SessionService
from ams.application.custody_confirmation import CustodyConfirmationService
from ams.application.event_days import EventDayService
from ams.application.event_reminders import EventReminderScheduler
from ams.application.evidence import EvidenceStorageService
from ams.application.handovers import HandoverService
from ams.application.health import IReadinessProbe
from ams.application.idempotency import IdempotentCommandExecutor
from ams.application.member_roles import MemberRoleService
from ams.application.operational_queries import OperationalReadService
from ams.application.reconciliations import ReconciliationService
from ams.application.reliability import IClock
from ams.application.responsible_team import ResponsibleTeamService
from ams.application.shift_execution import ShiftExecutionService
from ams.application.ticket_commands import TicketLifecycleService
from ams.application.ticket_queries import TicketReadService
from ams.application.ticket_reminders import TicketReminderScheduler
from ams.application.tickets import TicketCreationService
from ams.application.transfers import TransferService
from ams.infrastructure.asset_import_parser import CsvXlsxAssetImportParser
from ams.infrastructure.asset_read_persistence import SqlAlchemyAssetReadRepository
from ams.infrastructure.authentication import (
    Argon2PasswordHasher,
    JwtTokenService,
    LocalIdentityProvider,
    TokenSettings,
)
from ams.infrastructure.config import ApiSettings
from ams.infrastructure.notifications import SystemClock
from ams.infrastructure.operational_read_persistence import (
    SqlAlchemyOperationalReadRepository,
)
from ams.infrastructure.persistence import (
    SqlAlchemyMemberRoleRepository,
    SqlAlchemyRefreshSessionRepository,
    SqlAlchemyUserRepository,
)
from ams.infrastructure.readiness import AlwaysReadyProbe, PostgreSqlReadinessProbe
from ams.infrastructure.reliability_persistence import SqlAlchemyUnitOfWork
from ams.infrastructure.storage import LocalFileStorage
from ams.infrastructure.ticket_read_persistence import SqlAlchemyTicketReadRepository
from ams.interface.api.asset_import_routes import router as asset_import_router
from ams.interface.api.asset_read_routes import router as asset_read_router
from ams.interface.api.asset_routes import router as asset_router
from ams.interface.api.custody_routes import router as custody_router
from ams.interface.api.dependencies import (
    AssetCreationServiceFactory,
    AssetImportServiceFactory,
    AssetReadServiceFactory,
    CustodyConfirmationServiceFactory,
    EventDayServiceFactory,
    HandoverServiceFactory,
    MemberRoleServiceFactory,
    OperationalReadServiceFactory,
    ReconciliationServiceFactory,
    ResponsibleTeamServiceFactory,
    SessionServiceFactory,
    ShiftExecutionServiceFactory,
    TicketCreationServiceFactory,
    TicketLifecycleServiceFactory,
    TicketReadServiceFactory,
    TransferServiceFactory,
)
from ams.interface.api.errors import register_exception_handlers
from ams.interface.api.event_routes import router as event_day_router
from ams.interface.api.handover_routes import router as handover_router
from ams.interface.api.health import router as health_router
from ams.interface.api.member_routes import router as member_router
from ams.interface.api.observability import RequestObservabilityMiddleware, configure_logging
from ams.interface.api.operational_routes import router as operational_router
from ams.interface.api.reconciliation_routes import router as reconciliation_router
from ams.interface.api.routes import AuthCookieSettings
from ams.interface.api.routes import router as auth_router
from ams.interface.api.shift_routes import router as shift_router
from ams.interface.api.team_asset_routes import router as team_asset_router
from ams.interface.api.ticket_routes import router as ticket_router
from ams.interface.api.transfer_routes import router as transfer_router


class SqlAlchemySessionServiceFactory:
    """Build one transaction-scoped authentication service per request."""

    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        token_settings: TokenSettings,
    ) -> None:
        self._session_maker = session_maker
        self._token_settings = token_settings
        self._password_hasher = Argon2PasswordHasher()

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[SessionService]:
        """Commit successful requests and roll back rejected/failed requests."""

        async with self._session_maker() as database_session:
            users = SqlAlchemyUserRepository(database_session)
            refresh_sessions = SqlAlchemyRefreshSessionRepository(database_session)
            service = SessionService(
                identity_provider=LocalIdentityProvider(users, self._password_hasher),
                token_service=JwtTokenService(self._token_settings, refresh_sessions),
                user_repository=users,
            )
            try:
                yield service
                await database_session.commit()
            except Exception:
                await database_session.rollback()
                raise


class SqlAlchemyAssetCreationServiceFactory:
    """Build the F02.04 service around fresh generalized transactions."""

    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self._session_maker = session_maker

    def __call__(self) -> AssetCreationService:
        """Return a service with one shared UTC clock and transaction factory."""

        clock = SystemClock()
        executor = IdempotentCommandExecutor(
            lambda: SqlAlchemyUnitOfWork(self._session_maker),
            clock,
        )
        return AssetCreationService(executor, clock)


class SqlAlchemyCustodyConfirmationServiceFactory:
    """Build F02.05 around fresh transactions and injected private storage."""

    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        evidence_storage: EvidenceStorageService,
    ) -> None:
        self._session_maker = session_maker
        self._evidence_storage = evidence_storage

    def __call__(self) -> CustodyConfirmationService:
        """Return a service with one UTC clock and generalized transaction factory."""

        clock = SystemClock()
        executor = IdempotentCommandExecutor(
            lambda: SqlAlchemyUnitOfWork(self._session_maker),
            clock,
        )
        return CustodyConfirmationService(executor, self._evidence_storage, clock)


class SqlAlchemyAssetReadServiceFactory:
    """Own one read-only SQLAlchemy session for each Asset query request."""

    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self._session_maker = session_maker

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[AssetReadService]:
        """Yield a query service and close its session after response mapping."""

        async with self._session_maker() as database_session:
            yield AssetReadService(SqlAlchemyAssetReadRepository(database_session))


class SqlAlchemyAssetImportServiceFactory:
    """Build baseline import commands around generalized transactions."""

    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self._session_maker = session_maker

    def __call__(self) -> AssetImportService:
        """Return a service using the bounded CSV/XLSX parser and UTC clock."""

        clock = SystemClock()
        executor = IdempotentCommandExecutor(
            lambda: SqlAlchemyUnitOfWork(self._session_maker),
            clock,
        )
        return AssetImportService(executor, CsvXlsxAssetImportParser(), clock)


class SqlAlchemyResponsibleTeamServiceFactory:
    """Build Responsible Team commands around fresh SQLAlchemy transactions."""

    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self._session_maker = session_maker

    def __call__(self) -> ResponsibleTeamService:
        """Return a service whose Unit of Work owns the update transaction."""

        return ResponsibleTeamService(lambda: SqlAlchemyUnitOfWork(self._session_maker))


class SqlAlchemyTicketCreationServiceFactory:
    """Build grouped Ticket creation around fresh generalized transactions."""

    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        clock: IClock | None = None,
    ) -> None:
        self._session_maker = session_maker
        self._clock = clock or SystemClock()

    def __call__(self) -> TicketCreationService:
        """Return an idempotent Ticket creation service with a UTC clock."""

        executor = IdempotentCommandExecutor(
            lambda: SqlAlchemyUnitOfWork(self._session_maker), self._clock
        )
        return TicketCreationService(
            executor,
            self._clock,
            TicketReminderScheduler(self._clock),
        )


class SqlAlchemyTicketLifecycleServiceFactory:
    """Build Ticket transitions around fresh transactions and private storage."""

    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        evidence_storage: EvidenceStorageService,
        clock: IClock | None = None,
    ) -> None:
        self._session_maker = session_maker
        self._evidence_storage = evidence_storage
        self._clock = clock or SystemClock()

    def __call__(self) -> TicketLifecycleService:
        """Return an idempotent lifecycle service with a UTC clock."""

        executor = IdempotentCommandExecutor(
            lambda: SqlAlchemyUnitOfWork(self._session_maker), self._clock
        )
        return TicketLifecycleService(
            executor,
            self._evidence_storage,
            self._clock,
            TicketReminderScheduler(self._clock),
        )


class SqlAlchemyTicketReadServiceFactory:
    """Own one read-only SQLAlchemy session for each Ticket query request."""

    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self._session_maker = session_maker

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[TicketReadService]:
        """Yield a query service and close its session after response mapping."""

        async with self._session_maker() as database_session:
            yield TicketReadService(SqlAlchemyTicketReadRepository(database_session))


class SqlAlchemyOperationalReadServiceFactory:
    """Own one read-only SQLAlchemy session for each operational query request."""

    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self._session_maker = session_maker

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[OperationalReadService]:
        """Yield the operational query service and close its session afterward."""

        async with self._session_maker() as database_session:
            yield OperationalReadService(
                SqlAlchemyOperationalReadRepository(database_session), SystemClock()
            )


class SqlAlchemyEventDayServiceFactory:
    """Build Event configuration around generalized SQLAlchemy transactions."""

    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self._session_maker = session_maker

    def __call__(self) -> EventDayService:
        """Return an idempotent service and its 30-minute reminder scheduler."""

        clock = SystemClock()

        def unit_of_work_factory() -> SqlAlchemyUnitOfWork:
            return SqlAlchemyUnitOfWork(self._session_maker)

        return EventDayService(
            IdempotentCommandExecutor(unit_of_work_factory, clock),
            unit_of_work_factory,
            clock,
            EventReminderScheduler(clock),
        )


class SqlAlchemyShiftExecutionServiceFactory:
    """Build F08 Shift commands and queries around generalized transactions."""

    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self._session_maker = session_maker

    def __call__(self) -> ShiftExecutionService:
        """Return an idempotent Shift execution service with a shared UTC clock."""

        clock = SystemClock()

        def unit_of_work_factory() -> SqlAlchemyUnitOfWork:
            return SqlAlchemyUnitOfWork(self._session_maker)

        return ShiftExecutionService(
            IdempotentCommandExecutor(unit_of_work_factory, clock),
            unit_of_work_factory,
            clock,
        )


class SqlAlchemyReconciliationServiceFactory:
    """Build F09 commands and reads around generalized transactions."""

    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        evidence_storage: EvidenceStorageService,
    ) -> None:
        self._session_maker = session_maker
        self._evidence_storage = evidence_storage

    def __call__(self) -> ReconciliationService:
        """Return one idempotent reconciliation service with shared ports."""

        clock = SystemClock()

        def unit_of_work_factory() -> SqlAlchemyUnitOfWork:
            return SqlAlchemyUnitOfWork(self._session_maker)

        return ReconciliationService(
            IdempotentCommandExecutor(unit_of_work_factory, clock),
            unit_of_work_factory,
            self._evidence_storage,
            clock,
        )


class SqlAlchemyTransferServiceFactory:
    """Build F10 Transfer commands and queries around generalized transactions."""

    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        evidence_storage: EvidenceStorageService,
    ) -> None:
        self._session_maker = session_maker
        self._evidence_storage = evidence_storage

    def __call__(self) -> TransferService:
        """Return an idempotent two-party Transfer service."""

        clock = SystemClock()

        def unit_of_work_factory() -> SqlAlchemyUnitOfWork:
            return SqlAlchemyUnitOfWork(self._session_maker)

        return TransferService(
            IdempotentCommandExecutor(unit_of_work_factory, clock),
            unit_of_work_factory,
            self._evidence_storage,
            clock,
        )


class SqlAlchemyHandoverServiceFactory:
    """Build F11 term snapshot, confirmation, and export use cases."""

    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self._session_maker = session_maker

    def __call__(self) -> HandoverService:
        """Return an idempotent Handover service with one UTC clock."""

        clock = SystemClock()

        def unit_of_work_factory() -> SqlAlchemyUnitOfWork:
            return SqlAlchemyUnitOfWork(self._session_maker)

        return HandoverService(
            IdempotentCommandExecutor(unit_of_work_factory, clock),
            unit_of_work_factory,
            clock,
        )


class SqlAlchemyMemberRoleServiceFactory:
    """Own one transaction per member role request."""

    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self._session_maker = session_maker

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[MemberRoleService]:
        """Commit successful requests and roll back rejected/failed requests."""

        async with self._session_maker() as database_session:
            try:
                yield MemberRoleService(SqlAlchemyMemberRoleRepository(database_session))
                await database_session.commit()
            except Exception:
                await database_session.rollback()
                raise


def create_app(
    session_service_factory: SessionServiceFactory | None = None,
    settings: ApiSettings | None = None,
    readiness_probe: IReadinessProbe | None = None,
    cors_allowed_origins: tuple[str, ...] | None = None,
    log_level: str | None = None,
    asset_creation_service_factory: AssetCreationServiceFactory | None = None,
    custody_confirmation_service_factory: CustodyConfirmationServiceFactory | None = None,
    asset_read_service_factory: AssetReadServiceFactory | None = None,
    asset_import_service_factory: AssetImportServiceFactory | None = None,
    responsible_team_service_factory: ResponsibleTeamServiceFactory | None = None,
    ticket_creation_service_factory: TicketCreationServiceFactory | None = None,
    ticket_lifecycle_service_factory: TicketLifecycleServiceFactory | None = None,
    ticket_read_service_factory: TicketReadServiceFactory | None = None,
    operational_read_service_factory: OperationalReadServiceFactory | None = None,
    event_day_service_factory: EventDayServiceFactory | None = None,
    shift_execution_service_factory: ShiftExecutionServiceFactory | None = None,
    reconciliation_service_factory: ReconciliationServiceFactory | None = None,
    transfer_service_factory: TransferServiceFactory | None = None,
    handover_service_factory: HandoverServiceFactory | None = None,
    member_role_service_factory: MemberRoleServiceFactory | None = None,
) -> FastAPI:
    """Create the API with explicit dependencies or environment-backed production wiring."""

    engine: AsyncEngine | None = None
    maker: async_sessionmaker[AsyncSession] | None = None
    resolved_settings: ApiSettings | None = settings
    cookie_settings = AuthCookieSettings()
    if session_service_factory is None:
        resolved_settings = settings or ApiSettings()  # type: ignore[call-arg]
        engine = create_async_engine(resolved_settings.database_url, pool_pre_ping=True)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        session_service_factory = SqlAlchemySessionServiceFactory(
            maker, resolved_settings.to_token_settings()
        )
        cookie_settings = AuthCookieSettings(
            secure=resolved_settings.refresh_cookie_secure,
            max_age_seconds=resolved_settings.refresh_token_days * 24 * 60 * 60,
        )
    if asset_creation_service_factory is None and maker is not None:
        asset_creation_service_factory = SqlAlchemyAssetCreationServiceFactory(maker)
    if custody_confirmation_service_factory is None and maker is not None:
        assert resolved_settings is not None
        custody_confirmation_service_factory = SqlAlchemyCustodyConfirmationServiceFactory(
            maker,
            EvidenceStorageService(LocalFileStorage(resolved_settings.evidence_storage_root)),
        )
    if asset_read_service_factory is None and maker is not None:
        asset_read_service_factory = SqlAlchemyAssetReadServiceFactory(maker)
    if asset_import_service_factory is None and maker is not None:
        asset_import_service_factory = SqlAlchemyAssetImportServiceFactory(maker)
    if responsible_team_service_factory is None and maker is not None:
        responsible_team_service_factory = SqlAlchemyResponsibleTeamServiceFactory(maker)
    if ticket_creation_service_factory is None and maker is not None:
        ticket_creation_service_factory = SqlAlchemyTicketCreationServiceFactory(maker)
    if ticket_read_service_factory is None and maker is not None:
        ticket_read_service_factory = SqlAlchemyTicketReadServiceFactory(maker)
    if ticket_lifecycle_service_factory is None and maker is not None:
        assert resolved_settings is not None
        ticket_lifecycle_service_factory = SqlAlchemyTicketLifecycleServiceFactory(
            maker,
            EvidenceStorageService(LocalFileStorage(resolved_settings.evidence_storage_root)),
        )
    if operational_read_service_factory is None and maker is not None:
        operational_read_service_factory = SqlAlchemyOperationalReadServiceFactory(maker)
    if event_day_service_factory is None and maker is not None:
        event_day_service_factory = SqlAlchemyEventDayServiceFactory(maker)
    if shift_execution_service_factory is None and maker is not None:
        shift_execution_service_factory = SqlAlchemyShiftExecutionServiceFactory(maker)
    if reconciliation_service_factory is None and maker is not None:
        assert resolved_settings is not None
        reconciliation_service_factory = SqlAlchemyReconciliationServiceFactory(
            maker,
            EvidenceStorageService(LocalFileStorage(resolved_settings.evidence_storage_root)),
        )
    if transfer_service_factory is None and maker is not None:
        assert resolved_settings is not None
        transfer_service_factory = SqlAlchemyTransferServiceFactory(
            maker,
            EvidenceStorageService(LocalFileStorage(resolved_settings.evidence_storage_root)),
        )
    if handover_service_factory is None and maker is not None:
        handover_service_factory = SqlAlchemyHandoverServiceFactory(maker)
    if member_role_service_factory is None and maker is not None:
        member_role_service_factory = SqlAlchemyMemberRoleServiceFactory(maker)

    resolved_origins = (
        cors_allowed_origins
        if cors_allowed_origins is not None
        else resolved_settings.cors_allowed_origins
        if resolved_settings is not None
        else ()
    )
    resolved_log_level = (
        log_level
        if log_level is not None
        else resolved_settings.log_level
        if resolved_settings is not None
        else "INFO"
    )
    configure_logging(resolved_log_level)

    if readiness_probe is None:
        readiness_probe = (
            PostgreSqlReadinessProbe(engine) if engine is not None else AlwaysReadyProbe()
        )

    close_engine: Callable[[], Awaitable[None]] | None = (
        engine.dispose if engine is not None else None
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if close_engine is not None:
            await close_engine()

    application = FastAPI(title="AMS API", version="1.0.0", lifespan=lifespan)
    application.state.session_service_factory = session_service_factory
    application.state.auth_cookie_settings = cookie_settings
    application.state.readiness_probe = readiness_probe
    application.state.asset_creation_service_factory = asset_creation_service_factory
    application.state.custody_confirmation_service_factory = (
        custody_confirmation_service_factory
    )
    application.state.asset_read_service_factory = asset_read_service_factory
    application.state.asset_import_service_factory = asset_import_service_factory
    application.state.responsible_team_service_factory = responsible_team_service_factory
    application.state.ticket_creation_service_factory = ticket_creation_service_factory
    application.state.ticket_lifecycle_service_factory = ticket_lifecycle_service_factory
    application.state.ticket_read_service_factory = ticket_read_service_factory
    application.state.operational_read_service_factory = operational_read_service_factory
    application.state.event_day_service_factory = event_day_service_factory
    application.state.shift_execution_service_factory = shift_execution_service_factory
    application.state.reconciliation_service_factory = reconciliation_service_factory
    application.state.transfer_service_factory = transfer_service_factory
    application.state.handover_service_factory = handover_service_factory
    application.state.member_role_service_factory = member_role_service_factory
    application.include_router(auth_router, prefix="/api/v1")
    application.include_router(asset_router, prefix="/api/v1")
    application.include_router(asset_import_router, prefix="/api/v1")
    application.include_router(asset_read_router, prefix="/api/v1")
    application.include_router(team_asset_router, prefix="/api/v1")
    application.include_router(custody_router, prefix="/api/v1")
    application.include_router(ticket_router, prefix="/api/v1")
    application.include_router(operational_router, prefix="/api/v1")
    application.include_router(event_day_router, prefix="/api/v1")
    application.include_router(shift_router, prefix="/api/v1")
    application.include_router(reconciliation_router, prefix="/api/v1")
    application.include_router(transfer_router, prefix="/api/v1")
    application.include_router(handover_router, prefix="/api/v1")
    application.include_router(member_router, prefix="/api/v1")
    application.include_router(health_router)
    register_exception_handlers(application)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        expose_headers=["X-Trace-ID"],
    )
    application.add_middleware(RequestObservabilityMiddleware)
    return application
