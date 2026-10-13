"""Shift execution aggregate, transitions, and persistence contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from ams.domain.events import EventDayStatus, ShiftStatus


class ShiftExecutionError(Exception):
    """Base class for expected Shift execution failures."""

    code = "SHIFT_EXECUTION_INVALID"


class ShiftNotFoundError(ShiftExecutionError):
    """The requested Shift or Event Day is unavailable."""

    code = "SHIFT_NOT_FOUND"


class ShiftActionForbiddenError(ShiftExecutionError):
    """The actor is not the active assigned Shift Lead."""

    code = "SHIFT_ACTION_FORBIDDEN"


class InvalidShiftTransitionError(ShiftExecutionError):
    """The Shift state cannot accept the requested transition."""

    code = "INVALID_SHIFT_TRANSITION"


class ShiftScheduleUnavailableError(ShiftExecutionError):
    """The Shift cannot open at the current instant or before its predecessor closes."""

    code = "SHIFT_SCHEDULE_UNAVAILABLE"


class ShiftQuantityError(ShiftExecutionError):
    """Submitted item quantities do not match the complete Shift snapshot."""

    code = "SHIFT_QUANTITY_INVALID"


class ShiftPersistenceError(Exception):
    """Sanitized persistence failure at the Shift execution boundary."""

    code = "SHIFT_PERSISTENCE_FAILED"


@dataclass(slots=True)
class ShiftExecutionItem:
    """One mutable operational Asset snapshot within a Shift."""

    id: UUID
    asset_id: UUID
    quota_quantity: int
    carry_over_quantity: int = 0
    opening_actual_quantity: int | None = None
    closing_actual_quantity: int | None = None
    consumed_quantity: int | None = None
    discrepancy_reason: str | None = None

    def __post_init__(self) -> None:
        """Validate all persisted quantity and formula invariants."""

        values = (
            self.quota_quantity,
            self.carry_over_quantity,
            self.opening_actual_quantity,
            self.closing_actual_quantity,
        )
        if any(value is not None and value < 0 for value in values):
            raise ShiftQuantityError("Shift quantities cannot be negative.")
        if self.closing_actual_quantity is None:
            if self.consumed_quantity is not None:
                raise ShiftQuantityError("Consumption requires a closing quantity.")
        elif self.consumed_quantity != (
            self.expected_open_quantity - self.closing_actual_quantity
        ):
            raise ShiftQuantityError("Stored Shift consumption does not match its formula.")
        if self.discrepancy_reason is not None and not self.discrepancy_reason.strip():
            raise ShiftQuantityError("A discrepancy reason must not be blank.")

    @property
    def expected_open_quantity(self) -> int:
        """Return quota plus predecessor carry-over."""

        return self.quota_quantity + self.carry_over_quantity


@dataclass(slots=True)
class ShiftExecution:
    """Aggregate root enforcing one assigned Shift's operational lifecycle."""

    id: UUID
    event_day_id: UUID
    event_day_name: str
    event_day_creator_user_id: UUID
    sequence: int
    name: str
    starts_at: datetime
    ends_at: datetime
    shift_lead_user_id: UUID
    items: tuple[ShiftExecutionItem, ...]
    status: ShiftStatus = ShiftStatus.SCHEDULED

    def __post_init__(self) -> None:
        """Reject malformed persisted execution snapshots."""

        _assert_utc(self.starts_at, "Shift start")
        _assert_utc(self.ends_at, "Shift end")
        if self.sequence <= 0 or self.ends_at <= self.starts_at or not self.items:
            raise ShiftExecutionError("Shift execution snapshot is invalid.")
        asset_ids = [item.asset_id for item in self.items]
        if len(asset_ids) != len(set(asset_ids)):
            raise ShiftQuantityError("A Shift Asset may appear only once.")

    def open(
        self,
        *,
        actor_id: UUID,
        now: datetime,
        predecessor: ShiftExecution | None,
    ) -> None:
        """Enter OPENING during the schedule after applying predecessor carry-over."""

        self._require_assignee(actor_id)
        _assert_utc(now, "Current time")
        if self.status is not ShiftStatus.SCHEDULED:
            raise InvalidShiftTransitionError("Only a scheduled Shift can be opened.")
        if now < self.starts_at or now >= self.ends_at:
            raise ShiftScheduleUnavailableError(
                "A Shift can open only from its start and before its end."
            )
        if self.sequence == 1:
            if predecessor is not None:
                raise ShiftExecutionError("The first Shift cannot have a predecessor.")
            carry_over: dict[UUID, int] = {}
        else:
            if predecessor is None or predecessor.sequence != self.sequence - 1:
                raise ShiftScheduleUnavailableError("The preceding Shift is unavailable.")
            if predecessor.status is not ShiftStatus.CLOSED:
                raise ShiftScheduleUnavailableError(
                    "The preceding Shift must close before this Shift opens."
                )
            carry_over = {
                item.asset_id: item.closing_actual_quantity or 0 for item in predecessor.items
            }
        for item in self.items:
            item.carry_over_quantity = carry_over.get(item.asset_id, 0)
        self.status = ShiftStatus.OPENING

    def confirm_received(
        self,
        *,
        actor_id: UUID,
        quantities: dict[UUID, int],
    ) -> None:
        """Enter ACTIVE only when every received quantity exactly matches expectation."""

        self._require_assignee(actor_id)
        self._require_opening()
        self._require_complete_quantities(quantities)
        if any(quantities[item.asset_id] != item.expected_open_quantity for item in self.items):
            raise ShiftQuantityError(
                "Exact opening quantities are required for normal confirmation."
            )
        for item in self.items:
            item.opening_actual_quantity = quantities[item.asset_id]
            item.discrepancy_reason = None
        self.status = ShiftStatus.ACTIVE

    def report_opening_deviation(
        self,
        *,
        actor_id: UUID,
        quantities: dict[UUID, int],
        reason: str,
    ) -> None:
        """Enter ACTIVE_WITH_DEVIATION with a required explanation."""

        self._require_assignee(actor_id)
        self._require_opening()
        self._require_complete_quantities(quantities)
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ShiftQuantityError("Opening deviation requires a reason.")
        if all(quantities[item.asset_id] == item.expected_open_quantity for item in self.items):
            raise ShiftQuantityError("Opening deviation requires at least one mismatch.")
        for item in self.items:
            actual = quantities[item.asset_id]
            item.opening_actual_quantity = actual
            item.discrepancy_reason = (
                normalized_reason if actual != item.expected_open_quantity else None
            )
        self.status = ShiftStatus.ACTIVE_WITH_DEVIATION

    def close(self, *, actor_id: UUID, quantities: dict[UUID, int]) -> None:
        """Close an active Shift and calculate immutable consumption values."""

        self._require_assignee(actor_id)
        if self.status not in {ShiftStatus.ACTIVE, ShiftStatus.ACTIVE_WITH_DEVIATION}:
            raise InvalidShiftTransitionError("Only an active Shift can be closed.")
        self._require_complete_quantities(quantities)
        for item in self.items:
            actual = quantities[item.asset_id]
            item.closing_actual_quantity = actual
            item.consumed_quantity = item.expected_open_quantity - actual
        self.status = ShiftStatus.CLOSED

    def _require_assignee(self, actor_id: UUID) -> None:
        if actor_id != self.shift_lead_user_id:
            raise ShiftActionForbiddenError("Only the assigned Shift Lead may act.")

    def _require_opening(self) -> None:
        if self.status is not ShiftStatus.OPENING:
            raise InvalidShiftTransitionError("The Shift is not awaiting opening confirmation.")

    def _require_complete_quantities(self, quantities: dict[UUID, int]) -> None:
        expected_ids = {item.asset_id for item in self.items}
        if set(quantities) != expected_ids:
            raise ShiftQuantityError("Every configured Shift Asset must appear exactly once.")
        if any(quantity < 0 for quantity in quantities.values()):
            raise ShiftQuantityError("Shift quantities cannot be negative.")


@dataclass(frozen=True, slots=True)
class ShiftEvent:
    """Immutable audit event emitted for one successful Shift transition."""

    id: UUID
    shift_id: UUID
    actor_user_id: UUID
    event_type: str
    from_status: ShiftStatus
    to_status: ShiftStatus
    metadata: dict[str, object]
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class EventDayExecutionSummary:
    """Progress and optional final per-Asset Event Day aggregate."""

    event_day_id: UUID
    status: EventDayStatus
    total_shift_count: int
    closed_shift_count: int
    items: tuple[EventDayAssetSummary, ...] | None

    @property
    def complete(self) -> bool:
        """Return whether every configured Shift is closed."""

        return self.total_shift_count > 0 and self.closed_shift_count == self.total_shift_count


@dataclass(frozen=True, slots=True)
class EventDayAssetSummary:
    """Final reproducible totals for one Asset across an Event Day."""

    asset_id: UUID
    total_quota_quantity: int
    total_consumed_quantity: int
    opening_variance_quantity: int
    final_carry_over_quantity: int


class IShiftExecutionRepository(ABC):
    """Persist Shift transitions and expose assigned operational read models."""

    @abstractmethod
    async def list_for_assignee(
        self, actor_id: UUID, *, offset: int, limit: int
    ) -> tuple[tuple[ShiftExecution, ...], int]:
        """Return one stable page of Shifts assigned to the actor."""

    @abstractmethod
    async def get(self, shift_id: UUID) -> ShiftExecution | None:
        """Load one Shift execution snapshot without a write lock."""

    @abstractmethod
    async def get_for_update(self, shift_id: UUID) -> ShiftExecution | None:
        """Lock and load one Shift execution snapshot."""

    @abstractmethod
    async def get_predecessor_for_update(
        self, event_day_id: UUID, sequence: int
    ) -> ShiftExecution | None:
        """Lock and load the immediately preceding Shift if one exists."""

    @abstractmethod
    async def save_transition(
        self,
        shift: ShiftExecution,
        event: ShiftEvent,
        *,
        event_day_status: EventDayStatus,
    ) -> None:
        """Stage item values, status, event state, and immutable audit history."""

    @abstractmethod
    async def all_other_shifts_closed(self, event_day_id: UUID, shift_id: UUID) -> bool:
        """Return whether every other Shift in the Event Day is already closed."""

    @abstractmethod
    async def get_event_day_summary(
        self, event_day_id: UUID
    ) -> EventDayExecutionSummary | None:
        """Return progress and final aggregate only after all Shifts close."""


def _assert_utc(value: datetime, label: str) -> None:
    if value.utcoffset() != timedelta(0):
        raise ShiftExecutionError(f"{label} must be timezone-aware UTC.")
