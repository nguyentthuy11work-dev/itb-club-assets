"""Unit tests for F03.05 fingerprints and idempotent command orchestration."""

from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import cast
from uuid import UUID

from ams.application.idempotency import (
    CommandResponse,
    IdempotencyError,
    IdempotencyFingerprintError,
    IdempotencyInProgressError,
    IdempotencyKeyReusedError,
    IdempotentCommandExecutor,
    MultipartFileFingerprint,
    canonical_json_fingerprint,
    multipart_fingerprint,
)
from ams.application.reliability import (
    IClock,
    IdempotencyRecord,
    IdempotencyStatus,
    IIdempotencyRepository,
    INotificationRepository,
    IOutboxRepository,
    StagedFile,
)
from ams.application.unit_of_work import IUnitOfWork
from ams.domain.repositories import IAssetRepository, ICustodyRepository

ACTOR_ID = UUID("00000000-0000-4000-8000-000000001301")
RECORD_ID = UUID("00000000-0000-4000-8000-000000001302")
OTHER_RECORD_ID = UUID("00000000-0000-4000-8000-000000001303")
NOW = datetime(2026, 10, 4, 16, 0, tzinfo=UTC)


class FixedClock(IClock):
    """Return one deterministic command instant."""

    def now(self) -> datetime:
        """Return the configured UTC instant."""

        return NOW


class FakeStore:
    """Hold committed fake idempotency records and business effects."""

    def __init__(self) -> None:
        self.records: dict[tuple[UUID, str, str], IdempotencyRecord] = {}
        self.effects: list[str] = []


class FakeIdempotencyRepository(IIdempotencyRepository):
    """Stage idempotency changes inside one fake Unit of Work."""

    def __init__(self, unit_of_work: FakeUnitOfWork) -> None:
        self._unit_of_work = unit_of_work

    async def acquire(self, candidate: IdempotencyRecord) -> IdempotencyRecord:
        """Return a copied committed record or stage the new candidate."""

        key = self._key(candidate.actor_id, candidate.command_name, candidate.idempotency_key)
        current = self._unit_of_work.store.records.get(key)
        if current is not None:
            return deepcopy(current)
        self._unit_of_work.staged_record = deepcopy(candidate)
        return candidate

    async def get_for_update(
        self, actor_id: UUID, command_name: str, idempotency_key: str
    ) -> IdempotencyRecord | None:
        """Return a copied committed record for contract completeness."""

        record = self._unit_of_work.store.records.get(
            self._key(actor_id, command_name, idempotency_key)
        )
        return None if record is None else deepcopy(record)

    async def add(self, record: IdempotencyRecord) -> None:
        """Stage a record for contract completeness."""

        self._unit_of_work.staged_record = deepcopy(record)

    async def save(self, record: IdempotencyRecord) -> None:
        """Stage the completed replay response."""

        self._unit_of_work.staged_record = deepcopy(record)

    @staticmethod
    def _key(actor_id: UUID, command_name: str, idempotency_key: str) -> tuple[UUID, str, str]:
        return actor_id, command_name, idempotency_key


class FakeUnitOfWork(IUnitOfWork):
    """Commit or discard staged replay state and business effects together."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.staged_record: IdempotencyRecord | None = None
        self.staged_effects: list[str] = []
        self.idempotency = FakeIdempotencyRepository(self)
        self.assets = cast(IAssetRepository, object())
        self.custodies = cast(ICustodyRepository, object())
        self.outbox = cast(IOutboxRepository, object())
        self.notifications = cast(INotificationRepository, object())
        self._committed = False

    async def __aenter__(self) -> FakeUnitOfWork:
        """Open the fake transaction."""

        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Discard all work unless it was explicitly committed."""

        if not self._committed:
            await self.rollback()

    async def commit(self) -> None:
        """Publish the record and effects atomically."""

        if self.staged_record is not None:
            record = deepcopy(self.staged_record)
            key = (record.actor_id, record.command_name, record.idempotency_key)
            self.store.records[key] = record
        self.store.effects.extend(self.staged_effects)
        self._committed = True

    async def rollback(self) -> None:
        """Discard every staged record and business effect."""

        self.staged_record = None
        self.staged_effects.clear()


class FingerprintTests(unittest.TestCase):
    """Verify deterministic JSON and multipart request identities."""

    def test_json_fingerprint_is_key_order_independent_and_value_sensitive(self) -> None:
        """Equivalent JSON hashes equally while a changed value does not."""

        first = canonical_json_fingerprint(
            {
                "name": "Máy ảnh",
                "quantity": 2,
                "weight": 1.5,
                "nested": {"active": True},
            }
        )
        reordered = canonical_json_fingerprint(
            {
                "nested": {"active": True},
                "weight": 1.5,
                "quantity": 2,
                "name": "Máy ảnh",
            }
        )
        changed = canonical_json_fingerprint(
            {
                "name": "Máy ảnh",
                "quantity": 3,
                "weight": 1.5,
                "nested": {"active": True},
            }
        )
        self.assertEqual(first, reordered)
        self.assertNotEqual(first, changed)
        self.assertEqual(len(first), 64)

    def test_non_json_and_non_finite_values_are_rejected(self) -> None:
        """Python-only values, non-string keys, and non-finite numbers are ambiguous."""

        for payload in (
            {"value": object()},
            {1: "value"},
            {"value": float("nan")},
            {"value": (1, 2)},
        ):
            with self.subTest(payload=payload), self.assertRaises(IdempotencyFingerprintError):
                canonical_json_fingerprint(payload)

    def test_multipart_fingerprint_uses_fields_and_validated_file_content(self) -> None:
        """Transport part order is ignored while content identity remains significant."""

        first = MultipartFileFingerprint("evidence", "image/png", "a" * 64, 1024)
        second = MultipartFileFingerprint.from_staged(
            "attachment",
            StagedFile("staging/key", "image/jpeg", "b" * 64, 2048),
        )
        fingerprint = multipart_fingerprint(
            {"custodyId": "c-1", "confirmed": True}, [first, second]
        )
        reordered = multipart_fingerprint(
            {"confirmed": True, "custodyId": "c-1"}, [second, first]
        )
        changed = multipart_fingerprint(
            {"custodyId": "c-1", "confirmed": True},
            [MultipartFileFingerprint("evidence", "image/png", "c" * 64, 1024), second],
        )
        self.assertEqual(fingerprint, reordered)
        self.assertNotEqual(fingerprint, changed)

    def test_invalid_multipart_metadata_and_command_response_are_rejected(self) -> None:
        """Malformed hashes, negative sizes, blank fields, and non-2xx output fail early."""

        invalid_files = (
            ("", "image/png", "a" * 64, 1),
            ("file", "", "a" * 64, 1),
            ("file", "image/png", "INVALID", 1),
            ("file", "image/png", "a" * 64, -1),
        )
        for values in invalid_files:
            with self.subTest(values=values), self.assertRaises(IdempotencyFingerprintError):
                MultipartFileFingerprint(*values)
        with self.assertRaises(IdempotencyError):
            CommandResponse(409, {"error": "conflict"})
        with self.assertRaises(IdempotencyFingerprintError):
            CommandResponse(201, {"created": object()})


class IdempotentCommandExecutorTests(unittest.IsolatedAsyncioTestCase):
    """Verify first execution, replay, conflict, in-progress, and rollback behavior."""

    async def test_success_commits_once_and_exact_retry_replays_response(self) -> None:
        """The operation runs once and an exact retry returns its stored output."""

        store = FakeStore()
        executor = self._executor(store)

        first = await executor.execute(
            actor_id=ACTOR_ID,
            command_name="CREATE_ASSET",
            idempotency_key="request-1",
            request_fingerprint="a" * 64,
            operation=self._successful_operation,
        )
        replay = await executor.execute(
            actor_id=ACTOR_ID,
            command_name="CREATE_ASSET",
            idempotency_key="request-1",
            request_fingerprint="a" * 64,
            operation=self._unexpected_operation,
        )

        self.assertEqual(first.status_code, 201)
        self.assertFalse(first.replayed)
        self.assertEqual(replay.body, {"assetId": "asset-1"})
        self.assertTrue(replay.replayed)
        self.assertEqual(store.effects, ["asset-created"])

    async def test_changed_payload_and_committed_in_progress_record_conflict(self) -> None:
        """A key cannot change payload or bypass an existing incomplete execution."""

        store = FakeStore()
        executor = self._executor(store)
        await executor.execute(
            actor_id=ACTOR_ID,
            command_name="CREATE_ASSET",
            idempotency_key="request-1",
            request_fingerprint="a" * 64,
            operation=self._successful_operation,
        )
        with self.assertRaises(IdempotencyKeyReusedError):
            await executor.execute(
                actor_id=ACTOR_ID,
                command_name="CREATE_ASSET",
                idempotency_key="request-1",
                request_fingerprint="b" * 64,
                operation=self._unexpected_operation,
            )

        in_progress = self._record(record_id=OTHER_RECORD_ID, key="request-2")
        store.records[(ACTOR_ID, "CREATE_ASSET", "request-2")] = in_progress
        with self.assertRaises(IdempotencyInProgressError):
            await executor.execute(
                actor_id=ACTOR_ID,
                command_name="CREATE_ASSET",
                idempotency_key="request-2",
                request_fingerprint="a" * 64,
                operation=self._unexpected_operation,
            )

    async def test_operation_failure_rolls_back_candidate_and_effect(self) -> None:
        """A failed command leaves neither a replay record nor a business effect."""

        store = FakeStore()
        executor = self._executor(store)
        with self.assertRaises(RuntimeError):
            await executor.execute(
                actor_id=ACTOR_ID,
                command_name="CREATE_ASSET",
                idempotency_key="request-1",
                request_fingerprint="a" * 64,
                operation=self._failing_operation,
            )
        self.assertEqual(store.records, {})
        self.assertEqual(store.effects, [])

    def test_non_positive_record_ttl_is_rejected(self) -> None:
        """Replay retention must always extend beyond command creation."""

        with self.assertRaises(ValueError):
            IdempotentCommandExecutor(
                lambda: FakeUnitOfWork(FakeStore()),
                FixedClock(),
                record_ttl=timedelta(0),
            )

    @staticmethod
    async def _successful_operation(unit_of_work: IUnitOfWork) -> CommandResponse:
        assert isinstance(unit_of_work, FakeUnitOfWork)
        unit_of_work.staged_effects.append("asset-created")
        return CommandResponse(201, {"assetId": "asset-1"})

    @staticmethod
    async def _unexpected_operation(unit_of_work: IUnitOfWork) -> CommandResponse:
        raise AssertionError("A replay must not execute the operation.")

    @staticmethod
    async def _failing_operation(unit_of_work: IUnitOfWork) -> CommandResponse:
        assert isinstance(unit_of_work, FakeUnitOfWork)
        unit_of_work.staged_effects.append("asset-created")
        raise RuntimeError("business failure")

    @staticmethod
    def _executor(store: FakeStore) -> IdempotentCommandExecutor:
        return IdempotentCommandExecutor(
            lambda: FakeUnitOfWork(store),
            FixedClock(),
            id_factory=lambda: RECORD_ID,
        )

    @staticmethod
    def _record(*, record_id: UUID, key: str) -> IdempotencyRecord:
        return IdempotencyRecord(
            id=record_id,
            actor_id=ACTOR_ID,
            command_name="CREATE_ASSET",
            idempotency_key=key,
            request_fingerprint="a" * 64,
            status=IdempotencyStatus.IN_PROGRESS,
            expires_at=NOW + timedelta(days=1),
            created_at=NOW,
        )
