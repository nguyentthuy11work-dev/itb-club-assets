"""PostgreSQL adapter for transaction-history and dashboard projections."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ams.application.operational_queries import OperationalReadPersistenceError
from ams.domain.operational_views import (
    AssetTransactionKind,
    AssetTransactionView,
    DashboardAction,
    DashboardReferenceType,
    DashboardTodoKind,
    DashboardTodoView,
    HrDashboardStats,
    HrDashboardView,
    IOperationalReadRepository,
    MemberDashboardStats,
    MemberDashboardView,
)

TRANSACTION_CTE = """
WITH transactions AS (
    SELECT 'ASSET_CREATED:' || assets.id::TEXT AS id,
           assets.id AS asset_id, assets.name AS asset_name,
           'ASSET_CREATED' AS kind, 'ASSET_CREATED' AS event_type,
           assets.total_quantity AS quantity, assets.created_at AS occurred_at,
           assets.created_by_user_id AS actor_user_id,
           creator.display_name AS actor_name,
           NULL::UUID AS ticket_id, NULL::UUID AS custody_id,
           NULL::UUID AS from_user_id, NULL::UUID AS to_user_id
    FROM assets
    JOIN users AS creator ON creator.id = assets.created_by_user_id
    UNION ALL
    SELECT 'CUSTODY_ASSIGNED:' || custodies.id::TEXT,
           assets.id, assets.name, 'CUSTODY_ASSIGNED', 'CUSTODY_ASSIGNED',
           custodies.on_hand_quantity, custodies.assigned_at,
           custodies.assigned_by_user_id, assigner.display_name,
           NULL::UUID, custodies.id, NULL::UUID, custodies.custodian_user_id
    FROM custodies
    JOIN assets ON assets.id = custodies.asset_id
    JOIN users AS assigner ON assigner.id = custodies.assigned_by_user_id
    UNION ALL
    SELECT 'CUSTODY_CONFIRMED:' || custodies.id::TEXT,
           assets.id, assets.name, 'CUSTODY_CONFIRMED', 'CUSTODY_CONFIRMED',
           custodies.on_hand_quantity, custodies.confirmed_at,
           custodies.confirmed_by_user_id, confirmer.display_name,
           NULL::UUID, custodies.id, NULL::UUID, custodies.custodian_user_id
    FROM custodies
    JOIN assets ON assets.id = custodies.asset_id
    JOIN users AS confirmer ON confirmer.id = custodies.confirmed_by_user_id
    WHERE custodies.confirmed_at IS NOT NULL
    UNION ALL
    SELECT 'TICKET_EVENT:' || events.id::TEXT || ':' || items.id::TEXT,
           assets.id, assets.name, 'TICKET_EVENT', events.event_type,
           items.quantity, events.occurred_at,
           events.actor_user_id, actor.display_name,
           events.ticket_id, items.source_custody_id,
           tickets.source_custodian_user_id, tickets.borrower_user_id
    FROM ticket_events AS events
    JOIN borrow_tickets AS tickets ON tickets.id = events.ticket_id
    JOIN ticket_items AS items ON items.ticket_id = tickets.id
    JOIN assets ON assets.id = items.asset_id
    JOIN users AS actor ON actor.id = events.actor_user_id
)
"""


class SqlAlchemyOperationalReadRepository(IOperationalReadRepository):
    """Map SQL-first operational projections into immutable view objects."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_asset_scope(self, asset_id: UUID) -> tuple[bool, UUID | None]:
        """Return Asset existence and Responsible Team without loading an aggregate."""

        try:
            result = await self._session.execute(
                text("SELECT responsible_team_id FROM assets WHERE id = :asset_id"),
                {"asset_id": asset_id},
            )
        except SQLAlchemyError as exception:
            raise OperationalReadPersistenceError("Asset scope query failed.") from exception
        row = result.one_or_none()
        if row is None:
            return False, None
        return True, UUID(str(row.responsible_team_id)) if row.responsible_team_id else None

    async def list_transactions(
        self,
        *,
        asset_id: UUID | None,
        from_time: datetime | None,
        to_time: datetime | None,
        offset: int,
        limit: int,
    ) -> tuple[tuple[AssetTransactionView, ...], int]:
        """Return stable history filtered before pagination."""

        filters: list[str] = []
        parameters: dict[str, object] = {"offset": offset, "limit": limit}
        if asset_id is not None:
            filters.append("asset_id = :asset_id")
            parameters["asset_id"] = asset_id
        if from_time is not None:
            filters.append("occurred_at >= :from_time")
            parameters["from_time"] = from_time
        if to_time is not None:
            filters.append("occurred_at <= :to_time")
            parameters["to_time"] = to_time
        where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
        try:
            count_result = await self._session.execute(
                text(f"{TRANSACTION_CTE} SELECT COUNT(*) FROM transactions {where_sql}"),
                parameters,
            )
            page_result = await self._session.execute(
                text(
                    f"""{TRANSACTION_CTE}
                    SELECT * FROM transactions
                    {where_sql}
                    ORDER BY occurred_at DESC, id DESC
                    OFFSET :offset LIMIT :limit
                    """
                ),
                parameters,
            )
        except SQLAlchemyError as exception:
            raise OperationalReadPersistenceError(
                "Asset transaction query failed."
            ) from exception
        return (
            tuple(self._transaction(row) for row in page_result.mappings()),
            int(count_result.scalar_one()),
        )

    async def get_member_dashboard(self, actor_id: UUID, *, today: date) -> MemberDashboardView:
        """Load actor-scoped counters and actionable aggregate references."""

        parameters = {"actor_id": actor_id, "today": today}
        try:
            stats_result = await self._session.execute(
                text(
                    """
                    SELECT
                      (SELECT COUNT(*) FROM borrow_tickets
                       WHERE borrower_user_id = :actor_id
                         AND method = 'TEMPORARY'
                         AND status IN ('APPROVED', 'BORROWING', 'RETURNING'))
                        AS active_borrow_count,
                      (SELECT COUNT(*) FROM borrow_tickets
                       WHERE borrower_user_id = :actor_id
                         AND method = 'TEMPORARY' AND due_date < :today
                         AND status IN ('APPROVED', 'BORROWING')) AS overdue_count,
                      (SELECT COUNT(*) FROM borrow_tickets
                       WHERE source_custodian_user_id = :actor_id
                         AND status = 'PENDING') AS pending_approval_count,
                      (SELECT COUNT(*) FROM borrow_tickets
                       WHERE source_custodian_user_id = :actor_id
                         AND status = 'RETURNING') AS awaiting_return_confirmation_count,
                      (SELECT COALESCE(SUM(on_hand_quantity), 0) FROM custodies
                       WHERE custodian_user_id = :actor_id
                         AND status IN ('PENDING', 'ACTIVE')) AS held_quantity,
                      (SELECT COUNT(*) FROM custodies
                       WHERE custodian_user_id = :actor_id
                         AND status = 'PENDING') AS pending_custody_confirmation_count
                    """
                ),
                parameters,
            )
            todo_result = await self._session.execute(
                text(
                    """
                    SELECT 'OVERDUE_RETURN:' || id::TEXT AS id,
                           'OVERDUE_RETURN' AS kind, 'MARK_RETURNED' AS action,
                           'TICKET' AS reference_type, id AS reference_id,
                           due_date
                    FROM borrow_tickets
                    WHERE borrower_user_id = :actor_id AND method = 'TEMPORARY'
                      AND due_date < :today AND status IN ('APPROVED', 'BORROWING')
                    UNION ALL
                    SELECT 'TICKET_APPROVAL:' || id::TEXT, 'TICKET_APPROVAL',
                           'REVIEW_TICKET', 'TICKET', id, due_date
                    FROM borrow_tickets
                    WHERE source_custodian_user_id = :actor_id AND status = 'PENDING'
                    UNION ALL
                    SELECT 'RETURN_CONFIRMATION:' || id::TEXT, 'RETURN_CONFIRMATION',
                           'CONFIRM_RETURNED', 'TICKET', id, due_date
                    FROM borrow_tickets
                    WHERE source_custodian_user_id = :actor_id AND status = 'RETURNING'
                    UNION ALL
                    SELECT 'CUSTODY_CONFIRMATION:' || id::TEXT, 'CUSTODY_CONFIRMATION',
                           'CONFIRM_CUSTODY', 'CUSTODY', id, NULL::DATE
                    FROM custodies
                    WHERE custodian_user_id = :actor_id AND status = 'PENDING'
                    ORDER BY due_date NULLS LAST, id
                    """
                ),
                parameters,
            )
        except SQLAlchemyError as exception:
            raise OperationalReadPersistenceError(
                "Member dashboard query failed."
            ) from exception
        row = stats_result.mappings().one()
        return MemberDashboardView(
            MemberDashboardStats(
                active_borrow_count=int(row["active_borrow_count"]),
                overdue_count=int(row["overdue_count"]),
                pending_approval_count=int(row["pending_approval_count"]),
                awaiting_return_confirmation_count=int(
                    row["awaiting_return_confirmation_count"]
                ),
                held_quantity=int(row["held_quantity"]),
                pending_custody_confirmation_count=int(
                    row["pending_custody_confirmation_count"]
                ),
            ),
            tuple(self._todo(item) for item in todo_result.mappings()),
        )

    async def get_hr_dashboard(
        self, *, today: date, custody_overdue_before: datetime
    ) -> HrDashboardView:
        """Load club-wide counters and overdue Custody confirmation references."""

        parameters = {"today": today, "overdue_before": custody_overdue_before}
        try:
            stats_result = await self._session.execute(
                text(
                    """
                    SELECT
                      (SELECT COALESCE(SUM(total_quantity), 0) FROM assets)
                        AS total_asset_quantity,
                      (SELECT COUNT(*) FROM assets) AS asset_type_count,
                      (SELECT COALESCE(SUM(temporarily_loaned_quantity), 0)
                       FROM custodies) AS temporarily_loaned_quantity,
                      (SELECT COUNT(*) FROM borrow_tickets
                       WHERE method = 'TEMPORARY' AND due_date < :today
                         AND status IN ('APPROVED', 'BORROWING')) AS overdue_ticket_count,
                      (SELECT COUNT(*) FROM custodies WHERE status = 'PENDING')
                        AS pending_custody_confirmation_count,
                      (SELECT COUNT(*) FROM custodies
                       WHERE status = 'PENDING' AND assigned_at <= :overdue_before)
                        AS overdue_custody_confirmation_count
                    """
                ),
                parameters,
            )
            todo_result = await self._session.execute(
                text(
                    """
                    SELECT 'OVERDUE_CUSTODY_CONFIRMATION:' || id::TEXT AS id,
                           'OVERDUE_CUSTODY_CONFIRMATION' AS kind,
                           'REVIEW_CUSTODY' AS action, 'CUSTODY' AS reference_type,
                           id AS reference_id, NULL::DATE AS due_date
                    FROM custodies
                    WHERE status = 'PENDING' AND assigned_at <= :overdue_before
                    ORDER BY assigned_at, id
                    """
                ),
                parameters,
            )
        except SQLAlchemyError as exception:
            raise OperationalReadPersistenceError("HR dashboard query failed.") from exception
        row = stats_result.mappings().one()
        return HrDashboardView(
            HrDashboardStats(
                total_asset_quantity=int(row["total_asset_quantity"]),
                asset_type_count=int(row["asset_type_count"]),
                temporarily_loaned_quantity=int(row["temporarily_loaned_quantity"]),
                overdue_ticket_count=int(row["overdue_ticket_count"]),
                pending_custody_confirmation_count=int(
                    row["pending_custody_confirmation_count"]
                ),
                overdue_custody_confirmation_count=int(
                    row["overdue_custody_confirmation_count"]
                ),
            ),
            tuple(self._todo(item) for item in todo_result.mappings()),
        )

    @staticmethod
    def _transaction(row: RowMapping) -> AssetTransactionView:
        return AssetTransactionView(
            id=str(row["id"]),
            asset_id=UUID(str(row["asset_id"])),
            asset_name=str(row["asset_name"]),
            kind=AssetTransactionKind(str(row["kind"])),
            event_type=str(row["event_type"]),
            quantity=int(row["quantity"]),
            occurred_at=row["occurred_at"],
            actor_user_id=UUID(str(row["actor_user_id"])),
            actor_name=str(row["actor_name"]),
            ticket_id=UUID(str(row["ticket_id"])) if row["ticket_id"] else None,
            custody_id=UUID(str(row["custody_id"])) if row["custody_id"] else None,
            from_user_id=(UUID(str(row["from_user_id"])) if row["from_user_id"] else None),
            to_user_id=UUID(str(row["to_user_id"])) if row["to_user_id"] else None,
        )

    @staticmethod
    def _todo(row: RowMapping) -> DashboardTodoView:
        return DashboardTodoView(
            id=str(row["id"]),
            kind=DashboardTodoKind(str(row["kind"])),
            action=DashboardAction(str(row["action"])),
            reference_type=DashboardReferenceType(str(row["reference_type"])),
            reference_id=UUID(str(row["reference_id"])),
            due_date=row["due_date"],
        )
