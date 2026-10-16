"""FastAPI dependency boundaries for authentication and authorization."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import Annotated, Protocol

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ams.application.asset_creation import AssetCreationService
from ams.application.asset_imports import AssetImportService
from ams.application.asset_queries import AssetReadService
from ams.application.authentication import SessionService
from ams.application.custody_confirmation import CustodyConfirmationService
from ams.application.event_days import EventDayService
from ams.application.handovers import HandoverService
from ams.application.member_roles import MemberRoleService
from ams.application.operational_queries import OperationalReadService
from ams.application.reconciliations import ReconciliationService
from ams.application.responsible_team import ResponsibleTeamService
from ams.application.shift_execution import ShiftExecutionService
from ams.application.ticket_commands import TicketLifecycleService
from ams.application.ticket_queries import TicketReadService
from ams.application.tickets import TicketCreationService
from ams.application.transfers import TransferService
from ams.domain.authentication import TokenExpiredError
from ams.domain.identity import AuthorizationError, CurrentActor, OperationalRole
from ams.interface.api.observability import bind_actor


class SessionServiceFactory(Protocol):
    """Create one transaction-scoped session service per request."""

    def __call__(self) -> AbstractAsyncContextManager[SessionService]:
        """Return an async context manager owning the request transaction."""


class AssetCreationServiceFactory(Protocol):
    """Create a service whose command executor owns its transaction."""

    def __call__(self) -> AssetCreationService:
        """Return a configured Asset creation service."""


class CustodyConfirmationServiceFactory(Protocol):
    """Create an evidence-backed Custody confirmation service."""

    def __call__(self) -> CustodyConfirmationService:
        """Return a configured Custody confirmation service."""


class AssetReadServiceFactory(Protocol):
    """Create one session-scoped Asset read service."""

    def __call__(self) -> AbstractAsyncContextManager[AssetReadService]:
        """Return a context manager owning the read session."""


class AssetImportServiceFactory(Protocol):
    """Create the idempotent baseline Asset import service."""

    def __call__(self) -> AssetImportService:
        """Return a configured import service."""


class ResponsibleTeamServiceFactory(Protocol):
    """Create a transaction-owning Responsible Team update service."""

    def __call__(self) -> ResponsibleTeamService:
        """Return a configured Responsible Team update service."""


class TicketCreationServiceFactory(Protocol):
    """Create an idempotent grouped Ticket creation service."""

    def __call__(self) -> TicketCreationService:
        """Return a configured Ticket creation service."""


class TicketLifecycleServiceFactory(Protocol):
    """Create an evidence-aware Ticket lifecycle service."""

    def __call__(self) -> TicketLifecycleService:
        """Return a configured Ticket lifecycle service."""


class TicketReadServiceFactory(Protocol):
    """Create one session-scoped Ticket read service."""

    def __call__(self) -> AbstractAsyncContextManager[TicketReadService]:
        """Return a context manager owning the read session."""


class OperationalReadServiceFactory(Protocol):
    """Create one session-scoped operational read service."""

    def __call__(self) -> AbstractAsyncContextManager[OperationalReadService]:
        """Return a context manager owning the read session."""


class EventDayServiceFactory(Protocol):
    """Create the idempotent Event configuration service."""

    def __call__(self) -> EventDayService:
        """Return a configured Event Day service."""


class ShiftExecutionServiceFactory(Protocol):
    """Create the idempotent Shift execution service."""

    def __call__(self) -> ShiftExecutionService:
        """Return a configured Shift execution service."""


class ReconciliationServiceFactory(Protocol):
    """Create the idempotent reconciliation workflow service."""

    def __call__(self) -> ReconciliationService:
        """Return a configured reconciliation service."""


class TransferServiceFactory(Protocol):
    """Create the idempotent standalone Transfer service."""

    def __call__(self) -> TransferService:
        """Return a configured Transfer service."""


class HandoverServiceFactory(Protocol):
    """Create the idempotent term Handover service."""

    def __call__(self) -> HandoverService:
        """Return a configured Handover service."""


class MemberRoleServiceFactory(Protocol):
    """Create one transaction-scoped member role service per request."""

    def __call__(self) -> AbstractAsyncContextManager[MemberRoleService]:
        """Return a context manager owning the request transaction."""


BEARER_SCHEME = HTTPBearer(auto_error=False)


async def get_session_service(request: Request) -> AsyncIterator[SessionService]:
    """Yield the request-scoped service configured by the application factory."""

    factory: SessionServiceFactory = request.app.state.session_service_factory
    async with factory() as service:
        yield service


def get_asset_creation_service(request: Request) -> AssetCreationService:
    """Return the configured idempotent Asset creation service."""

    factory: AssetCreationServiceFactory | None = (
        request.app.state.asset_creation_service_factory
    )
    if factory is None:
        raise RuntimeError("Asset creation service is not configured.")
    return factory()


def get_custody_confirmation_service(request: Request) -> CustodyConfirmationService:
    """Return the configured evidence-backed Custody confirmation service."""

    factory: CustodyConfirmationServiceFactory | None = (
        request.app.state.custody_confirmation_service_factory
    )
    if factory is None:
        raise RuntimeError("Custody confirmation service is not configured.")
    return factory()


async def get_asset_read_service(request: Request) -> AsyncIterator[AssetReadService]:
    """Yield an Asset read service while its database session remains open."""

    factory: AssetReadServiceFactory | None = request.app.state.asset_read_service_factory
    if factory is None:
        raise RuntimeError("Asset read service is not configured.")
    async with factory() as service:
        yield service


def get_asset_import_service(request: Request) -> AssetImportService:
    """Return the configured baseline Asset import service."""

    factory: AssetImportServiceFactory | None = request.app.state.asset_import_service_factory
    if factory is None:
        raise RuntimeError("Asset import service is not configured.")
    return factory()


def get_responsible_team_service(request: Request) -> ResponsibleTeamService:
    """Return the configured Responsible Team classification service."""

    factory: ResponsibleTeamServiceFactory | None = (
        request.app.state.responsible_team_service_factory
    )
    if factory is None:
        raise RuntimeError("Responsible Team service is not configured.")
    return factory()


def get_ticket_creation_service(request: Request) -> TicketCreationService:
    """Return the configured grouped Ticket creation service."""

    factory: TicketCreationServiceFactory | None = (
        request.app.state.ticket_creation_service_factory
    )
    if factory is None:
        raise RuntimeError("Ticket creation service is not configured.")
    return factory()


def get_ticket_lifecycle_service(request: Request) -> TicketLifecycleService:
    """Return the configured Ticket transition service."""

    factory: TicketLifecycleServiceFactory | None = (
        request.app.state.ticket_lifecycle_service_factory
    )
    if factory is None:
        raise RuntimeError("Ticket lifecycle service is not configured.")
    return factory()


async def get_ticket_read_service(request: Request) -> AsyncIterator[TicketReadService]:
    """Yield a Ticket query service while its database session remains open."""

    factory: TicketReadServiceFactory | None = request.app.state.ticket_read_service_factory
    if factory is None:
        raise RuntimeError("Ticket read service is not configured.")
    async with factory() as service:
        yield service


async def get_operational_read_service(
    request: Request,
) -> AsyncIterator[OperationalReadService]:
    """Yield an operational query service while its database session remains open."""

    factory: OperationalReadServiceFactory | None = (
        request.app.state.operational_read_service_factory
    )
    if factory is None:
        raise RuntimeError("Operational read service is not configured.")
    async with factory() as service:
        yield service


def get_event_day_service(request: Request) -> EventDayService:
    """Return the configured Event Day command/query service."""

    factory: EventDayServiceFactory | None = request.app.state.event_day_service_factory
    if factory is None:
        raise RuntimeError("Event Day service is not configured.")
    return factory()


def get_shift_execution_service(request: Request) -> ShiftExecutionService:
    """Return the configured Shift command/query service."""

    factory: ShiftExecutionServiceFactory | None = (
        request.app.state.shift_execution_service_factory
    )
    if factory is None:
        raise RuntimeError("Shift execution service is not configured.")
    return factory()


def get_reconciliation_service(request: Request) -> ReconciliationService:
    """Return the configured reconciliation workflow service."""

    factory: ReconciliationServiceFactory | None = (
        request.app.state.reconciliation_service_factory
    )
    if factory is None:
        raise RuntimeError("Reconciliation service is not configured.")
    return factory()


def get_transfer_service(request: Request) -> TransferService:
    """Return the configured standalone Transfer service."""

    factory: TransferServiceFactory | None = request.app.state.transfer_service_factory
    if factory is None:
        raise RuntimeError("Transfer service is not configured.")
    return factory()


def get_handover_service(request: Request) -> HandoverService:
    """Return the configured term Handover service."""

    factory: HandoverServiceFactory | None = request.app.state.handover_service_factory
    if factory is None:
        raise RuntimeError("Handover service is not configured.")
    return factory()


async def get_member_role_service(request: Request) -> AsyncIterator[MemberRoleService]:
    """Yield a member role service while its transaction remains open."""

    factory: MemberRoleServiceFactory | None = request.app.state.member_role_service_factory
    if factory is None:
        raise RuntimeError("Member role service is not configured.")
    async with factory() as service:
        yield service


async def get_current_actor(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(BEARER_SCHEME)],
    service: Annotated[SessionService, Depends(get_session_service)],
) -> CurrentActor:
    """Resolve a Bearer access token into a fresh, typed current actor."""

    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise TokenExpiredError("Authentication credentials are required.")
    actor = await service.get_current_actor(credentials.credentials)
    bind_actor(actor.id)
    return actor


class RequireRoles:
    """FastAPI dependency enforcing at least one server-derived operational role."""

    def __init__(self, *roles: OperationalRole) -> None:
        if not roles:
            raise ValueError("At least one required role must be configured.")
        self._roles = frozenset(roles)

    async def __call__(
        self, actor: Annotated[CurrentActor, Depends(get_current_actor)]
    ) -> CurrentActor:
        """Return an authorized actor or raise a domain authorization error."""

        if actor.roles.isdisjoint(self._roles):
            raise AuthorizationError("Actor does not have the required role.")
        return actor
