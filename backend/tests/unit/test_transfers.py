"""Unit proofs for F10 two-party Transfer state and orchestration."""

from __future__ import annotations

import unittest
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import cast
from uuid import UUID

from ams.application.evidence import EvidenceFileInput, EvidenceStorageService
from ams.application.idempotency import (
    IdempotentCommandResult,
    IdempotentOperation,
    IIdempotentCommandExecutor,
)
from ams.application.reliability import IClock, OutboxEvent, StagedFile
from ams.application.transfers import (
    ConfirmTransferMetadata,
    CreateTransferCommand,
    TransferService,
)
from ams.application.unit_of_work import IUnitOfWork
from ams.domain.assets import Asset, AssetCondition, Custody, CustodyEvidence, CustodyStatus
from ams.domain.authentication import UserIdentity
from ams.domain.identity import CurrentActor, OperationalRole
from ams.domain.transfers import (
    CustodyTransfer,
    InvalidTransferTransitionError,
    ReconciliationTransferReference,
    TransferConfirmationForbiddenError,
    TransferDataError,
    TransferEvidence,
    TransferEvidenceSide,
    TransferNotFoundError,
    TransferQuantityError,
    TransferRoleRequiredError,
    TransferStatus,
)

NOW = datetime(2030, 2, 1, 3, 0, tzinfo=UTC)
TRANSFER_ID = UUID("00000000-0000-4000-8000-000000010001")
ASSET_ID = UUID("00000000-0000-4000-8000-000000010002")
SOURCE_ID = UUID("00000000-0000-4000-8000-000000010003")
TARGET_CUSTODY_ID = UUID("00000000-0000-4000-8000-000000010004")
CURRENT_ID = UUID("00000000-0000-4000-8000-000000010005")
TARGET_ID = UUID("00000000-0000-4000-8000-000000010006")
HR_ID = UUID("00000000-0000-4000-8000-000000010007")
RECON_ITEM_ID = UUID("00000000-0000-4000-8000-000000010008")


class TransferDomainTests(unittest.TestCase):
    """Protect either-order confirmations and exactly-once completion."""

    def test_current_then_target_completes_only_after_both(self) -> None:
        transfer = _transfer()
        self.assertFalse(
            transfer.confirm_current(
                actor_id=CURRENT_ID,
                evidence_ids=(UUID(int=1),),
                confirmed_at=NOW,
            )
        )
        self.assertEqual(transfer.status, TransferStatus.PENDING_TARGET_CONFIRMATION)
        self.assertTrue(
            transfer.confirm_target(
                actor_id=TARGET_ID,
                evidence_ids=(UUID(int=2),),
                confirmed_at=NOW,
            )
        )
        transfer.mark_custody_completed(TARGET_CUSTODY_ID)
        self.assertEqual(transfer.status, TransferStatus.COMPLETED)

    def test_target_then_current_is_equally_valid(self) -> None:
        transfer = _transfer()
        self.assertFalse(
            transfer.confirm_target(
                actor_id=TARGET_ID,
                evidence_ids=(UUID(int=1),),
                confirmed_at=NOW,
            )
        )
        self.assertEqual(transfer.status, TransferStatus.PENDING_CURRENT_CONFIRMATION)
        self.assertTrue(
            transfer.confirm_current(
                actor_id=CURRENT_ID,
                evidence_ids=(UUID(int=2),),
                confirmed_at=NOW,
            )
        )
        transfer.mark_custody_completed(TARGET_CUSTODY_ID)
        self.assertEqual(transfer.target_custody_id, TARGET_CUSTODY_ID)

    def test_wrong_actor_duplicate_evidence_and_completion_guards(self) -> None:
        transfer = _transfer()
        with self.assertRaises(TransferConfirmationForbiddenError):
            transfer.confirm_current(
                actor_id=TARGET_ID, evidence_ids=(UUID(int=1),), confirmed_at=NOW
            )
        with self.assertRaises(TransferConfirmationForbiddenError):
            transfer.confirm_target(
                actor_id=CURRENT_ID, evidence_ids=(UUID(int=1),), confirmed_at=NOW
            )
        with self.assertRaises(TransferDataError):
            transfer.confirm_current(actor_id=CURRENT_ID, evidence_ids=(), confirmed_at=NOW)
        with self.assertRaises(InvalidTransferTransitionError):
            transfer.mark_custody_completed(TARGET_CUSTODY_ID)
        transfer.confirm_current(
            actor_id=CURRENT_ID, evidence_ids=(UUID(int=1),), confirmed_at=NOW
        )
        with self.assertRaises(InvalidTransferTransitionError):
            transfer.confirm_current(
                actor_id=CURRENT_ID, evidence_ids=(UUID(int=2),), confirmed_at=NOW
            )
        transfer.confirm_target(
            actor_id=TARGET_ID, evidence_ids=(UUID(int=3),), confirmed_at=NOW
        )
        transfer.mark_custody_completed(TARGET_CUSTODY_ID)
        with self.assertRaises(InvalidTransferTransitionError):
            transfer.mark_custody_completed(UUID(int=4))

    def test_duplicate_target_and_pre_creation_confirmations_are_rejected(self) -> None:
        transfer = _transfer()
        with self.assertRaises(InvalidTransferTransitionError):
            transfer.confirm_current(
                actor_id=CURRENT_ID,
                evidence_ids=(UUID(int=1),),
                confirmed_at=NOW - timedelta(seconds=1),
            )
        with self.assertRaises(InvalidTransferTransitionError):
            transfer.confirm_target(
                actor_id=TARGET_ID,
                evidence_ids=(UUID(int=1),),
                confirmed_at=NOW - timedelta(seconds=1),
            )
        transfer.confirm_target(
            actor_id=TARGET_ID, evidence_ids=(UUID(int=2),), confirmed_at=NOW
        )
        with self.assertRaises(InvalidTransferTransitionError):
            transfer.confirm_target(
                actor_id=TARGET_ID, evidence_ids=(UUID(int=3),), confirmed_at=NOW
            )

    def test_invalid_identity_quantity_time_and_rehydrated_states_are_rejected(self) -> None:
        with self.assertRaises(TransferDataError):
            _transfer(target_custodian_user_id=CURRENT_ID)
        with self.assertRaises(TransferQuantityError):
            _transfer(quantity=0)
        with self.assertRaises(TransferDataError):
            _transfer(created_at=NOW.replace(tzinfo=None))
        with self.assertRaises(TransferDataError):
            _transfer(
                status=TransferStatus.PENDING_TARGET_CONFIRMATION,
                current_confirmed_by_user_id=CURRENT_ID,
            )
        with self.assertRaises(TransferDataError):
            _transfer(status=TransferStatus.PENDING_TARGET_CONFIRMATION)
        with self.assertRaises(TransferDataError):
            _transfer(completed_at=NOW)
        with self.assertRaises(TransferDataError):
            _transfer(
                status=TransferStatus.COMPLETED,
                current_confirmed_by_user_id=CURRENT_ID,
                current_confirmed_at=NOW,
                target_confirmed_by_user_id=TARGET_ID,
                target_confirmed_at=NOW,
            )


class FixedClock(IClock):
    """Return the deterministic Transfer test instant."""

    def now(self) -> datetime:
        """Return the fixed UTC time."""

        return NOW


class FakeTransferRepository:
    """Store one Transfer and side evidence in memory."""

    def __init__(self, source: Custody, target: Custody | None = None) -> None:
        self.transfer: CustodyTransfer | None = None
        self.source = source
        self.target = target
        self.evidence: list[TransferEvidence] = []
        self.reference: ReconciliationTransferReference | None = None

    async def add(self, transfer: CustodyTransfer) -> None:
        self.transfer = transfer

    async def get_for_update(self, transfer_id: UUID) -> CustodyTransfer | None:
        return self.transfer if self.transfer and self.transfer.id == transfer_id else None

    async def list_visible(
        self,
        actor_id: UUID,
        *,
        include_all: bool,
        status: TransferStatus | None,
        offset: int,
        limit: int,
    ) -> tuple[tuple[CustodyTransfer, ...], int]:
        values: tuple[CustodyTransfer, ...] = () if self.transfer is None else (self.transfer,)
        if not include_all:
            values = tuple(
                value
                for value in values
                if actor_id
                in {
                    value.current_custodian_user_id,
                    value.target_custodian_user_id,
                }
            )
        if status is not None:
            values = tuple(value for value in values if value.status is status)
        return values[offset : offset + limit], len(values)

    async def save(self, transfer: CustodyTransfer, *, updated_at: datetime) -> None:
        self.transfer = transfer

    async def add_evidence(self, evidence: TransferEvidence) -> None:
        self.evidence.append(evidence)

    async def list_evidence(
        self, transfer_id: UUID, side: TransferEvidenceSide
    ) -> tuple[TransferEvidence, ...]:
        return tuple(
            value
            for value in self.evidence
            if value.transfer_id == transfer_id and value.side is side
        )

    async def lock_completion_custodies(
        self, transfer: CustodyTransfer
    ) -> tuple[Custody, Custody | None]:
        return self.source, self.target

    async def get_reconciliation_reference(
        self, item_id: UUID
    ) -> ReconciliationTransferReference | None:
        return self.reference if self.reference and self.reference.item_id == item_id else None


class FakeAssets:
    """Lock and persist one Asset."""

    def __init__(self, asset: Asset | None) -> None:
        self.asset = asset

    async def get_for_update(self, asset_id: UUID) -> Asset | None:
        return self.asset if self.asset is not None and self.asset.id == asset_id else None

    async def save(self, asset: Asset) -> None:
        self.asset = asset


class FakeCustodies:
    """Lock and persist source/target Custodies."""

    def __init__(self, repository: FakeTransferRepository) -> None:
        self.repository = repository
        self.added: list[Custody] = []

    async def get_for_update(self, custody_id: UUID) -> Custody | None:
        return self.repository.source if self.repository.source.id == custody_id else None

    async def save(self, custody: Custody) -> None:
        if custody.id == self.repository.source.id:
            self.repository.source = custody
        else:
            self.repository.target = custody

    async def add(self, custody: Custody) -> None:
        self.added.append(custody)
        self.repository.target = custody


class FakeCustodyEvidence:
    """Collect target Custody evidence metadata."""

    def __init__(self) -> None:
        self.values: list[CustodyEvidence] = []

    async def add(self, evidence: CustodyEvidence) -> None:
        self.values.append(evidence)


class FakeUsers:
    """Expose deterministic active Transfer actors."""

    def __init__(self) -> None:
        self.missing: set[UUID] = set()

    async def get_identity(self, user_id: UUID) -> UserIdentity | None:
        if user_id in self.missing:
            return None
        return UserIdentity(
            user_id,
            f"user-{user_id.int}",
            f"{user_id.int}@example.test",
            "User",
            frozenset(),
            frozenset(),
            True,
        )


class FakeOutbox:
    """Collect Transfer notification/completion events."""

    def __init__(self) -> None:
        self.events: list[OutboxEvent] = []

    async def add(self, event: OutboxEvent) -> None:
        self.events.append(event)


class FakeUnitOfWork:
    """Expose F10 ports with no-op transaction methods."""

    def __init__(self, repository: FakeTransferRepository) -> None:
        self.transfers = repository
        self.assets = FakeAssets(_asset())
        self.custodies = FakeCustodies(repository)
        self.custody_evidence = FakeCustodyEvidence()
        self.users = FakeUsers()
        self.outbox = FakeOutbox()

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class FakeExecutor(IIdempotentCommandExecutor):
    """Execute operations against one fake Unit of Work."""

    def __init__(self, unit_of_work: FakeUnitOfWork) -> None:
        self.unit_of_work = unit_of_work
        self.replayed = False

    async def execute(
        self,
        *,
        actor_id: UUID,
        command_name: str,
        idempotency_key: str,
        request_fingerprint: str,
        operation: IdempotentOperation,
    ) -> IdempotentCommandResult:
        response = await operation(cast(IUnitOfWork, self.unit_of_work))
        return IdempotentCommandResult(response.status_code, response.body, self.replayed)


class FakeEvidenceStorage:
    """Provide deterministic evidence staging and private keys."""

    def __init__(self) -> None:
        self.staged = (StagedFile("staged", "image/jpeg", "a" * 64, 5),)
        self.discarded = False
        self.deleted: tuple[str, ...] = ()

    async def stage_images(self, files: object) -> tuple[StagedFile, ...]:
        return self.staged

    async def finalize(
        self, files: tuple[StagedFile, ...], *, owner_type: str, owner_id: UUID
    ) -> tuple[str, ...]:
        return (f"{owner_type}/{owner_id}/{_next_uuid()}",)

    async def discard(self, files: tuple[StagedFile, ...]) -> None:
        self.discarded = True

    async def delete_objects(self, object_keys: tuple[str, ...]) -> None:
        self.deleted = object_keys


class TransferServiceTests(unittest.IsolatedAsyncioTestCase):
    """Verify reservation, authorization, evidence, completion, and notifications."""

    async def test_create_target_first_then_current_completes_new_custody_once(self) -> None:
        repository = FakeTransferRepository(_source())
        repository.reference = ReconciliationTransferReference(RECON_ITEM_ID, SOURCE_ID, True)
        service, unit_of_work, _, _ = _service(repository)
        created = await service.create(
            CreateTransferCommand(SOURCE_ID, TARGET_ID, 2, RECON_ITEM_ID),
            _actor(HR_ID, OperationalRole.HR),
            "create",
        )
        self.assertEqual(created.transfer.status, TransferStatus.PENDING_BOTH_CONFIRMATIONS)
        self.assertEqual(repository.source.reserved_quantity, 2)
        assert unit_of_work.assets.asset is not None
        self.assertEqual(unit_of_work.assets.asset.available_quantity, 3)
        self.assertEqual(len(unit_of_work.outbox.events), 1)

        target = await service.confirm_target(
            created.transfer.id,
            ConfirmTransferMetadata("received"),
            (_file(),),
            _actor(TARGET_ID),
            "target",
        )
        self.assertEqual(target.transfer.status, TransferStatus.PENDING_CURRENT_CONFIRMATION)
        self.assertEqual(repository.source.on_hand_quantity, 5)

        completed = await service.confirm_current(
            created.transfer.id,
            ConfirmTransferMetadata("released"),
            (_file(),),
            _actor(CURRENT_ID),
            "current",
        )
        self.assertEqual(completed.transfer.status, TransferStatus.COMPLETED)
        self.assertEqual(repository.source.on_hand_quantity, 3)
        self.assertEqual(repository.source.reserved_quantity, 0)
        self.assertIsNotNone(repository.target)
        assert repository.target is not None
        self.assertEqual(repository.target.on_hand_quantity, 2)
        assert unit_of_work.assets.asset is not None
        self.assertEqual(unit_of_work.assets.asset.available_quantity, 5)
        self.assertEqual(len(unit_of_work.custody_evidence.values), 1)
        self.assertEqual(
            unit_of_work.custody_evidence.values[0].evidence_type, "TRANSFER_CONFIRMATION"
        )
        self.assertEqual(
            [event.event_type for event in unit_of_work.outbox.events],
            ["CUSTODY_TRANSFER_CREATED", "CUSTODY_TRANSFER_COMPLETED"],
        )

    async def test_current_first_merges_existing_target_and_list_scopes(self) -> None:
        repository = FakeTransferRepository(_source(), _target())
        service, _, executor, storage = _service(repository)
        created = await service.create(
            CreateTransferCommand(SOURCE_ID, TARGET_ID, 1),
            _actor(HR_ID, OperationalRole.HR),
            "create",
        )
        await service.confirm_current(
            created.transfer.id,
            ConfirmTransferMetadata(),
            (_file(),),
            _actor(CURRENT_ID),
            "current",
        )
        executor.replayed = True
        completed = await service.confirm_target(
            created.transfer.id,
            ConfirmTransferMetadata(),
            (_file(),),
            _actor(TARGET_ID),
            "target",
        )
        self.assertTrue(completed.replayed)
        self.assertTrue(storage.discarded)
        self.assertIsNotNone(repository.target)
        assert repository.target is not None
        self.assertEqual(repository.target.on_hand_quantity, 2)
        self.assertEqual((await service.list(_actor(CURRENT_ID))).total, 1)
        self.assertEqual((await service.list(_actor(HR_ID, OperationalRole.HR))).total, 1)
        self.assertEqual((await service.list(_actor(UUID(int=404)))).total, 0)

    async def test_full_lendable_quantity_closes_source_at_completion(self) -> None:
        repository = FakeTransferRepository(_source())
        service, _, _, _ = _service(repository)
        created = await service.create(
            CreateTransferCommand(SOURCE_ID, TARGET_ID, 5),
            _actor(HR_ID, OperationalRole.HR),
            "create-full-balance",
        )
        await service.confirm_current(
            created.transfer.id,
            ConfirmTransferMetadata(),
            (_file(),),
            _actor(CURRENT_ID),
            "current-full-balance",
        )
        await service.confirm_target(
            created.transfer.id,
            ConfirmTransferMetadata(),
            (_file(),),
            _actor(TARGET_ID),
            "target-full-balance",
        )
        self.assertEqual(repository.source.status, CustodyStatus.CLOSED)
        self.assertEqual(repository.source.on_hand_quantity, 0)
        self.assertEqual(repository.source.reserved_quantity, 0)

    async def test_create_and_confirmation_failure_paths_are_safe(self) -> None:
        repository = FakeTransferRepository(_source())
        service, unit_of_work, _, storage = _service(repository)
        with self.assertRaises(TransferRoleRequiredError):
            await service.create(
                CreateTransferCommand(SOURCE_ID, TARGET_ID, 1),
                _actor(HR_ID),
                "role",
            )
        with self.assertRaises(TransferNotFoundError):
            await service.create(
                CreateTransferCommand(UUID(int=404), TARGET_ID, 1),
                _actor(HR_ID, OperationalRole.HR),
                "source",
            )
        unit_of_work.users.missing.add(TARGET_ID)
        with self.assertRaises(TransferNotFoundError):
            await service.create(
                CreateTransferCommand(SOURCE_ID, TARGET_ID, 1),
                _actor(HR_ID, OperationalRole.HR),
                "target",
            )
        unit_of_work.users.missing.clear()
        with self.assertRaises(TransferQuantityError):
            await service.create(
                CreateTransferCommand(SOURCE_ID, TARGET_ID, 99),
                _actor(HR_ID, OperationalRole.HR),
                "quantity",
            )
        with self.assertRaises(TransferNotFoundError):
            await service.create(
                CreateTransferCommand(SOURCE_ID, TARGET_ID, 1, RECON_ITEM_ID),
                _actor(HR_ID, OperationalRole.HR),
                "recon",
            )
        with self.assertRaises(TransferNotFoundError):
            await service.confirm_current(
                TRANSFER_ID,
                ConfirmTransferMetadata(),
                (_file(),),
                _actor(CURRENT_ID),
                "missing-transfer",
            )
        self.assertTrue(storage.discarded)
        with self.assertRaises(TransferRoleRequiredError):
            await service.list(CurrentActor(CURRENT_ID, frozenset(), is_active=False))

    async def test_remaining_creation_and_completion_guards(self) -> None:
        repository = FakeTransferRepository(_source())
        service, unit_of_work, _, _ = _service(repository)
        with self.assertRaises(TransferDataError):
            await service.create(
                CreateTransferCommand(SOURCE_ID, CURRENT_ID, 1),
                _actor(HR_ID, OperationalRole.HR),
                "same-user",
            )
        unit_of_work.assets.asset = None
        with self.assertRaises(TransferNotFoundError):
            await service.create(
                CreateTransferCommand(SOURCE_ID, TARGET_ID, 1),
                _actor(HR_ID, OperationalRole.HR),
                "missing-asset",
            )

        repository = FakeTransferRepository(_source())
        service, unit_of_work, _, _ = _service(repository)
        unit_of_work.users.missing.add(CURRENT_ID)
        with self.assertRaises(TransferNotFoundError):
            await service.create(
                CreateTransferCommand(SOURCE_ID, TARGET_ID, 1),
                _actor(HR_ID, OperationalRole.HR),
                "missing-current",
            )

        repository = FakeTransferRepository(_source())
        service, _, _, _ = _service(repository)
        created = await service.create(
            CreateTransferCommand(SOURCE_ID, TARGET_ID, 1),
            _actor(HR_ID, OperationalRole.HR),
            "create-inactive",
        )
        with self.assertRaises(TransferRoleRequiredError):
            await service.confirm_current(
                created.transfer.id,
                ConfirmTransferMetadata(),
                (_file(),),
                CurrentActor(CURRENT_ID, frozenset(), is_active=False),
                "inactive",
            )

        repository = FakeTransferRepository(_source())
        service, unit_of_work, _, _ = _service(repository)
        created = await service.create(
            CreateTransferCommand(SOURCE_ID, TARGET_ID, 1),
            _actor(HR_ID, OperationalRole.HR),
            "create-missing-completion-asset",
        )
        await service.confirm_target(
            created.transfer.id,
            ConfirmTransferMetadata(),
            (_file(),),
            _actor(TARGET_ID),
            "target-before-missing-asset",
        )
        unit_of_work.assets.asset = None
        with self.assertRaises(TransferNotFoundError):
            await service.confirm_current(
                created.transfer.id,
                ConfirmTransferMetadata(),
                (_file(),),
                _actor(CURRENT_ID),
                "missing-completion-asset",
            )

    async def test_completion_rejects_lost_reservation_evidence_and_pending_target(
        self,
    ) -> None:
        repository = FakeTransferRepository(_source())
        service, _, _, _ = _service(repository)
        created = await service.create(
            CreateTransferCommand(SOURCE_ID, TARGET_ID, 1),
            _actor(HR_ID, OperationalRole.HR),
            "create-lost-reservation",
        )
        await service.confirm_target(
            created.transfer.id,
            ConfirmTransferMetadata(),
            (_file(),),
            _actor(TARGET_ID),
            "target-lost-reservation",
        )
        repository.source.reserved_quantity = 0
        with self.assertRaises(TransferQuantityError):
            await service.confirm_current(
                created.transfer.id,
                ConfirmTransferMetadata(),
                (_file(),),
                _actor(CURRENT_ID),
                "lost-reservation",
            )

        repository = FakeTransferRepository(_source())
        service, _, _, _ = _service(repository)
        created = await service.create(
            CreateTransferCommand(SOURCE_ID, TARGET_ID, 1),
            _actor(HR_ID, OperationalRole.HR),
            "create-lost-evidence",
        )
        await service.confirm_target(
            created.transfer.id,
            ConfirmTransferMetadata(),
            (_file(),),
            _actor(TARGET_ID),
            "target-lost-evidence",
        )
        repository.evidence.clear()
        with self.assertRaises(TransferDataError):
            await service.confirm_current(
                created.transfer.id,
                ConfirmTransferMetadata(),
                (_file(),),
                _actor(CURRENT_ID),
                "lost-evidence",
            )

        pending_target = Custody.create_pending(
            custody_id=TARGET_CUSTODY_ID,
            asset_id=ASSET_ID,
            custodian_user_id=TARGET_ID,
            assigned_by_user_id=HR_ID,
            quantity=1,
            assigned_at=NOW,
        )
        repository = FakeTransferRepository(_source(), pending_target)
        service, _, _, _ = _service(repository)
        created = await service.create(
            CreateTransferCommand(SOURCE_ID, TARGET_ID, 1),
            _actor(HR_ID, OperationalRole.HR),
            "create-pending-target",
        )
        await service.confirm_target(
            created.transfer.id,
            ConfirmTransferMetadata(),
            (_file(),),
            _actor(TARGET_ID),
            "target-pending-target",
        )
        with self.assertRaises(TransferDataError):
            await service.confirm_current(
                created.transfer.id,
                ConfirmTransferMetadata(),
                (_file(),),
                _actor(CURRENT_ID),
                "pending-target",
            )


def _transfer(**changes: object) -> CustodyTransfer:
    values: dict[str, object] = {
        "id": TRANSFER_ID,
        "source_custody_id": SOURCE_ID,
        "asset_id": ASSET_ID,
        "current_custodian_user_id": CURRENT_ID,
        "target_custodian_user_id": TARGET_ID,
        "quantity": 2,
        "condition_snapshot": AssetCondition.GOOD,
        "created_by_user_id": HR_ID,
        "created_at": NOW,
    }
    values.update(changes)
    return CustodyTransfer(**values)  # type: ignore[arg-type]


def _asset() -> Asset:
    return Asset(
        ASSET_ID,
        UUID(int=101),
        None,
        "Camera",
        "PURCHASE",
        5,
        5,
        HR_ID,
        NOW,
        contributing_custody_ids=frozenset({SOURCE_ID}),
    )


def _source() -> Custody:
    return Custody(
        SOURCE_ID,
        ASSET_ID,
        CURRENT_ID,
        HR_ID,
        CustodyStatus.ACTIVE,
        5,
        0,
        0,
        AssetCondition.GOOD,
        NOW,
        confirmed_at=NOW,
        confirmed_by_user_id=CURRENT_ID,
        evidence_ids=(UUID(int=102),),
    )


def _target() -> Custody:
    return Custody(
        TARGET_CUSTODY_ID,
        ASSET_ID,
        TARGET_ID,
        HR_ID,
        CustodyStatus.ACTIVE,
        1,
        0,
        0,
        AssetCondition.GOOD,
        NOW,
        confirmed_at=NOW,
        confirmed_by_user_id=TARGET_ID,
        evidence_ids=(UUID(int=103),),
    )


def _actor(user_id: UUID, *roles: OperationalRole) -> CurrentActor:
    return CurrentActor(user_id, frozenset(roles))


class _StorageBundle:
    def __init__(self, repository: FakeTransferRepository) -> None:
        self.unit_of_work = FakeUnitOfWork(repository)
        self.executor = FakeExecutor(self.unit_of_work)
        self.storage = FakeEvidenceStorage()


def _service(
    repository: FakeTransferRepository,
) -> tuple[TransferService, FakeUnitOfWork, FakeExecutor, FakeEvidenceStorage]:
    bundle = _StorageBundle(repository)
    service = TransferService(
        bundle.executor,
        lambda: cast(IUnitOfWork, bundle.unit_of_work),
        cast(EvidenceStorageService, bundle.storage),
        FixedClock(),
        id_factory=_next_uuid,
    )
    return service, bundle.unit_of_work, bundle.executor, bundle.storage


_id_value = 10500


def _next_uuid() -> UUID:
    global _id_value
    _id_value += 1
    return UUID(int=_id_value)


async def _content() -> AsyncIterator[bytes]:
    yield b"image"


def _file() -> EvidenceFileInput:
    return EvidenceFileInput(_content(), "image/jpeg")
