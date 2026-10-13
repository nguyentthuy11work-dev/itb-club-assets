"""Event-day configuration commands, authorization, and transaction orchestration."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID, uuid4

from ams.application.event_reminders import EventReminderScheduler
from ams.application.idempotency import (
    CommandResponse,
    IIdempotentCommandExecutor,
    canonical_json_fingerprint,
)
from ams.application.reliability import IClock
from ams.application.unit_of_work import IUnitOfWork
from ams.domain.events import (
    EventConfigurationError,
    EventDay,
    EventReferenceError,
    EventScheduleConflictError,
    Shift,
    ShiftAsset,
)
from ams.domain.identity import AuthorizationError, CurrentActor, OperationalRole


class EventDayNotFoundError(EventConfigurationError):
    """The requested Event Day is absent or unavailable to the operation."""

    code = "EVENT_DAY_NOT_FOUND"


class EventPersistenceError(Exception):
    """Sanitized Event persistence failure exposed to the application boundary."""

    code = "EVENT_PERSISTENCE_FAILED"


@dataclass(frozen=True, slots=True)
class ConfigureShiftAssetCommand:
    """One selected Asset and non-negative per-Shift quota."""

    asset_id: UUID
    quota_quantity: int


@dataclass(frozen=True, slots=True)
class ConfigureShiftCommand:
    """One complete Shift configuration; ID is required only for retained updates."""

    sequence: int
    name: str
    starts_at: datetime
    ends_at: datetime
    shift_lead_user_id: UUID
    assets: tuple[ConfigureShiftAssetCommand, ...]
    id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ConfigureEventDayCommand:
    """Complete Event Day configuration used by create and replacement commands."""

    name: str
    event_date: date
    shifts: tuple[ConfigureShiftCommand, ...]


@dataclass(frozen=True, slots=True)
class EventDayResult:
    """Application result containing the complete safe Event configuration."""

    event_day: EventDay


class EventDayService:
    """Create, replace, and read Event configuration through injected ports."""

    def __init__(
        self,
        executor: IIdempotentCommandExecutor,
        unit_of_work_factory: Callable[[], IUnitOfWork],
        clock: IClock,
        reminder_scheduler: EventReminderScheduler,
        *,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._executor = executor
        self._unit_of_work_factory = unit_of_work_factory
        self._clock = clock
        self._reminder_scheduler = reminder_scheduler
        self._id_factory = id_factory

    async def create(
        self,
        command: ConfigureEventDayCommand,
        actor: CurrentActor,
        idempotency_key: str,
    ) -> EventDayResult:
        """Create one future Event Day as an active Asset Team Lead."""

        self._require_team_lead(actor)
        fingerprint = canonical_json_fingerprint(self._fingerprint(command))

        async def operation(unit_of_work: IUnitOfWork) -> CommandResponse:
            now = self._clock.now()
            shifts = self._build_new_shifts(command.shifts)
            self._require_future_schedule(shifts, now)
            await self._validate_references(unit_of_work, shifts)
            event_day = EventDay(
                id=self._id_factory(),
                name=command.name.strip(),
                event_date=command.event_date,
                created_by_user_id=actor.id,
                shifts=shifts,
            )
            await unit_of_work.event_days.add(event_day, created_at=now)
            for shift in shifts:
                await self._reminder_scheduler.synchronize(unit_of_work, shift)
            return CommandResponse(201, self._to_body(event_day))

        result = await self._executor.execute(
            actor_id=actor.id,
            command_name="CREATE_EVENT_DAY",
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            operation=operation,
        )
        return EventDayResult(self._from_body(result.body))

    async def update(
        self,
        event_day_id: UUID,
        command: ConfigureEventDayCommand,
        actor: CurrentActor,
        idempotency_key: str,
    ) -> EventDayResult:
        """Replace an unstarted Event Day and reschedule only worker-unseen reminders."""

        self._require_team_lead(actor)
        payload = {"eventDayId": str(event_day_id), **self._fingerprint(command)}

        async def operation(unit_of_work: IUnitOfWork) -> CommandResponse:
            current = await unit_of_work.event_days.get_for_update(event_day_id)
            if current is None:
                raise EventDayNotFoundError("Event Day is unavailable.")
            now = self._clock.now()
            shifts = self._build_updated_shifts(command.shifts, current)
            self._require_future_schedule(shifts, now)
            await self._validate_references(unit_of_work, shifts)
            removed = {shift.id for shift in current.shifts} - {shift.id for shift in shifts}
            current.replace_configuration(
                name=command.name,
                event_date=command.event_date,
                shifts=shifts,
                now=now,
            )
            await unit_of_work.event_days.save(current, updated_at=now)
            for shift_id in sorted(removed, key=str):
                await unit_of_work.event_reminders.cancel_unattempted(shift_id)
            for shift in shifts:
                await self._reminder_scheduler.synchronize(unit_of_work, shift)
            return CommandResponse(200, self._to_body(current))

        result = await self._executor.execute(
            actor_id=actor.id,
            command_name="UPDATE_EVENT_DAY",
            idempotency_key=idempotency_key,
            request_fingerprint=canonical_json_fingerprint(payload),
            operation=operation,
        )
        return EventDayResult(self._from_body(result.body))

    async def get(self, event_day_id: UUID, actor: CurrentActor) -> EventDayResult:
        """Return Event configuration to Asset Team Leads or HR."""

        if not actor.is_active or actor.roles.isdisjoint(
            {OperationalRole.TEAM_LEAD_ASSET, OperationalRole.HR}
        ):
            raise AuthorizationError("Asset Team Lead or HR role is required.")
        async with self._unit_of_work_factory() as unit_of_work:
            event_day = await unit_of_work.event_days.get(event_day_id)
        if event_day is None:
            raise EventDayNotFoundError("Event Day is unavailable.")
        return EventDayResult(event_day)

    async def _validate_references(
        self, unit_of_work: IUnitOfWork, shifts: tuple[Shift, ...]
    ) -> None:
        asset_ids = tuple(
            sorted({item.asset_id for shift in shifts for item in shift.assets}, key=str)
        )
        if not await unit_of_work.event_days.assets_exist(asset_ids):
            raise EventReferenceError("One or more selected Assets are unavailable.")
        for lead_id in sorted({shift.shift_lead_user_id for shift in shifts}, key=str):
            identity = await unit_of_work.users.get_identity(lead_id)
            if (
                identity is None
                or not identity.is_active
                or OperationalRole.SHIFT_LEAD not in identity.roles
            ):
                raise EventReferenceError("One or more Shift Leads are unavailable.")

    def _build_new_shifts(
        self, commands: tuple[ConfigureShiftCommand, ...]
    ) -> tuple[Shift, ...]:
        if any(command.id is not None for command in commands):
            raise EventConfigurationError("New Event Day Shifts cannot provide IDs.")
        return tuple(
            self._build_shift(command, self._id_factory())
            for command in sorted(commands, key=lambda item: item.sequence)
        )

    def _build_updated_shifts(
        self, commands: tuple[ConfigureShiftCommand, ...], current: EventDay
    ) -> tuple[Shift, ...]:
        existing = {shift.id: shift for shift in current.shifts}
        supplied = [command.id for command in commands if command.id is not None]
        if len(supplied) != len(set(supplied)) or any(
            value not in existing for value in supplied
        ):
            raise EventConfigurationError("Updated Shift IDs must belong to the Event Day.")
        return tuple(
            self._build_shift(command, command.id or self._id_factory())
            for command in sorted(commands, key=lambda item: item.sequence)
        )

    def _build_shift(self, command: ConfigureShiftCommand, shift_id: UUID) -> Shift:
        return Shift(
            id=shift_id,
            sequence=command.sequence,
            name=command.name.strip(),
            starts_at=command.starts_at,
            ends_at=command.ends_at,
            shift_lead_user_id=command.shift_lead_user_id,
            assets=tuple(
                ShiftAsset(
                    id=self._id_factory(),
                    asset_id=item.asset_id,
                    quota_quantity=item.quota_quantity,
                )
                for item in sorted(command.assets, key=lambda value: str(value.asset_id))
            ),
        )

    @staticmethod
    def _require_future_schedule(shifts: tuple[Shift, ...], now: datetime) -> None:
        if any(shift.starts_at <= now for shift in shifts):
            raise EventScheduleConflictError("Configured Shifts must start in the future.")

    @staticmethod
    def _require_team_lead(actor: CurrentActor) -> None:
        if not actor.is_active or not actor.has_role(OperationalRole.TEAM_LEAD_ASSET):
            raise AuthorizationError("Asset Team Lead role is required.")

    @staticmethod
    def _fingerprint(command: ConfigureEventDayCommand) -> dict[str, object]:
        return {
            "name": command.name.strip(),
            "eventDate": command.event_date.isoformat(),
            "shifts": [
                {
                    "id": str(shift.id) if shift.id else None,
                    "sequence": shift.sequence,
                    "name": shift.name.strip(),
                    "startsAt": shift.starts_at.isoformat(),
                    "endsAt": shift.ends_at.isoformat(),
                    "shiftLeadUserId": str(shift.shift_lead_user_id),
                    "assets": [
                        {"assetId": str(item.asset_id), "quotaQuantity": item.quota_quantity}
                        for item in sorted(shift.assets, key=lambda value: str(value.asset_id))
                    ],
                }
                for shift in sorted(command.shifts, key=lambda item: item.sequence)
            ],
        }

    @staticmethod
    def _to_body(event_day: EventDay) -> dict[str, object]:
        return {
            "id": str(event_day.id),
            "name": event_day.name,
            "eventDate": event_day.event_date.isoformat(),
            "status": event_day.status.value,
            "coreStockChanged": False,
            "createdByUserId": str(event_day.created_by_user_id),
            "shifts": [
                {
                    "id": str(shift.id),
                    "sequence": shift.sequence,
                    "name": shift.name,
                    "startsAt": shift.starts_at.isoformat(),
                    "endsAt": shift.ends_at.isoformat(),
                    "shiftLeadUserId": str(shift.shift_lead_user_id),
                    "status": shift.status.value,
                    "assets": [
                        {
                            "assetId": str(item.asset_id),
                            "quotaQuantity": item.quota_quantity,
                            "carryOverQuantity": item.carry_over_quantity,
                            "expectedOpenQuantity": item.expected_open_quantity,
                        }
                        for item in shift.assets
                    ],
                }
                for shift in event_day.shifts
            ],
        }

    @classmethod
    def _from_body(cls, body: Mapping[str, object]) -> EventDay:
        raw_shifts = body["shifts"]
        assert isinstance(raw_shifts, list)
        shifts: list[Shift] = []
        for raw_shift in raw_shifts:
            assert isinstance(raw_shift, Mapping)
            raw_assets = raw_shift["assets"]
            assert isinstance(raw_assets, list)
            shifts.append(
                Shift(
                    id=UUID(str(raw_shift["id"])),
                    sequence=int(str(raw_shift["sequence"])),
                    name=str(raw_shift["name"]),
                    starts_at=datetime.fromisoformat(str(raw_shift["startsAt"])),
                    ends_at=datetime.fromisoformat(str(raw_shift["endsAt"])),
                    shift_lead_user_id=UUID(str(raw_shift["shiftLeadUserId"])),
                    assets=tuple(
                        ShiftAsset(
                            id=uuid4(),
                            asset_id=UUID(str(item["assetId"])),
                            quota_quantity=int(str(item["quotaQuantity"])),
                            carry_over_quantity=int(str(item["carryOverQuantity"])),
                        )
                        for item in raw_assets
                        if isinstance(item, Mapping)
                    ),
                )
            )
        from ams.domain.events import EventDayStatus

        return EventDay(
            id=UUID(str(body["id"])),
            name=str(body["name"]),
            event_date=date.fromisoformat(str(body["eventDate"])),
            created_by_user_id=UUID(str(body["createdByUserId"])),
            shifts=tuple(shifts),
            status=EventDayStatus(str(body["status"])),
        )
