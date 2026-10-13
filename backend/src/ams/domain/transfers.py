"""Standalone two-party Custody Transfer aggregate and repository contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from ams.domain.assets import AssetCondition, Custody


class TransferError(Exception):
    """Base class for expected standalone Transfer failures."""

    code = "TRANSFER_INVALID"


class TransferNotFoundError(TransferError):
    """The requested Transfer or linked resource is unavailable."""

    code = "TRANSFER_NOT_FOUND"


class TransferConfirmationForbiddenError(TransferError):
    """The actor does not own the requested confirmation side."""

    code = "TRANSFER_CONFIRMATION_FORBIDDEN"


class TransferRoleRequiredError(TransferError):
    """The actor does not hold the HR role required to create a Transfer."""

    code = "ROLE_REQUIRED"


class InvalidTransferTransitionError(TransferError):
    """The Transfer state cannot accept the requested action."""

    code = "INVALID_TRANSFER_TRANSITION"


class TransferQuantityError(TransferError):
    """The requested Transfer quantity is not available at its source."""

    code = "TRANSFER_QUANTITY_INVALID"


class TransferDataError(TransferError):
    """Transfer identities, evidence, or persisted values are invalid."""

    code = "TRANSFER_DATA_INVALID"


class TransferPersistenceError(Exception):
    """Sanitized Transfer persistence failure."""

    code = "TRANSFER_PERSISTENCE_FAILED"


class TransferStatus(StrEnum):
    """Two independent confirmations and one terminal state."""

    PENDING_BOTH_CONFIRMATIONS = "PENDING_BOTH_CONFIRMATIONS"
    PENDING_CURRENT_CONFIRMATION = "PENDING_CURRENT_CONFIRMATION"
    PENDING_TARGET_CONFIRMATION = "PENDING_TARGET_CONFIRMATION"
    COMPLETED = "COMPLETED"


class TransferEvidenceSide(StrEnum):
    """Owner side for immutable confirmation evidence."""

    CURRENT = "CURRENT"
    TARGET = "TARGET"


@dataclass(frozen=True, slots=True)
class TransferEvidence:
    """Private evidence metadata attached to one confirmation side."""

    id: UUID
    transfer_id: UUID
    side: TransferEvidenceSide
    uploaded_by_user_id: UUID
    object_key: str
    content_type: str
    content_sha256: str
    size_bytes: int
    created_at: datetime
    note: str | None = None


@dataclass(slots=True)
class CustodyTransfer:
    """Aggregate requiring independent current and target confirmations."""

    id: UUID
    source_custody_id: UUID
    asset_id: UUID
    current_custodian_user_id: UUID
    target_custodian_user_id: UUID
    quantity: int
    condition_snapshot: AssetCondition
    created_by_user_id: UUID
    created_at: datetime
    reconciliation_item_id: UUID | None = None
    status: TransferStatus = TransferStatus.PENDING_BOTH_CONFIRMATIONS
    current_confirmed_by_user_id: UUID | None = None
    current_confirmed_at: datetime | None = None
    target_confirmed_by_user_id: UUID | None = None
    target_confirmed_at: datetime | None = None
    target_custody_id: UUID | None = None
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        """Validate identity, quantity, time, and confirmation-state invariants."""

        if self.current_custodian_user_id == self.target_custodian_user_id:
            raise TransferDataError("Current and target custodians must differ.")
        if self.quantity <= 0:
            raise TransferQuantityError("Transfer quantity must be positive.")
        _assert_utc(self.created_at, "Transfer creation time")
        self._validate_state()

    def confirm_current(
        self, *, actor_id: UUID, evidence_ids: tuple[UUID, ...], confirmed_at: datetime
    ) -> bool:
        """Confirm the current side and return whether both sides are now complete."""

        if actor_id != self.current_custodian_user_id:
            raise TransferConfirmationForbiddenError("Only current custodian may confirm.")
        if self.current_confirmed_at is not None:
            raise InvalidTransferTransitionError("Current side is already confirmed.")
        _validate_evidence(evidence_ids)
        _assert_utc(confirmed_at, "Current confirmation time")
        if confirmed_at < self.created_at:
            raise InvalidTransferTransitionError(
                "Current confirmation cannot precede Transfer creation."
            )
        self.current_confirmed_by_user_id = actor_id
        self.current_confirmed_at = confirmed_at
        return self._advance_after_confirmation(confirmed_at)

    def confirm_target(
        self, *, actor_id: UUID, evidence_ids: tuple[UUID, ...], confirmed_at: datetime
    ) -> bool:
        """Confirm the target side and return whether both sides are now complete."""

        if actor_id != self.target_custodian_user_id:
            raise TransferConfirmationForbiddenError("Only target custodian may confirm.")
        if self.target_confirmed_at is not None:
            raise InvalidTransferTransitionError("Target side is already confirmed.")
        _validate_evidence(evidence_ids)
        _assert_utc(confirmed_at, "Target confirmation time")
        if confirmed_at < self.created_at:
            raise InvalidTransferTransitionError(
                "Target confirmation cannot precede Transfer creation."
            )
        self.target_confirmed_by_user_id = actor_id
        self.target_confirmed_at = confirmed_at
        return self._advance_after_confirmation(confirmed_at)

    def mark_custody_completed(self, target_custody_id: UUID) -> None:
        """Attach the exactly-once target Custody produced by both confirmations."""

        if self.status is not TransferStatus.COMPLETED or self.completed_at is None:
            raise InvalidTransferTransitionError(
                "Both confirmations are required before custody completion."
            )
        if self.target_custody_id is not None:
            raise InvalidTransferTransitionError("Transfer custody is already completed.")
        self.target_custody_id = target_custody_id

    def _advance_after_confirmation(self, confirmed_at: datetime) -> bool:
        if self.current_confirmed_at is not None and self.target_confirmed_at is not None:
            self.status = TransferStatus.COMPLETED
            self.completed_at = confirmed_at
            return True
        self.status = (
            TransferStatus.PENDING_CURRENT_CONFIRMATION
            if self.current_confirmed_at is None
            else TransferStatus.PENDING_TARGET_CONFIRMATION
        )
        return False

    def _validate_state(self) -> None:
        current = (
            self.current_confirmed_by_user_id is not None
            and self.current_confirmed_at is not None
        )
        target = (
            self.target_confirmed_by_user_id is not None
            and self.target_confirmed_at is not None
        )
        if (self.current_confirmed_by_user_id is None) != (
            self.current_confirmed_at is None
        ) or (self.target_confirmed_by_user_id is None) != (self.target_confirmed_at is None):
            raise TransferDataError("Confirmation actor and timestamp must be paired.")
        expected = {
            (False, False): TransferStatus.PENDING_BOTH_CONFIRMATIONS,
            (False, True): TransferStatus.PENDING_CURRENT_CONFIRMATION,
            (True, False): TransferStatus.PENDING_TARGET_CONFIRMATION,
            (True, True): TransferStatus.COMPLETED,
        }[(current, target)]
        if self.status is not expected:
            raise TransferDataError("Transfer status does not match its confirmations.")
        if self.status is TransferStatus.COMPLETED:
            if self.completed_at is None or self.target_custody_id is None:
                raise TransferDataError("Completed Transfer requires custody completion data.")
        elif self.completed_at is not None or self.target_custody_id is not None:
            raise TransferDataError("Pending Transfer cannot contain completion data.")


@dataclass(frozen=True, slots=True)
class ReconciliationTransferReference:
    """Minimal linked item state required by Transfer creation."""

    item_id: UUID
    source_custody_id: UUID
    session_open: bool


class ITransferRepository(ABC):
    """Persist Transfer aggregates and expose ownership-scoped use cases."""

    @abstractmethod
    async def add(self, transfer: CustodyTransfer) -> None:
        """Stage a new reserved Transfer without committing."""

    @abstractmethod
    async def get_for_update(self, transfer_id: UUID) -> CustodyTransfer | None:
        """Lock and load one Transfer aggregate."""

    @abstractmethod
    async def list_visible(
        self,
        actor_id: UUID,
        *,
        include_all: bool,
        status: TransferStatus | None,
        offset: int,
        limit: int,
    ) -> tuple[tuple[CustodyTransfer, ...], int]:
        """Return a stable ownership-filtered page."""

    @abstractmethod
    async def save(self, transfer: CustodyTransfer, *, updated_at: datetime) -> None:
        """Stage confirmation and completion fields."""

    @abstractmethod
    async def add_evidence(self, evidence: TransferEvidence) -> None:
        """Stage one immutable side-owned evidence row."""

    @abstractmethod
    async def list_evidence(
        self, transfer_id: UUID, side: TransferEvidenceSide
    ) -> tuple[TransferEvidence, ...]:
        """Return one side's immutable evidence metadata."""

    @abstractmethod
    async def lock_completion_custodies(
        self, transfer: CustodyTransfer
    ) -> tuple[Custody, Custody | None]:
        """Lock source and optional target Custodies in ascending UUID order."""

    @abstractmethod
    async def get_reconciliation_reference(
        self, item_id: UUID
    ) -> ReconciliationTransferReference | None:
        """Return linked-item scope needed for HR creation validation."""


def _validate_evidence(evidence_ids: tuple[UUID, ...]) -> None:
    if not 1 <= len(evidence_ids) <= 3 or len(set(evidence_ids)) != len(evidence_ids):
        raise TransferDataError("One to three unique evidence images are required.")


def _assert_utc(value: datetime, label: str) -> None:
    if value.utcoffset() != timedelta(0):
        raise TransferDataError(f"{label} must be timezone-aware UTC.")
