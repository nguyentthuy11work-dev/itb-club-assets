"""Time-controlled unit tests for temporary-Ticket reminder scheduling."""

from __future__ import annotations

import unittest
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from typing import cast
from uuid import UUID

from ams.application.reliability import IClock, OutboxEvent
from ams.application.ticket_reminders import (
    TicketReminderError,
    TicketReminderScheduler,
)
from ams.application.unit_of_work import IUnitOfWork
from ams.domain.authentication import UserIdentity
from ams.domain.tickets import BorrowTicket, TicketItem, TicketMethod, TicketStatus

BORROWER_ID = UUID("00000000-0000-4000-8000-000000005601")
CUSTODIAN_ID = UUID("00000000-0000-4000-8000-000000005602")
ASSET_ID = UUID("00000000-0000-4000-8000-000000005603")
CUSTODY_ID = UUID("00000000-0000-4000-8000-000000005604")
TICKET_ID = UUID("00000000-0000-4000-8000-000000005605")


class FixedClock(IClock):
    """Return one configured UTC scheduler instant."""

    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        """Return the configured instant."""

        return self.value


class FakeReminderRepository:
    """Model replace semantics as one current tuple per Ticket."""

    def __init__(self) -> None:
        self.rows: dict[UUID, tuple[OutboxEvent, ...]] = {}
        self.cancelled: list[UUID] = []

    async def replace_unattempted(
        self, ticket_id: UUID, reminders: tuple[OutboxEvent, ...]
    ) -> None:
        self.rows[ticket_id] = reminders

    async def cancel_unattempted(self, ticket_id: UUID) -> None:
        self.rows.pop(ticket_id, None)
        self.cancelled.append(ticket_id)


class FakeUsers:
    """Return one configurable borrower identity."""

    def __init__(self, user: UserIdentity | None) -> None:
        self.user = user

    async def get_identity(self, user_id: UUID) -> UserIdentity | None:
        del user_id
        return self.user


class FakeUnitOfWork:
    """Provide only ports consumed by the reminder scheduler."""

    def __init__(self, user: UserIdentity | None) -> None:
        self.ticket_reminders = FakeReminderRepository()
        self.users = FakeUsers(user)


class TicketReminderSchedulerTests(unittest.IsolatedAsyncioTestCase):
    """Verify local-time slots, eligibility, replacement, and overdue behavior."""

    async def test_temporary_ticket_schedules_three_borrower_slots_without_duplicates(
        self,
    ) -> None:
        """Three-day, one-day, and overdue slots use 09:00 Vietnam time."""

        now = datetime(2026, 10, 5, 0, 0, tzinfo=UTC)
        unit_of_work = FakeUnitOfWork(_borrower())
        scheduler = TicketReminderScheduler(FixedClock(now), id_factory=_ids())
        ticket = _ticket(due_date=date(2026, 10, 10))

        await scheduler.synchronize(cast(IUnitOfWork, unit_of_work), ticket)
        await scheduler.synchronize(cast(IUnitOfWork, unit_of_work), ticket)

        reminders = unit_of_work.ticket_reminders.rows[TICKET_ID]
        self.assertEqual(len(reminders), 3)
        self.assertEqual(
            [(event.event_type, event.scheduled_at) for event in reminders],
            [
                ("TICKET_DUE_3_DAYS", datetime(2026, 10, 7, 2, 0, tzinfo=UTC)),
                ("TICKET_DUE_1_DAY", datetime(2026, 10, 9, 2, 0, tzinfo=UTC)),
                ("TICKET_OVERDUE", datetime(2026, 10, 11, 2, 0, tzinfo=UTC)),
            ],
        )
        for event in reminders:
            emails = event.payload["emails"]
            assert isinstance(emails, list)
            self.assertEqual(len(emails), 1)
            email = cast(Mapping[str, object], emails[0])
            self.assertEqual(email["recipientId"], str(BORROWER_ID))
            self.assertEqual(event.payload["action"], "MARK_RETURNED")
            self.assertEqual(
                event.payload["reference"],
                {"type": "TICKET", "id": str(TICKET_ID)},
            )

    async def test_transfer_and_terminal_ticket_cancel_unattempted_slots(self) -> None:
        """Transfer, rejected, cancelled, and closed Tickets never retain reminders."""

        now = datetime(2026, 10, 5, 0, 0, tzinfo=UTC)
        for ticket in (
            _ticket(method=TicketMethod.CUSTODY_TRANSFER, due_date=None),
            _ticket(status=TicketStatus.REJECTED),
            _ticket(status=TicketStatus.CANCELLED),
            _ticket(status=TicketStatus.CLOSED),
        ):
            unit_of_work = FakeUnitOfWork(_borrower())
            unit_of_work.ticket_reminders.rows[TICKET_ID] = ()
            await TicketReminderScheduler(FixedClock(now)).synchronize(
                cast(IUnitOfWork, unit_of_work), ticket
            )
            self.assertNotIn(TICKET_ID, unit_of_work.ticket_reminders.rows)
            self.assertEqual(unit_of_work.ticket_reminders.cancelled, [TICKET_ID])

    async def test_past_pre_due_slots_are_omitted_and_overdue_runs_immediately(self) -> None:
        """A late scheduler does not send stale pre-due reminders and catches up overdue now."""

        now = datetime(2026, 10, 12, 4, 0, tzinfo=UTC)
        unit_of_work = FakeUnitOfWork(_borrower())
        await TicketReminderScheduler(FixedClock(now), id_factory=_ids()).synchronize(
            cast(IUnitOfWork, unit_of_work), _ticket(due_date=date(2026, 10, 10))
        )
        reminders = unit_of_work.ticket_reminders.rows[TICKET_ID]
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0].event_type, "TICKET_OVERDUE")
        self.assertEqual(reminders[0].scheduled_at, now)

    async def test_missing_due_date_or_active_borrower_is_rejected(self) -> None:
        """Corrupt Ticket data or unavailable delivery identity rolls back scheduling."""

        now = datetime(2026, 10, 5, 0, 0, tzinfo=UTC)
        missing_due = _ticket()
        missing_due.due_date = None
        cases = (
            (FakeUnitOfWork(_borrower()), missing_due),
            (FakeUnitOfWork(None), _ticket()),
            (FakeUnitOfWork(_borrower(active=False)), _ticket()),
        )
        for unit_of_work, ticket in cases:
            with (
                self.subTest(user=unit_of_work.users.user),
                self.assertRaises(TicketReminderError),
            ):
                await TicketReminderScheduler(FixedClock(now)).synchronize(
                    cast(IUnitOfWork, unit_of_work), ticket
                )


def _ticket(
    *,
    method: TicketMethod = TicketMethod.TEMPORARY,
    status: TicketStatus = TicketStatus.PENDING,
    due_date: date | None = date(2026, 10, 10),
) -> BorrowTicket:
    return BorrowTicket(
        id=TICKET_ID,
        borrower_user_id=BORROWER_ID,
        source_custodian_user_id=CUSTODIAN_ID,
        method=method,
        status=status,
        due_date=due_date,
        items=(TicketItem(ASSET_ID, CUSTODY_ID, 1),),
    )


def _borrower(*, active: bool = True) -> UserIdentity:
    return UserIdentity(
        id=BORROWER_ID,
        username="borrower",
        email="borrower@example.test",
        display_name="Borrower",
        roles=frozenset(),
        team_ids=frozenset(),
        is_active=active,
    )


def _ids() -> Callable[[], UUID]:
    values = iter(UUID(int=value) for value in range(200, 300))
    return lambda: next(values)
