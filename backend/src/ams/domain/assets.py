"""Asset and physical-custody domain model without persistence or HTTP concerns."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID


class AssetCustodyError(Exception):
    """Base class for expected Asset/Custody business-rule failures."""

    code = "ASSET_CUSTODY_RULE_VIOLATION"


class InvalidQuantityError(AssetCustodyError):
    """Raised when a business quantity is zero, negative, or outside its bounds."""

    code = "INVALID_QUANTITY"


class InsufficientLendableQuantityError(AssetCustodyError):
    """Raised when a Custody cannot reserve the requested lendable quantity."""

    code = "INSUFFICIENT_LENDABLE_QUANTITY"


class AllocationQuantityMismatchError(AssetCustodyError):
    """Raised when initial Custody allocations exceed Asset total quantity."""

    code = "ALLOCATION_QUANTITY_MISMATCH"


class InvalidCustodyTransitionError(AssetCustodyError):
    """Raised when a Custody cannot move from its current state."""

    code = "INVALID_CUSTODY_TRANSITION"


class CustodyConfirmationForbiddenError(AssetCustodyError):
    """Raised when someone other than the assigned custodian confirms receipt."""

    code = "CUSTODY_CONFIRMATION_FORBIDDEN"


class CustodyEvidenceRequiredError(AssetCustodyError):
    """Raised when confirmation lacks the approved number of evidence references."""

    code = "CUSTODY_EVIDENCE_REQUIRED"


class InvalidAssetDataError(AssetCustodyError):
    """Raised when required Asset text or identity relationships are invalid."""

    code = "INVALID_ASSET_DATA"


class CustodyStatus(StrEnum):
    """Lifecycle states for a physical Asset holding."""

    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"


class AssetCondition(StrEnum):
    """Approved structured condition values; notes carry additional context."""

    GOOD = "GOOD"
    DAMAGED = "DAMAGED"
    LOST = "LOST"


@dataclass(frozen=True, slots=True)
class CustodyAllocation:
    """Initial quantity assigned to one prospective custodian."""

    custodian_user_id: UUID
    quantity: int

    def __post_init__(self) -> None:
        """Reject allocations that cannot represent physical inventory."""

        if self.quantity <= 0:
            raise InvalidQuantityError("Custody allocation quantity must be positive.")


@dataclass(slots=True)
class Asset:
    """Club-wide inventory totals and their transactionally maintained availability."""

    id: UUID
    category_id: UUID
    responsible_team_id: UUID | None
    name: str
    source: str
    total_quantity: int
    available_quantity: int
    created_by_user_id: UUID
    created_at: datetime
    description: str | None = None
    contributing_custody_ids: frozenset[UUID] = frozenset()

    def __post_init__(self) -> None:
        """Protect Asset invariants during creation and persistence rehydration."""

        if not self.name.strip():
            raise InvalidAssetDataError("Asset name must not be blank.")
        if not self.source.strip():
            raise InvalidAssetDataError("Asset source must not be blank.")
        if self.total_quantity < 0:
            raise InvalidQuantityError("Asset total quantity cannot be negative.")
        self._assert_available_bounds(self.available_quantity)
        _assert_utc_timestamp(self.created_at, "Asset creation time")

    @classmethod
    def create(
        cls,
        *,
        asset_id: UUID,
        category_id: UUID,
        responsible_team_id: UUID | None,
        name: str,
        source: str,
        total_quantity: int,
        allocations: tuple[CustodyAllocation, ...],
        created_by_user_id: UUID,
        created_at: datetime,
        description: str | None = None,
    ) -> Asset:
        """Create an Asset whose allocated initial quantity is pending assignment.

        Pending Custodies do not contribute to availability. Each confirmation
        activates its Custody quantity through ``activate_custody``.
        """

        if total_quantity <= 0:
            raise InvalidQuantityError("New Asset total quantity must be positive.")
        if not allocations:
            raise AllocationQuantityMismatchError(
                "A new Asset requires at least one initial Custody allocation."
            )
        allocated_quantity = sum(allocation.quantity for allocation in allocations)
        if allocated_quantity > total_quantity:
            raise AllocationQuantityMismatchError(
                "Initial Custody allocations cannot exceed Asset total quantity."
            )
        custodian_ids = [allocation.custodian_user_id for allocation in allocations]
        if len(custodian_ids) != len(set(custodian_ids)):
            raise InvalidAssetDataError(
                "An Asset cannot contain duplicate initial custodian allocations."
            )
        return cls(
            id=asset_id,
            category_id=category_id,
            responsible_team_id=responsible_team_id,
            name=name.strip(),
            source=source.strip(),
            total_quantity=total_quantity,
            available_quantity=0,
            created_by_user_id=created_by_user_id,
            created_at=created_at,
            description=description,
        )

    def activate_custody(self, custody: Custody) -> None:
        """Add one newly active Custody's lendable amount to availability."""

        if custody.asset_id != self.id:
            raise InvalidAssetDataError("Custody belongs to a different Asset.")
        if custody.status is not CustodyStatus.ACTIVE:
            raise InvalidCustodyTransitionError(
                "Only an active Custody can contribute to Asset availability."
            )
        if custody.id in self.contributing_custody_ids:
            raise InvalidCustodyTransitionError(
                "Custody already contributes to Asset availability."
            )
        new_available = self.available_quantity + custody.lendable_quantity
        self._assert_available_bounds(new_available)
        self.available_quantity = new_available
        self.contributing_custody_ids = self.contributing_custody_ids | {custody.id}

    def classify_responsible_team(self, team_id: UUID | None) -> None:
        """Assign or clear the Team classification without changing physical custody."""

        self.responsible_team_id = team_id

    def reserve_available(self, quantity: int) -> None:
        """Decrease the transactionally maintained availability for a reservation."""

        if quantity <= 0:
            raise InvalidQuantityError("Reserved Asset quantity must be positive.")
        self._assert_available_bounds(self.available_quantity - quantity)
        self.available_quantity -= quantity

    def release_available(self, quantity: int) -> None:
        """Increase availability when reservation or temporary loan is released."""

        if quantity <= 0:
            raise InvalidQuantityError("Released Asset quantity must be positive.")
        self._assert_available_bounds(self.available_quantity + quantity)
        self.available_quantity += quantity

    def _assert_available_bounds(self, quantity: int) -> None:
        if quantity < 0 or quantity > self.total_quantity:
            raise InvalidQuantityError(
                "Asset available quantity must be between zero and total quantity."
            )


@dataclass(slots=True)
class Custody:
    """A quantity of one Asset physically assigned to a specific person."""

    id: UUID
    asset_id: UUID
    custodian_user_id: UUID
    assigned_by_user_id: UUID
    status: CustodyStatus
    on_hand_quantity: int
    reserved_quantity: int
    temporarily_loaned_quantity: int
    condition: AssetCondition
    assigned_at: datetime
    confirmed_at: datetime | None = None
    confirmed_by_user_id: UUID | None = None
    closed_at: datetime | None = None
    condition_note: str | None = None
    evidence_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        """Protect quantity partition and lifecycle timestamp invariants."""

        self._validate_quantity_partition()
        _assert_utc_timestamp(self.assigned_at, "Custody assignment time")
        if self.confirmed_at is not None:
            _assert_utc_timestamp(self.confirmed_at, "Custody confirmation time")
        if self.closed_at is not None:
            _assert_utc_timestamp(self.closed_at, "Custody closure time")
        self._validate_state()

    @classmethod
    def create_pending(
        cls,
        *,
        custody_id: UUID,
        asset_id: UUID,
        custodian_user_id: UUID,
        assigned_by_user_id: UUID,
        quantity: int,
        assigned_at: datetime,
        condition: AssetCondition = AssetCondition.GOOD,
        condition_note: str | None = None,
    ) -> Custody:
        """Create a non-lendable Custody awaiting evidence-backed receipt."""

        if quantity <= 0:
            raise InvalidQuantityError("Pending Custody quantity must be positive.")
        return cls(
            id=custody_id,
            asset_id=asset_id,
            custodian_user_id=custodian_user_id,
            assigned_by_user_id=assigned_by_user_id,
            status=CustodyStatus.PENDING,
            on_hand_quantity=quantity,
            reserved_quantity=0,
            temporarily_loaned_quantity=0,
            condition=condition,
            assigned_at=assigned_at,
            condition_note=condition_note,
        )

    @property
    def lendable_quantity(self) -> int:
        """Return currently lendable quantity; pending/closed Custody lends nothing."""

        if self.status is not CustodyStatus.ACTIVE:
            return 0
        return self.on_hand_quantity - self.reserved_quantity - self.temporarily_loaned_quantity

    def confirm(
        self,
        *,
        actor_id: UUID,
        evidence_ids: tuple[UUID, ...],
        confirmed_at: datetime,
    ) -> None:
        """Activate a pending Custody after assignee evidence confirmation."""

        if actor_id != self.custodian_user_id:
            raise CustodyConfirmationForbiddenError(
                "Only the assigned custodian may confirm this Custody."
            )
        if self.status is not CustodyStatus.PENDING:
            raise InvalidCustodyTransitionError("Only a pending Custody can be confirmed.")
        if not 1 <= len(evidence_ids) <= 3 or len(set(evidence_ids)) != len(evidence_ids):
            raise CustodyEvidenceRequiredError(
                "Custody confirmation requires one to three distinct evidence images."
            )
        _assert_utc_timestamp(confirmed_at, "Custody confirmation time")
        if confirmed_at < self.assigned_at:
            raise InvalidCustodyTransitionError(
                "Custody confirmation cannot precede assignment."
            )

        self.status = CustodyStatus.ACTIVE
        self.confirmed_at = confirmed_at
        self.confirmed_by_user_id = actor_id
        self.evidence_ids = evidence_ids

    def reserve(self, quantity: int) -> None:
        """Reserve active lendable quantity for one pending Ticket item."""

        self._require_positive_quantity(quantity)
        if self.status is not CustodyStatus.ACTIVE:
            raise InvalidCustodyTransitionError("Only active Custody can reserve quantity.")
        if quantity > self.lendable_quantity:
            raise InsufficientLendableQuantityError(
                "Requested quantity exceeds source Custody lendable quantity."
            )
        self.reserved_quantity += quantity

    def release_reservation(self, quantity: int) -> None:
        """Release quantity reserved by a rejected or cancelled Ticket exactly once."""

        self._require_positive_quantity(quantity)
        if quantity > self.reserved_quantity:
            raise InvalidQuantityError("Cannot release more than reserved quantity.")
        self.reserved_quantity -= quantity

    def checkout_reserved(self, quantity: int) -> None:
        """Move approved reservation into temporary-loan tracking at pickup."""

        self._require_positive_quantity(quantity)
        if quantity > self.reserved_quantity:
            raise InvalidQuantityError("Cannot check out more than reserved quantity.")
        self.reserved_quantity -= quantity
        self.temporarily_loaned_quantity += quantity

    def return_temporary_loan(self, quantity: int) -> None:
        """Return temporary-loan quantity to the source Custody's lendable balance."""

        self._require_positive_quantity(quantity)
        if quantity > self.temporarily_loaned_quantity:
            raise InvalidQuantityError("Cannot return more than temporarily loaned quantity.")
        self.temporarily_loaned_quantity -= quantity

    def transfer_out(self, quantity: int, *, transferred_at: datetime) -> None:
        """Consume reserved stock when custody passes permanently to another actor."""

        self._require_positive_quantity(quantity)
        _assert_utc_timestamp(transferred_at, "Custody transfer time")
        if self.status is not CustodyStatus.ACTIVE:
            raise InvalidCustodyTransitionError("Only active Custody can transfer quantity.")
        if quantity > self.reserved_quantity:
            raise InvalidQuantityError("Cannot transfer more than reserved quantity.")
        self.reserved_quantity -= quantity
        self.on_hand_quantity -= quantity
        if self.on_hand_quantity == 0:
            self.status = CustodyStatus.CLOSED
            self.closed_at = transferred_at

    def receive_transfer(self, quantity: int) -> None:
        """Add confirmed transferred stock to an existing active Custody."""

        self._require_positive_quantity(quantity)
        if self.status is not CustodyStatus.ACTIVE:
            raise InvalidCustodyTransitionError("Only active Custody can receive quantity.")
        self.on_hand_quantity += quantity

    @classmethod
    def create_from_confirmed_transfer(
        cls,
        *,
        custody_id: UUID,
        asset_id: UUID,
        custodian_user_id: UUID,
        assigned_by_user_id: UUID,
        quantity: int,
        confirmed_at: datetime,
        evidence_ids: tuple[UUID, ...],
        condition: AssetCondition,
    ) -> Custody:
        """Create active recipient Custody after evidence-backed transfer receipt."""

        cls._require_positive_quantity(quantity)
        if not evidence_ids:
            raise CustodyEvidenceRequiredError("Transferred Custody requires evidence.")
        return cls(
            id=custody_id,
            asset_id=asset_id,
            custodian_user_id=custodian_user_id,
            assigned_by_user_id=assigned_by_user_id,
            status=CustodyStatus.ACTIVE,
            on_hand_quantity=quantity,
            reserved_quantity=0,
            temporarily_loaned_quantity=0,
            condition=condition,
            assigned_at=confirmed_at,
            confirmed_at=confirmed_at,
            confirmed_by_user_id=custodian_user_id,
            evidence_ids=evidence_ids,
        )

    @staticmethod
    def _require_positive_quantity(quantity: int) -> None:
        if quantity <= 0:
            raise InvalidQuantityError("Quantity must be positive.")

    def _validate_quantity_partition(self) -> None:
        quantities = (
            self.on_hand_quantity,
            self.reserved_quantity,
            self.temporarily_loaned_quantity,
        )
        if any(quantity < 0 for quantity in quantities):
            raise InvalidQuantityError("Custody quantities cannot be negative.")
        if self.reserved_quantity + self.temporarily_loaned_quantity > self.on_hand_quantity:
            raise InvalidQuantityError(
                "Reserved and temporarily loaned quantities cannot exceed on-hand quantity."
            )

    def _validate_state(self) -> None:
        has_confirmation = (
            self.confirmed_at is not None and self.confirmed_by_user_id is not None
        )
        has_partial_confirmation = (self.confirmed_at is None) != (
            self.confirmed_by_user_id is None
        )
        if has_partial_confirmation:
            raise InvalidCustodyTransitionError(
                "Custody confirmation actor and time must be recorded together."
            )
        if self.status is CustodyStatus.PENDING:
            valid = not has_confirmation and self.closed_at is None and not self.evidence_ids
        elif self.status is CustodyStatus.ACTIVE:
            valid = has_confirmation and self.closed_at is None and bool(self.evidence_ids)
        else:
            valid = has_confirmation and self.closed_at is not None and bool(self.evidence_ids)
        if not valid:
            raise InvalidCustodyTransitionError(
                "Custody state does not match its confirmation and closure data."
            )
        if self.confirmed_at is not None and self.confirmed_at < self.assigned_at:
            raise InvalidCustodyTransitionError(
                "Custody confirmation cannot precede assignment."
            )
        if (
            self.closed_at is not None
            and self.confirmed_at is not None
            and self.closed_at < self.confirmed_at
        ):
            raise InvalidCustodyTransitionError("Custody closure cannot precede confirmation.")


@dataclass(frozen=True, slots=True)
class CustodyEvidence:
    """Private evidence metadata owned by one confirmed Custody."""

    id: UUID
    custody_id: UUID
    uploaded_by_user_id: UUID
    object_key: str
    content_type: str
    content_sha256: str
    size_bytes: int
    created_at: datetime
    note: str | None = None
    evidence_type: str = "ASSIGNMENT_CONFIRMATION"

    def __post_init__(self) -> None:
        """Validate storage metadata before it reaches the SQL schema."""

        if not self.object_key.strip():
            raise InvalidAssetDataError("Evidence object key must not be blank.")
        if self.evidence_type not in {
            "ASSIGNMENT_CONFIRMATION",
            "TRANSFER_CONFIRMATION",
        }:
            raise InvalidAssetDataError("Evidence type is invalid.")
        if self.content_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise InvalidAssetDataError("Evidence content type is invalid.")
        if len(self.content_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.content_sha256
        ):
            raise InvalidAssetDataError("Evidence fingerprint must be lowercase SHA-256.")
        if not 1 <= self.size_bytes <= 10 * 1024 * 1024:
            raise InvalidAssetDataError("Evidence size is outside the approved limit.")
        if self.note is not None and len(self.note) > 1000:
            raise InvalidAssetDataError("Evidence note exceeds its maximum length.")
        _assert_utc_timestamp(self.created_at, "Evidence creation time")


def _assert_utc_timestamp(value: datetime, label: str) -> None:
    if value.utcoffset() != timedelta(0):
        raise InvalidAssetDataError(f"{label} must be timezone-aware UTC.")
