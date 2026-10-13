"""Unit proofs for F08 Shift execution, carry-over, summary, and notifications."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import cast
from uuid import UUID

from ams.application.idempotency import (
    IdempotentCommandResult,
    IdempotentOperation,
    IIdempotentCommandExecutor,
)
from ams.application.reliability import IClock, OutboxEvent
from ams.application.shift_execution import ShiftExecutionService, ShiftQuantityCommand
from ams.application.unit_of_work import IUnitOfWork
from ams.domain.authentication import UserIdentity
from ams.domain.events import EventDayStatus, ShiftStatus
from ams.domain.identity import CurrentActor, OperationalRole
from ams.domain.shift_execution import (
    EventDayExecutionSummary,
    InvalidShiftTransitionError,
    ShiftActionForbiddenError,
    ShiftEvent,
    ShiftExecution,
    ShiftExecutionError,
    ShiftExecutionItem,
    ShiftNotFoundError,
    ShiftQuantityError,
    ShiftScheduleUnavailableError,
)

NOW = datetime(2030, 1, 2, 4, 30, tzinfo=UTC)
EVENT_ID = UUID("00000000-0000-4000-8000-000000008001")
FIRST_ID = UUID("00000000-0000-4000-8000-000000008002")
SECOND_ID = UUID("00000000-0000-4000-8000-000000008003")
LEAD_ID = UUID("00000000-0000-4000-8000-000000008004")
TEAM_LEAD_ID = UUID("00000000-0000-4000-8000-000000008005")
ASSET_ID = UUID("00000000-0000-4000-8000-000000008006")


class FixedClock(IClock):
    """Return the deterministic in-window execution instant."""

    def now(self) -> datetime:
        """Return the configured UTC instant."""

        return NOW


class FakeShiftRepository:
    """Store mutable Shift aggregates and audit events in memory."""

    def __init__(self, shifts: tuple[ShiftExecution, ...]) -> None:
        self.shifts = {shift.id: shift for shift in shifts}
        self.events: list[ShiftEvent] = []
        self.event_status = EventDayStatus.DRAFT
        self.summary: EventDayExecutionSummary | None = EventDayExecutionSummary(
            EVENT_ID, EventDayStatus.IN_PROGRESS, len(shifts), 0, None
        )

    async def list_for_assignee(
        self, actor_id: UUID, *, offset: int, limit: int
    ) -> tuple[tuple[ShiftExecution, ...], int]:
        values = tuple(
            shift for shift in self.shifts.values() if shift.shift_lead_user_id == actor_id
        )
        return values[offset : offset + limit], len(values)

    async def get(self, shift_id: UUID) -> ShiftExecution | None:
        return self.shifts.get(shift_id)

    async def get_for_update(self, shift_id: UUID) -> ShiftExecution | None:
        return self.shifts.get(shift_id)

    async def get_predecessor_for_update(
        self, event_day_id: UUID, sequence: int
    ) -> ShiftExecution | None:
        return next(
            (
                shift
                for shift in self.shifts.values()
                if shift.event_day_id == event_day_id and shift.sequence == sequence - 1
            ),
            None,
        )

    async def save_transition(
        self,
        shift: ShiftExecution,
        event: ShiftEvent,
        *,
        event_day_status: EventDayStatus,
    ) -> None:
        self.shifts[shift.id] = shift
        self.events.append(event)
        self.event_status = event_day_status

    async def all_other_shifts_closed(self, event_day_id: UUID, shift_id: UUID) -> bool:
        return all(
            shift.status is ShiftStatus.CLOSED
            for shift in self.shifts.values()
            if shift.event_day_id == event_day_id and shift.id != shift_id
        )

    async def get_event_day_summary(
        self, event_day_id: UUID
    ) -> EventDayExecutionSummary | None:
        return self.summary if event_day_id == EVENT_ID else None


class FakeUsers:
    """Expose the Event creator identity."""

    def __init__(self, available: bool = True) -> None:
        self.available = available

    async def get_identity(self, user_id: UUID) -> UserIdentity | None:
        if not self.available or user_id != TEAM_LEAD_ID:
            return None
        return UserIdentity(
            TEAM_LEAD_ID,
            "team.lead",
            "team.lead@example.test",
            "Team Lead",
            frozenset({OperationalRole.TEAM_LEAD_ASSET}),
            frozenset(),
            True,
        )


class FakeOutbox:
    """Collect staged deviation notifications."""

    def __init__(self) -> None:
        self.events: list[OutboxEvent] = []

    async def add(self, event: OutboxEvent) -> None:
        self.events.append(event)


class FakeUnitOfWork:
    """Expose only F08 ports with no-op transaction behavior."""

    def __init__(self, repository: FakeShiftRepository) -> None:
        self.shifts = repository
        self.users = FakeUsers()
        self.outbox = FakeOutbox()

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class FakeExecutor(IIdempotentCommandExecutor):
    """Execute each operation and optionally mark its result replayed."""

    def __init__(self, unit_of_work: FakeUnitOfWork) -> None:
        self.unit_of_work = unit_of_work
        self.replayed = False
        self.calls: list[tuple[str, str]] = []

    async def execute(
        self,
        *,
        actor_id: UUID,
        command_name: str,
        idempotency_key: str,
        request_fingerprint: str,
        operation: IdempotentOperation,
    ) -> IdempotentCommandResult:
        self.calls.append((command_name, idempotency_key))
        response = await operation(cast(IUnitOfWork, self.unit_of_work))
        return IdempotentCommandResult(response.status_code, response.body, self.replayed)


class ShiftExecutionDomainTests(unittest.TestCase):
    """Protect assignment, schedule, state, quantity, and formula rules."""

    def test_first_shift_open_confirm_and_close_calculates_consumption(self) -> None:
        shift = _shift()
        shift.open(actor_id=LEAD_ID, now=NOW, predecessor=None)
        self.assertEqual(shift.status, ShiftStatus.OPENING)
        shift.confirm_received(actor_id=LEAD_ID, quantities={ASSET_ID: 3})
        self.assertEqual(shift.status, ShiftStatus.ACTIVE)
        shift.close(actor_id=LEAD_ID, quantities={ASSET_ID: 1})
        self.assertEqual(shift.items[0].consumed_quantity, 2)
        self.assertEqual(shift.status, ShiftStatus.CLOSED)

    def test_next_shift_requires_closed_predecessor_and_applies_carry_over(self) -> None:
        previous = _shift()
        current = _shift(id=SECOND_ID, sequence=2)
        with self.assertRaises(ShiftScheduleUnavailableError):
            current.open(actor_id=LEAD_ID, now=NOW, predecessor=previous)
        previous.status = ShiftStatus.CLOSED
        previous.items[0].closing_actual_quantity = 2
        previous.items[0].consumed_quantity = 1
        current.open(actor_id=LEAD_ID, now=NOW, predecessor=previous)
        self.assertEqual(current.items[0].carry_over_quantity, 2)
        self.assertEqual(current.items[0].expected_open_quantity, 5)

    def test_three_shift_chain_uses_each_predecessors_actual_close(self) -> None:
        first = _shift()
        first.open(actor_id=LEAD_ID, now=NOW, predecessor=None)
        first.confirm_received(actor_id=LEAD_ID, quantities={ASSET_ID: 3})
        first.close(actor_id=LEAD_ID, quantities={ASSET_ID: 2})

        second = _shift(id=SECOND_ID, sequence=2)
        second.open(actor_id=LEAD_ID, now=NOW, predecessor=first)
        second.confirm_received(actor_id=LEAD_ID, quantities={ASSET_ID: 5})
        second.close(actor_id=LEAD_ID, quantities={ASSET_ID: 1})

        third = _shift(id=UUID(int=83), sequence=3)
        third.open(actor_id=LEAD_ID, now=NOW, predecessor=second)

        self.assertEqual(second.items[0].carry_over_quantity, 2)
        self.assertEqual(third.items[0].carry_over_quantity, 1)
        self.assertEqual(third.items[0].expected_open_quantity, 4)

    def test_schedule_assignment_and_predecessor_shape_are_enforced(self) -> None:
        with self.assertRaises(ShiftActionForbiddenError):
            _shift().open(actor_id=UUID(int=99), now=NOW, predecessor=None)
        with self.assertRaises(ShiftScheduleUnavailableError):
            _shift().open(actor_id=LEAD_ID, now=NOW - timedelta(hours=1), predecessor=None)
        with self.assertRaises(ShiftScheduleUnavailableError):
            _shift().open(actor_id=LEAD_ID, now=NOW + timedelta(hours=1), predecessor=None)
        with self.assertRaises(ShiftScheduleUnavailableError):
            _shift(id=SECOND_ID, sequence=2).open(actor_id=LEAD_ID, now=NOW, predecessor=None)
        with self.assertRaises(ShiftExecutionError):
            _shift().open(
                actor_id=LEAD_ID,
                now=NOW,
                predecessor=_shift(id=SECOND_ID, sequence=2),
            )

    def test_opening_deviation_requires_mismatch_and_reason(self) -> None:
        shift = _opening_shift()
        with self.assertRaises(ShiftQuantityError):
            shift.confirm_received(actor_id=LEAD_ID, quantities={ASSET_ID: 2})
        with self.assertRaises(ShiftQuantityError):
            shift.report_opening_deviation(
                actor_id=LEAD_ID, quantities={ASSET_ID: 2}, reason=" "
            )
        with self.assertRaises(ShiftQuantityError):
            shift.report_opening_deviation(
                actor_id=LEAD_ID, quantities={ASSET_ID: 3}, reason="No mismatch"
            )
        shift.report_opening_deviation(
            actor_id=LEAD_ID, quantities={ASSET_ID: 2}, reason="One missing"
        )
        self.assertEqual(shift.status, ShiftStatus.ACTIVE_WITH_DEVIATION)
        self.assertEqual(shift.items[0].discrepancy_reason, "One missing")

    def test_bad_state_item_sets_and_persisted_values_are_rejected(self) -> None:
        with self.assertRaises(InvalidShiftTransitionError):
            _shift().close(actor_id=LEAD_ID, quantities={ASSET_ID: 0})
        with self.assertRaises(InvalidShiftTransitionError):
            _shift().confirm_received(actor_id=LEAD_ID, quantities={ASSET_ID: 3})
        with self.assertRaises(ShiftQuantityError):
            _opening_shift().confirm_received(actor_id=LEAD_ID, quantities={})
        with self.assertRaises(ShiftQuantityError):
            _opening_shift().confirm_received(actor_id=LEAD_ID, quantities={ASSET_ID: -1})
        with self.assertRaises(ShiftQuantityError):
            ShiftExecutionItem(UUID(int=1), ASSET_ID, -1)
        with self.assertRaises(ShiftQuantityError):
            ShiftExecutionItem(UUID(int=1), ASSET_ID, 3, consumed_quantity=1)
        with self.assertRaises(ShiftQuantityError):
            ShiftExecutionItem(
                UUID(int=1),
                ASSET_ID,
                3,
                closing_actual_quantity=1,
                consumed_quantity=1,
            )
        with self.assertRaises(ShiftQuantityError):
            ShiftExecutionItem(UUID(int=1), ASSET_ID, 3, discrepancy_reason=" ")
        with self.assertRaises(ShiftExecutionError):
            _shift(sequence=0)
        with self.assertRaises(ShiftQuantityError):
            ShiftExecution(
                FIRST_ID,
                EVENT_ID,
                "Event",
                TEAM_LEAD_ID,
                1,
                "Shift",
                NOW,
                NOW + timedelta(hours=1),
                LEAD_ID,
                (_item(), _item()),
            )
        with self.assertRaises(InvalidShiftTransitionError):
            _opening_shift().open(actor_id=LEAD_ID, now=NOW, predecessor=None)
        with self.assertRaises(ShiftExecutionError):
            _shift().open(actor_id=LEAD_ID, now=NOW.replace(tzinfo=None), predecessor=None)


class ShiftExecutionServiceTests(unittest.IsolatedAsyncioTestCase):
    """Verify application authorization, audit, notification, and summary orchestration."""

    async def test_list_get_open_confirm_and_close(self) -> None:
        unit_of_work = FakeUnitOfWork(FakeShiftRepository((_shift(),)))
        service, executor = _service(unit_of_work)
        actor = _actor(OperationalRole.SHIFT_LEAD)
        page = await service.list(actor)
        self.assertEqual(page.total, 1)
        self.assertEqual((await service.get(FIRST_ID, actor)).shift.id, FIRST_ID)

        opened = await service.open(FIRST_ID, actor, "open")
        self.assertEqual(opened.shift.status, ShiftStatus.OPENING)
        confirmed = await service.confirm_received(FIRST_ID, (_quantity(3),), actor, "confirm")
        self.assertEqual(confirmed.shift.status, ShiftStatus.ACTIVE)
        executor.replayed = True
        closed = await service.close(FIRST_ID, (_quantity(1),), actor, "close")
        self.assertTrue(closed.replayed)
        self.assertEqual(closed.shift.items[0].consumed_quantity, 2)
        self.assertEqual(unit_of_work.shifts.event_status, EventDayStatus.COMPLETED)
        self.assertEqual(len(unit_of_work.shifts.events), 3)

    async def test_deviation_stages_creator_email_and_complete_metadata(self) -> None:
        shift = _opening_shift()
        unit_of_work = FakeUnitOfWork(FakeShiftRepository((shift,)))
        service, _ = _service(unit_of_work)
        result = await service.report_opening_deviation(
            FIRST_ID,
            (_quantity(2),),
            "One item missing",
            _actor(OperationalRole.SHIFT_LEAD),
            "deviation",
        )
        self.assertEqual(result.shift.status, ShiftStatus.ACTIVE_WITH_DEVIATION)
        self.assertEqual(len(unit_of_work.outbox.events), 1)
        self.assertEqual(unit_of_work.outbox.events[0].payload["reason"], "One item missing")
        self.assertEqual(
            unit_of_work.shifts.events[0].event_type,
            "SHIFT_OPENING_DEVIATION_REPORTED",
        )

    async def test_access_missing_duplicate_and_creator_failures_are_rejected(self) -> None:
        unit_of_work = FakeUnitOfWork(FakeShiftRepository((_shift(),)))
        service, _ = _service(unit_of_work)
        with self.assertRaises(ShiftActionForbiddenError):
            await service.list(_actor())
        with self.assertRaises(ShiftActionForbiddenError):
            await service.get(
                FIRST_ID, CurrentActor(UUID(int=99), frozenset({OperationalRole.SHIFT_LEAD}))
            )
        with self.assertRaises(ShiftNotFoundError):
            await service.get(UUID(int=99), _actor(OperationalRole.SHIFT_LEAD))
        with self.assertRaises(ShiftQuantityError):
            await service.confirm_received(
                FIRST_ID,
                (_quantity(3), _quantity(3)),
                _actor(OperationalRole.SHIFT_LEAD),
                "duplicate",
            )

        unit_of_work.shifts.shifts[FIRST_ID] = _opening_shift()
        unit_of_work.users.available = False
        with self.assertRaises(ShiftNotFoundError):
            await service.report_opening_deviation(
                FIRST_ID,
                (_quantity(2),),
                "Missing",
                _actor(OperationalRole.SHIFT_LEAD),
                "missing-creator",
            )

    async def test_summary_authorization_progress_and_missing_event(self) -> None:
        repository = FakeShiftRepository((_shift(),))
        unit_of_work = FakeUnitOfWork(repository)
        service, _ = _service(unit_of_work)
        summary = await service.get_summary(EVENT_ID, _actor(OperationalRole.HR))
        self.assertFalse(summary.complete)
        with self.assertRaises(ShiftActionForbiddenError):
            await service.get_summary(EVENT_ID, _actor())
        repository.summary = None
        with self.assertRaises(ShiftNotFoundError):
            await service.get_summary(EVENT_ID, _actor(OperationalRole.TEAM_LEAD_ASSET))


def _item(*, quota: int = 3) -> ShiftExecutionItem:
    return ShiftExecutionItem(UUID(int=81), ASSET_ID, quota)


def _shift(
    *,
    id: UUID = FIRST_ID,
    sequence: int = 1,
    status: ShiftStatus = ShiftStatus.SCHEDULED,
) -> ShiftExecution:
    return ShiftExecution(
        id=id,
        event_day_id=EVENT_ID,
        event_day_name="Event",
        event_day_creator_user_id=TEAM_LEAD_ID,
        sequence=sequence,
        name=f"Shift {sequence}",
        starts_at=NOW - timedelta(minutes=30),
        ends_at=NOW + timedelta(minutes=30),
        shift_lead_user_id=LEAD_ID,
        items=(_item(),),
        status=status,
    )


def _opening_shift() -> ShiftExecution:
    return _shift(status=ShiftStatus.OPENING)


def _quantity(value: int) -> ShiftQuantityCommand:
    return ShiftQuantityCommand(ASSET_ID, value)


def _actor(*roles: OperationalRole) -> CurrentActor:
    return CurrentActor(LEAD_ID, frozenset(roles))


def _service(
    unit_of_work: FakeUnitOfWork,
) -> tuple[ShiftExecutionService, FakeExecutor]:
    executor = FakeExecutor(unit_of_work)
    ids = iter(UUID(int=value) for value in range(900, 1000))
    return (
        ShiftExecutionService(
            executor,
            lambda: cast(IUnitOfWork, unit_of_work),
            FixedClock(),
            id_factory=lambda: next(ids),
        ),
        executor,
    )
