"""Application authorization and pagination for Borrow Ticket read models."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from ams.domain.identity import AuthorizationError, CurrentActor
from ams.domain.ticket_views import (
    ITicketReadRepository,
    TicketDetailView,
    TicketListItemView,
    TicketPerspective,
)
from ams.domain.tickets import TicketStatus


class TicketReadNotFoundError(Exception):
    """The Ticket does not exist or is outside the actor's ownership scope."""

    code = "TICKET_NOT_FOUND"


@dataclass(frozen=True, slots=True)
class TicketPage:
    """Stable offset page of ownership-scoped Tickets."""

    items: tuple[TicketListItemView, ...]
    total: int
    offset: int
    limit: int


class TicketReadService:
    """Expose Ticket projections without leaking unrelated actors' records."""

    def __init__(self, repository: ITicketReadRepository) -> None:
        self._repository = repository

    async def list_tickets(
        self,
        actor: CurrentActor,
        *,
        perspective: TicketPerspective,
        status: TicketStatus | None,
        search: str | None,
        asset_id: UUID | None,
        offset: int,
        limit: int,
    ) -> TicketPage:
        """Return a bounded page for one active actor."""

        self._require_active(actor)
        items, total = await self._repository.list_for_actor(
            actor.id,
            perspective=perspective,
            status=status,
            search=search.strip() if search and search.strip() else None,
            asset_id=asset_id,
            offset=offset,
            limit=limit,
        )
        return TicketPage(items, total, offset, limit)

    async def get_ticket(self, actor: CurrentActor, ticket_id: UUID) -> TicketDetailView:
        """Return one involved actor's Ticket or a non-enumerating not-found error."""

        self._require_active(actor)
        ticket = await self._repository.get_for_actor(ticket_id, actor.id)
        if ticket is None:
            raise TicketReadNotFoundError("Ticket was not found.")
        return ticket

    @staticmethod
    def _require_active(actor: CurrentActor) -> None:
        if not actor.is_active:
            raise AuthorizationError("Inactive accounts cannot view Borrow Tickets.")
