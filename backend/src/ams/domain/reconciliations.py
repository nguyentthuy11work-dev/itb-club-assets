"""Reconciliation snapshot aggregate, transitions, and repository contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from ams.domain.assets import AssetCondition


class ReconciliationError(Exception):
    """Base class for expected reconciliation failures."""

    code = "RECONCILIATION_INVALID"


class ReconciliationNotFoundError(ReconciliationError):
    """The requested session or item is unavailable."""

    code = "RECONCILIATION_NOT_FOUND"


class ReconciliationForbiddenError(ReconciliationError):
    """The actor cannot perform the requested reconciliation action."""

    code = "RECONCILIATION_ITEM_FORBIDDEN"


class CustodianScopeRequiredError(ReconciliationForbiddenError):
    """The actor has no snapshotted Custody work queue."""

    code = "CUSTODIAN_SCOPE_REQUIRED"


class ReconciliationRoleRequiredError(ReconciliationForbiddenError):
    """The actor does not hold the HR role required by the command."""

    code = "ROLE_REQUIRED"


class InvalidReconciliationDateError(ReconciliationError):
    """The selected business date precedes the club-local current date."""

    code = "INVALID_RECONCILIATION_DATE"


class InvalidReconciliationTransitionError(ReconciliationError):
    """The current reconciliation state cannot accept an action."""

    code = "INVALID_RECONCILIATION_TRANSITION"


class ReconciliationCloseBlockedError(ReconciliationError):
    """Pending or unresolved items prevent session closure."""

    code = "RECONCILIATION_HAS_UNRESOLVED_ITEMS"


class ReconciliationDataError(ReconciliationError):
    """Submitted or persisted reconciliation values violate an invariant."""

    code = "RECONCILIATION_DATA_INVALID"


class ReconciliationPersistenceError(Exception):
    """Sanitized reconciliation persistence failure."""

    code = "RECONCILIATION_PERSISTENCE_FAILED"


class ReconciliationStatus(StrEnum):
    """Session lifecycle."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"


class ReconciliationItemStatus(StrEnum):
    """Truthful confirmation and discrepancy lifecycle."""

    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    CONFIRMED = "CONFIRMED"
    DISCREPANCY = "DISCREPANCY"
    RESOLVED = "RESOLVED"


@dataclass(frozen=True, slots=True)
class CustodySnapshotCandidate:
    """One active Custody projected for immutable snapshot creation."""

    custody_id: UUID
    asset_id: UUID
    custodian_user_id: UUID
    asset_name: str
    expected_quantity: int
    expected_condition: AssetCondition


@dataclass(frozen=True, slots=True)
class TemporaryLoanReturnTarget:
    """One outstanding temporary Ticket and its borrower email target."""

    ticket_id: UUID
    borrower_user_id: UUID
    borrower_email: str


@dataclass(frozen=True, slots=True)
class ReconciliationEvidence:
    """Private evidence metadata owned by one reconciliation item."""

    id: UUID
    reconciliation_item_id: UUID
    uploaded_by_user_id: UUID
    object_key: str
    content_type: str
    content_sha256: str
    size_bytes: int
    created_at: datetime


@dataclass(slots=True)
class ReconciliationItem:
    """Immutable expected snapshot plus its one authorized confirmation."""

    id: UUID
    reconciliation_id: UUID
    custody_id: UUID
    asset_id: UUID
    custodian_user_id: UUID
    asset_name: str
    expected_quantity: int
    expected_condition: AssetCondition
    snapshot_at: datetime
    status: ReconciliationItemStatus = ReconciliationItemStatus.PENDING_CONFIRMATION
    actual_quantity: int | None = None
    actual_condition: AssetCondition | None = None
    condition_note: str | None = None
    discrepancy_reason: str | None = None
    confirmed_by_user_id: UUID | None = None
    confirmed_at: datetime | None = None
    resolution_note: str | None = None
    resolved_by_user_id: UUID | None = None
    resolved_at: datetime | None = None
    last_reminded_at: datetime | None = None

    def __post_init__(self) -> None:
        """Validate snapshot and state-dependent fields during rehydration."""

        if not self.asset_name.strip() or self.expected_quantity < 0:
            raise ReconciliationDataError("Reconciliation snapshot is invalid.")
        _assert_utc(self.snapshot_at, "Snapshot time")
        self._validate_state()

    def confirm(
        self,
        *,
        actor_id: UUID,
        actual_quantity: int,
        actual_condition: AssetCondition,
        condition_note: str | None,
        discrepancy_reason: str | None,
        evidence_ids: tuple[UUID, ...],
        confirmed_at: datetime,
    ) -> None:
        """Record one evidence-backed confirmation and classify its result."""

        if actor_id != self.custodian_user_id:
            raise ReconciliationForbiddenError("Only assigned custodian may confirm.")
        if self.status is not ReconciliationItemStatus.PENDING_CONFIRMATION:
            raise InvalidReconciliationTransitionError("Item is already confirmed.")
        if actual_quantity < 0:
            raise ReconciliationDataError("Actual quantity cannot be negative.")
        if not 1 <= len(evidence_ids) <= 3 or len(set(evidence_ids)) != len(evidence_ids):
            raise ReconciliationDataError("One to three unique evidence images are required.")
        _assert_utc(confirmed_at, "Confirmation time")
        mismatch = (
            actual_quantity != self.expected_quantity
            or actual_condition is not self.expected_condition
        )
        reason = _optional_text(discrepancy_reason)
        if mismatch and reason is None:
            raise ReconciliationDataError("A discrepancy reason is required.")
        if not mismatch and reason is not None:
            raise ReconciliationDataError(
                "A matching confirmation cannot have a discrepancy reason."
            )
        self.actual_quantity = actual_quantity
        self.actual_condition = actual_condition
        self.condition_note = _optional_text(condition_note)
        self.discrepancy_reason = reason
        self.confirmed_by_user_id = actor_id
        self.confirmed_at = confirmed_at
        self.status = (
            ReconciliationItemStatus.DISCREPANCY
            if mismatch
            else ReconciliationItemStatus.CONFIRMED
        )

    def resolve(self, *, actor_id: UUID, note: str, resolved_at: datetime) -> None:
        """Resolve an explained discrepancy exactly once."""

        if self.status is not ReconciliationItemStatus.DISCREPANCY:
            raise InvalidReconciliationTransitionError(
                "Only an unresolved discrepancy can be resolved."
            )
        normalized = note.strip()
        if not normalized:
            raise ReconciliationDataError("Resolution note is required.")
        _assert_utc(resolved_at, "Resolution time")
        self.status = ReconciliationItemStatus.RESOLVED
        self.resolution_note = normalized
        self.resolved_by_user_id = actor_id
        self.resolved_at = resolved_at

    def remind(self, *, reminded_at: datetime) -> None:
        """Record a reminder only while confirmation remains pending."""

        if self.status is not ReconciliationItemStatus.PENDING_CONFIRMATION:
            raise InvalidReconciliationTransitionError("Item is already confirmed.")
        _assert_utc(reminded_at, "Reminder time")
        self.last_reminded_at = reminded_at

    def _validate_state(self) -> None:
        confirmation = (
            self.actual_quantity,
            self.actual_condition,
            self.confirmed_by_user_id,
            self.confirmed_at,
        )
        if self.actual_quantity is not None and self.actual_quantity < 0:
            raise ReconciliationDataError("Actual quantity cannot be negative.")
        if self.status is ReconciliationItemStatus.PENDING_CONFIRMATION:
            if any(value is not None for value in confirmation):
                raise ReconciliationDataError("Pending item cannot contain confirmation data.")
            return
        if any(value is None for value in confirmation):
            raise ReconciliationDataError("Confirmed item data is incomplete.")
        mismatch = (
            self.actual_quantity != self.expected_quantity
            or self.actual_condition is not self.expected_condition
        )
        if self.status is ReconciliationItemStatus.CONFIRMED and (
            mismatch or self.discrepancy_reason is not None
        ):
            raise ReconciliationDataError("Confirmed item must match its snapshot.")
        if self.status in {
            ReconciliationItemStatus.DISCREPANCY,
            ReconciliationItemStatus.RESOLVED,
        } and (not mismatch or _optional_text(self.discrepancy_reason) is None):
            raise ReconciliationDataError("Discrepancy data is incomplete.")
        resolution = (self.resolution_note, self.resolved_by_user_id, self.resolved_at)
        if self.status is ReconciliationItemStatus.RESOLVED:
            if (
                any(value is None for value in resolution)
                or self.resolution_note is None
                or not self.resolution_note.strip()
            ):
                raise ReconciliationDataError("Resolved item data is incomplete.")
        elif any(value is not None for value in resolution):
            raise ReconciliationDataError("Only resolved items may contain resolution data.")


@dataclass(slots=True)
class ReconciliationSession:
    """Session aggregate controlling requests and immutable closure."""

    id: UUID
    reconciliation_date: date
    opened_by_user_id: UUID
    opened_at: datetime
    items: tuple[ReconciliationItem, ...]
    temporary_loan_ticket_count: int = 0
    status: ReconciliationStatus = ReconciliationStatus.OPEN
    confirmation_requested_at: datetime | None = None
    closed_by_user_id: UUID | None = None
    closed_at: datetime | None = None

    def __post_init__(self) -> None:
        """Reject malformed session state and duplicate snapshots."""

        _assert_utc(self.opened_at, "Opening time")
        if self.temporary_loan_ticket_count < 0:
            raise ReconciliationDataError("Temporary-loan count cannot be negative.")
        custody_ids = [item.custody_id for item in self.items]
        if len(custody_ids) != len(set(custody_ids)):
            raise ReconciliationDataError("A Custody may be snapshotted only once.")
        if any(item.reconciliation_id != self.id for item in self.items):
            raise ReconciliationDataError("Item belongs to another reconciliation.")
        if self.status is ReconciliationStatus.OPEN:
            if self.closed_by_user_id is not None or self.closed_at is not None:
                raise ReconciliationDataError("Open session cannot contain closure data.")
        elif self.closed_by_user_id is None or self.closed_at is None:
            raise ReconciliationDataError("Closed session requires closure data.")

    def request_confirmation(self, *, requested_at: datetime) -> None:
        """Mark the first confirmation request for an open session."""

        self._require_open()
        if self.confirmation_requested_at is not None:
            raise InvalidReconciliationTransitionError(
                "Confirmation was already requested for this session."
            )
        _assert_utc(requested_at, "Request time")
        self.confirmation_requested_at = requested_at

    def close(self, *, actor_id: UUID, closed_at: datetime, unresolved_count: int) -> None:
        """Close only after every snapshot item reaches an allowed terminal state."""

        self._require_open()
        if unresolved_count:
            raise ReconciliationCloseBlockedError(
                "Resolve all pending confirmations and discrepancies before closing."
            )
        _assert_utc(closed_at, "Closure time")
        self.status = ReconciliationStatus.CLOSED
        self.closed_by_user_id = actor_id
        self.closed_at = closed_at

    def _require_open(self) -> None:
        if self.status is not ReconciliationStatus.OPEN:
            raise InvalidReconciliationTransitionError(
                "Only an open reconciliation accepts this action."
            )


class IReconciliationRepository(ABC):
    """Persist sessions and expose use-case-specific snapshot projections."""

    @abstractmethod
    async def lock_open_policy(self) -> None:
        """Serialize global open-session creation within the transaction."""

    @abstractmethod
    async def has_open_session(self) -> bool:
        """Return whether the one-open-session policy is occupied."""

    @abstractmethod
    async def list_snapshot_candidates(self) -> tuple[CustodySnapshotCandidate, ...]:
        """Read eligible active Custodies in deterministic order."""

    @abstractmethod
    async def list_temporary_loan_return_targets(
        self,
    ) -> tuple[TemporaryLoanReturnTarget, ...]:
        """Return active temporary Tickets and borrower email targets."""

    @abstractmethod
    async def add(self, session: ReconciliationSession) -> None:
        """Stage one session and all immutable snapshot items."""

    @abstractmethod
    async def get(self, reconciliation_id: UUID) -> ReconciliationSession | None:
        """Load one complete session without write locks."""

    @abstractmethod
    async def get_for_update(self, reconciliation_id: UUID) -> ReconciliationSession | None:
        """Lock and load one complete session."""

    @abstractmethod
    async def get_item_session_id(self, item_id: UUID) -> UUID | None:
        """Resolve an item's parent without exposing item state."""

    @abstractmethod
    async def get_item_for_update(self, item_id: UUID) -> ReconciliationItem | None:
        """Lock and load one item after locking its parent session."""

    @abstractmethod
    async def list_for_custodian(
        self, custodian_id: UUID, *, offset: int, limit: int
    ) -> tuple[tuple[ReconciliationItem, ...], int]:
        """Return a deterministic custodian-owned item page."""

    @abstractmethod
    async def count_unresolved_for_update(self, reconciliation_id: UUID) -> int:
        """Lock session items and count pending/discrepancy states."""

    @abstractmethod
    async def save_session(
        self, session: ReconciliationSession, *, updated_at: datetime
    ) -> None:
        """Stage mutable session request/closure fields."""

    @abstractmethod
    async def save_item(self, item: ReconciliationItem, *, updated_at: datetime) -> None:
        """Stage mutable confirmation/resolution/reminder fields."""

    @abstractmethod
    async def add_evidence(self, evidence: ReconciliationEvidence) -> None:
        """Stage one immutable private evidence metadata row."""


def _assert_utc(value: datetime, label: str) -> None:
    if value.utcoffset() != timedelta(0):
        raise ReconciliationDataError(f"{label} must be timezone-aware UTC.")


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None
