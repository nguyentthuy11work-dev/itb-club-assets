"""Unit tests for F02.02 Asset and Custody domain invariants."""

from __future__ import annotations

import unittest
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from ams.domain.assets import (
    AllocationQuantityMismatchError,
    Asset,
    AssetCondition,
    Custody,
    CustodyAllocation,
    CustodyConfirmationForbiddenError,
    CustodyEvidenceRequiredError,
    CustodyStatus,
    InsufficientLendableQuantityError,
    InvalidAssetDataError,
    InvalidCustodyTransitionError,
    InvalidQuantityError,
)

ASSET_ID = UUID("00000000-0000-4000-8000-000000000201")
OTHER_ASSET_ID = UUID("00000000-0000-4000-8000-000000000202")
CATEGORY_ID = UUID("00000000-0000-4000-8000-000000000203")
TEAM_ID = UUID("00000000-0000-4000-8000-000000000204")
HR_ID = UUID("00000000-0000-4000-8000-000000000205")
CUSTODIAN_ID = UUID("00000000-0000-4000-8000-000000000206")
OTHER_USER_ID = UUID("00000000-0000-4000-8000-000000000207")
CUSTODY_ID = UUID("00000000-0000-4000-8000-000000000208")
EVIDENCE_ID = UUID("00000000-0000-4000-8000-000000000209")
OTHER_EVIDENCE_ID = UUID("00000000-0000-4000-8000-000000000210")
NOW = datetime(2026, 10, 4, 9, 0, tzinfo=UTC)


class AssetTests(unittest.TestCase):
    """Exercise Asset creation, allocation, and availability invariants."""

    def test_create_asset_starts_unavailable_while_custodies_are_pending(self) -> None:
        """Exact initial allocations create an Asset with no active lendable stock."""

        asset = self._asset(
            total=5,
            allocations=(
                CustodyAllocation(CUSTODIAN_ID, 2),
                CustodyAllocation(OTHER_USER_ID, 3),
            ),
        )

        self.assertEqual(asset.total_quantity, 5)
        self.assertEqual(asset.available_quantity, 0)
        self.assertEqual(asset.responsible_team_id, TEAM_ID)

    def test_new_asset_rejects_zero_negative_or_excessive_allocations(self) -> None:
        """Creation requires positive total and allocations within that total."""

        for quantity in (0, -1):
            with self.subTest(total=quantity), self.assertRaises(InvalidQuantityError):
                self._asset(total=quantity, allocations=())
        with self.assertRaises(InvalidQuantityError):
            CustodyAllocation(CUSTODIAN_ID, 0)
        for quantities in ((), (2, 4)):
            with (
                self.subTest(quantities=quantities),
                self.assertRaises(AllocationQuantityMismatchError),
            ):
                self._asset(
                    total=5,
                    allocations=tuple(
                        CustodyAllocation(UUID(int=index + 1), quantity)
                        for index, quantity in enumerate(quantities)
                    ),
                )

    def test_new_asset_rejects_duplicate_custodian_and_blank_data(self) -> None:
        """One initial allocation per actor and meaningful text are required."""

        with self.assertRaises(InvalidAssetDataError):
            self._asset(
                total=2,
                allocations=(
                    CustodyAllocation(CUSTODIAN_ID, 1),
                    CustodyAllocation(CUSTODIAN_ID, 1),
                ),
            )
        with self.assertRaises(InvalidAssetDataError):
            self._asset(
                total=1,
                allocations=(CustodyAllocation(CUSTODIAN_ID, 1),),
                name=" ",
            )

    def test_asset_rehydration_rejects_blank_source_negative_total_and_bad_time(self) -> None:
        """Persistence cannot bypass required text, quantity, or UTC invariants."""

        with self.assertRaises(InvalidAssetDataError):
            self._rehydrated_asset(source=" ")
        with self.assertRaises(InvalidQuantityError):
            self._rehydrated_asset(total=-1)
        with self.assertRaises(InvalidQuantityError):
            self._rehydrated_asset(total=1, available=2)
        with self.assertRaises(InvalidAssetDataError):
            self._rehydrated_asset(created_at=datetime(2026, 10, 4, 9, 0))

    def test_activated_custody_contributes_only_its_lendable_quantity(self) -> None:
        """Activation adds active on-hand quantity after reserve/loan partition."""

        asset = self._asset(
            total=5,
            allocations=(CustodyAllocation(CUSTODIAN_ID, 5),),
        )
        custody = self._active_custody(on_hand=5, reserved=1, loaned=2)

        asset.activate_custody(custody)

        self.assertEqual(custody.lendable_quantity, 2)
        self.assertEqual(asset.available_quantity, 2)
        self.assertEqual(asset.contributing_custody_ids, frozenset({CUSTODY_ID}))

    def test_asset_rejects_wrong_pending_or_overflowing_custody_activation(self) -> None:
        """Only a matching active Custody can safely affect availability."""

        asset = self._asset(
            total=2,
            allocations=(CustodyAllocation(CUSTODIAN_ID, 2),),
        )
        pending = self._pending_custody(quantity=2)
        with self.assertRaises(InvalidCustodyTransitionError):
            asset.activate_custody(pending)

        wrong_asset = self._active_custody(asset_id=OTHER_ASSET_ID)
        with self.assertRaises(InvalidAssetDataError):
            asset.activate_custody(wrong_asset)

        active = self._active_custody(on_hand=2)
        asset.activate_custody(active)
        with self.assertRaises(InvalidCustodyTransitionError):
            asset.activate_custody(active)

    def _asset(
        self,
        *,
        total: int,
        allocations: tuple[CustodyAllocation, ...],
        name: str = "Camera",
    ) -> Asset:
        return Asset.create(
            asset_id=ASSET_ID,
            category_id=CATEGORY_ID,
            responsible_team_id=TEAM_ID,
            name=name,
            source="DONATION",
            total_quantity=total,
            allocations=allocations,
            created_by_user_id=HR_ID,
            created_at=NOW,
        )

    def _pending_custody(self, quantity: int = 1) -> Custody:
        return Custody.create_pending(
            custody_id=CUSTODY_ID,
            asset_id=ASSET_ID,
            custodian_user_id=CUSTODIAN_ID,
            assigned_by_user_id=HR_ID,
            quantity=quantity,
            assigned_at=NOW,
        )

    def _rehydrated_asset(
        self,
        *,
        source: str = "DONATION",
        total: int = 1,
        available: int = 0,
        created_at: datetime = NOW,
    ) -> Asset:
        return Asset(
            id=ASSET_ID,
            category_id=CATEGORY_ID,
            responsible_team_id=None,
            name="Camera",
            source=source,
            total_quantity=total,
            available_quantity=available,
            created_by_user_id=HR_ID,
            created_at=created_at,
        )

    def _active_custody(
        self,
        *,
        asset_id: UUID = ASSET_ID,
        on_hand: int = 1,
        reserved: int = 0,
        loaned: int = 0,
    ) -> Custody:
        return Custody(
            id=CUSTODY_ID,
            asset_id=asset_id,
            custodian_user_id=CUSTODIAN_ID,
            assigned_by_user_id=HR_ID,
            status=CustodyStatus.ACTIVE,
            on_hand_quantity=on_hand,
            reserved_quantity=reserved,
            temporarily_loaned_quantity=loaned,
            condition=AssetCondition.GOOD,
            assigned_at=NOW,
            confirmed_at=NOW,
            confirmed_by_user_id=CUSTODIAN_ID,
            evidence_ids=(EVIDENCE_ID,),
        )


class CustodyTests(unittest.TestCase):
    """Exercise pending confirmation, actor, evidence, and partition rules."""

    def test_assignee_confirms_pending_custody_with_evidence(self) -> None:
        """Confirmation records actor/time/evidence and makes quantity lendable."""

        custody = self._pending()
        custody.confirm(
            actor_id=CUSTODIAN_ID,
            evidence_ids=(EVIDENCE_ID,),
            confirmed_at=NOW + timedelta(minutes=1),
        )

        self.assertEqual(custody.status, CustodyStatus.ACTIVE)
        self.assertEqual(custody.confirmed_by_user_id, CUSTODIAN_ID)
        self.assertEqual(custody.evidence_ids, (EVIDENCE_ID,))
        self.assertEqual(custody.lendable_quantity, 3)

    def test_pending_custody_is_not_lendable(self) -> None:
        """Assignment alone cannot expose inventory before physical receipt."""

        self.assertEqual(self._pending().lendable_quantity, 0)

    def test_reserve_checkout_release_and_return_preserve_partition(self) -> None:
        """Ticket quantity operations move only the intended Custody partition."""

        custody = self._active(quantity=5)
        custody.reserve(3)
        self.assertEqual((custody.reserved_quantity, custody.lendable_quantity), (3, 2))
        custody.release_reservation(1)
        self.assertEqual((custody.reserved_quantity, custody.lendable_quantity), (2, 3))
        custody.checkout_reserved(2)
        self.assertEqual(
            (custody.reserved_quantity, custody.temporarily_loaned_quantity), (0, 2)
        )
        custody.return_temporary_loan(2)
        self.assertEqual(
            (custody.temporarily_loaned_quantity, custody.lendable_quantity), (0, 5)
        )

    def test_reservation_rejects_inactive_insufficient_and_invalid_quantities(self) -> None:
        """Reservation and partition moves reject invalid state and bounds."""

        with self.assertRaises(InvalidCustodyTransitionError):
            self._pending().reserve(1)
        with self.assertRaises(InsufficientLendableQuantityError):
            self._active(quantity=2).reserve(3)

        operations: tuple[Callable[[Custody], None], ...] = (
            lambda custody: custody.reserve(0),
            lambda custody: custody.release_reservation(0),
            lambda custody: custody.checkout_reserved(0),
            lambda custody: custody.return_temporary_loan(0),
            lambda custody: custody.release_reservation(1),
            lambda custody: custody.checkout_reserved(1),
            lambda custody: custody.return_temporary_loan(1),
        )
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaises(InvalidQuantityError):
                operation(self._active(quantity=2))

    def test_asset_reserve_and_release_available_enforce_bounds(self) -> None:
        """Asset availability follows reservations and rejects invalid or overflowing moves."""

        asset = Asset(
            id=ASSET_ID,
            category_id=CATEGORY_ID,
            responsible_team_id=None,
            name="Camera",
            source="Purchase",
            total_quantity=3,
            available_quantity=3,
            created_by_user_id=HR_ID,
            created_at=NOW,
        )
        asset.reserve_available(2)
        self.assertEqual(asset.available_quantity, 1)
        asset.release_available(2)
        self.assertEqual(asset.available_quantity, 3)
        operations: tuple[Callable[[], None], ...] = (
            lambda: asset.reserve_available(0),
            lambda: asset.release_available(0),
            lambda: asset.reserve_available(4),
            lambda: asset.release_available(1),
        )
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaises(InvalidQuantityError):
                operation()

    def test_transfer_quantity_closes_empty_source_or_increases_active_target(self) -> None:
        """Confirmed transfer consumes reserve and can merge into recipient Custody."""

        source = self._active(quantity=2)
        source.reserve(2)
        source.transfer_out(2, transferred_at=NOW + timedelta(minutes=1))
        self.assertEqual(source.status, CustodyStatus.CLOSED)
        self.assertEqual(source.on_hand_quantity, 0)
        self.assertEqual(source.reserved_quantity, 0)

        target = self._active(quantity=1)
        target.receive_transfer(2)
        self.assertEqual(target.on_hand_quantity, 3)

        transferred = Custody.create_from_confirmed_transfer(
            custody_id=OTHER_EVIDENCE_ID,
            asset_id=ASSET_ID,
            custodian_user_id=OTHER_USER_ID,
            assigned_by_user_id=CUSTODIAN_ID,
            quantity=2,
            confirmed_at=NOW,
            evidence_ids=(EVIDENCE_ID,),
            condition=AssetCondition.GOOD,
        )
        self.assertEqual(transferred.status, CustodyStatus.ACTIVE)
        self.assertEqual(transferred.lendable_quantity, 2)

    def test_transfer_quantity_rejects_invalid_state_bounds_and_missing_evidence(self) -> None:
        """Transfer helpers preserve actor holdings and evidence-backed activation."""

        with self.assertRaises(InvalidCustodyTransitionError):
            self._pending().transfer_out(1, transferred_at=NOW)
        with self.assertRaises(InvalidQuantityError):
            self._active(quantity=1).transfer_out(1, transferred_at=NOW)
        with self.assertRaises(InvalidCustodyTransitionError):
            self._pending().receive_transfer(1)
        with self.assertRaises(InvalidQuantityError):
            self._active(quantity=1).receive_transfer(0)
        with self.assertRaises(CustodyEvidenceRequiredError):
            Custody.create_from_confirmed_transfer(
                custody_id=OTHER_EVIDENCE_ID,
                asset_id=ASSET_ID,
                custodian_user_id=OTHER_USER_ID,
                assigned_by_user_id=CUSTODIAN_ID,
                quantity=1,
                confirmed_at=NOW,
                evidence_ids=(),
                condition=AssetCondition.GOOD,
            )

    def test_wrong_actor_cannot_confirm(self) -> None:
        """Custodian authority is derived from this exact Custody assignment."""

        custody = self._pending()
        with self.assertRaises(CustodyConfirmationForbiddenError) as caught:
            custody.confirm(
                actor_id=OTHER_USER_ID,
                evidence_ids=(EVIDENCE_ID,),
                confirmed_at=NOW,
            )
        self.assertEqual(caught.exception.code, "CUSTODY_CONFIRMATION_FORBIDDEN")
        self.assertEqual(custody.status, CustodyStatus.PENDING)

    def test_confirmation_requires_one_to_three_distinct_evidence_references(self) -> None:
        """Missing, duplicate, or excessive evidence cannot activate Custody."""

        invalid_sets = (
            (),
            (EVIDENCE_ID, EVIDENCE_ID),
            (EVIDENCE_ID, OTHER_EVIDENCE_ID, UUID(int=1), UUID(int=2)),
        )
        for evidence_ids in invalid_sets:
            with (
                self.subTest(evidence_ids=evidence_ids),
                self.assertRaises(CustodyEvidenceRequiredError),
            ):
                self._pending().confirm(
                    actor_id=CUSTODIAN_ID,
                    evidence_ids=evidence_ids,
                    confirmed_at=NOW,
                )

    def test_duplicate_confirmation_is_rejected_without_state_change(self) -> None:
        """An active Custody cannot record confirmation twice."""

        custody = self._pending()
        custody.confirm(
            actor_id=CUSTODIAN_ID,
            evidence_ids=(EVIDENCE_ID,),
            confirmed_at=NOW,
        )
        with self.assertRaises(InvalidCustodyTransitionError):
            custody.confirm(
                actor_id=CUSTODIAN_ID,
                evidence_ids=(OTHER_EVIDENCE_ID,),
                confirmed_at=NOW + timedelta(minutes=1),
            )
        self.assertEqual(custody.evidence_ids, (EVIDENCE_ID,))

    def test_confirmation_cannot_precede_assignment_or_use_non_utc_time(self) -> None:
        """Business timestamps are ordered and normalized to UTC."""

        with self.assertRaises(InvalidCustodyTransitionError):
            self._pending().confirm(
                actor_id=CUSTODIAN_ID,
                evidence_ids=(EVIDENCE_ID,),
                confirmed_at=NOW - timedelta(seconds=1),
            )
        with self.assertRaises(InvalidAssetDataError):
            self._pending().confirm(
                actor_id=CUSTODIAN_ID,
                evidence_ids=(EVIDENCE_ID,),
                confirmed_at=datetime(2026, 10, 4, 9, 0),
            )

    def test_pending_creation_rejects_zero_and_negative_quantity(self) -> None:
        """A pending physical holding must contain positive quantity."""

        for quantity in (0, -1):
            with self.subTest(quantity=quantity), self.assertRaises(InvalidQuantityError):
                self._pending(quantity=quantity)

    def test_rehydration_rejects_every_invalid_quantity_partition(self) -> None:
        """Negative fields and reserved-plus-loaned overflow are always invalid."""

        partitions = ((-1, 0, 0), (1, -1, 0), (1, 0, -1), (2, 2, 1))
        for on_hand, reserved, loaned in partitions:
            with (
                self.subTest(partition=(on_hand, reserved, loaned)),
                self.assertRaises(InvalidQuantityError),
            ):
                Custody(
                    id=CUSTODY_ID,
                    asset_id=ASSET_ID,
                    custodian_user_id=CUSTODIAN_ID,
                    assigned_by_user_id=HR_ID,
                    status=CustodyStatus.PENDING,
                    on_hand_quantity=on_hand,
                    reserved_quantity=reserved,
                    temporarily_loaned_quantity=loaned,
                    condition=AssetCondition.GOOD,
                    assigned_at=NOW,
                )

    def test_rehydration_rejects_state_timestamp_and_evidence_mismatch(self) -> None:
        """Persistence cannot construct lifecycle states missing their evidence trail."""

        with self.assertRaises(InvalidCustodyTransitionError):
            Custody(
                id=CUSTODY_ID,
                asset_id=ASSET_ID,
                custodian_user_id=CUSTODIAN_ID,
                assigned_by_user_id=HR_ID,
                status=CustodyStatus.ACTIVE,
                on_hand_quantity=1,
                reserved_quantity=0,
                temporarily_loaned_quantity=0,
                condition=AssetCondition.GOOD,
                assigned_at=NOW,
            )

    def test_rehydration_validates_confirmation_and_closure_history(self) -> None:
        """Actor pairs and ordered UTC timestamps protect active/closed history."""

        with self.assertRaises(InvalidCustodyTransitionError):
            self._rehydrated_custody(
                status=CustodyStatus.ACTIVE,
                confirmed_at=NOW,
            )
        with self.assertRaises(InvalidCustodyTransitionError):
            self._rehydrated_custody(
                status=CustodyStatus.ACTIVE,
                confirmed_at=NOW - timedelta(seconds=1),
                confirmed_by_user_id=CUSTODIAN_ID,
            )
        with self.assertRaises(InvalidCustodyTransitionError):
            self._rehydrated_custody(
                status=CustodyStatus.CLOSED,
                confirmed_at=NOW,
                confirmed_by_user_id=CUSTODIAN_ID,
                closed_at=NOW - timedelta(seconds=1),
            )
        with self.assertRaises(InvalidAssetDataError):
            self._rehydrated_custody(
                status=CustodyStatus.CLOSED,
                confirmed_at=NOW,
                confirmed_by_user_id=CUSTODIAN_ID,
                closed_at=datetime(2026, 10, 4, 10, 0),
            )

        closed = self._rehydrated_custody(
            status=CustodyStatus.CLOSED,
            confirmed_at=NOW,
            confirmed_by_user_id=CUSTODIAN_ID,
            closed_at=NOW + timedelta(hours=1),
        )
        self.assertEqual(closed.lendable_quantity, 0)

    def _pending(self, quantity: int = 3) -> Custody:
        return Custody.create_pending(
            custody_id=CUSTODY_ID,
            asset_id=ASSET_ID,
            custodian_user_id=CUSTODIAN_ID,
            assigned_by_user_id=HR_ID,
            quantity=quantity,
            assigned_at=NOW,
        )

    def _active(self, quantity: int) -> Custody:
        custody = self._pending(quantity)
        custody.confirm(
            actor_id=CUSTODIAN_ID,
            evidence_ids=(EVIDENCE_ID,),
            confirmed_at=NOW,
        )
        return custody

    def _rehydrated_custody(
        self,
        *,
        status: CustodyStatus,
        confirmed_at: datetime | None,
        confirmed_by_user_id: UUID | None = None,
        closed_at: datetime | None = None,
    ) -> Custody:
        return Custody(
            id=CUSTODY_ID,
            asset_id=ASSET_ID,
            custodian_user_id=CUSTODIAN_ID,
            assigned_by_user_id=HR_ID,
            status=status,
            on_hand_quantity=1,
            reserved_quantity=0,
            temporarily_loaned_quantity=0,
            condition=AssetCondition.GOOD,
            assigned_at=NOW,
            confirmed_at=confirmed_at,
            confirmed_by_user_id=confirmed_by_user_id,
            closed_at=closed_at,
            evidence_ids=(EVIDENCE_ID,),
        )
