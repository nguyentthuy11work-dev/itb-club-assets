"""General transaction boundary shared by business and reliability repositories."""

from __future__ import annotations

from abc import abstractmethod

from ams.application.assets import IAssetUnitOfWork
from ams.application.reliability import (
    IEventReminderRepository,
    IIdempotencyRepository,
    INotificationRepository,
    IOutboxRepository,
    ITicketReminderRepository,
)
from ams.domain.asset_imports import IAssetImportRepository
from ams.domain.events import IEventDayRepository
from ams.domain.handovers import IHandoverRepository
from ams.domain.reconciliations import IReconciliationRepository
from ams.domain.repositories import ICustodyEvidenceRepository, IUserRepository
from ams.domain.shift_execution import IShiftExecutionRepository
from ams.domain.tickets import ITicketRepository
from ams.domain.transfers import ITransferRepository


class IUnitOfWork(IAssetUnitOfWork):
    """One transaction containing aggregates, idempotency, and outbox records."""

    outbox: IOutboxRepository
    notifications: INotificationRepository
    idempotency: IIdempotencyRepository
    users: IUserRepository
    custody_evidence: ICustodyEvidenceRepository
    asset_imports: IAssetImportRepository
    tickets: ITicketRepository
    ticket_reminders: ITicketReminderRepository
    event_days: IEventDayRepository
    event_reminders: IEventReminderRepository
    shifts: IShiftExecutionRepository
    reconciliations: IReconciliationRepository
    transfers: ITransferRepository
    handovers: IHandoverRepository

    @abstractmethod
    async def __aenter__(self) -> IUnitOfWork:
        """Open the generalized transaction and return its complete port surface."""
