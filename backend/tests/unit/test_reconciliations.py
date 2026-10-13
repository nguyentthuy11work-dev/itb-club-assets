"""Unit proofs for F09 reconciliation state and close-gate invariants."""

from __future__ import annotations

import unittest
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from types import TracebackType
from typing import cast
from uuid import UUID

from ams.application.evidence import EvidenceFileInput, EvidenceStorageService
from ams.application.idempotency import (
    IdempotentCommandResult,
    IdempotentOperation,
    IIdempotentCommandExecutor,
)
from ams.application.reconciliations import (
    ConfirmReconciliationMetadata,
    ReconciliationService,
)
from ams.application.reliability import IClock, OutboxEvent, StagedFile
from ams.application.unit_of_work import IUnitOfWork
from ams.domain.assets import AssetCondition
from ams.domain.authentication import UserIdentity
from ams.domain.identity import CurrentActor, OperationalRole
from ams.domain.reconciliations import (
    CustodianScopeRequiredError,
    CustodySnapshotCandidate,
    InvalidReconciliationDateError,
    InvalidReconciliationTransitionError,
    ReconciliationCloseBlockedError,
    ReconciliationDataError,
    ReconciliationForbiddenError,
    ReconciliationItem,
    ReconciliationItemStatus,
    ReconciliationNotFoundError,
    ReconciliationSession,
    ReconciliationStatus,
    TemporaryLoanReturnTarget,
)

NOW = datetime(2030, 1, 2, 3, 0, tzinfo=UTC)
SESSION_ID = UUID("00000000-0000-4000-8000-000000009001")
ITEM_ID = UUID("00000000-0000-4000-8000-000000009002")
CUSTODY_ID = UUID("00000000-0000-4000-8000-000000009003")
ASSET_ID = UUID("00000000-0000-4000-8000-000000009004")
CUSTODIAN_ID = UUID("00000000-0000-4000-8000-000000009005")
HR_ID = UUID("00000000-0000-4000-8000-000000009006")


class ReconciliationDomainTests(unittest.TestCase):
    """Protect snapshot, confirmation, discrepancy, resolution, and closure rules."""

    def test_matching_confirmation_is_terminal_confirmed(self) -> None:
        item = _item()
        item.confirm(
            actor_id=CUSTODIAN_ID,
            actual_quantity=2,
            actual_condition=AssetCondition.GOOD,
            condition_note="Counted",
            discrepancy_reason=None,
            evidence_ids=(UUID(int=1),),
            confirmed_at=NOW,
        )
        self.assertEqual(item.status, ReconciliationItemStatus.CONFIRMED)
        self.assertEqual(item.condition_note, "Counted")

    def test_quantity_or_condition_mismatch_requires_reason_then_resolves(self) -> None:
        for quantity, condition in (
            (1, AssetCondition.GOOD),
            (2, AssetCondition.DAMAGED),
        ):
            item = _item()
            with self.assertRaises(ReconciliationDataError):
                item.confirm(
                    actor_id=CUSTODIAN_ID,
                    actual_quantity=quantity,
                    actual_condition=condition,
                    condition_note=None,
                    discrepancy_reason=" ",
                    evidence_ids=(UUID(int=1),),
                    confirmed_at=NOW,
                )
            item.confirm(
                actor_id=CUSTODIAN_ID,
                actual_quantity=quantity,
                actual_condition=condition,
                condition_note="Observed",
                discrepancy_reason="Mismatch found",
                evidence_ids=(UUID(int=1),),
                confirmed_at=NOW,
            )
            self.assertEqual(item.status, ReconciliationItemStatus.DISCREPANCY)
            item.resolve(actor_id=HR_ID, note=" Recounted ", resolved_at=NOW)
            self.assertEqual(item.status, ReconciliationItemStatus.RESOLVED)
            self.assertEqual(item.resolution_note, "Recounted")

    def test_wrong_actor_repeat_bad_evidence_and_bad_values_are_rejected(self) -> None:
        item = _item()
        with self.assertRaises(ReconciliationForbiddenError):
            _confirm_item(item, actor_id=HR_ID)
        with self.assertRaises(ReconciliationDataError):
            _confirm_item(item, actual_quantity=-1)
        with self.assertRaises(ReconciliationDataError):
            _confirm_item(item, evidence_ids=())
        with self.assertRaises(ReconciliationDataError):
            _confirm_item(item, discrepancy_reason="not a discrepancy")
        _confirm_item(item)
        with self.assertRaises(InvalidReconciliationTransitionError):
            _confirm_item(item)
        with self.assertRaises(InvalidReconciliationTransitionError):
            item.resolve(actor_id=HR_ID, note="No discrepancy", resolved_at=NOW)

    def test_request_reminder_and_close_gates_are_enforced(self) -> None:
        session = _session()
        session.request_confirmation(requested_at=NOW)
        self.assertEqual(session.confirmation_requested_at, NOW)
        with self.assertRaises(InvalidReconciliationTransitionError):
            session.request_confirmation(requested_at=NOW)
        session.items[0].remind(reminded_at=NOW)
        self.assertEqual(session.items[0].last_reminded_at, NOW)
        with self.assertRaises(ReconciliationCloseBlockedError):
            session.close(actor_id=HR_ID, closed_at=NOW, unresolved_count=1)
        session.close(actor_id=HR_ID, closed_at=NOW, unresolved_count=0)
        self.assertEqual(session.status, ReconciliationStatus.CLOSED)
        with self.assertRaises(InvalidReconciliationTransitionError):
            session.close(actor_id=HR_ID, closed_at=NOW, unresolved_count=0)

    def test_malformed_rehydrated_snapshots_are_rejected(self) -> None:
        with self.assertRaises(ReconciliationDataError):
            _item(asset_name=" ")
        with self.assertRaises(ReconciliationDataError):
            _item(expected_quantity=-1)
        with self.assertRaises(ReconciliationDataError):
            _item(snapshot_at=NOW.replace(tzinfo=None))
        with self.assertRaises(ReconciliationDataError):
            _session(temporary_loan_ticket_count=-1)
        with self.assertRaises(ReconciliationDataError):
            ReconciliationSession(
                SESSION_ID,
                date(2030, 1, 3),
                HR_ID,
                NOW,
                (_item(), _item(id=UUID(int=99))),
            )
        with self.assertRaises(ReconciliationDataError):
            _session(
                status=ReconciliationStatus.CLOSED,
                closed_by_user_id=None,
                closed_at=None,
            )
        with self.assertRaises(ReconciliationDataError):
            _session(closed_by_user_id=HR_ID, closed_at=NOW)
        with self.assertRaises(ReconciliationDataError):
            _session(items=(_item(reconciliation_id=UUID(int=404)),))

    def test_all_rehydrated_item_state_guards_and_terminal_actions(self) -> None:
        with self.assertRaises(ReconciliationDataError):
            _rehydrated_item(
                ReconciliationItemStatus.CONFIRMED,
                actual_quantity=-1,
                actual_condition=AssetCondition.GOOD,
                confirmed_by_user_id=CUSTODIAN_ID,
                confirmed_at=NOW,
            )
        with self.assertRaises(ReconciliationDataError):
            _rehydrated_item(ReconciliationItemStatus.PENDING_CONFIRMATION, actual_quantity=1)
        with self.assertRaises(ReconciliationDataError):
            _rehydrated_item(ReconciliationItemStatus.CONFIRMED)
        with self.assertRaises(ReconciliationDataError):
            _rehydrated_item(
                ReconciliationItemStatus.CONFIRMED,
                actual_quantity=1,
                actual_condition=AssetCondition.GOOD,
                confirmed_by_user_id=CUSTODIAN_ID,
                confirmed_at=NOW,
            )
        with self.assertRaises(ReconciliationDataError):
            _rehydrated_item(
                ReconciliationItemStatus.DISCREPANCY,
                actual_quantity=2,
                actual_condition=AssetCondition.GOOD,
                discrepancy_reason="Missing",
                confirmed_by_user_id=CUSTODIAN_ID,
                confirmed_at=NOW,
            )
        with self.assertRaises(ReconciliationDataError):
            _rehydrated_item(
                ReconciliationItemStatus.RESOLVED,
                actual_quantity=1,
                actual_condition=AssetCondition.GOOD,
                discrepancy_reason="Missing",
                confirmed_by_user_id=CUSTODIAN_ID,
                confirmed_at=NOW,
            )
        with self.assertRaises(ReconciliationDataError):
            _rehydrated_item(
                ReconciliationItemStatus.CONFIRMED,
                actual_quantity=2,
                actual_condition=AssetCondition.GOOD,
                confirmed_by_user_id=CUSTODIAN_ID,
                confirmed_at=NOW,
                resolution_note="Unexpected",
            )
        discrepancy = _rehydrated_item(
            ReconciliationItemStatus.DISCREPANCY,
            actual_quantity=1,
            actual_condition=AssetCondition.GOOD,
            discrepancy_reason="Missing",
            confirmed_by_user_id=CUSTODIAN_ID,
            confirmed_at=NOW,
        )
        with self.assertRaises(ReconciliationDataError):
            discrepancy.resolve(actor_id=HR_ID, note=" ", resolved_at=NOW)
        confirmed = _rehydrated_item(
            ReconciliationItemStatus.CONFIRMED,
            actual_quantity=2,
            actual_condition=AssetCondition.GOOD,
            confirmed_by_user_id=CUSTODIAN_ID,
            confirmed_at=NOW,
        )
        with self.assertRaises(InvalidReconciliationTransitionError):
            confirmed.remind(reminded_at=NOW)


class FixedClock(IClock):
    """Return the deterministic F09 test instant."""

    def now(self) -> datetime:
        """Return a timezone-aware UTC instant."""

        return NOW


class FakeReconciliationRepository:
    """Store one mutable reconciliation aggregate in memory."""

    def __init__(self) -> None:
        self.session: ReconciliationSession | None = None
        self.saved_items: list[ReconciliationItem] = []
        self.evidence: list[object] = []
        self.orphan_parent_id: UUID | None = None
        self.candidates: tuple[CustodySnapshotCandidate, ...] = (
            CustodySnapshotCandidate(
                CUSTODY_ID,
                ASSET_ID,
                CUSTODIAN_ID,
                "Camera",
                2,
                AssetCondition.GOOD,
            ),
        )
        self.loan_targets: tuple[TemporaryLoanReturnTarget, ...] = (
            TemporaryLoanReturnTarget(UUID(int=90), CUSTODIAN_ID, "holder@example.test"),
            TemporaryLoanReturnTarget(UUID(int=91), CUSTODIAN_ID, "holder@example.test"),
        )

    async def lock_open_policy(self) -> None:
        return None

    async def has_open_session(self) -> bool:
        return self.session is not None and self.session.status is ReconciliationStatus.OPEN

    async def list_snapshot_candidates(self) -> tuple[CustodySnapshotCandidate, ...]:
        return self.candidates

    async def list_temporary_loan_return_targets(
        self,
    ) -> tuple[TemporaryLoanReturnTarget, ...]:
        return self.loan_targets

    async def add(self, session: ReconciliationSession) -> None:
        self.session = session

    async def get(self, reconciliation_id: UUID) -> ReconciliationSession | None:
        return self.session if self.session and self.session.id == reconciliation_id else None

    async def get_for_update(self, reconciliation_id: UUID) -> ReconciliationSession | None:
        return await self.get(reconciliation_id)

    async def get_item_session_id(self, item_id: UUID) -> UUID | None:
        item = self._find_item(item_id)
        return self.orphan_parent_id if item is None else item.reconciliation_id

    async def get_item_for_update(self, item_id: UUID) -> ReconciliationItem | None:
        return self._find_item(item_id)

    async def list_for_custodian(
        self, custodian_id: UUID, *, offset: int, limit: int
    ) -> tuple[tuple[ReconciliationItem, ...], int]:
        values = tuple(
            item
            for item in (() if self.session is None else self.session.items)
            if item.custodian_user_id == custodian_id
        )
        return values[offset : offset + limit], len(values)

    async def count_unresolved_for_update(self, reconciliation_id: UUID) -> int:
        assert self.session is not None
        return sum(
            item.status
            in {
                ReconciliationItemStatus.PENDING_CONFIRMATION,
                ReconciliationItemStatus.DISCREPANCY,
            }
            for item in self.session.items
        )

    async def save_session(
        self, session: ReconciliationSession, *, updated_at: datetime
    ) -> None:
        self.session = session

    async def save_item(self, item: ReconciliationItem, *, updated_at: datetime) -> None:
        self.saved_items.append(item)

    async def add_evidence(self, evidence: object) -> None:
        self.evidence.append(evidence)

    def _find_item(self, item_id: UUID) -> ReconciliationItem | None:
        if self.session is None:
            return None
        return next((item for item in self.session.items if item.id == item_id), None)


class FakeUsers:
    """Return one active snapshot custodian identity."""

    def __init__(self) -> None:
        self.available = True

    async def get_identity(self, user_id: UUID) -> UserIdentity | None:
        if not self.available or user_id != CUSTODIAN_ID:
            return None
        return UserIdentity(
            CUSTODIAN_ID,
            "holder",
            "holder@example.test",
            "Holder",
            frozenset(),
            frozenset(),
            True,
        )


class FakeOutbox:
    """Collect transactional notification events."""

    def __init__(self) -> None:
        self.events: list[OutboxEvent] = []

    async def add(self, event: OutboxEvent) -> None:
        self.events.append(event)


class FakeUnitOfWork:
    """Expose only the ports used by the reconciliation service."""

    def __init__(self, repository: FakeReconciliationRepository) -> None:
        self.reconciliations = repository
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
    """Execute operations against one fake transaction and mark optional replay."""

    def __init__(self, unit_of_work: FakeUnitOfWork) -> None:
        self.unit_of_work = unit_of_work
        self.replayed = False
        self.commands: list[str] = []

    async def execute(
        self,
        *,
        actor_id: UUID,
        command_name: str,
        idempotency_key: str,
        request_fingerprint: str,
        operation: IdempotentOperation,
    ) -> IdempotentCommandResult:
        self.commands.append(command_name)
        response = await operation(cast(IUnitOfWork, self.unit_of_work))
        return IdempotentCommandResult(response.status_code, response.body, self.replayed)


class FakeEvidenceStorage:
    """Provide deterministic staging/finalization and cleanup observations."""

    def __init__(self) -> None:
        self.staged = (StagedFile("staged", "image/jpeg", "a" * 64, 5),)
        self.discarded = False
        self.deleted: tuple[str, ...] = ()

    async def stage_images(self, files: object) -> tuple[StagedFile, ...]:
        return self.staged

    async def finalize(
        self, files: tuple[StagedFile, ...], *, owner_type: str, owner_id: UUID
    ) -> tuple[str, ...]:
        return (f"{owner_type}/{owner_id}/proof",)

    async def discard(self, files: tuple[StagedFile, ...]) -> None:
        self.discarded = True

    async def delete_objects(self, object_keys: tuple[str, ...]) -> None:
        self.deleted = object_keys


class ReconciliationServiceTests(unittest.IsolatedAsyncioTestCase):
    """Verify F09 authorization, snapshot, notification, and transaction orchestration."""

    async def test_complete_workflow_and_notification_deduplication(self) -> None:
        repository = FakeReconciliationRepository()
        service, unit_of_work, executor, storage = _service(repository)
        hr = _actor(HR_ID, OperationalRole.HR)

        opened = await service.open(date(2030, 1, 6), hr, "open")
        self.assertEqual(opened.session.temporary_loan_ticket_count, 2)
        self.assertEqual(len(opened.session.items), 1)
        self.assertEqual(len(unit_of_work.outbox.events), 3)
        emails = unit_of_work.outbox.events[0].payload["emails"]
        self.assertIsInstance(emails, list)
        assert isinstance(emails, list)
        self.assertEqual(len(emails), 1)
        self.assertEqual(
            (await service.get(opened.session.id, hr)).session.id, opened.session.id
        )

        requested = await service.request_confirmation(opened.session.id, hr, "request")
        self.assertEqual(requested.email_recipients, 1)
        page = await service.list_mine(_actor(CUSTODIAN_ID), limit=10)
        self.assertEqual(page.total, 1)

        item_id = opened.session.items[0].id
        discrepancy = await service.confirm_item(
            item_id,
            ConfirmReconciliationMetadata(1, AssetCondition.DAMAGED, "Scratch", "One missing"),
            (_evidence_input(),),
            _actor(CUSTODIAN_ID),
            "confirm",
        )
        self.assertEqual(discrepancy.item.status, ReconciliationItemStatus.DISCREPANCY)
        self.assertEqual(len(repository.evidence), 1)

        reminded_repository = FakeReconciliationRepository()
        reminded_repository.session = _session()
        reminder_service, reminder_uow, _, _ = _service(reminded_repository)
        reminded = await reminder_service.remind_item(ITEM_ID, hr, "remind")
        self.assertTrue(reminded.email_queued)
        self.assertEqual(len(reminder_uow.outbox.events), 1)

        resolved = await service.resolve_item(item_id, "Recounted", hr, "resolve")
        self.assertEqual(resolved.item.status, ReconciliationItemStatus.RESOLVED)
        closed = await service.close(opened.session.id, hr, "close")
        self.assertEqual(closed.session.status, ReconciliationStatus.CLOSED)
        self.assertEqual(
            executor.commands,
            [
                "OPEN_RECONCILIATION",
                "REQUEST_RECONCILIATION_CONFIRMATION",
                "CONFIRM_RECONCILIATION_ITEM",
                "RESOLVE_RECONCILIATION_ITEM",
                "CLOSE_RECONCILIATION",
            ],
        )
        self.assertFalse(storage.discarded)

    async def test_authorization_dates_missing_scope_and_open_policy(self) -> None:
        repository = FakeReconciliationRepository()
        service, _, _, _ = _service(repository)
        inactive = CurrentActor(HR_ID, frozenset({OperationalRole.HR}), is_active=False)
        with self.assertRaises(ReconciliationForbiddenError):
            await service.open(date(2030, 1, 3), _actor(HR_ID), "role")
        with self.assertRaises(ReconciliationForbiddenError):
            await service.open(date(2030, 1, 3), inactive, "inactive")
        with self.assertRaises(InvalidReconciliationDateError):
            await service.open(date(2030, 1, 1), _actor(HR_ID, OperationalRole.HR), "date")

        hr = _actor(HR_ID, OperationalRole.HR)
        await service.open(date(2030, 1, 3), hr, "first")
        with self.assertRaises(InvalidReconciliationTransitionError):
            await service.open(date(2030, 1, 4), hr, "second")
        with self.assertRaises(ReconciliationNotFoundError):
            await service.get(UUID(int=404), hr)
        with self.assertRaises(ReconciliationForbiddenError):
            await service.list_mine(inactive)
        with self.assertRaises(CustodianScopeRequiredError):
            await service.list_mine(_actor(UUID(int=404)))

    async def test_missing_closed_and_replay_cleanup_paths(self) -> None:
        repository = FakeReconciliationRepository()
        service, unit_of_work, executor, storage = _service(repository)
        hr = _actor(HR_ID, OperationalRole.HR)
        with self.assertRaises(ReconciliationNotFoundError):
            await service.request_confirmation(SESSION_ID, hr, "missing")
        with self.assertRaises(ReconciliationNotFoundError):
            await service.resolve_item(ITEM_ID, "note", hr, "missing-item")
        with self.assertRaises(ReconciliationNotFoundError):
            await service.close(SESSION_ID, hr, "missing-close")
        repository.orphan_parent_id = SESSION_ID
        with self.assertRaises(ReconciliationNotFoundError):
            await service.resolve_item(ITEM_ID, "note", hr, "orphan-item")
        repository.orphan_parent_id = None

        with self.assertRaises(ReconciliationForbiddenError):
            await service.confirm_item(
                ITEM_ID,
                ConfirmReconciliationMetadata(2, AssetCondition.GOOD),
                (_evidence_input(),),
                CurrentActor(CUSTODIAN_ID, frozenset(), is_active=False),
                "inactive-confirm",
            )

        repository.session = _session()
        repository.session.close(actor_id=HR_ID, closed_at=NOW, unresolved_count=0)
        with self.assertRaises(InvalidReconciliationTransitionError):
            await service.confirm_item(
                ITEM_ID,
                ConfirmReconciliationMetadata(2, AssetCondition.GOOD),
                (_evidence_input(),),
                _actor(CUSTODIAN_ID),
                "closed-confirm",
            )
        self.assertTrue(storage.discarded)

        discrepancy = _rehydrated_item(
            ReconciliationItemStatus.DISCREPANCY,
            actual_quantity=1,
            actual_condition=AssetCondition.GOOD,
            discrepancy_reason="Missing",
            confirmed_by_user_id=CUSTODIAN_ID,
            confirmed_at=NOW,
        )
        repository.session = _closed_session(discrepancy)
        with self.assertRaises(InvalidReconciliationTransitionError):
            await service.resolve_item(ITEM_ID, "Resolved", hr, "closed-resolve")
        repository.session = _closed_session(_item())
        with self.assertRaises(InvalidReconciliationTransitionError):
            await service.remind_item(ITEM_ID, hr, "closed-remind")

        repository.session = _session()
        executor.replayed = True
        result = await service.confirm_item(
            ITEM_ID,
            ConfirmReconciliationMetadata(2, AssetCondition.GOOD),
            (_evidence_input(),),
            _actor(CUSTODIAN_ID),
            "replay",
        )
        self.assertTrue(result.replayed)
        self.assertTrue(storage.discarded)
        self.assertEqual(len(repository.evidence), 1)
        self.assertEqual(len(unit_of_work.outbox.events), 0)

    async def test_empty_recipient_and_unavailable_reminder_paths(self) -> None:
        repository = FakeReconciliationRepository()
        repository.candidates = ()
        repository.loan_targets = ()
        service, unit_of_work, _, _ = _service(repository)
        hr = _actor(HR_ID, OperationalRole.HR)
        opened = await service.open(date(2030, 1, 3), hr, "empty")
        result = await service.request_confirmation(opened.session.id, hr, "empty-request")
        self.assertEqual(result.email_recipients, 0)
        self.assertEqual(unit_of_work.outbox.events, [])

        repository.session = _session()
        unit_of_work.users.available = False
        with self.assertRaises(ReconciliationNotFoundError):
            await service.remind_item(ITEM_ID, hr, "unavailable")


def _service(
    repository: FakeReconciliationRepository,
) -> tuple[ReconciliationService, FakeUnitOfWork, FakeExecutor, FakeEvidenceStorage]:
    unit_of_work = FakeUnitOfWork(repository)
    executor = FakeExecutor(unit_of_work)
    storage = FakeEvidenceStorage()
    service = ReconciliationService(
        executor,
        lambda: cast(IUnitOfWork, unit_of_work),
        cast(EvidenceStorageService, storage),
        FixedClock(),
        id_factory=_sequential_id,
    )
    return service, unit_of_work, executor, storage


_next_id = 9500


def _sequential_id() -> UUID:
    global _next_id
    _next_id += 1
    return UUID(int=_next_id)


def _actor(user_id: UUID, *roles: OperationalRole) -> CurrentActor:
    return CurrentActor(user_id, frozenset(roles))


async def _file_content() -> AsyncIterator[bytes]:
    yield b"image"


def _evidence_input() -> EvidenceFileInput:
    return EvidenceFileInput(_file_content(), "image/jpeg")


def _item(
    *,
    id: UUID = ITEM_ID,
    asset_name: str = "Camera",
    expected_quantity: int = 2,
    snapshot_at: datetime = NOW,
    reconciliation_id: UUID = SESSION_ID,
) -> ReconciliationItem:
    return ReconciliationItem(
        id=id,
        reconciliation_id=reconciliation_id,
        custody_id=CUSTODY_ID,
        asset_id=ASSET_ID,
        custodian_user_id=CUSTODIAN_ID,
        asset_name=asset_name,
        expected_quantity=expected_quantity,
        expected_condition=AssetCondition.GOOD,
        snapshot_at=snapshot_at,
    )


def _rehydrated_item(status: ReconciliationItemStatus, **changes: object) -> ReconciliationItem:
    values: dict[str, object] = {
        "id": ITEM_ID,
        "reconciliation_id": SESSION_ID,
        "custody_id": CUSTODY_ID,
        "asset_id": ASSET_ID,
        "custodian_user_id": CUSTODIAN_ID,
        "asset_name": "Camera",
        "expected_quantity": 2,
        "expected_condition": AssetCondition.GOOD,
        "snapshot_at": NOW,
        "status": status,
    }
    values.update(changes)
    return ReconciliationItem(**values)  # type: ignore[arg-type]


def _closed_session(item: ReconciliationItem) -> ReconciliationSession:
    return ReconciliationSession(
        SESSION_ID,
        date(2030, 1, 3),
        HR_ID,
        NOW,
        (item,),
        status=ReconciliationStatus.CLOSED,
        closed_by_user_id=HR_ID,
        closed_at=NOW,
    )


def _confirm_item(
    item: ReconciliationItem,
    *,
    actor_id: UUID = CUSTODIAN_ID,
    actual_quantity: int = 2,
    evidence_ids: tuple[UUID, ...] = (UUID(int=1),),
    discrepancy_reason: str | None = None,
) -> None:
    item.confirm(
        actor_id=actor_id,
        actual_quantity=actual_quantity,
        actual_condition=AssetCondition.GOOD,
        condition_note=None,
        discrepancy_reason=discrepancy_reason,
        evidence_ids=evidence_ids,
        confirmed_at=NOW,
    )


def _session(**changes: object) -> ReconciliationSession:
    values: dict[str, object] = {
        "id": SESSION_ID,
        "reconciliation_date": date(2030, 1, 3),
        "opened_by_user_id": HR_ID,
        "opened_at": NOW,
        "items": (_item(),),
    }
    values.update(changes)
    return ReconciliationSession(**values)  # type: ignore[arg-type]
