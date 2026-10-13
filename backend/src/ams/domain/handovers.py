"""Term-bound immutable Handover aggregate and repository contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from ams.domain.assets import AssetCondition, CustodyStatus


class HandoverError(Exception):
    """Base class for expected term Handover failures."""

    code = "HANDOVER_INVALID"


class HandoverNotFoundError(HandoverError):
    """The requested Handover is unavailable or outside actor scope."""

    code = "HANDOVER_NOT_FOUND"


class TermNotFoundError(HandoverError):
    """The configured term is unavailable."""

    code = "TERM_NOT_FOUND"


class TermNotEndedError(HandoverError):
    """The configured term has not reached its inclusive end date."""

    code = "TERM_NOT_ENDED"


class HandoverConfirmationForbiddenError(HandoverError):
    """The actor does not own the requested Handover side."""

    code = "HANDOVER_CONFIRMATION_FORBIDDEN"


class HandoverExportForbiddenError(HandoverError):
    """The actor cannot export the requested Handover."""

    code = "HANDOVER_EXPORT_FORBIDDEN"


class HandoverRoleRequiredError(HandoverError):
    """The actor lacks the HR role required for this workflow."""

    code = "ROLE_REQUIRED"


class InvalidHandoverTransitionError(HandoverError):
    """The requested confirmation conflicts with current Handover state."""

    code = "INVALID_HANDOVER_TRANSITION"


class HandoverDataError(HandoverError):
    """Term, snapshot, or persisted Handover values are invalid."""

    code = "HANDOVER_DATA_INVALID"


class HandoverAlreadyExistsError(HandoverError):
    """A term already owns its single Handover snapshot."""

    code = "HANDOVER_ALREADY_EXISTS"


class HandoverExportLimitError(HandoverError):
    """The synchronous CSV exceeds the approved bounded export policy."""

    code = "HANDOVER_EXPORT_LIMIT_EXCEEDED"


class HandoverPersistenceError(Exception):
    """Sanitized Handover persistence failure."""

    code = "HANDOVER_PERSISTENCE_FAILED"


class HandoverStatus(StrEnum):
    """Pending dual confirmation and terminal immutable state."""

    PENDING_CONFIRMATIONS = "PENDING_CONFIRMATIONS"
    LOCKED = "LOCKED"


@dataclass(frozen=True, slots=True)
class Term:
    """Configured organizational term and its explicit Handover actors."""

    id: UUID
    code: str
    name: str
    starts_on: date
    ends_on: date
    old_hr_user_id: UUID
    new_hr_user_id: UUID

    def __post_init__(self) -> None:
        """Validate calendar, names, and distinct Handover actors."""

        if not self.code.strip() or not self.name.strip():
            raise HandoverDataError("Term code and name must not be blank.")
        if self.starts_on > self.ends_on:
            raise HandoverDataError("Term start date must not follow its end date.")
        if self.old_hr_user_id == self.new_hr_user_id:
            raise HandoverDataError("Old and new HR actors must differ.")

    def is_stale(self, last_transaction_date: date | None) -> bool:
        """Flag an Asset with no qualifying transaction inside this inclusive term."""

        return last_transaction_date is None or not (
            self.starts_on <= last_transaction_date <= self.ends_on
        )


@dataclass(frozen=True, slots=True)
class HandoverCustodySnapshot:
    """Immutable current Custody data nested under one Asset snapshot."""

    custody_id: UUID
    custodian_user_id: UUID
    status: CustodyStatus
    on_hand_quantity: int
    reserved_quantity: int
    temporarily_loaned_quantity: int
    condition: AssetCondition
    condition_note: str | None

    def __post_init__(self) -> None:
        """Protect current-state and quantity partition invariants."""

        if self.status is CustodyStatus.CLOSED:
            raise HandoverDataError("Closed Custody cannot enter a current snapshot.")
        if (
            min(
                self.on_hand_quantity,
                self.reserved_quantity,
                self.temporarily_loaned_quantity,
            )
            < 0
        ):
            raise HandoverDataError("Custody snapshot quantities cannot be negative.")
        if self.reserved_quantity + self.temporarily_loaned_quantity > self.on_hand_quantity:
            raise HandoverDataError("Custody snapshot quantity partition is invalid.")


@dataclass(frozen=True, slots=True)
class AssetHandoverSnapshot:
    """Source projection used to create one immutable Handover item."""

    asset_id: UUID
    asset_name: str
    total_quantity: int
    available_quantity: int
    condition: AssetCondition
    custodies: tuple[HandoverCustodySnapshot, ...]
    last_qualifying_transaction_at: datetime | None

    def __post_init__(self) -> None:
        """Validate Asset bounds and UTC transaction time."""

        if not self.asset_name.strip():
            raise HandoverDataError("Asset snapshot name must not be blank.")
        if self.total_quantity < 0 or not 0 <= self.available_quantity <= self.total_quantity:
            raise HandoverDataError("Asset snapshot quantity bounds are invalid.")
        if self.last_qualifying_transaction_at is not None:
            _assert_utc(self.last_qualifying_transaction_at, "Transaction time")


@dataclass(frozen=True, slots=True)
class HandoverItem:
    """Immutable Asset, condition, Custody, and stale-status snapshot."""

    id: UUID
    handover_id: UUID
    asset_id: UUID
    asset_name: str
    total_quantity: int
    available_quantity: int
    condition: AssetCondition
    custodies: tuple[HandoverCustodySnapshot, ...]
    is_stale: bool
    snapshot_at: datetime
    last_qualifying_transaction_at: datetime | None = None

    def __post_init__(self) -> None:
        """Validate immutable snapshot bounds and timestamps."""

        AssetHandoverSnapshot(
            self.asset_id,
            self.asset_name,
            self.total_quantity,
            self.available_quantity,
            self.condition,
            self.custodies,
            self.last_qualifying_transaction_at,
        )
        _assert_utc(self.snapshot_at, "Handover snapshot time")
        if (
            self.last_qualifying_transaction_at is not None
            and self.last_qualifying_transaction_at > self.snapshot_at
        ):
            raise HandoverDataError("Qualifying transaction cannot follow snapshot time.")


@dataclass(slots=True)
class Handover:
    """Term snapshot locked exactly once after both HR actors confirm."""

    id: UUID
    term_id: UUID
    term_code: str
    term_name: str
    term_starts_on: date
    term_ends_on: date
    old_hr_user_id: UUID
    new_hr_user_id: UUID
    created_by_user_id: UUID
    created_at: datetime
    items: tuple[HandoverItem, ...]
    status: HandoverStatus = HandoverStatus.PENDING_CONFIRMATIONS
    old_confirmed_by_user_id: UUID | None = None
    old_confirmed_at: datetime | None = None
    new_confirmed_by_user_id: UUID | None = None
    new_confirmed_at: datetime | None = None
    locked_at: datetime | None = None

    def __post_init__(self) -> None:
        """Validate snapshot identity, actors, timestamps, and lock state."""

        Term(
            self.term_id,
            self.term_code,
            self.term_name,
            self.term_starts_on,
            self.term_ends_on,
            self.old_hr_user_id,
            self.new_hr_user_id,
        )
        _assert_utc(self.created_at, "Handover creation time")
        if not self.items:
            raise HandoverDataError("Handover requires at least one Asset snapshot.")
        if any(item.handover_id != self.id for item in self.items):
            raise HandoverDataError("Handover item belongs to another record.")
        if len({item.asset_id for item in self.items}) != len(self.items):
            raise HandoverDataError("Handover cannot contain duplicate Asset snapshots.")
        self._validate_state()

    def confirm_old(self, *, actor_id: UUID, confirmed_at: datetime) -> bool:
        """Confirm the old-term side and return whether the record became locked."""

        if actor_id != self.old_hr_user_id:
            raise HandoverConfirmationForbiddenError("Only old-side actor may confirm.")
        if self.old_confirmed_at is not None:
            raise InvalidHandoverTransitionError("Old side is already confirmed.")
        self._validate_confirmation_time(confirmed_at)
        self.old_confirmed_by_user_id = actor_id
        self.old_confirmed_at = confirmed_at
        return self._lock_if_complete(confirmed_at)

    def confirm_new(self, *, actor_id: UUID, confirmed_at: datetime) -> bool:
        """Confirm the new-term side and return whether the record became locked."""

        if actor_id != self.new_hr_user_id:
            raise HandoverConfirmationForbiddenError("Only new-side actor may confirm.")
        if self.new_confirmed_at is not None:
            raise InvalidHandoverTransitionError("New side is already confirmed.")
        self._validate_confirmation_time(confirmed_at)
        self.new_confirmed_by_user_id = actor_id
        self.new_confirmed_at = confirmed_at
        return self._lock_if_complete(confirmed_at)

    def _lock_if_complete(self, confirmed_at: datetime) -> bool:
        if self.old_confirmed_at is None or self.new_confirmed_at is None:
            return False
        self.status = HandoverStatus.LOCKED
        self.locked_at = confirmed_at
        return True

    def _validate_confirmation_time(self, value: datetime) -> None:
        _assert_utc(value, "Handover confirmation time")
        if value < self.created_at:
            raise InvalidHandoverTransitionError(
                "Handover confirmation cannot precede creation."
            )

    def _validate_state(self) -> None:
        old = self.old_confirmed_at is not None
        new = self.new_confirmed_at is not None
        if (self.old_confirmed_by_user_id is None) != (not old) or (
            self.new_confirmed_by_user_id is None
        ) != (not new):
            raise HandoverDataError("Confirmation actor and timestamp must be paired.")
        if old and self.old_confirmed_by_user_id != self.old_hr_user_id:
            raise HandoverDataError("Old confirmation actor is invalid.")
        if new and self.new_confirmed_by_user_id != self.new_hr_user_id:
            raise HandoverDataError("New confirmation actor is invalid.")
        for value in (self.old_confirmed_at, self.new_confirmed_at, self.locked_at):
            if value is not None:
                _assert_utc(value, "Handover state time")
                if value < self.created_at:
                    raise HandoverDataError("Handover state time cannot precede creation.")
        if self.status is HandoverStatus.LOCKED:
            if not old or not new or self.locked_at is None:
                raise HandoverDataError("Locked Handover requires both confirmations.")
        elif self.locked_at is not None or (old and new):
            raise HandoverDataError("Pending Handover cannot contain completed lock state.")


class IHandoverRepository(ABC):
    """Persist term snapshots and lock confirmation roots."""

    @abstractmethod
    async def get_term(self, term_id: UUID) -> Term | None:
        """Return one configured term and its two HR actors."""

    @abstractmethod
    async def has_handover_for_term(self, term_id: UUID) -> bool:
        """Return whether the term already owns its unique snapshot."""

    @abstractmethod
    async def snapshot_assets(self, term: Term) -> tuple[AssetHandoverSnapshot, ...]:
        """Return all Assets with current Custodies and qualifying term activity."""

    @abstractmethod
    async def add(self, handover: Handover) -> None:
        """Stage one Handover and all immutable items."""

    @abstractmethod
    async def get(self, handover_id: UUID) -> Handover | None:
        """Return one complete Handover snapshot."""

    @abstractmethod
    async def get_for_update(self, handover_id: UUID) -> Handover | None:
        """Lock and return one complete Handover aggregate."""

    @abstractmethod
    async def save(self, handover: Handover, *, updated_at: datetime) -> None:
        """Stage confirmation and lock fields without changing items."""


def _assert_utc(value: datetime, label: str) -> None:
    if value.utcoffset() != timedelta(0):
        raise HandoverDataError(f"{label} must be timezone-aware UTC.")
