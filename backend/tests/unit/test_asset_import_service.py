"""Unit tests for baseline Asset import preview and confirmation orchestration."""

from __future__ import annotations

import unittest
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import cast
from uuid import UUID

from ams.application.asset_imports import (
    AssetImportFile,
    AssetImportResultError,
    AssetImportService,
    IAssetImportParser,
    ImportPreviewUnavailableError,
)
from ams.application.idempotency import IdempotentCommandResult, IdempotentOperation
from ams.application.reliability import (
    ClaimedOutboxEvent,
    IClock,
    IIdempotencyRepository,
    INotificationRepository,
    IOutboxRepository,
    OutboxEvent,
)
from ams.application.unit_of_work import IUnitOfWork
from ams.domain.asset_imports import (
    AssetImport,
    AssetImportRow,
    AssetImportStatus,
    BaselineAssetData,
    IAssetImportRepository,
)
from ams.domain.assets import Asset, AssetCondition, Custody
from ams.domain.authentication import LocalCredential, UserIdentity
from ams.domain.identity import AuthorizationError, CurrentActor, OperationalRole
from ams.domain.repositories import (
    IAssetRepository,
    ICustodyEvidenceRepository,
    ICustodyRepository,
    IUserRepository,
)

HR_ID = UUID("00000000-0000-4000-8000-000000002501")
CUSTODIAN_ID = UUID("00000000-0000-4000-8000-000000002502")
CATEGORY_ID = UUID("00000000-0000-4000-8000-000000002503")
TEAM_ID = UUID("00000000-0000-4000-8000-000000002504")
IMPORT_ID = UUID("00000000-0000-4000-8000-000000002505")
ASSET_ID = UUID("00000000-0000-4000-8000-000000002506")
CUSTODY_ID = UUID("00000000-0000-4000-8000-000000002507")
ASSIGNMENT_EVENT_ID = UUID("00000000-0000-4000-8000-000000002508")
REMINDER_EVENT_ID = UUID("00000000-0000-4000-8000-000000002509")
NOW = datetime(2026, 10, 4, 5, 0, tzinfo=UTC)


class FixedClock(IClock):
    """Return one deterministic preview/confirmation time."""

    def now(self) -> datetime:
        """Return the configured instant."""

        return NOW


class FakeParser(IAssetImportParser):
    """Return configured syntactic row results."""

    def __init__(self, rows: tuple[AssetImportRow, ...]) -> None:
        self.rows = rows

    def parse(self, source: AssetImportFile) -> tuple[AssetImportRow, ...]:
        """Return rows without framework or file-format behavior."""

        return self.rows


class FakeAssetRepository(IAssetRepository):
    """Capture imported Assets and expose reference switches."""

    def __init__(self) -> None:
        self.category_exists = True
        self.team_exists = True
        self.added: list[Asset] = []

    async def get_by_id(self, asset_id: UUID) -> Asset | None:
        return None

    async def get_for_update(self, asset_id: UUID) -> Asset | None:
        return None

    async def add(self, asset: Asset) -> None:
        self.added.append(asset)

    async def save(self, asset: Asset) -> None:
        raise AssertionError("not used")

    async def active_category_exists(self, category_id: UUID) -> bool:
        return self.category_exists

    async def active_team_exists(self, team_id: UUID) -> bool:
        return self.team_exists


class FakeCustodyRepository(ICustodyRepository):
    """Capture pending Custodies created from valid rows."""

    def __init__(self) -> None:
        self.added: list[Custody] = []

    async def get_by_id(self, custody_id: UUID) -> Custody | None:
        return None

    async def get_for_update(self, custody_id: UUID) -> Custody | None:
        return None

    async def get_many_for_update(self, custody_ids: tuple[UUID, ...]) -> tuple[Custody, ...]:
        """Reject unused bulk locking in Asset import."""

        raise AssertionError("not used")

    async def get_current_for_update(
        self, asset_id: UUID, custodian_user_id: UUID
    ) -> Custody | None:
        """Reject unused current-Custody locking in Asset import."""

        raise AssertionError("not used")

    async def add(self, custody: Custody) -> None:
        self.added.append(custody)

    async def save(self, custody: Custody) -> None:
        raise AssertionError("not used")


class FakeImportRepository(IAssetImportRepository):
    """Store one preview and capture completion."""

    def __init__(self, record: AssetImport | None = None) -> None:
        self.record = record
        self.saved = False

    async def add(self, asset_import: AssetImport) -> None:
        self.record = asset_import

    async def get_for_update(self, import_id: UUID) -> AssetImport | None:
        return self.record

    async def save(self, asset_import: AssetImport) -> None:
        self.record = asset_import
        self.saved = True


class FakeOutboxRepository(IOutboxRepository):
    """Capture assignment and reminder events."""

    def __init__(self) -> None:
        self.added: list[OutboxEvent] = []

    async def add(self, event: OutboxEvent) -> None:
        self.added.append(event)

    async def claim_due(
        self,
        *,
        now: datetime,
        worker_id: str,
        lease_expires_at: datetime,
        limit: int,
    ) -> tuple[ClaimedOutboxEvent, ...]:
        raise AssertionError("not used")

    async def mark_delivered(
        self, event_id: UUID, *, worker_id: str, completed_at: datetime
    ) -> None:
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
        raise AssertionError("not used")


class FakeUserRepository(IUserRepository):
    """Resolve configured identities and optionally remove an assignee after preview checks."""

    def __init__(self, users: Mapping[UUID, UserIdentity]) -> None:
        self.users = dict(users)
        self.custodian_reads = 0
        self.disappear_on_second_custodian_read = False

    async def get_local_credential(self, login: str) -> LocalCredential | None:
        raise AssertionError("not used")

    async def get_identity(self, user_id: UUID) -> UserIdentity | None:
        if user_id == CUSTODIAN_ID:
            self.custodian_reads += 1
            if self.disappear_on_second_custodian_read and self.custodian_reads >= 2:
                return None
        return self.users.get(user_id)


class FakeUnitOfWork(IUnitOfWork):
    """Expose all repositories used by import commands."""

    def __init__(self, record: AssetImport | None = None) -> None:
        self.assets: FakeAssetRepository = FakeAssetRepository()
        self.custodies: FakeCustodyRepository = FakeCustodyRepository()
        self.asset_imports: FakeImportRepository = FakeImportRepository(record)
        self.outbox: FakeOutboxRepository = FakeOutboxRepository()
        self.users: FakeUserRepository = FakeUserRepository(_users())
        self.idempotency = cast(IIdempotencyRepository, object())
        self.notifications = cast(INotificationRepository, object())
        self.custody_evidence = cast(ICustodyEvidenceRepository, object())

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


class FakeExecutor:
    """Execute against one fake UoW or replay a stored body."""

    def __init__(
        self, uow: FakeUnitOfWork, stored_body: Mapping[str, object] | None = None
    ) -> None:
        self.uow = uow
        self.stored_body = stored_body
        self.calls = 0

    async def execute(
        self,
        *,
        actor_id: UUID,
        command_name: str,
        idempotency_key: str,
        request_fingerprint: str,
        operation: IdempotentOperation,
    ) -> IdempotentCommandResult:
        self.calls += 1
        if self.stored_body is not None:
            return IdempotentCommandResult(200, self.stored_body, True)
        response = await operation(self.uow)
        return IdempotentCommandResult(response.status_code, response.body, False)


class AssetImportServiceTests(unittest.IsolatedAsyncioTestCase):
    """Verify preview, confirmation, replay, authorization, and stale references."""

    async def test_preview_persists_valid_and_invalid_row_outcomes(self) -> None:
        """Preview validates references and writes no Asset aggregate."""

        uow = FakeUnitOfWork()
        result = await self._service(FakeExecutor(uow), self._rows()).preview(
            _source(), _hr(), "preview-key"
        )

        self.assertEqual((result.valid_row_count, result.invalid_row_count), (1, 1))
        self.assertFalse(result.replayed)
        self.assertEqual(result.import_id, IMPORT_ID)
        self.assertIsNotNone(uow.asset_imports.record)
        self.assertEqual(uow.assets.added, [])

    async def test_preview_converts_unavailable_references_to_row_errors(self) -> None:
        """Missing category, Team, and assignee remain row-level preview failures."""

        uow = FakeUnitOfWork()
        uow.assets.category_exists = False
        uow.assets.team_exists = False
        uow.users.users.pop(CUSTODIAN_ID)

        result = await self._service(FakeExecutor(uow), (self._valid_row(),)).preview(
            _source(), _hr(), "preview-key"
        )

        self.assertEqual(result.valid_row_count, 0)
        self.assertEqual(len(result.rows[0].errors), 3)

    async def test_confirmation_imports_valid_rows_and_retains_rejections(self) -> None:
        """One transaction creates only valid Assets/Custodies and completes the preview."""

        uow = FakeUnitOfWork(_preview())
        result = await self._service(FakeExecutor(uow), ()).confirm(
            IMPORT_ID, _hr(), "confirm-key"
        )

        self.assertEqual(result.imported_asset_count, 1)
        self.assertEqual(len(result.rejected_rows), 1)
        self.assertEqual(len(uow.assets.added), 1)
        self.assertEqual(len(uow.custodies.added), 1)
        self.assertEqual(
            [event.event_type for event in uow.outbox.added],
            ["CUSTODY_ASSIGNED", "CUSTODY_PENDING_REMINDER"],
        )
        self.assertEqual(uow.outbox.added[1].scheduled_at, NOW + timedelta(hours=24))
        self.assertTrue(uow.asset_imports.saved)

    async def test_exact_preview_and_confirmation_replays_have_no_effects(self) -> None:
        """Stored command responses reconstruct typed results without repositories."""

        preview_body: Mapping[str, object] = {
            "importId": str(IMPORT_ID),
            "status": "PREVIEWED",
            "validRowCount": 1,
            "invalidRowCount": 0,
            "rows": [{"rowNumber": 2, "valid": True, "errors": []}],
        }
        confirm_body: Mapping[str, object] = {
            "importId": str(IMPORT_ID),
            "status": "COMPLETED",
            "importedAssetCount": 1,
            "rejectedRows": [],
        }
        preview = await self._service(FakeExecutor(FakeUnitOfWork(), preview_body), ()).preview(
            _source(), _hr(), "preview-key"
        )
        uow = FakeUnitOfWork()
        confirmed = await self._service(FakeExecutor(uow, confirm_body), ()).confirm(
            IMPORT_ID, _hr(), "confirm-key"
        )
        self.assertTrue(preview.replayed)
        self.assertTrue(confirmed.replayed)
        self.assertEqual(uow.assets.added, [])

    async def test_unavailable_or_stale_preview_is_rejected(self) -> None:
        """Unavailable lifecycle states and changed references cannot import."""

        completed = _preview()
        completed.complete(NOW - timedelta(hours=1))
        foreign = _preview(owner=CUSTODIAN_ID)
        expired = _preview(expires_at=NOW)
        cases = (
            FakeUnitOfWork(None),
            FakeUnitOfWork(foreign),
            FakeUnitOfWork(completed),
            FakeUnitOfWork(expired),
        )
        for uow in cases:
            with self.subTest(uow=uow), self.assertRaises(ImportPreviewUnavailableError):
                await self._service(FakeExecutor(uow), ()).confirm(
                    IMPORT_ID, _hr(), "confirm-key"
                )

        changed = FakeUnitOfWork(_preview())
        changed.assets.category_exists = False
        with self.assertRaises(ImportPreviewUnavailableError):
            await self._service(FakeExecutor(changed), ()).confirm(
                IMPORT_ID, _hr(), "confirm-key"
            )

        future = _preview()
        future.created_at = NOW + timedelta(hours=1)
        future_uow = FakeUnitOfWork(future)
        with self.assertRaises(ImportPreviewUnavailableError):
            await self._service(FakeExecutor(future_uow), ()).confirm(
                IMPORT_ID, _hr(), "confirm-key"
            )

    async def test_unavailable_owner_or_disappearing_assignee_is_rejected(self) -> None:
        """Identity changes cannot produce partially imported aggregates."""

        missing_owner = FakeUnitOfWork(_preview())
        missing_owner.users.users.pop(HR_ID)
        with self.assertRaises(ImportPreviewUnavailableError):
            await self._service(FakeExecutor(missing_owner), ()).confirm(
                IMPORT_ID, _hr(), "confirm-key"
            )

        disappearing = FakeUnitOfWork(_preview())
        disappearing.users.disappear_on_second_custodian_read = True
        with self.assertRaises(ImportPreviewUnavailableError):
            await self._service(FakeExecutor(disappearing), ()).confirm(
                IMPORT_ID, _hr(), "confirm-key"
            )

    async def test_non_hr_is_rejected_before_idempotency(self) -> None:
        """Both commands enforce HR authorization outside the HTTP route."""

        actor = CurrentActor(HR_ID, frozenset(), is_active=True)
        executor = FakeExecutor(FakeUnitOfWork())
        service = self._service(executor, self._rows())
        with self.assertRaises(AuthorizationError):
            await service.preview(_source(), actor, "preview-key")
        with self.assertRaises(AuthorizationError):
            await service.confirm(IMPORT_ID, actor, "confirm-key")
        self.assertEqual(executor.calls, 0)

    async def test_corrupt_replay_results_are_rejected(self) -> None:
        """Malformed persisted JSON cannot escape as a successful import response."""

        invalid_preview: tuple[Mapping[str, object], ...] = (
            {},
            {
                "importId": str(IMPORT_ID),
                "status": "PREVIEWED",
                "validRowCount": True,
                "invalidRowCount": 0,
                "rows": [],
            },
            {
                "importId": str(IMPORT_ID),
                "status": "PREVIEWED",
                "validRowCount": 0,
                "invalidRowCount": 1,
                "rows": ["bad"],
            },
            {
                "importId": str(IMPORT_ID),
                "status": "PREVIEWED",
                "validRowCount": 0,
                "invalidRowCount": 1,
                "rows": "bad",
            },
            {
                "importId": str(IMPORT_ID),
                "status": "PREVIEWED",
                "validRowCount": 0,
                "invalidRowCount": 1,
                "rows": [{"rowNumber": True, "valid": False, "errors": []}],
            },
        )
        for body in invalid_preview:
            with self.subTest(body=body), self.assertRaises(AssetImportResultError):
                await self._service(FakeExecutor(FakeUnitOfWork(), body), ()).preview(
                    _source(), _hr(), "preview-key"
                )

        invalid_confirm: tuple[Mapping[str, object], ...] = (
            {},
            {
                "importId": str(IMPORT_ID),
                "status": "COMPLETED",
                "importedAssetCount": True,
                "rejectedRows": [],
            },
        )
        for body in invalid_confirm:
            with self.subTest(body=body), self.assertRaises(AssetImportResultError):
                await self._service(FakeExecutor(FakeUnitOfWork(), body), ()).confirm(
                    IMPORT_ID, _hr(), "confirm-key"
                )

    @staticmethod
    def _service(
        executor: FakeExecutor, rows: tuple[AssetImportRow, ...]
    ) -> AssetImportService:
        ids = iter(
            (
                IMPORT_ID,
                ASSET_ID,
                CUSTODY_ID,
                ASSIGNMENT_EVENT_ID,
                REMINDER_EVENT_ID,
            )
        )
        return AssetImportService(
            executor,
            FakeParser(rows),
            FixedClock(),
            id_factory=lambda: next(ids),
        )

    @staticmethod
    def _valid_row() -> AssetImportRow:
        return AssetImportRow(2, _data(), ())

    @classmethod
    def _rows(cls) -> tuple[AssetImportRow, ...]:
        return (
            cls._valid_row(),
            AssetImportRow(3, None, ("name is required",)),
        )


def _data() -> BaselineAssetData:
    return BaselineAssetData(
        "Camera",
        "Club camera",
        "DONATION",
        2,
        CATEGORY_ID,
        TEAM_ID,
        CUSTODIAN_ID,
        AssetCondition.GOOD,
        "New",
    )


def _preview(
    *,
    owner: UUID = HR_ID,
    expires_at: datetime = NOW + timedelta(hours=24),
) -> AssetImport:
    return AssetImport(
        id=IMPORT_ID,
        created_by_user_id=owner,
        status=AssetImportStatus.PREVIEWED,
        source_filename="baseline.csv",
        source_content_type="text/csv",
        source_sha256="a" * 64,
        rows=(
            AssetImportRow(2, _data(), ()),
            AssetImportRow(3, None, ("name is required",)),
        ),
        expires_at=expires_at,
        created_at=NOW - timedelta(hours=2),
    )


def _identity(user_id: UUID, *, active: bool = True) -> UserIdentity:
    return UserIdentity(
        id=user_id,
        username=f"user.{str(user_id)[-4:]}",
        email=f"user.{str(user_id)[-4:]}@example.test",
        display_name="Import User",
        roles=frozenset(),
        team_ids=frozenset(),
        is_active=active,
    )


def _users() -> Mapping[UUID, UserIdentity]:
    return {HR_ID: _identity(HR_ID), CUSTODIAN_ID: _identity(CUSTODIAN_ID)}


def _hr() -> CurrentActor:
    return CurrentActor(HR_ID, frozenset({OperationalRole.HR}), is_active=True)


def _source() -> AssetImportFile:
    return AssetImportFile("baseline.csv", "text/csv", b"content")
