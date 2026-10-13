"""Event-day configuration aggregate and repository contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from uuid import UUID


class EventConfigurationError(Exception):
    """Base class for expected Event configuration failures."""

    code = "EVENT_CONFIGURATION_INVALID"


class EventScheduleConflictError(EventConfigurationError):
    """The proposed schedule overlaps or cannot safely replace current state."""

    code = "EVENT_SCHEDULE_CONFLICT"


class EventReferenceError(EventConfigurationError):
    """A referenced Asset or Shift Lead is unavailable."""

    code = "EVENT_REFERENCE_UNAVAILABLE"


class EventDayStatus(StrEnum):
    """Event-day lifecycle persisted by the V5 schema."""

    DRAFT = "DRAFT"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"


class ShiftStatus(StrEnum):
    """Canonical Shift lifecycle shared with F08 execution."""

    SCHEDULED = "SCHEDULED"
    OPENING = "OPENING"
    ACTIVE = "ACTIVE"
    ACTIVE_WITH_DEVIATION = "ACTIVE_WITH_DEVIATION"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class ShiftAsset:
    """One Asset quota configured for a Shift without changing core stock."""

    id: UUID
    asset_id: UUID
    quota_quantity: int
    carry_over_quantity: int = 0

    def __post_init__(self) -> None:
        """Require non-negative operational quantities."""

        if self.quota_quantity < 0 or self.carry_over_quantity < 0:
            raise EventConfigurationError("Shift Asset quantities cannot be negative.")

    @property
    def expected_open_quantity(self) -> int:
        """Return the immutable opening formula input for F08."""

        return self.quota_quantity + self.carry_over_quantity


@dataclass(frozen=True, slots=True)
class Shift:
    """One ordered, assigned, scheduled operational Shift."""

    id: UUID
    sequence: int
    name: str
    starts_at: datetime
    ends_at: datetime
    shift_lead_user_id: UUID
    assets: tuple[ShiftAsset, ...]
    status: ShiftStatus = ShiftStatus.SCHEDULED

    def __post_init__(self) -> None:
        """Protect identity, time, ordering, and Asset uniqueness invariants."""

        if self.sequence <= 0:
            raise EventConfigurationError("Shift sequence must be positive.")
        if not self.name.strip():
            raise EventConfigurationError("Shift name must not be blank.")
        _assert_utc(self.starts_at, "Shift start")
        _assert_utc(self.ends_at, "Shift end")
        if self.ends_at <= self.starts_at:
            raise EventConfigurationError("Shift end must follow its start.")
        if not self.assets:
            raise EventConfigurationError("A Shift requires at least one Asset quota.")
        asset_ids = [item.asset_id for item in self.assets]
        if len(asset_ids) != len(set(asset_ids)):
            raise EventConfigurationError("An Asset may appear only once in a Shift.")


@dataclass(slots=True)
class EventDay:
    """Aggregate root protecting an ordered, non-overlapping Shift schedule."""

    id: UUID
    name: str
    event_date: date
    created_by_user_id: UUID
    shifts: tuple[Shift, ...]
    status: EventDayStatus = EventDayStatus.DRAFT

    def __post_init__(self) -> None:
        """Validate creation and persistence rehydration through one rule set."""

        self._validate(self.name, self.shifts)

    def replace_configuration(
        self,
        *,
        name: str,
        event_date: date,
        shifts: tuple[Shift, ...],
        now: datetime,
    ) -> None:
        """Replace only an unstarted draft schedule with another valid schedule."""

        _assert_utc(now, "Current time")
        if self.status is not EventDayStatus.DRAFT or any(
            shift.status is not ShiftStatus.SCHEDULED or shift.starts_at <= now
            for shift in self.shifts
        ):
            raise EventScheduleConflictError("An Event Day cannot change after a Shift begins.")
        self._validate(name, shifts)
        self.name = name.strip()
        self.event_date = event_date
        self.shifts = shifts

    @staticmethod
    def _validate(name: str, shifts: tuple[Shift, ...]) -> None:
        if not name.strip():
            raise EventConfigurationError("Event Day name must not be blank.")
        if not shifts:
            raise EventConfigurationError("An Event Day requires at least one Shift.")
        sequences = [shift.sequence for shift in shifts]
        if sequences != list(range(1, len(shifts) + 1)):
            raise EventConfigurationError("Shift sequences must be contiguous from one.")
        shift_ids = [shift.id for shift in shifts]
        if len(shift_ids) != len(set(shift_ids)):
            raise EventConfigurationError("Shift IDs must be unique in an Event Day.")
        for previous, current in zip(shifts, shifts[1:], strict=False):
            if current.starts_at < previous.ends_at:
                raise EventScheduleConflictError("Event Day Shifts cannot overlap.")


class IEventDayRepository(ABC):
    """Persist Event configuration through use-case-specific operations."""

    @abstractmethod
    async def add(self, event_day: EventDay, *, created_at: datetime) -> None:
        """Stage a complete Event Day without committing."""

    @abstractmethod
    async def get(self, event_day_id: UUID) -> EventDay | None:
        """Load one Event Day and its complete ordered configuration."""

    @abstractmethod
    async def get_for_update(self, event_day_id: UUID) -> EventDay | None:
        """Lock and load an Event Day for safe configuration replacement."""

    @abstractmethod
    async def save(self, event_day: EventDay, *, updated_at: datetime) -> None:
        """Replace a draft schedule while retaining referenced Shift identities."""

    @abstractmethod
    async def assets_exist(self, asset_ids: tuple[UUID, ...]) -> bool:
        """Return whether all selected Asset identities exist."""

    @abstractmethod
    async def get_shift_for_update(self, shift_id: UUID) -> Shift | None:
        """Lock and load one Shift for scheduled-event eligibility checks."""


def _assert_utc(value: datetime, label: str) -> None:
    if value.utcoffset() != timedelta(0):
        raise EventConfigurationError(f"{label} must be timezone-aware UTC.")
