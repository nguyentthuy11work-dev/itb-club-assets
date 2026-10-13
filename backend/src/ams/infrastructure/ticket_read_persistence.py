"""PostgreSQL read adapter for ownership-scoped Borrow Ticket projections."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ams.application.tickets import TicketPersistenceError
from ams.domain.ticket_views import (
    ITicketReadRepository,
    TicketDetailView,
    TicketEventView,
    TicketEvidenceView,
    TicketItemView,
    TicketListItemView,
    TicketPerspective,
)
from ams.domain.tickets import TicketMethod, TicketStatus


class SqlAlchemyTicketReadRepository(ITicketReadRepository):
    """Map SQL-first Ticket joins into safe immutable view objects."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_actor(
        self,
        actor_id: UUID,
        *,
        perspective: TicketPerspective,
        status: TicketStatus | None,
        search: str | None,
        asset_id: UUID | None,
        offset: int,
        limit: int,
    ) -> tuple[tuple[TicketListItemView, ...], int]:
        """Return a stable page constrained by actor involvement in SQL."""

        ownership = self._ownership_sql(perspective)
        filters = [ownership]
        parameters: dict[str, object] = {
            "actor_id": actor_id,
            "offset": offset,
            "limit": limit,
        }
        if status is not None:
            filters.append("tickets.status = :status")
            parameters["status"] = status.value
        if asset_id is not None:
            filters.append("items.asset_id = :asset_id")
            parameters["asset_id"] = asset_id
        if search is not None:
            filters.append(
                "(tickets.id::TEXT ILIKE :search OR assets.name ILIKE :search "
                "OR borrower.display_name ILIKE :search "
                "OR custodian.display_name ILIKE :search)"
            )
            parameters["search"] = f"%{search}%"
        where_sql = " AND ".join(filters)
        try:
            count_result = await self._session.execute(
                text(
                    f"""
                    SELECT COUNT(DISTINCT tickets.id)
                    FROM borrow_tickets AS tickets
                    JOIN users AS borrower ON borrower.id = tickets.borrower_user_id
                    JOIN users AS custodian
                      ON custodian.id = tickets.source_custodian_user_id
                    JOIN ticket_items AS items ON items.ticket_id = tickets.id
                    JOIN assets ON assets.id = items.asset_id
                    WHERE {where_sql}
                    """
                ),
                parameters,
            )
            page_result = await self._session.execute(
                text(
                    f"""
                    SELECT tickets.id,
                           tickets.borrower_user_id,
                           borrower.display_name AS borrower_name,
                           tickets.source_custodian_user_id,
                           custodian.display_name AS source_custodian_name,
                           tickets.method,
                           tickets.status,
                           tickets.due_date,
                           SUM(items.quantity)::INTEGER AS total_quantity,
                           ARRAY_AGG(DISTINCT assets.name ORDER BY assets.name) AS asset_names,
                           tickets.created_at,
                           tickets.updated_at
                    FROM borrow_tickets AS tickets
                    JOIN users AS borrower ON borrower.id = tickets.borrower_user_id
                    JOIN users AS custodian
                      ON custodian.id = tickets.source_custodian_user_id
                    JOIN ticket_items AS items ON items.ticket_id = tickets.id
                    JOIN assets ON assets.id = items.asset_id
                    WHERE {where_sql}
                    GROUP BY tickets.id, borrower.display_name, custodian.display_name
                    ORDER BY tickets.created_at DESC, tickets.id
                    OFFSET :offset LIMIT :limit
                    """
                ),
                parameters,
            )
        except SQLAlchemyError as exception:
            raise TicketPersistenceError("Ticket query operation failed.") from exception
        return (
            tuple(self._summary(row) for row in page_result.mappings()),
            int(count_result.scalar_one()),
        )

    async def get_for_actor(self, ticket_id: UUID, actor_id: UUID) -> TicketDetailView | None:
        """Load detail children only after enforcing Ticket ownership in SQL."""

        parameters = {"ticket_id": ticket_id, "actor_id": actor_id}
        try:
            summary_result = await self._session.execute(
                text(
                    """
                    SELECT tickets.id,
                           tickets.borrower_user_id,
                           borrower.display_name AS borrower_name,
                           tickets.source_custodian_user_id,
                           custodian.display_name AS source_custodian_name,
                           tickets.method,
                           tickets.status,
                           tickets.due_date,
                           SUM(items.quantity)::INTEGER AS total_quantity,
                           ARRAY_AGG(DISTINCT assets.name ORDER BY assets.name) AS asset_names,
                           tickets.created_at,
                           tickets.updated_at
                    FROM borrow_tickets AS tickets
                    JOIN users AS borrower ON borrower.id = tickets.borrower_user_id
                    JOIN users AS custodian
                      ON custodian.id = tickets.source_custodian_user_id
                    JOIN ticket_items AS items ON items.ticket_id = tickets.id
                    JOIN assets ON assets.id = items.asset_id
                    WHERE tickets.id = :ticket_id
                      AND (:actor_id = tickets.borrower_user_id
                           OR :actor_id = tickets.source_custodian_user_id)
                    GROUP BY tickets.id, borrower.display_name, custodian.display_name
                    """
                ),
                parameters,
            )
            summary_row = summary_result.mappings().one_or_none()
            if summary_row is None:
                return None
            item_result = await self._session.execute(
                text(
                    """
                    SELECT items.id, items.asset_id, assets.name AS asset_name,
                           items.source_custody_id, items.quantity
                    FROM ticket_items AS items
                    JOIN assets ON assets.id = items.asset_id
                    WHERE items.ticket_id = :ticket_id
                    ORDER BY items.source_custody_id, items.id
                    """
                ),
                parameters,
            )
            evidence_result = await self._session.execute(
                text(
                    """
                    SELECT id, uploaded_by_user_id, evidence_type, content_type,
                           size_bytes, note, created_at
                    FROM ticket_evidence
                    WHERE ticket_id = :ticket_id
                    ORDER BY created_at, id
                    """
                ),
                parameters,
            )
            event_result = await self._session.execute(
                text(
                    """
                    SELECT events.id, events.actor_user_id,
                           users.display_name AS actor_name, events.event_type,
                           events.from_status, events.to_status, events.note,
                           events.occurred_at
                    FROM ticket_events AS events
                    JOIN users ON users.id = events.actor_user_id
                    WHERE events.ticket_id = :ticket_id
                    ORDER BY events.occurred_at, events.id
                    """
                ),
                parameters,
            )
        except SQLAlchemyError as exception:
            raise TicketPersistenceError("Ticket detail query failed.") from exception
        return TicketDetailView(
            summary=self._summary(summary_row),
            items=tuple(
                TicketItemView(
                    id=UUID(str(row["id"])),
                    asset_id=UUID(str(row["asset_id"])),
                    asset_name=str(row["asset_name"]),
                    source_custody_id=UUID(str(row["source_custody_id"])),
                    quantity=int(row["quantity"]),
                )
                for row in item_result.mappings()
            ),
            evidence=tuple(
                TicketEvidenceView(
                    id=UUID(str(row["id"])),
                    uploaded_by_user_id=UUID(str(row["uploaded_by_user_id"])),
                    evidence_type=str(row["evidence_type"]),
                    content_type=str(row["content_type"]),
                    size_bytes=int(row["size_bytes"]),
                    note=str(row["note"]) if row["note"] is not None else None,
                    created_at=row["created_at"],
                )
                for row in evidence_result.mappings()
            ),
            events=tuple(
                TicketEventView(
                    id=UUID(str(row["id"])),
                    actor_user_id=UUID(str(row["actor_user_id"])),
                    actor_name=str(row["actor_name"]),
                    event_type=str(row["event_type"]),
                    from_status=(
                        TicketStatus(str(row["from_status"]))
                        if row["from_status"] is not None
                        else None
                    ),
                    to_status=TicketStatus(str(row["to_status"])),
                    note=str(row["note"]) if row["note"] is not None else None,
                    occurred_at=row["occurred_at"],
                )
                for row in event_result.mappings()
            ),
        )

    @staticmethod
    def _ownership_sql(perspective: TicketPerspective) -> str:
        if perspective is TicketPerspective.BORROWER:
            return "tickets.borrower_user_id = :actor_id"
        if perspective is TicketPerspective.CUSTODIAN:
            return "tickets.source_custodian_user_id = :actor_id"
        return (
            "(tickets.borrower_user_id = :actor_id "
            "OR tickets.source_custodian_user_id = :actor_id)"
        )

    @staticmethod
    def _summary(row: RowMapping) -> TicketListItemView:
        return TicketListItemView(
            id=UUID(str(row["id"])),
            borrower_user_id=UUID(str(row["borrower_user_id"])),
            borrower_name=str(row["borrower_name"]),
            source_custodian_user_id=UUID(str(row["source_custodian_user_id"])),
            source_custodian_name=str(row["source_custodian_name"]),
            method=TicketMethod(str(row["method"])),
            status=TicketStatus(str(row["status"])),
            due_date=row["due_date"],
            total_quantity=int(row["total_quantity"]),
            asset_names=tuple(str(value) for value in row["asset_names"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
