"""Durable Borrow Ticket due-date reminder scheduling policy."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from ams.application.reliability import IClock, OutboxEvent
from ams.application.unit_of_work import IUnitOfWork
from ams.domain.tickets import BorrowTicket, TicketMethod, TicketStatus

REMINDER_TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")
REMINDER_LOCAL_TIME = time(9, 0)
REMINDER_EVENT_TYPES = frozenset(
    {
        "TICKET_DUE_3_DAYS",
        "TICKET_DUE_1_DAY",
        "TICKET_OVERDUE",
    }
)
TERMINAL_TICKET_STATUSES = frozenset(
    {TicketStatus.REJECTED, TicketStatus.CANCELLED, TicketStatus.CLOSED}
)


class TicketReminderError(Exception):
    """A Ticket reminder cannot be scheduled from current durable identity data."""

    code = "TICKET_REMINDER_SCHEDULING_FAILED"


class TicketReminderScheduler:
    """Replace future temporary-Ticket reminder slots in the business transaction."""

    def __init__(
        self,
        clock: IClock,
        *,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._clock = clock
        self._id_factory = id_factory

    async def synchronize(self, unit_of_work: IUnitOfWork, ticket: BorrowTicket) -> None:
        """Schedule active temporary reminders or cancel them for ineligible Tickets."""

        if (
            ticket.method is not TicketMethod.TEMPORARY
            or ticket.status in TERMINAL_TICKET_STATUSES
        ):
            await unit_of_work.ticket_reminders.cancel_unattempted(ticket.id)
            return
        if ticket.due_date is None:
            raise TicketReminderError("Temporary Ticket due date is unavailable.")
        borrower = await unit_of_work.users.get_identity(ticket.borrower_user_id)
        if borrower is None or not borrower.is_active:
            raise TicketReminderError("Ticket borrower is unavailable for due reminders.")

        now = self._clock.now()
        reminders = tuple(
            self._event(ticket, borrower.id, borrower.email, event_type, scheduled_at, now)
            for event_type, scheduled_at in self._desired_slots(ticket.due_date, now)
        )
        await unit_of_work.ticket_reminders.replace_unattempted(ticket.id, reminders)

    @staticmethod
    def _desired_slots(due_date: date, now: datetime) -> tuple[tuple[str, datetime], ...]:
        slots = (
            ("TICKET_DUE_3_DAYS", _local_schedule(due_date - timedelta(days=3))),
            ("TICKET_DUE_1_DAY", _local_schedule(due_date - timedelta(days=1))),
            ("TICKET_OVERDUE", _local_schedule(due_date + timedelta(days=1))),
        )
        return tuple(
            (event_type, scheduled_at)
            for event_type, scheduled_at in slots
            if event_type == "TICKET_OVERDUE" or scheduled_at > now
        )

    def _event(
        self,
        ticket: BorrowTicket,
        borrower_id: UUID,
        borrower_email: str,
        event_type: str,
        scheduled_at: datetime,
        now: datetime,
    ) -> OutboxEvent:
        effective_schedule = max(scheduled_at, now)
        label = {
            "TICKET_DUE_3_DAYS": "due in three days",
            "TICKET_DUE_1_DAY": "due tomorrow",
            "TICKET_OVERDUE": "overdue",
        }[event_type]
        return OutboxEvent(
            id=self._id_factory(),
            event_type=event_type,
            aggregate_type="BORROW_TICKET",
            aggregate_id=ticket.id,
            payload={
                "ticketId": str(ticket.id),
                "dueDate": ticket.due_date.isoformat() if ticket.due_date else None,
                "reminderKind": event_type,
                "reference": {"type": "TICKET", "id": str(ticket.id)},
                "action": "MARK_RETURNED",
                "emails": [
                    {
                        "recipientId": str(borrower_id),
                        "address": borrower_email,
                        "subject": "Borrow Ticket return reminder",
                        "body": f"Ticket {ticket.id} is {label}.",
                    }
                ],
            },
            scheduled_at=effective_schedule,
            created_at=now,
        )


def _local_schedule(value: date) -> datetime:
    """Convert 09:00 Asia/Ho_Chi_Minh on a business date to UTC."""

    return datetime.combine(value, REMINDER_LOCAL_TIME, REMINDER_TIMEZONE).astimezone(UTC)
