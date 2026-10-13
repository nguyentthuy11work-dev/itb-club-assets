"""Unit tests for F07 Event configuration and reminder policy."""

from __future__ import annotations

import unittest
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from types import TracebackType
from typing import cast
from uuid import UUID

from ams.application.event_days import (
    ConfigureEventDayCommand,
    ConfigureShiftAssetCommand,
    ConfigureShiftCommand,
    EventDayNotFoundError,
    EventDayService,
)
from ams.application.event_reminders import EventReminderError, EventReminderScheduler
from ams.application.idempotency import (
    IdempotentCommandResult,
    IdempotentOperation,
    IIdempotentCommandExecutor,
)
from ams.application.notifications import CurrentStateReminderEligibilityPolicy
from ams.application.reliability import ClaimedOutboxEvent, IClock, OutboxEvent
from ams.application.unit_of_work import IUnitOfWork
from ams.domain.authentication import UserIdentity
from ams.domain.events import (
    EventConfigurationError,
    EventDay,
    EventReferenceError,
    EventScheduleConflictError,
    Shift,
    ShiftAsset,
    ShiftStatus,
)
from ams.domain.identity import AuthorizationError, CurrentActor, OperationalRole

NOW = datetime(2026, 10, 6, 1, 0, tzinfo=UTC)
EVENT_ID = UUID("00000000-0000-4000-8000-000000007001")
SHIFT_ONE_ID = UUID("00000000-0000-4000-8000-000000007002")
SHIFT_TWO_ID = UUID("00000000-0000-4000-8000-000000007003")
TEAM_LEAD_ID = UUID("00000000-0000-4000-8000-000000007004")
SHIFT_LEAD_ID = UUID("00000000-0000-4000-8000-000000007005")
ASSET_ID = UUID("00000000-0000-4000-8000-000000007006")


class FixedClock(IClock):
    """Return one deterministic UTC instant."""

    def now(self) -> datetime:
        """Return the configured instant."""

        return NOW


class FakeEventRepository:
    """Keep one Event aggregate and reference availability in memory."""

    def __init__(self, *, assets_exist: bool = True) -> None:
        self.event: EventDay | None = None
        self.assets_available = assets_exist

    async def add(self, event_day: EventDay, *, created_at: datetime) -> None:
        self.event = event_day

    async def get(self, event_day_id: UUID) -> EventDay | None:
        return self.event if self.event and self.event.id == event_day_id else None

    async def get_for_update(self, event_day_id: UUID) -> EventDay | None:
        return await self.get(event_day_id)

    async def save(self, event_day: EventDay, *, updated_at: datetime) -> None:
        self.event = event_day

    async def assets_exist(self, asset_ids: tuple[UUID, ...]) -> bool:
        return self.assets_available and asset_ids == (ASSET_ID,)

    async def get_shift_for_update(self, shift_id: UUID) -> Shift | None:
        if self.event is None:
            return None
        return next((shift for shift in self.event.shifts if shift.id == shift_id), None)


class FakeReminderRepository:
    """Record current reminder slots and cancellations."""

    def __init__(self) -> None:
        self.rows: dict[UUID, tuple[OutboxEvent, ...]] = {}
        self.cancelled: list[UUID] = []

    async def replace_unattempted(
        self, shift_id: UUID, reminders: tuple[OutboxEvent, ...]
    ) -> None:
        self.rows[shift_id] = reminders

    async def cancel_unattempted(self, shift_id: UUID) -> None:
        self.rows.pop(shift_id, None)
        self.cancelled.append(shift_id)


class FakeUsers:
    """Return configured current identities."""

    def __init__(self, identities: dict[UUID, UserIdentity]) -> None:
        self.identities = identities

    async def get_identity(self, user_id: UUID) -> UserIdentity | None:
        return self.identities.get(user_id)


class FakeUnitOfWork:
    """Expose only Event service ports with no-op transaction behavior."""

    def __init__(self, repository: FakeEventRepository, users: FakeUsers) -> None:
        self.event_days = repository
        self.event_reminders = FakeReminderRepository()
        self.users = users

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
    """Execute operations immediately against one fake transaction."""

    def __init__(self, unit_of_work: FakeUnitOfWork) -> None:
        self.unit_of_work = unit_of_work
        self.calls: list[tuple[UUID, str, str, str]] = []

    async def execute(
        self,
        *,
        actor_id: UUID,
        command_name: str,
        idempotency_key: str,
        request_fingerprint: str,
        operation: IdempotentOperation,
    ) -> IdempotentCommandResult:
        self.calls.append((actor_id, command_name, idempotency_key, request_fingerprint))
        response = await operation(cast(IUnitOfWork, self.unit_of_work))
        return IdempotentCommandResult(response.status_code, response.body, False)


class EventDomainTests(unittest.TestCase):
    """Protect schedule, ordering, quota, and safe-edit invariants."""

    def test_valid_schedule_exposes_expected_open_quantity(self) -> None:
        event = _event()
        self.assertEqual(event.shifts[0].assets[0].expected_open_quantity, 2)
        self.assertEqual(event.status.value, "DRAFT")

    def test_invalid_shift_and_schedule_inputs_are_rejected(self) -> None:
        invalid_factories: tuple[Callable[[], object], ...] = (
            lambda: ShiftAsset(UUID(int=91), ASSET_ID, -1),
            lambda: _shift(sequence=0),
            lambda: _shift(name=" "),
            lambda: _shift(starts_at=NOW.replace(tzinfo=None)),
            lambda: _shift(
                ends_at=NOW + timedelta(hours=1), starts_at=NOW + timedelta(hours=2)
            ),
            lambda: _shift(assets=()),
            lambda: _shift(
                assets=(
                    ShiftAsset(UUID(int=92), ASSET_ID, 1),
                    ShiftAsset(UUID(int=93), ASSET_ID, 2),
                )
            ),
            lambda: EventDay(EVENT_ID, " ", date(2026, 10, 7), TEAM_LEAD_ID, (_shift(),)),
            lambda: EventDay(EVENT_ID, "Event", date(2026, 10, 7), TEAM_LEAD_ID, ()),
            lambda: EventDay(
                EVENT_ID,
                "Event",
                date(2026, 10, 7),
                TEAM_LEAD_ID,
                (_shift(sequence=2),),
            ),
        )
        for factory in invalid_factories:
            with self.subTest(factory=factory), self.assertRaises(EventConfigurationError):
                factory()

        with self.assertRaises(EventScheduleConflictError):
            EventDay(
                EVENT_ID,
                "Event",
                date(2026, 10, 7),
                TEAM_LEAD_ID,
                (
                    _shift(),
                    _shift(id=SHIFT_TWO_ID, sequence=2, starts_at=NOW + timedelta(hours=2)),
                ),
            )
        with self.assertRaises(EventConfigurationError):
            EventDay(
                EVENT_ID,
                "Event",
                date(2026, 10, 7),
                TEAM_LEAD_ID,
                (
                    _shift(),
                    _shift(
                        id=SHIFT_ONE_ID,
                        sequence=2,
                        starts_at=NOW + timedelta(hours=3),
                        ends_at=NOW + timedelta(hours=4),
                    ),
                ),
            )

    def test_started_or_non_draft_event_cannot_be_reconfigured(self) -> None:
        event = _event()
        event.shifts = (_shift(starts_at=NOW),)
        with self.assertRaises(EventScheduleConflictError):
            event.replace_configuration(
                name="Changed",
                event_date=event.event_date,
                shifts=event.shifts,
                now=NOW,
            )


class EventReminderSchedulerTests(unittest.IsolatedAsyncioTestCase):
    """Verify approved 30-minute slots, recipient, and cancellation behavior."""

    async def test_schedules_open_and_close_slots_for_active_shift_lead(self) -> None:
        unit_of_work = _unit_of_work()
        scheduler = EventReminderScheduler(FixedClock(), id_factory=_ids(200))
        shift = _shift()
        await scheduler.synchronize(cast(IUnitOfWork, unit_of_work), shift)
        reminders = unit_of_work.event_reminders.rows[shift.id]
        self.assertEqual(
            [(event.event_type, event.scheduled_at) for event in reminders],
            [
                ("SHIFT_OPEN_REMINDER", shift.starts_at - timedelta(minutes=30)),
                ("SHIFT_CLOSE_REMINDER", shift.ends_at - timedelta(minutes=30)),
            ],
        )
        self.assertEqual(reminders[0].payload["action"], "OPEN_SHIFT")

    async def test_non_scheduled_shift_cancels_and_invalid_lead_is_rejected(self) -> None:
        unit_of_work = _unit_of_work()
        shift = _shift(status=ShiftStatus.CLOSED)
        await EventReminderScheduler(FixedClock()).synchronize(
            cast(IUnitOfWork, unit_of_work), shift
        )
        self.assertEqual(unit_of_work.event_reminders.cancelled, [shift.id])

        unit_of_work = FakeUnitOfWork(FakeEventRepository(), FakeUsers({}))
        with self.assertRaises(EventReminderError):
            await EventReminderScheduler(FixedClock()).synchronize(
                cast(IUnitOfWork, unit_of_work), _shift()
            )

    async def test_worker_policy_rechecks_current_shift_state(self) -> None:
        unit_of_work = _unit_of_work()
        unit_of_work.event_days.event = _event()
        policy = CurrentStateReminderEligibilityPolicy(lambda: cast(IUnitOfWork, unit_of_work))
        self.assertFalse(await policy.is_eligible(_claimed_event(aggregate_type="EVENT_DAY")))
        self.assertTrue(await policy.is_eligible(_claimed_event()))
        self.assertTrue(
            await policy.is_eligible(_claimed_event(event_type="SHIFT_CLOSE_REMINDER"))
        )
        unit_of_work.event_days.event = None
        self.assertFalse(await policy.is_eligible(_claimed_event()))


class EventDayServiceTests(unittest.IsolatedAsyncioTestCase):
    """Verify authorization, references, creation, reads, and safe replacement."""

    async def test_create_read_and_update_configuration(self) -> None:
        unit_of_work = _unit_of_work()
        service = _service(unit_of_work)
        actor = _actor(OperationalRole.TEAM_LEAD_ASSET)
        created = await service.create(_command(), actor, "create-key")
        event_id = created.event_day.id
        self.assertEqual(len(created.event_day.shifts), 1)
        self.assertEqual(len(unit_of_work.event_reminders.rows), 1)

        read = await service.get(event_id, _actor(OperationalRole.HR))
        self.assertEqual(read.event_day.id, event_id)

        shift_id = created.event_day.shifts[0].id
        updated_command = _command(
            name="Updated Event",
            shift_id=shift_id,
            starts_at=NOW + timedelta(hours=5),
        )
        updated = await service.update(event_id, updated_command, actor, "update-key")
        self.assertEqual(updated.event_day.name, "Updated Event")
        self.assertEqual(updated.event_day.shifts[0].id, shift_id)

    async def test_authorization_references_and_missing_event_are_rejected(self) -> None:
        unit_of_work = _unit_of_work()
        service = _service(unit_of_work)
        with self.assertRaises(AuthorizationError):
            await service.create(_command(), _actor(), "denied")
        with self.assertRaises(AuthorizationError):
            await service.get(EVENT_ID, _actor())
        with self.assertRaises(EventDayNotFoundError):
            await service.get(EVENT_ID, _actor(OperationalRole.HR))

        unavailable = _unit_of_work(assets_exist=False)
        with self.assertRaises(EventReferenceError):
            await _service(unavailable).create(
                _command(), _actor(OperationalRole.TEAM_LEAD_ASSET), "bad-asset"
            )

    async def test_past_schedule_and_foreign_update_shift_id_are_rejected(self) -> None:
        unit_of_work = _unit_of_work()
        service = _service(unit_of_work)
        actor = _actor(OperationalRole.TEAM_LEAD_ASSET)
        with self.assertRaises(EventScheduleConflictError):
            await service.create(_command(starts_at=NOW), actor, "past")
        created = await service.create(_command(), actor, "create")
        with self.assertRaises(EventConfigurationError):
            await service.update(
                created.event_day.id,
                _command(shift_id=UUID(int=999)),
                actor,
                "foreign-shift",
            )
        with self.assertRaises(EventDayNotFoundError):
            await _service(_unit_of_work()).update(EVENT_ID, _command(), actor, "missing-event")
        with self.assertRaises(EventConfigurationError):
            await _service(_unit_of_work()).create(
                _command(shift_id=SHIFT_ONE_ID), actor, "create-with-id"
            )

    async def test_removed_shift_is_cancelled_and_invalid_lead_is_rejected(self) -> None:
        unit_of_work = _unit_of_work()
        service = _service(unit_of_work)
        actor = _actor(OperationalRole.TEAM_LEAD_ASSET)
        created = await service.create(_two_shift_command(), actor, "two-shifts")
        retained_id = created.event_day.shifts[0].id
        removed_id = created.event_day.shifts[1].id
        await service.update(
            created.event_day.id,
            _command(shift_id=retained_id),
            actor,
            "remove-shift",
        )
        self.assertIn(removed_id, unit_of_work.event_reminders.cancelled)

        unit_of_work.users.identities.clear()
        with self.assertRaises(EventReferenceError):
            await service.create(_command(), actor, "invalid-lead")


def _shift(
    *,
    id: UUID = SHIFT_ONE_ID,
    sequence: int = 1,
    name: str = "Morning",
    starts_at: datetime = NOW + timedelta(hours=2),
    ends_at: datetime = NOW + timedelta(hours=3),
    assets: tuple[ShiftAsset, ...] | None = None,
    status: ShiftStatus = ShiftStatus.SCHEDULED,
) -> Shift:
    return Shift(
        id=id,
        sequence=sequence,
        name=name,
        starts_at=starts_at,
        ends_at=ends_at,
        shift_lead_user_id=SHIFT_LEAD_ID,
        assets=assets if assets is not None else (ShiftAsset(UUID(int=90), ASSET_ID, 2),),
        status=status,
    )


def _event() -> EventDay:
    return EventDay(EVENT_ID, "Event", date(2026, 10, 7), TEAM_LEAD_ID, (_shift(),))


def _identity() -> UserIdentity:
    return UserIdentity(
        id=SHIFT_LEAD_ID,
        username="shift.lead",
        email="shift.lead@example.test",
        display_name="Shift Lead",
        roles=frozenset({OperationalRole.SHIFT_LEAD}),
        team_ids=frozenset(),
        is_active=True,
    )


def _unit_of_work(*, assets_exist: bool = True) -> FakeUnitOfWork:
    return FakeUnitOfWork(
        FakeEventRepository(assets_exist=assets_exist), FakeUsers({SHIFT_LEAD_ID: _identity()})
    )


def _service(unit_of_work: FakeUnitOfWork) -> EventDayService:
    return EventDayService(
        FakeExecutor(unit_of_work),
        lambda: cast(IUnitOfWork, unit_of_work),
        FixedClock(),
        EventReminderScheduler(FixedClock(), id_factory=_ids(500)),
        id_factory=_ids(1),
    )


def _actor(*roles: OperationalRole) -> CurrentActor:
    return CurrentActor(TEAM_LEAD_ID, frozenset(roles))


def _command(
    *,
    name: str = "Event",
    shift_id: UUID | None = None,
    starts_at: datetime = NOW + timedelta(hours=2),
) -> ConfigureEventDayCommand:
    return ConfigureEventDayCommand(
        name=name,
        event_date=date(2026, 10, 7),
        shifts=(
            ConfigureShiftCommand(
                id=shift_id,
                sequence=1,
                name="Morning",
                starts_at=starts_at,
                ends_at=starts_at + timedelta(hours=1),
                shift_lead_user_id=SHIFT_LEAD_ID,
                assets=(ConfigureShiftAssetCommand(ASSET_ID, 2),),
            ),
        ),
    )


def _two_shift_command() -> ConfigureEventDayCommand:
    first = _command().shifts[0]
    second_start = NOW + timedelta(hours=4)
    return ConfigureEventDayCommand(
        name="Event",
        event_date=date(2026, 10, 7),
        shifts=(
            first,
            ConfigureShiftCommand(
                sequence=2,
                name="Afternoon",
                starts_at=second_start,
                ends_at=second_start + timedelta(hours=1),
                shift_lead_user_id=SHIFT_LEAD_ID,
                assets=(ConfigureShiftAssetCommand(ASSET_ID, 1),),
            ),
        ),
    )


def _claimed_event(
    *,
    event_type: str = "SHIFT_OPEN_REMINDER",
    aggregate_type: str = "SHIFT",
) -> ClaimedOutboxEvent:
    return ClaimedOutboxEvent(
        id=UUID(int=800),
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=SHIFT_ONE_ID,
        payload={},
        attempt_count=1,
        max_attempts=8,
        created_at=NOW,
    )


def _ids(start: int) -> Callable[[], UUID]:
    values = iter(UUID(int=value) for value in range(start, start + 200))
    return lambda: next(values)
