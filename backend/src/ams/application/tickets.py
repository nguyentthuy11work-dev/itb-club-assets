"""Application services and stable failures for Borrow Ticket workflows."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from uuid import UUID, uuid4

from ams.application.idempotency import (
    CommandResponse,
    IIdempotentCommandExecutor,
    canonical_json_fingerprint,
)
from ams.application.reliability import IClock, OutboxEvent
from ams.application.ticket_reminders import TicketReminderScheduler
from ams.application.unit_of_work import IUnitOfWork
from ams.domain.assets import CustodyStatus, InsufficientLendableQuantityError
from ams.domain.identity import AuthorizationError, CurrentActor
from ams.domain.tickets import (
    BorrowTicket,
    TicketAllocation,
    TicketEvent,
    TicketGroupingFactory,
    TicketMethod,
)


class TicketPersistenceError(Exception):
    """Infrastructure could not persist Ticket state safely."""

    code = "TICKET_PERSISTENCE_FAILED"


class TicketCreationResultError(Exception):
    """Stored Ticket creation response does not match the frozen contract."""

    code = "TICKET_CREATION_RESULT_INVALID"


@dataclass(frozen=True, slots=True)
class CreateTicketAllocationCommand:
    """One requested Asset quantity from an explicit source Custody."""

    asset_id: UUID
    source_custody_id: UUID
    quantity: int
    method: TicketMethod
    due_date: date | None


@dataclass(frozen=True, slots=True)
class CreateTicketsCommand:
    """All requested allocations before server-side grouping."""

    allocations: tuple[CreateTicketAllocationCommand, ...]

    def fingerprint_payload(self) -> Mapping[str, object]:
        """Return canonical command data for actor-scoped retry identity."""

        return {
            "allocations": [
                {
                    "assetId": str(item.asset_id),
                    "sourceCustodyId": str(item.source_custody_id),
                    "quantity": item.quantity,
                    "method": item.method.value,
                    "dueDate": item.due_date.isoformat() if item.due_date else None,
                }
                for item in self.allocations
            ]
        }


@dataclass(frozen=True, slots=True)
class CreatedTicketResult:
    """One successfully persisted grouped Ticket."""

    id: UUID
    status: str = "PENDING"


@dataclass(frozen=True, slots=True)
class RejectedTicketGroup:
    """One allocation group rejected without persistent business effects."""

    source_custody_ids: tuple[UUID, ...]
    reason_code: str


@dataclass(frozen=True, slots=True)
class CreateTicketsResult:
    """Partial-success response for all independently validated groups."""

    created_tickets: tuple[CreatedTicketResult, ...]
    rejected_groups: tuple[RejectedTicketGroup, ...]
    replayed: bool


class TicketCreationService:
    """Group, lock, reserve, and persist Borrow Tickets idempotently."""

    def __init__(
        self,
        idempotency: IIdempotentCommandExecutor,
        clock: IClock,
        reminder_scheduler: TicketReminderScheduler,
        *,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._idempotency = idempotency
        self._clock = clock
        self._id_factory = id_factory
        self._grouping = TicketGroupingFactory()
        self._reminder_scheduler = reminder_scheduler

    async def create(
        self,
        command: CreateTicketsCommand,
        actor: CurrentActor,
        idempotency_key: str,
    ) -> CreateTicketsResult:
        """Create every valid group once and report invalid groups without writes."""

        if not actor.is_active:
            raise AuthorizationError("Inactive accounts cannot create Borrow Tickets.")
        fingerprint = canonical_json_fingerprint(command.fingerprint_payload())

        async def operation(unit_of_work: IUnitOfWork) -> CommandResponse:
            return await self._create_once(unit_of_work, command, actor.id)

        response = await self._idempotency.execute(
            actor_id=actor.id,
            command_name="CREATE_BORROW_TICKETS",
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            operation=operation,
        )
        return self._to_result(response.body, response.replayed)

    async def _create_once(
        self,
        unit_of_work: IUnitOfWork,
        command: CreateTicketsCommand,
        actor_id: UUID,
    ) -> CommandResponse:
        now = self._clock.now()
        requested_ids = tuple(item.source_custody_id for item in command.allocations)
        custodies = await unit_of_work.custodies.get_many_for_update(requested_ids)
        custody_by_id = {custody.id: custody for custody in custodies}
        valid: list[TicketAllocation] = []
        rejected: list[RejectedTicketGroup] = []
        for allocation in command.allocations:
            custody = custody_by_id.get(allocation.source_custody_id)
            if (
                custody is None
                or custody.status is not CustodyStatus.ACTIVE
                or custody.asset_id != allocation.asset_id
            ):
                rejected.append(
                    RejectedTicketGroup(
                        (allocation.source_custody_id,), "SOURCE_CUSTODY_UNAVAILABLE"
                    )
                )
                continue
            valid.append(
                TicketAllocation(
                    asset_id=allocation.asset_id,
                    source_custody_id=allocation.source_custody_id,
                    source_custodian_user_id=custody.custodian_user_id,
                    quantity=allocation.quantity,
                    method=allocation.method,
                    due_date=allocation.due_date,
                )
            )

        groups = self._grouping.group(tuple(valid)) if valid else ()
        asset_ids = sorted({item.asset_id for group in groups for item in group.items}, key=str)
        assets = {}
        for asset_id in asset_ids:
            asset = await unit_of_work.assets.get_for_update(asset_id)
            if asset is not None:
                assets[asset_id] = asset

        created: list[CreatedTicketResult] = []
        for group in groups:
            source_ids = tuple(item.source_custody_id for item in group.items)
            if any(item.asset_id not in assets for item in group.items):
                rejected.append(RejectedTicketGroup(source_ids, "ASSET_UNAVAILABLE"))
                continue
            asset_totals: dict[UUID, int] = {}
            for group_item in group.items:
                asset_totals[group_item.asset_id] = (
                    asset_totals.get(group_item.asset_id, 0) + group_item.quantity
                )
            if any(
                custody_by_id[group_item.source_custody_id].lendable_quantity
                < group_item.quantity
                for group_item in group.items
            ) or any(
                assets[asset_id].available_quantity < quantity
                for asset_id, quantity in asset_totals.items()
            ):
                rejected.append(
                    RejectedTicketGroup(source_ids, InsufficientLendableQuantityError.code)
                )
                continue

            custodian = await unit_of_work.users.get_identity(group.source_custodian_user_id)
            if custodian is None:
                rejected.append(RejectedTicketGroup(source_ids, "CUSTODIAN_UNAVAILABLE"))
                continue

            ticket = BorrowTicket.create(
                ticket_id=self._id_factory(), borrower_user_id=actor_id, group=group
            )
            for group_item in group.items:
                custody = custody_by_id[group_item.source_custody_id]
                custody.reserve(group_item.quantity)
                assets[group_item.asset_id].reserve_available(group_item.quantity)
                await unit_of_work.custodies.save(custody)
            for asset_id in asset_totals:
                await unit_of_work.assets.save(assets[asset_id])
            await unit_of_work.tickets.add(ticket, created_at=now)
            await unit_of_work.tickets.add_event(
                TicketEvent(
                    id=self._id_factory(),
                    ticket_id=ticket.id,
                    actor_user_id=actor_id,
                    event_type="CREATED",
                    from_status=None,
                    to_status=ticket.status,
                    occurred_at=now,
                )
            )
            await self._reminder_scheduler.synchronize(unit_of_work, ticket)
            await unit_of_work.outbox.add(
                OutboxEvent(
                    id=self._id_factory(),
                    event_type="TICKET_CREATED",
                    aggregate_type="BORROW_TICKET",
                    aggregate_id=ticket.id,
                    payload={
                        "ticketId": str(ticket.id),
                        "reference": {"type": "TICKET", "id": str(ticket.id)},
                        "action": "REVIEW_TICKET",
                        "emails": [
                            {
                                "recipientId": str(custodian.id),
                                "address": custodian.email,
                                "subject": "Borrow Ticket awaiting approval",
                                "body": f"Ticket {ticket.id} is awaiting your approval.",
                            }
                        ],
                    },
                    scheduled_at=now,
                    created_at=now,
                )
            )
            created.append(CreatedTicketResult(ticket.id))

        return CommandResponse(
            201,
            {
                "createdTickets": [
                    {"id": str(item.id), "status": "PENDING"} for item in created
                ],
                "rejectedGroups": [
                    {
                        "sourceCustodyIds": [str(value) for value in item.source_custody_ids],
                        "reasonCode": item.reason_code,
                    }
                    for item in rejected
                ],
            },
        )

    @staticmethod
    def _to_result(body: Mapping[str, object], replayed: bool) -> CreateTicketsResult:
        try:
            created_rows = body["createdTickets"]
            rejected_rows = body["rejectedGroups"]
            assert isinstance(created_rows, list)
            assert isinstance(rejected_rows, list)
            created = tuple(CreatedTicketResult(UUID(str(row["id"]))) for row in created_rows)
            rejected = tuple(
                RejectedTicketGroup(
                    tuple(UUID(str(value)) for value in row["sourceCustodyIds"]),
                    str(row["reasonCode"]),
                )
                for row in rejected_rows
            )
        except (AssertionError, KeyError, TypeError, ValueError) as exception:
            raise TicketCreationResultError("Stored Ticket result is invalid.") from exception
        return CreateTicketsResult(created, rejected, replayed)
