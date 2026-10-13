"""Unit tests for F02.05 evidence-backed Custody confirmation."""

from __future__ import annotations

import unittest
from collections.abc import AsyncIterable, Mapping
from datetime import UTC, datetime
from types import TracebackType
from typing import cast
from uuid import UUID

from ams.application.custody_confirmation import (
    ConfirmCustodyMetadata,
    CustodyConfirmationResultError,
    CustodyConfirmationService,
    CustodyResourceNotFoundError,
)
from ams.application.evidence import EvidenceFileInput, EvidenceStorageService
from ams.application.idempotency import IdempotentCommandResult, IdempotentOperation
from ams.application.reliability import (
    ClaimedOutboxEvent,
    IClock,
    IFileStorage,
    IIdempotencyRepository,
    INotificationRepository,
    IOutboxRepository,
    OutboxEvent,
    StagedFile,
)
from ams.application.unit_of_work import IUnitOfWork
from ams.domain.assets import (
    Asset,
    AssetCondition,
    Custody,
    CustodyConfirmationForbiddenError,
    CustodyEvidence,
    CustodyStatus,
)
from ams.domain.authentication import LocalCredential, UserIdentity
from ams.domain.identity import AuthorizationError, CurrentActor
from ams.domain.repositories import (
    IAssetRepository,
    ICustodyEvidenceRepository,
    ICustodyRepository,
    IUserRepository,
)

ASSET_ID = UUID("00000000-0000-4000-8000-000000001701")
CUSTODY_ID = UUID("00000000-0000-4000-8000-000000001702")
CUSTODIAN_ID = UUID("00000000-0000-4000-8000-000000001703")
OTHER_USER_ID = UUID("00000000-0000-4000-8000-000000001704")
HR_ID = UUID("00000000-0000-4000-8000-000000001705")
CATEGORY_ID = UUID("00000000-0000-4000-8000-000000001706")
EVIDENCE_ID = UUID("00000000-0000-4000-8000-000000001707")
OTHER_EVIDENCE_ID = UUID("00000000-0000-4000-8000-000000001708")
EVENT_ID = UUID("00000000-0000-4000-8000-000000001709")
NOW = datetime(2026, 10, 4, 19, 0, tzinfo=UTC)


async def _content() -> AsyncIterable[bytes]:
    yield b"image"


class FixedClock(IClock):
    """Return one deterministic confirmation instant."""

    def now(self) -> datetime:
        """Return the configured UTC instant."""

        return NOW


class FakeFileStorage(IFileStorage):
    """Record staged, finalized, and deleted object keys."""

    def __init__(self) -> None:
        self.stage_calls = 0
        self.finalize_calls = 0
        self.deleted: list[str] = []

    async def stage(
        self, content: AsyncIterable[bytes], declared_content_type: str
    ) -> StagedFile:
        """Return deterministic validated metadata."""

        self.stage_calls += 1
        return StagedFile(
            f"staging/{self.stage_calls:032x}",
            declared_content_type,
            f"{self.stage_calls:x}" * 64,
            5,
        )

    async def finalize(self, staging_key: str, owner_type: str, owner_id: UUID) -> str:
        """Return a deterministic permanent key."""

        self.finalize_calls += 1
        return f"objects/{self.finalize_calls:032x}"

    async def delete(self, object_key: str) -> None:
        """Record idempotent cleanup."""

        self.deleted.append(object_key)


class FakeAssetRepository(IAssetRepository):
    """Load and save one configured Asset."""

    def __init__(self, asset: Asset | None) -> None:
        self.asset = asset
        self.saved: list[Asset] = []

    async def get_by_id(self, asset_id: UUID) -> Asset | None:
        """Return the configured Asset."""

        return self.asset

    async def get_for_update(self, asset_id: UUID) -> Asset | None:
        """Return the configured Asset under the fake lock."""

        return self.asset

    async def add(self, asset: Asset) -> None:
        """Reject creation in this confirmation fake."""

        raise AssertionError("not used")

    async def save(self, asset: Asset) -> None:
        """Capture activated availability."""

        self.saved.append(asset)

    async def active_category_exists(self, category_id: UUID) -> bool:
        """Reject creation reference checks."""

        raise AssertionError("not used")

    async def active_team_exists(self, team_id: UUID) -> bool:
        """Reject creation reference checks."""

        raise AssertionError("not used")


class FakeCustodyRepository(ICustodyRepository):
    """Load and save one configured pending Custody."""

    def __init__(self, custody: Custody | None) -> None:
        self.custody = custody
        self.saved: list[Custody] = []

    async def get_by_id(self, custody_id: UUID) -> Custody | None:
        """Return the configured Custody."""

        return self.custody

    async def get_for_update(self, custody_id: UUID) -> Custody | None:
        """Return the configured Custody under the fake lock."""

        return self.custody

    async def get_many_for_update(self, custody_ids: tuple[UUID, ...]) -> tuple[Custody, ...]:
        """Reject unused bulk locking in confirmation."""

        raise AssertionError("not used")

    async def get_current_for_update(
        self, asset_id: UUID, custodian_user_id: UUID
    ) -> Custody | None:
        """Reject unused current-Custody locking in confirmation."""

        raise AssertionError("not used")

    async def add(self, custody: Custody) -> None:
        """Reject creation in this confirmation fake."""

        raise AssertionError("not used")

    async def save(self, custody: Custody) -> None:
        """Capture the active Custody."""

        self.saved.append(custody)


class FakeEvidenceRepository(ICustodyEvidenceRepository):
    """Capture evidence metadata staged in the transaction."""

    def __init__(self) -> None:
        self.added: list[CustodyEvidence] = []

    async def add(self, evidence: CustodyEvidence) -> None:
        """Capture one evidence record."""

        self.added.append(evidence)


class FakeOutboxRepository(IOutboxRepository):
    """Capture confirmation publication."""

    def __init__(self) -> None:
        self.added: list[OutboxEvent] = []

    async def add(self, event: OutboxEvent) -> None:
        """Capture one event."""

        self.added.append(event)

    async def claim_due(
        self,
        *,
        now: datetime,
        worker_id: str,
        lease_expires_at: datetime,
        limit: int,
    ) -> tuple[ClaimedOutboxEvent, ...]:
        """Reject worker behavior."""

        raise AssertionError("not used")

    async def mark_delivered(
        self, event_id: UUID, *, worker_id: str, completed_at: datetime
    ) -> None:
        """Reject worker behavior."""

        raise AssertionError("not used")

    async def mark_retry(
        self,
        event_id: UUID,
        *,
        worker_id: str,
        failed_at: datetime,
        next_attempt_at: datetime,
        error: str,
        dead_letter: bool,
    ) -> None:
        """Reject worker behavior."""

        raise AssertionError("not used")


class FakeUserRepository(IUserRepository):
    """Resolve only the assigning HR identity."""

    def __init__(self, assigner: UserIdentity | None) -> None:
        self.assigner = assigner

    async def get_local_credential(self, login: str) -> LocalCredential | None:
        """Reject credential lookup."""

        raise AssertionError("not used")

    async def get_identity(self, user_id: UUID) -> UserIdentity | None:
        """Return the configured assigner."""

        return self.assigner


class FakeUnitOfWork(IUnitOfWork):
    """Expose confirmation repositories to the fake executor."""

    def __init__(
        self,
        *,
        custody: Custody | None = None,
        asset: Asset | None = None,
        assigner: UserIdentity | None = None,
    ) -> None:
        self.assets: FakeAssetRepository = FakeAssetRepository(
            asset if asset is not None else _asset()
        )
        self.custodies: FakeCustodyRepository = FakeCustodyRepository(
            custody if custody is not None else _custody()
        )
        self.custody_evidence: FakeEvidenceRepository = FakeEvidenceRepository()
        self.outbox: FakeOutboxRepository = FakeOutboxRepository()
        self.users: FakeUserRepository = FakeUserRepository(
            assigner if assigner is not None else _assigner()
        )
        self.notifications = cast(INotificationRepository, object())
        self.idempotency = cast(IIdempotencyRepository, object())

    async def __aenter__(self) -> FakeUnitOfWork:
        """Open the fake transaction."""

        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the fake transaction."""

    async def commit(self) -> None:
        """Accept commit for contract completeness."""

    async def rollback(self) -> None:
        """Accept rollback for contract completeness."""


class FakeIdempotentExecutor:
    """Execute immediately, replay a body, or fail after operation processing."""

    def __init__(
        self,
        unit_of_work: FakeUnitOfWork,
        *,
        stored_body: Mapping[str, object] | None = None,
        fail_after_operation: bool = False,
    ) -> None:
        self.unit_of_work = unit_of_work
        self.stored_body = stored_body
        self.fail_after_operation = fail_after_operation
        self.request_fingerprint: str | None = None

    async def execute(
        self,
        *,
        actor_id: UUID,
        command_name: str,
        idempotency_key: str,
        request_fingerprint: str,
        operation: IdempotentOperation,
    ) -> IdempotentCommandResult:
        """Apply the configured execution mode."""

        self.request_fingerprint = request_fingerprint
        if self.stored_body is not None:
            return IdempotentCommandResult(200, self.stored_body, True)
        response = await operation(self.unit_of_work)
        if self.fail_after_operation:
            raise RuntimeError("commit failed")
        return IdempotentCommandResult(response.status_code, response.body, False)


class CustodyConfirmationServiceTests(unittest.IsolatedAsyncioTestCase):
    """Verify confirmation processing, replay, authorization, and object cleanup."""

    async def test_assignee_confirmation_activates_quantities_evidence_and_event(self) -> None:
        """A valid batch produces active state, metadata, availability, and publication."""

        unit_of_work = FakeUnitOfWork()
        storage = FakeFileStorage()
        service = self._service(FakeIdempotentExecutor(unit_of_work), storage)

        result = await service.confirm(
            custody_id=CUSTODY_ID,
            metadata=ConfirmCustodyMetadata("Received"),
            files=self._files(2),
            actor=self._actor(),
            idempotency_key="request-1",
        )

        self.assertEqual(result.status, CustodyStatus.ACTIVE)
        self.assertEqual(result.confirmed_at, NOW)
        self.assertFalse(result.replayed)
        self.assertEqual(len(unit_of_work.custody_evidence.added), 2)
        self.assertEqual(unit_of_work.custodies.saved[0].status, CustodyStatus.ACTIVE)
        self.assertEqual(unit_of_work.assets.saved[0].available_quantity, 3)
        self.assertEqual(unit_of_work.outbox.added[0].event_type, "CUSTODY_CONFIRMED")
        self.assertEqual(storage.deleted, [])

    async def test_exact_retry_discards_new_staging_and_returns_stored_result(self) -> None:
        """A multipart replay never re-finalizes or repeats database effects."""

        body: Mapping[str, object] = {
            "custodyId": str(CUSTODY_ID),
            "status": "ACTIVE",
            "confirmedAt": NOW.isoformat(),
        }
        unit_of_work = FakeUnitOfWork()
        storage = FakeFileStorage()
        result = await self._service(
            FakeIdempotentExecutor(unit_of_work, stored_body=body), storage
        ).confirm(
            custody_id=CUSTODY_ID,
            metadata=ConfirmCustodyMetadata(),
            files=self._files(1),
            actor=self._actor(),
            idempotency_key="request-1",
        )
        self.assertTrue(result.replayed)
        self.assertEqual(storage.finalize_calls, 0)
        self.assertEqual(storage.deleted, ["staging/00000000000000000000000000000001"])
        self.assertEqual(unit_of_work.custody_evidence.added, [])

    async def test_wrong_or_inactive_actor_cannot_confirm_and_staging_is_cleaned(self) -> None:
        """Inactive actors stage nothing; a wrong assignee's staged upload is discarded."""

        inactive_storage = FakeFileStorage()
        with self.assertRaises(AuthorizationError):
            await self._service(
                FakeIdempotentExecutor(FakeUnitOfWork()), inactive_storage
            ).confirm(
                custody_id=CUSTODY_ID,
                metadata=ConfirmCustodyMetadata(),
                files=self._files(1),
                actor=CurrentActor(CUSTODIAN_ID, frozenset(), is_active=False),
                idempotency_key="request-1",
            )
        self.assertEqual(inactive_storage.stage_calls, 0)

        wrong_storage = FakeFileStorage()
        with self.assertRaises(CustodyConfirmationForbiddenError):
            await self._service(
                FakeIdempotentExecutor(FakeUnitOfWork()), wrong_storage
            ).confirm(
                custody_id=CUSTODY_ID,
                metadata=ConfirmCustodyMetadata(),
                files=self._files(1),
                actor=CurrentActor(OTHER_USER_ID, frozenset()),
                idempotency_key="request-1",
            )
        self.assertEqual(wrong_storage.deleted, ["staging/00000000000000000000000000000001"])

    async def test_missing_custody_asset_or_assigner_cleans_staging(self) -> None:
        """Every missing reference rejects before permanent evidence can survive."""

        cases = (
            FakeUnitOfWork(custody=cast(Custody, None)),
            FakeUnitOfWork(asset=cast(Asset, None)),
            FakeUnitOfWork(assigner=cast(UserIdentity, None)),
        )
        cases[0].custodies.custody = None
        cases[1].assets.asset = None
        cases[2].users.assigner = None
        for unit_of_work in cases:
            storage = FakeFileStorage()
            with (
                self.subTest(unit_of_work=unit_of_work),
                self.assertRaises(CustodyResourceNotFoundError),
            ):
                await self._service(FakeIdempotentExecutor(unit_of_work), storage).confirm(
                    custody_id=CUSTODY_ID,
                    metadata=ConfirmCustodyMetadata(),
                    files=self._files(1),
                    actor=self._actor(),
                    idempotency_key="request-1",
                )
            self.assertIn("staging/00000000000000000000000000000001", storage.deleted)

    async def test_transaction_failure_deletes_finalized_objects(self) -> None:
        """A failed commit cannot leave permanent objects after database rollback."""

        storage = FakeFileStorage()
        with self.assertRaises(RuntimeError):
            await self._service(
                FakeIdempotentExecutor(FakeUnitOfWork(), fail_after_operation=True),
                storage,
            ).confirm(
                custody_id=CUSTODY_ID,
                metadata=ConfirmCustodyMetadata(),
                files=self._files(1),
                actor=self._actor(),
                idempotency_key="request-1",
            )
        self.assertIn("objects/00000000000000000000000000000001", storage.deleted)

    async def test_invalid_stored_response_is_rejected_after_replay_cleanup(self) -> None:
        """Corrupt replay JSON is rejected without retaining the retry upload."""

        invalid_bodies: tuple[Mapping[str, object], ...] = (
            {},
            {
                "custodyId": str(CUSTODY_ID),
                "status": "PENDING",
                "confirmedAt": NOW.isoformat(),
            },
            {
                "custodyId": str(CUSTODY_ID),
                "status": "ACTIVE",
                "confirmedAt": "not-a-timestamp",
            },
            {
                "custodyId": str(CUSTODY_ID),
                "status": "ACTIVE",
                "confirmedAt": "2026-10-04T19:00:00+07:00",
            },
        )
        for body in invalid_bodies:
            storage = FakeFileStorage()
            with self.subTest(body=body), self.assertRaises(CustodyConfirmationResultError):
                await self._service(
                    FakeIdempotentExecutor(FakeUnitOfWork(), stored_body=body),
                    storage,
                ).confirm(
                    custody_id=CUSTODY_ID,
                    metadata=ConfirmCustodyMetadata(),
                    files=self._files(1),
                    actor=self._actor(),
                    idempotency_key="request-1",
                )
            self.assertEqual(len(storage.deleted), 1)

    @staticmethod
    def _files(count: int) -> tuple[EvidenceFileInput, ...]:
        return tuple(EvidenceFileInput(_content(), "image/png") for _ in range(count))

    @staticmethod
    def _actor() -> CurrentActor:
        return CurrentActor(CUSTODIAN_ID, frozenset(), is_active=True)

    @staticmethod
    def _service(
        executor: FakeIdempotentExecutor, storage: FakeFileStorage
    ) -> CustodyConfirmationService:
        ids = iter((EVIDENCE_ID, OTHER_EVIDENCE_ID, EVENT_ID))
        return CustodyConfirmationService(
            executor,
            EvidenceStorageService(storage),
            FixedClock(),
            id_factory=lambda: next(ids),
        )


def _asset() -> Asset:
    return Asset(
        id=ASSET_ID,
        category_id=CATEGORY_ID,
        responsible_team_id=None,
        name="Camera",
        source="DONATION",
        total_quantity=3,
        available_quantity=0,
        created_by_user_id=HR_ID,
        created_at=NOW,
    )


def _custody() -> Custody:
    return Custody.create_pending(
        custody_id=CUSTODY_ID,
        asset_id=ASSET_ID,
        custodian_user_id=CUSTODIAN_ID,
        assigned_by_user_id=HR_ID,
        quantity=3,
        assigned_at=NOW,
        condition=AssetCondition.GOOD,
    )


def _assigner() -> UserIdentity:
    return UserIdentity(
        id=HR_ID,
        username="confirmation.hr",
        email="confirmation.hr@example.test",
        display_name="Confirmation HR",
        roles=frozenset(),
        team_ids=frozenset(),
        is_active=True,
    )
