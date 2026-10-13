"""Borrow Ticket aggregate, state transitions, and deterministic grouping rules."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

MIN_EVIDENCE_COUNT = 1
MAX_EVIDENCE_COUNT = 3


class TicketRuleError(Exception):
    """Base class for expected Borrow Ticket business-rule failures."""

    code = "TICKET_RULE_VIOLATION"


class InvalidTicketDataError(TicketRuleError):
    """Ticket identities, quantities, reasons, or grouping inputs are invalid."""

    code = "INVALID_TICKET_DATA"


class TicketDueDatePolicyError(TicketRuleError):
    """Ticket method and due date do not follow the canonical policy."""

    code = "TICKET_DUE_DATE_POLICY_VIOLATION"


class InvalidTicketTransitionError(TicketRuleError):
    """Requested state change is not legal from the current status."""

    code = "INVALID_TICKET_TRANSITION"


class TicketActionForbiddenError(TicketRuleError):
    """Actor is not the borrower or source custodian required by an action."""

    code = "TICKET_ACTION_FORBIDDEN"


class TicketEvidenceRequiredError(TicketRuleError):
    """An evidence-backed confirmation has no valid private evidence IDs."""

    code = "EVIDENCE_REQUIRED"


class TicketMethod(StrEnum):
    """Approved Ticket methods matching the V4 PostgreSQL enum."""

    TEMPORARY = "TEMPORARY"
    CUSTODY_TRANSFER = "CUSTODY_TRANSFER"


class TicketStatus(StrEnum):
    """Canonical Borrow Ticket lifecycle states."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    BORROWING = "BORROWING"
    RETURNING = "RETURNING"
    CANCEL_PENDING = "CANCEL_PENDING"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class TicketItem:
    """One normalized quantity request against an explicit source Custody."""

    asset_id: UUID
    source_custody_id: UUID
    quantity: int

    def __post_init__(self) -> None:
        """Reject quantities that cannot reserve physical inventory."""

        if self.quantity <= 0:
            raise InvalidTicketDataError("Ticket item quantity must be positive.")


@dataclass(frozen=True, slots=True)
class TicketAllocation:
    """Custody-enriched allocation input used by the grouping factory."""

    asset_id: UUID
    source_custody_id: UUID
    source_custodian_user_id: UUID
    quantity: int
    method: TicketMethod
    due_date: date | None

    def __post_init__(self) -> None:
        """Validate quantity and method-level due-date policy before grouping."""

        if self.quantity <= 0:
            raise InvalidTicketDataError("Ticket allocation quantity must be positive.")
        _validate_due_date(self.method, self.due_date)


@dataclass(frozen=True, slots=True)
class TicketGroup:
    """One independently creatable Ticket group with normalized items."""

    source_custodian_user_id: UUID
    method: TicketMethod
    due_date: date | None
    items: tuple[TicketItem, ...]


class TicketGroupingFactory:
    """Group enriched allocations by canonical Ticket ownership and policy."""

    def group(self, allocations: tuple[TicketAllocation, ...]) -> tuple[TicketGroup, ...]:
        """Return deterministic groups and merge exact duplicate source allocations."""

        if not allocations:
            raise InvalidTicketDataError("At least one Ticket allocation is required.")

        custody_identity: dict[UUID, tuple[UUID, UUID]] = {}
        grouped: dict[
            tuple[UUID, TicketMethod, date | None],
            dict[UUID, TicketItem],
        ] = {}
        for allocation in allocations:
            identity = (
                allocation.asset_id,
                allocation.source_custodian_user_id,
            )
            existing_identity = custody_identity.setdefault(
                allocation.source_custody_id, identity
            )
            if existing_identity != identity:
                raise InvalidTicketDataError(
                    "A source Custody cannot identify different Assets or custodians."
                )

            group_key = (
                allocation.source_custodian_user_id,
                allocation.method,
                allocation.due_date,
            )
            items = grouped.setdefault(group_key, {})
            existing_item = items.get(allocation.source_custody_id)
            quantity = allocation.quantity
            if existing_item is not None:
                quantity += existing_item.quantity
            items[allocation.source_custody_id] = TicketItem(
                asset_id=allocation.asset_id,
                source_custody_id=allocation.source_custody_id,
                quantity=quantity,
            )

        return tuple(
            TicketGroup(
                source_custodian_user_id=key[0],
                method=key[1],
                due_date=key[2],
                items=tuple(
                    sorted(items.values(), key=lambda item: str(item.source_custody_id))
                ),
            )
            for key, items in sorted(grouped.items(), key=self._group_sort_key)
        )

    @staticmethod
    def _group_sort_key(
        entry: tuple[
            tuple[UUID, TicketMethod, date | None],
            dict[UUID, TicketItem],
        ],
    ) -> tuple[str, str, str]:
        key = entry[0]
        return str(key[0]), key[1].value, key[2].isoformat() if key[2] is not None else ""


@dataclass(slots=True)
class BorrowTicket:
    """Grouped Ticket aggregate with explicit actor-aware lifecycle methods."""

    id: UUID
    borrower_user_id: UUID
    source_custodian_user_id: UUID
    method: TicketMethod
    status: TicketStatus
    due_date: date | None
    items: tuple[TicketItem, ...]
    pickup_evidence_ids: tuple[UUID, ...] = ()
    borrower_return_evidence_ids: tuple[UUID, ...] = ()
    custodian_return_evidence_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        """Protect group policy and normalized item identity on create/rehydration."""

        _validate_due_date(self.method, self.due_date)
        if not self.items:
            raise InvalidTicketDataError("A Ticket requires at least one item.")
        custody_ids = [item.source_custody_id for item in self.items]
        if len(custody_ids) != len(set(custody_ids)):
            raise InvalidTicketDataError("A source Custody may appear only once per Ticket.")
        for evidence_ids in (
            self.pickup_evidence_ids,
            self.borrower_return_evidence_ids,
            self.custodian_return_evidence_ids,
        ):
            if evidence_ids:
                self._validate_evidence(evidence_ids)

    @classmethod
    def create(
        cls,
        *,
        ticket_id: UUID,
        borrower_user_id: UUID,
        group: TicketGroup,
    ) -> BorrowTicket:
        """Create one independently managed pending Ticket from a normalized group."""

        return cls(
            id=ticket_id,
            borrower_user_id=borrower_user_id,
            source_custodian_user_id=group.source_custodian_user_id,
            method=group.method,
            status=TicketStatus.PENDING,
            due_date=group.due_date,
            items=group.items,
        )

    def approve(self, actor_id: UUID) -> None:
        """Allow the source custodian to approve a pending Ticket."""

        self._require_source_custodian(actor_id)
        self._transition(TicketStatus.PENDING, TicketStatus.APPROVED)

    def reject(self, actor_id: UUID, reason: str) -> None:
        """Allow the source custodian to reject a pending Ticket with a reason."""

        self._require_source_custodian(actor_id)
        self._require_reason(reason)
        self._transition(TicketStatus.PENDING, TicketStatus.REJECTED)

    def request_cancellation(self, actor_id: UUID, reason: str) -> None:
        """Cancel pending directly or request custodian resolution after approval."""

        self._require_borrower(actor_id)
        self._require_reason(reason)
        if self.status is TicketStatus.PENDING:
            self.status = TicketStatus.CANCELLED
            return
        if self.status is TicketStatus.APPROVED:
            self.status = TicketStatus.CANCEL_PENDING
            return
        raise InvalidTicketTransitionError(
            f"Ticket cannot request cancellation from {self.status.value}."
        )

    def resolve_cancellation(self, actor_id: UUID, *, approve: bool) -> None:
        """Let the source custodian accept cancellation or restore approval."""

        self._require_source_custodian(actor_id)
        self._transition(
            TicketStatus.CANCEL_PENDING,
            TicketStatus.CANCELLED if approve else TicketStatus.APPROVED,
        )

    def confirm_received(self, actor_id: UUID, evidence_ids: tuple[UUID, ...]) -> None:
        """Record borrower receipt and enter borrowing or close a custody transfer."""

        self._require_borrower(actor_id)
        self._validate_evidence(evidence_ids)
        self._transition(
            TicketStatus.APPROVED,
            (
                TicketStatus.BORROWING
                if self.method is TicketMethod.TEMPORARY
                else TicketStatus.CLOSED
            ),
        )
        self.pickup_evidence_ids = evidence_ids

    def mark_returned(self, actor_id: UUID, evidence_ids: tuple[UUID, ...]) -> None:
        """Record temporary-loan borrower return evidence and await the custodian."""

        self._require_borrower(actor_id)
        self._require_temporary()
        self._validate_evidence(evidence_ids)
        self._transition(TicketStatus.BORROWING, TicketStatus.RETURNING)
        self.borrower_return_evidence_ids = evidence_ids

    def confirm_returned(self, actor_id: UUID, evidence_ids: tuple[UUID, ...]) -> None:
        """Record source-custodian return evidence and close a temporary Ticket."""

        self._require_source_custodian(actor_id)
        self._require_temporary()
        self._validate_evidence(evidence_ids)
        self._transition(TicketStatus.RETURNING, TicketStatus.CLOSED)
        self.custodian_return_evidence_ids = evidence_ids

    def _transition(self, expected: TicketStatus, target: TicketStatus) -> None:
        if self.status is not expected:
            raise InvalidTicketTransitionError(
                f"Ticket must be {expected.value}, not {self.status.value}."
            )
        self.status = target

    def _require_borrower(self, actor_id: UUID) -> None:
        if actor_id != self.borrower_user_id:
            raise TicketActionForbiddenError(
                "Only the Ticket borrower may perform this action."
            )

    def _require_source_custodian(self, actor_id: UUID) -> None:
        if actor_id != self.source_custodian_user_id:
            raise TicketActionForbiddenError(
                "Only the source custodian may perform this action."
            )

    def _require_temporary(self) -> None:
        if self.method is not TicketMethod.TEMPORARY:
            raise InvalidTicketTransitionError(
                "Custody-transfer Tickets do not have a return workflow."
            )

    @staticmethod
    def _require_reason(reason: str) -> None:
        if not reason.strip():
            raise InvalidTicketDataError("A non-blank reason is required.")

    @staticmethod
    def _validate_evidence(evidence_ids: tuple[UUID, ...]) -> None:
        if not MIN_EVIDENCE_COUNT <= len(evidence_ids) <= MAX_EVIDENCE_COUNT:
            raise TicketEvidenceRequiredError(
                "Ticket confirmation requires between one and three evidence images."
            )
        if len(evidence_ids) != len(set(evidence_ids)):
            raise TicketEvidenceRequiredError("Ticket evidence IDs must be unique.")


@dataclass(frozen=True, slots=True)
class TicketEvent:
    """Immutable actor-attributed Ticket transition record."""

    id: UUID
    ticket_id: UUID
    actor_user_id: UUID
    event_type: str
    from_status: TicketStatus | None
    to_status: TicketStatus
    occurred_at: datetime
    note: str | None = None


@dataclass(frozen=True, slots=True)
class TicketEvidence:
    """Private evidence metadata owned by one Ticket action."""

    id: UUID
    ticket_id: UUID
    uploaded_by_user_id: UUID
    evidence_type: str
    object_key: str
    content_type: str
    content_sha256: str
    size_bytes: int
    created_at: datetime
    note: str | None = None


class ITicketRepository(ABC):
    """Persist and lock Borrow Ticket aggregates and immutable child history."""

    @abstractmethod
    async def add(self, ticket: BorrowTicket, *, created_at: datetime) -> None:
        """Stage a new Ticket and its items without committing."""

    @abstractmethod
    async def get_for_update(self, ticket_id: UUID) -> BorrowTicket | None:
        """Lock and return one Ticket aggregate with evidence references."""

    @abstractmethod
    async def save(self, ticket: BorrowTicket, *, updated_at: datetime) -> None:
        """Stage Ticket status and item-independent mutable state."""

    @abstractmethod
    async def add_event(self, event: TicketEvent) -> None:
        """Stage one immutable transition event."""

    @abstractmethod
    async def add_evidence(self, evidence: TicketEvidence) -> None:
        """Stage one immutable private evidence metadata row."""


def _validate_due_date(method: TicketMethod, due_date: date | None) -> None:
    if method is TicketMethod.TEMPORARY and due_date is None:
        raise TicketDueDatePolicyError("Temporary Tickets require a due date.")
    if method is TicketMethod.CUSTODY_TRANSFER and due_date is not None:
        raise TicketDueDatePolicyError("Custody-transfer Tickets cannot have a due date.")
