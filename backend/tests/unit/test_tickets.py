"""Unit tests for Borrow Ticket grouping and aggregate lifecycle rules."""

from __future__ import annotations

import unittest
from collections.abc import Callable
from datetime import date
from uuid import UUID

from ams.domain.tickets import (
    BorrowTicket,
    InvalidTicketDataError,
    InvalidTicketTransitionError,
    TicketActionForbiddenError,
    TicketAllocation,
    TicketDueDatePolicyError,
    TicketEvidenceRequiredError,
    TicketGroup,
    TicketGroupingFactory,
    TicketItem,
    TicketMethod,
    TicketStatus,
)

BORROWER_ID = UUID("00000000-0000-4000-8000-000000005201")
CUSTODIAN_ID = UUID("00000000-0000-4000-8000-000000005202")
OTHER_CUSTODIAN_ID = UUID("00000000-0000-4000-8000-000000005203")
ASSET_ID = UUID("00000000-0000-4000-8000-000000005204")
OTHER_ASSET_ID = UUID("00000000-0000-4000-8000-000000005205")
CUSTODY_ID = UUID("00000000-0000-4000-8000-000000005206")
OTHER_CUSTODY_ID = UUID("00000000-0000-4000-8000-000000005207")
TICKET_ID = UUID("00000000-0000-4000-8000-000000005208")
EVIDENCE_ID = UUID("00000000-0000-4000-8000-000000005209")
OTHER_EVIDENCE_ID = UUID("00000000-0000-4000-8000-000000005210")
DUE_DATE = date(2026, 10, 15)
OTHER_DUE_DATE = date(2026, 10, 16)


class TicketGroupingFactoryTests(unittest.TestCase):
    """Verify grouping keys, normalization, validation, and deterministic order."""

    def test_groups_by_custodian_method_and_temporary_due_date(self) -> None:
        """Exact duplicate sources merge while distinct canonical keys split."""

        allocations = (
            _allocation(CUSTODY_ID, quantity=1),
            _allocation(CUSTODY_ID, quantity=2),
            _allocation(OTHER_CUSTODY_ID, asset_id=OTHER_ASSET_ID),
            _allocation(CUSTODY_ID, due_date=OTHER_DUE_DATE),
            _allocation(
                CUSTODY_ID,
                method=TicketMethod.CUSTODY_TRANSFER,
                due_date=None,
            ),
            _allocation(
                CUSTODY_ID,
                custodian_id=OTHER_CUSTODIAN_ID,
                asset_id=ASSET_ID,
            ),
        )

        with self.assertRaises(InvalidTicketDataError):
            TicketGroupingFactory().group(allocations)

        groups = TicketGroupingFactory().group(allocations[:-1])
        self.assertEqual(len(groups), 3)
        temporary = next(group for group in groups if group.due_date == DUE_DATE)
        self.assertEqual(temporary.source_custodian_user_id, CUSTODIAN_ID)
        self.assertEqual(
            [(item.source_custody_id, item.quantity) for item in temporary.items],
            [(CUSTODY_ID, 3), (OTHER_CUSTODY_ID, 1)],
        )
        self.assertEqual(
            [(group.method, group.due_date) for group in groups],
            [
                (TicketMethod.CUSTODY_TRANSFER, None),
                (TicketMethod.TEMPORARY, DUE_DATE),
                (TicketMethod.TEMPORARY, OTHER_DUE_DATE),
            ],
        )

    def test_conflicting_source_custody_asset_is_rejected(self) -> None:
        """One source Custody cannot be presented as belonging to multiple Assets."""

        with self.assertRaises(InvalidTicketDataError):
            TicketGroupingFactory().group(
                (
                    _allocation(CUSTODY_ID),
                    _allocation(CUSTODY_ID, asset_id=OTHER_ASSET_ID),
                )
            )

    def test_empty_or_invalid_allocation_is_rejected(self) -> None:
        """Grouping requires positive, method-valid allocation inputs."""

        with self.assertRaises(InvalidTicketDataError):
            TicketGroupingFactory().group(())
        with self.assertRaises(InvalidTicketDataError):
            _allocation(CUSTODY_ID, quantity=0)
        with self.assertRaises(TicketDueDatePolicyError):
            _allocation(CUSTODY_ID, due_date=None)
        with self.assertRaises(TicketDueDatePolicyError):
            _allocation(
                CUSTODY_ID,
                method=TicketMethod.CUSTODY_TRANSFER,
                due_date=DUE_DATE,
            )


class BorrowTicketTests(unittest.TestCase):
    """Verify every approved lifecycle path and representative illegal transitions."""

    def test_temporary_ticket_full_approve_borrow_return_path(self) -> None:
        """Temporary receipt and two-sided return require evidence and close in order."""

        ticket = _ticket()

        ticket.approve(CUSTODIAN_ID)
        ticket.confirm_received(BORROWER_ID, (EVIDENCE_ID,))
        ticket.mark_returned(BORROWER_ID, (OTHER_EVIDENCE_ID,))
        ticket.confirm_returned(CUSTODIAN_ID, (EVIDENCE_ID, OTHER_EVIDENCE_ID))

        self.assertEqual(ticket.status, TicketStatus.CLOSED)
        self.assertEqual(ticket.pickup_evidence_ids, (EVIDENCE_ID,))
        self.assertEqual(ticket.borrower_return_evidence_ids, (OTHER_EVIDENCE_ID,))
        self.assertEqual(
            ticket.custodian_return_evidence_ids,
            (EVIDENCE_ID, OTHER_EVIDENCE_ID),
        )

    def test_custody_transfer_closes_on_borrower_receipt(self) -> None:
        """Transfer has no return workflow and closes after approved receipt evidence."""

        ticket = _ticket(method=TicketMethod.CUSTODY_TRANSFER)
        ticket.approve(CUSTODIAN_ID)
        ticket.confirm_received(BORROWER_ID, (EVIDENCE_ID,))

        self.assertEqual(ticket.status, TicketStatus.CLOSED)
        with self.assertRaises(InvalidTicketTransitionError):
            ticket.mark_returned(BORROWER_ID, (OTHER_EVIDENCE_ID,))
        with self.assertRaises(InvalidTicketTransitionError):
            ticket.confirm_returned(CUSTODIAN_ID, (OTHER_EVIDENCE_ID,))

    def test_pending_ticket_can_be_rejected_or_cancelled_directly(self) -> None:
        """Pending rejection belongs to custodian; pending cancellation belongs to borrower."""

        rejected = _ticket()
        rejected.reject(CUSTODIAN_ID, "Not available")
        self.assertEqual(rejected.status, TicketStatus.REJECTED)

        cancelled = _ticket()
        cancelled.request_cancellation(BORROWER_ID, "No longer needed")
        self.assertEqual(cancelled.status, TicketStatus.CANCELLED)

    def test_approved_cancellation_can_be_accepted_or_rejected(self) -> None:
        """Custodian resolution either cancels or restores exactly APPROVED."""

        accepted = _ticket()
        accepted.approve(CUSTODIAN_ID)
        accepted.request_cancellation(BORROWER_ID, "Changed plan")
        accepted.resolve_cancellation(CUSTODIAN_ID, approve=True)
        self.assertEqual(accepted.status, TicketStatus.CANCELLED)

        restored = _ticket()
        restored.approve(CUSTODIAN_ID)
        restored.request_cancellation(BORROWER_ID, "Changed plan")
        restored.resolve_cancellation(CUSTODIAN_ID, approve=False)
        self.assertEqual(restored.status, TicketStatus.APPROVED)

    def test_wrong_actor_is_forbidden_for_each_side(self) -> None:
        """Borrower and source-custodian actions cannot be performed by the other side."""

        custodian_actions: tuple[Callable[[BorrowTicket], None], ...] = (
            lambda ticket: ticket.approve(BORROWER_ID),
            lambda ticket: ticket.reject(BORROWER_ID, "reason"),
            lambda ticket: ticket.resolve_cancellation(BORROWER_ID, approve=True),
            lambda ticket: ticket.confirm_returned(BORROWER_ID, (EVIDENCE_ID,)),
        )
        for action in custodian_actions:
            ticket = _ticket(status=TicketStatus.CANCEL_PENDING)
            with self.subTest(action=action), self.assertRaises(TicketActionForbiddenError):
                action(ticket)

        borrower_actions: tuple[Callable[[BorrowTicket], None], ...] = (
            lambda ticket: ticket.request_cancellation(CUSTODIAN_ID, "reason"),
            lambda ticket: ticket.confirm_received(CUSTODIAN_ID, (EVIDENCE_ID,)),
            lambda ticket: ticket.mark_returned(CUSTODIAN_ID, (EVIDENCE_ID,)),
        )
        for action in borrower_actions:
            ticket = _ticket(status=TicketStatus.APPROVED)
            with self.subTest(action=action), self.assertRaises(TicketActionForbiddenError):
                action(ticket)

    def test_invalid_status_transitions_are_rejected(self) -> None:
        """No public transition method permits generic or out-of-order state mutation."""

        cases: tuple[tuple[BorrowTicket, Callable[[BorrowTicket], None]], ...] = (
            (_ticket(status=TicketStatus.APPROVED), lambda item: item.approve(CUSTODIAN_ID)),
            (
                _ticket(status=TicketStatus.APPROVED),
                lambda item: item.reject(CUSTODIAN_ID, "reason"),
            ),
            (
                _ticket(status=TicketStatus.BORROWING),
                lambda item: item.request_cancellation(BORROWER_ID, "reason"),
            ),
            (
                _ticket(status=TicketStatus.PENDING),
                lambda item: item.resolve_cancellation(CUSTODIAN_ID, approve=True),
            ),
            (
                _ticket(status=TicketStatus.PENDING),
                lambda item: item.confirm_received(BORROWER_ID, (EVIDENCE_ID,)),
            ),
            (
                _ticket(status=TicketStatus.APPROVED),
                lambda item: item.mark_returned(BORROWER_ID, (EVIDENCE_ID,)),
            ),
            (
                _ticket(status=TicketStatus.BORROWING),
                lambda item: item.confirm_returned(CUSTODIAN_ID, (EVIDENCE_ID,)),
            ),
        )
        for ticket, action in cases:
            original_status = ticket.status
            with (
                self.subTest(status=original_status),
                self.assertRaises(InvalidTicketTransitionError),
            ):
                action(ticket)
            self.assertEqual(ticket.status, original_status)

    def test_blank_reasons_are_rejected_without_transition(self) -> None:
        """Reject and cancellation commands require meaningful audit reasons."""

        pending = _ticket()
        with self.assertRaises(InvalidTicketDataError):
            pending.reject(CUSTODIAN_ID, "  ")
        with self.assertRaises(InvalidTicketDataError):
            pending.request_cancellation(BORROWER_ID, "")
        self.assertEqual(pending.status, TicketStatus.PENDING)

    def test_evidence_count_and_uniqueness_are_enforced_before_transition(self) -> None:
        """Evidence-backed commands accept one-to-three distinct references only."""

        evidence_sets = (
            (),
            (EVIDENCE_ID, EVIDENCE_ID),
            (EVIDENCE_ID, OTHER_EVIDENCE_ID, CUSTODY_ID, OTHER_CUSTODY_ID),
        )
        for evidence_ids in evidence_sets:
            ticket = _ticket(status=TicketStatus.APPROVED)
            with (
                self.subTest(evidence_ids=evidence_ids),
                self.assertRaises(TicketEvidenceRequiredError),
            ):
                ticket.confirm_received(BORROWER_ID, evidence_ids)
            self.assertEqual(ticket.status, TicketStatus.APPROVED)

    def test_rehydration_rejects_invalid_items_and_evidence(self) -> None:
        """Persisted aggregates must satisfy due, item identity, and evidence invariants."""

        item = TicketItem(ASSET_ID, CUSTODY_ID, 1)
        with self.assertRaises(InvalidTicketDataError):
            TicketItem(ASSET_ID, CUSTODY_ID, 0)
        with self.assertRaises(InvalidTicketDataError):
            _ticket(items=())
        with self.assertRaises(InvalidTicketDataError):
            _ticket(items=(item, item))
        with self.assertRaises(TicketEvidenceRequiredError):
            _ticket(pickup_evidence_ids=(EVIDENCE_ID, EVIDENCE_ID))


def _allocation(
    custody_id: UUID,
    *,
    quantity: int = 1,
    asset_id: UUID = ASSET_ID,
    custodian_id: UUID = CUSTODIAN_ID,
    method: TicketMethod = TicketMethod.TEMPORARY,
    due_date: date | None = DUE_DATE,
) -> TicketAllocation:
    return TicketAllocation(
        asset_id=asset_id,
        source_custody_id=custody_id,
        source_custodian_user_id=custodian_id,
        quantity=quantity,
        method=method,
        due_date=due_date,
    )


def _ticket(
    *,
    method: TicketMethod = TicketMethod.TEMPORARY,
    status: TicketStatus = TicketStatus.PENDING,
    items: tuple[TicketItem, ...] | None = None,
    pickup_evidence_ids: tuple[UUID, ...] = (),
) -> BorrowTicket:
    due_date = DUE_DATE if method is TicketMethod.TEMPORARY else None
    group = TicketGroup(
        source_custodian_user_id=CUSTODIAN_ID,
        method=method,
        due_date=due_date,
        items=items if items is not None else (TicketItem(ASSET_ID, CUSTODY_ID, 1),),
    )
    ticket = BorrowTicket.create(
        ticket_id=TICKET_ID,
        borrower_user_id=BORROWER_ID,
        group=group,
    )
    ticket.status = status
    ticket.pickup_evidence_ids = pickup_evidence_ids
    ticket.__post_init__()
    return ticket
