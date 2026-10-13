"""Unit proofs for F11 term snapshots, dual confirmation, stale flags, and CSV."""

from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta
from types import TracebackType
from typing import cast
from unittest.mock import patch
from uuid import UUID

from ams.application.handovers import HandoverService
from ams.application.idempotency import (
    IdempotentCommandResult,
    IdempotentOperation,
    IIdempotentCommandExecutor,
)
from ams.application.reliability import IClock, OutboxEvent
from ams.application.unit_of_work import IUnitOfWork
from ams.domain.assets import AssetCondition, CustodyStatus
from ams.domain.authentication import UserIdentity
from ams.domain.handovers import (
    AssetHandoverSnapshot,
    Handover,
    HandoverAlreadyExistsError,
    HandoverConfirmationForbiddenError,
    HandoverCustodySnapshot,
    HandoverDataError,
    HandoverExportForbiddenError,
    HandoverExportLimitError,
    HandoverItem,
    HandoverNotFoundError,
    HandoverRoleRequiredError,
    HandoverStatus,
    InvalidHandoverTransitionError,
    Term,
    TermNotEndedError,
    TermNotFoundError,
)
from ams.domain.identity import CurrentActor, OperationalRole

NOW = datetime(2030, 2, 2, 3, 0, tzinfo=UTC)
TERM_ID = UUID("00000000-0000-4000-8000-000000012001")
HANDOVER_ID = UUID("00000000-0000-4000-8000-000000012002")
ITEM_ID = UUID("00000000-0000-4000-8000-000000012003")
ASSET_ID = UUID("00000000-0000-4000-8000-000000012004")
CUSTODY_ID = UUID("00000000-0000-4000-8000-000000012005")
OLD_HR_ID = UUID("00000000-0000-4000-8000-000000012006")
NEW_HR_ID = UUID("00000000-0000-4000-8000-000000012007")
OUTSIDER_ID = UUID("00000000-0000-4000-8000-000000012008")


class HandoverDomainTests(unittest.TestCase):
    """Protect configurable terms, snapshots, either order, and terminal locking."""

    def test_old_then_new_locks_once(self) -> None:
        handover = _handover()
        self.assertFalse(handover.confirm_old(actor_id=OLD_HR_ID, confirmed_at=NOW))
        self.assertTrue(handover.confirm_new(actor_id=NEW_HR_ID, confirmed_at=NOW))
        self.assertEqual(handover.status, HandoverStatus.LOCKED)
        self.assertEqual(handover.locked_at, NOW)
        with self.assertRaises(InvalidHandoverTransitionError):
            handover.confirm_old(actor_id=OLD_HR_ID, confirmed_at=NOW)

    def test_new_then_old_is_equally_valid(self) -> None:
        handover = _handover()
        self.assertFalse(handover.confirm_new(actor_id=NEW_HR_ID, confirmed_at=NOW))
        self.assertTrue(handover.confirm_old(actor_id=OLD_HR_ID, confirmed_at=NOW))
        self.assertEqual(handover.status, HandoverStatus.LOCKED)
        with self.assertRaises(InvalidHandoverTransitionError):
            handover.confirm_new(actor_id=NEW_HR_ID, confirmed_at=NOW)

    def test_wrong_actor_and_pre_creation_time_are_rejected(self) -> None:
        handover = _handover()
        with self.assertRaises(HandoverConfirmationForbiddenError):
            handover.confirm_old(actor_id=NEW_HR_ID, confirmed_at=NOW)
        with self.assertRaises(HandoverConfirmationForbiddenError):
            handover.confirm_new(actor_id=OLD_HR_ID, confirmed_at=NOW)
        with self.assertRaises(InvalidHandoverTransitionError):
            handover.confirm_old(actor_id=OLD_HR_ID, confirmed_at=NOW - timedelta(seconds=1))

    def test_term_boundary_and_invalid_term_values(self) -> None:
        term = _term()
        self.assertFalse(term.is_stale(date(2030, 1, 1)))
        self.assertFalse(term.is_stale(date(2030, 1, 31)))
        self.assertTrue(term.is_stale(None))
        self.assertTrue(term.is_stale(date(2029, 12, 31)))
        with self.assertRaises(HandoverDataError):
            _term(code=" ")
        with self.assertRaises(HandoverDataError):
            _term(starts_on=date(2030, 2, 1))
        with self.assertRaises(HandoverDataError):
            _term(new_hr_user_id=OLD_HR_ID)

    def test_snapshot_and_rehydrated_state_invariants(self) -> None:
        with self.assertRaises(HandoverDataError):
            _custody(status=CustodyStatus.CLOSED)
        with self.assertRaises(HandoverDataError):
            _custody(reserved_quantity=-1)
        with self.assertRaises(HandoverDataError):
            _custody(reserved_quantity=3)
        with self.assertRaises(HandoverDataError):
            _projection(asset_name=" ")
        with self.assertRaises(HandoverDataError):
            _projection(available_quantity=3)
        with self.assertRaises(HandoverDataError):
            _projection(last_qualifying_transaction_at=NOW.replace(tzinfo=None))
        with self.assertRaises(HandoverDataError):
            _item(snapshot_at=NOW.replace(tzinfo=None))
        with self.assertRaises(HandoverDataError):
            _item(last_qualifying_transaction_at=NOW + timedelta(seconds=1))
        with self.assertRaises(HandoverDataError):
            _handover(items=())
        with self.assertRaises(HandoverDataError):
            _handover(items=(_item(handover_id=UUID(int=999)),))
        with self.assertRaises(HandoverDataError):
            _handover(items=(_item(), _item(id=UUID(int=998))))
        with self.assertRaises(HandoverDataError):
            _handover(
                old_confirmed_by_user_id=OLD_HR_ID,
                old_confirmed_at=None,
            )
        with self.assertRaises(HandoverDataError):
            _handover(
                old_confirmed_by_user_id=NEW_HR_ID,
                old_confirmed_at=NOW,
            )
        with self.assertRaises(HandoverDataError):
            _handover(
                new_confirmed_by_user_id=OLD_HR_ID,
                new_confirmed_at=NOW,
            )
        with self.assertRaises(HandoverDataError):
            _handover(status=HandoverStatus.LOCKED)
        with self.assertRaises(HandoverDataError):
            _handover(locked_at=NOW)
        with self.assertRaises(HandoverDataError):
            _handover(
                old_confirmed_by_user_id=OLD_HR_ID,
                old_confirmed_at=NOW - timedelta(seconds=1),
            )


class FixedClock(IClock):
    """Return the fixed post-term test instant."""

    def now(self) -> datetime:
        """Return deterministic UTC time."""

        return NOW


class FakeHandoverRepository:
    """Store one configured term, snapshot projections, and aggregate."""

    def __init__(self) -> None:
        self.term: Term | None = _term()
        self.handover: Handover | None = None
        self.already_exists = False
        self.projections: tuple[AssetHandoverSnapshot, ...] = (
            _projection(last_qualifying_transaction_at=datetime(2030, 1, 1, tzinfo=UTC)),
        )

    async def get_term(self, term_id: UUID) -> Term | None:
        return self.term if self.term and self.term.id == term_id else None

    async def has_handover_for_term(self, term_id: UUID) -> bool:
        return self.already_exists

    async def snapshot_assets(self, term: Term) -> tuple[AssetHandoverSnapshot, ...]:
        return self.projections

    async def add(self, handover: Handover) -> None:
        self.handover = handover

    async def get(self, handover_id: UUID) -> Handover | None:
        return self.handover if self.handover and self.handover.id == handover_id else None

    async def get_for_update(self, handover_id: UUID) -> Handover | None:
        return await self.get(handover_id)

    async def save(self, handover: Handover, *, updated_at: datetime) -> None:
        self.handover = handover


class FakeUsers:
    """Return active identities unless explicitly unavailable."""

    def __init__(self) -> None:
        self.missing: set[UUID] = set()
        self.inactive: set[UUID] = set()

    async def get_identity(self, user_id: UUID) -> UserIdentity | None:
        if user_id in self.missing:
            return None
        return UserIdentity(
            user_id,
            f"user-{user_id.int}",
            f"{user_id.int}@example.test",
            "HR",
            frozenset({OperationalRole.HR}),
            frozenset(),
            user_id not in self.inactive,
        )


class FakeOutbox:
    """Collect transactional Handover notifications."""

    def __init__(self) -> None:
        self.events: list[OutboxEvent] = []

    async def add(self, event: OutboxEvent) -> None:
        self.events.append(event)


class FakeUnitOfWork:
    """Expose the F11 ports under a no-op transaction boundary."""

    def __init__(self, repository: FakeHandoverRepository) -> None:
        self.handovers = repository
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
    """Execute one operation against the shared fake Unit of Work."""

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


class HandoverServiceTests(unittest.IsolatedAsyncioTestCase):
    """Verify authorization, stale snapshots, idempotency, notifications, and CSV."""

    async def test_create_new_first_old_locks_and_exports_stable_snapshot(self) -> None:
        service, repository, unit_of_work, executor = _service()
        created = await service.create(TERM_ID, _actor(OLD_HR_ID), "create")
        self.assertFalse(created.handover.items[0].is_stale)
        self.assertEqual(len(unit_of_work.outbox.events), 1)

        await service.confirm_new(created.handover.id, _actor(NEW_HR_ID), "confirm-new")
        executor.replayed = True
        locked = await service.confirm_old(
            created.handover.id, _actor(OLD_HR_ID), "confirm-old"
        )
        self.assertTrue(locked.replayed)
        self.assertEqual(locked.handover.status, HandoverStatus.LOCKED)
        self.assertEqual(
            [value.event_type for value in unit_of_work.outbox.events],
            ["HANDOVER_CREATED", "HANDOVER_NEW_CONFIRMED", "HANDOVER_LOCKED"],
        )
        export = await service.export_csv(created.handover.id, _actor(NEW_HR_ID))
        self.assertTrue(export.content.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"asset_name", export.content)
        self.assertIn(b"Camera", export.content)
        assert repository.handover is not None
        self.assertIsNone(repository.handover.items[0].custodies[0].condition_note)

    async def test_get_scope_creation_and_export_guards(self) -> None:
        service, repository, unit_of_work, _ = _service()
        with self.assertRaises(HandoverRoleRequiredError):
            await service.create(TERM_ID, CurrentActor(OLD_HR_ID, frozenset()), "role")
        with self.assertRaises(TermNotFoundError):
            await service.create(UUID(int=404), _actor(OLD_HR_ID), "term")
        repository.term = _term(ends_on=date(2030, 3, 1))
        with self.assertRaises(TermNotEndedError):
            await service.create(TERM_ID, _actor(OLD_HR_ID), "not-ended")
        repository.term = _term()
        with self.assertRaises(HandoverRoleRequiredError):
            await service.create(TERM_ID, _actor(NEW_HR_ID), "wrong-creator")
        repository.already_exists = True
        with self.assertRaises(HandoverAlreadyExistsError):
            await service.create(TERM_ID, _actor(OLD_HR_ID), "duplicate")
        repository.already_exists = False
        unit_of_work.users.inactive.add(NEW_HR_ID)
        with self.assertRaises(TermNotFoundError):
            await service.create(TERM_ID, _actor(OLD_HR_ID), "inactive-user")

        unit_of_work.users.inactive.clear()
        created = await service.create(TERM_ID, _actor(OLD_HR_ID), "created")
        loaded = await service.get(created.handover.id, _actor(OLD_HR_ID))
        self.assertEqual(loaded.handover.id, created.handover.id)
        with self.assertRaises(HandoverNotFoundError):
            await service.get(created.handover.id, _actor(OUTSIDER_ID))
        with self.assertRaises(HandoverExportForbiddenError):
            await service.export_csv(created.handover.id, _actor(OUTSIDER_ID))
        with (
            patch("ams.application.handovers.MAX_EXPORT_ITEMS", 0),
            self.assertRaises(HandoverExportLimitError),
        ):
            await service.export_csv(created.handover.id, _actor(OLD_HR_ID))
        with (
            patch("ams.application.handovers.MAX_EXPORT_BYTES", 1),
            self.assertRaises(HandoverExportLimitError),
        ):
            await service.export_csv(created.handover.id, _actor(OLD_HR_ID))

    async def test_export_neutralizes_spreadsheet_formula_injection(self) -> None:
        service, repository, _, _ = _service()
        repository.handover = _handover(items=(_item(asset_name="=1+1"),))

        export = await service.export_csv(HANDOVER_ID, _actor(OLD_HR_ID))

        self.assertIn(b"'=1+1", export.content)
        self.assertNotIn(b",=1+1,", export.content)

    async def test_missing_snapshot_and_confirmation_resources_are_rejected(self) -> None:
        service, repository, _, _ = _service()
        repository.projections = ()
        with self.assertRaises(HandoverDataError):
            await service.create(TERM_ID, _actor(OLD_HR_ID), "empty")
        with self.assertRaises(HandoverNotFoundError):
            await service.confirm_old(HANDOVER_ID, _actor(OLD_HR_ID), "missing")
        with self.assertRaises(HandoverNotFoundError):
            await service.export_csv(HANDOVER_ID, _actor(OLD_HR_ID))


def _term(**changes: object) -> Term:
    values: dict[str, object] = {
        "id": TERM_ID,
        "code": "2029-2030",
        "name": "Term 2029-2030",
        "starts_on": date(2030, 1, 1),
        "ends_on": date(2030, 1, 31),
        "old_hr_user_id": OLD_HR_ID,
        "new_hr_user_id": NEW_HR_ID,
    }
    values.update(changes)
    return Term(**values)  # type: ignore[arg-type]


def _custody(**changes: object) -> HandoverCustodySnapshot:
    values: dict[str, object] = {
        "custody_id": CUSTODY_ID,
        "custodian_user_id": OLD_HR_ID,
        "status": CustodyStatus.ACTIVE,
        "on_hand_quantity": 2,
        "reserved_quantity": 0,
        "temporarily_loaned_quantity": 0,
        "condition": AssetCondition.GOOD,
        "condition_note": None,
    }
    values.update(changes)
    return HandoverCustodySnapshot(**values)  # type: ignore[arg-type]


def _projection(**changes: object) -> AssetHandoverSnapshot:
    values: dict[str, object] = {
        "asset_id": ASSET_ID,
        "asset_name": "Camera",
        "total_quantity": 2,
        "available_quantity": 2,
        "condition": AssetCondition.GOOD,
        "custodies": (_custody(),),
        "last_qualifying_transaction_at": None,
    }
    values.update(changes)
    return AssetHandoverSnapshot(**values)  # type: ignore[arg-type]


def _item(**changes: object) -> HandoverItem:
    values: dict[str, object] = {
        "id": ITEM_ID,
        "handover_id": HANDOVER_ID,
        "asset_id": ASSET_ID,
        "asset_name": "Camera",
        "total_quantity": 2,
        "available_quantity": 2,
        "condition": AssetCondition.GOOD,
        "custodies": (_custody(),),
        "is_stale": False,
        "snapshot_at": NOW,
        "last_qualifying_transaction_at": datetime(2030, 1, 1, tzinfo=UTC),
    }
    values.update(changes)
    return HandoverItem(**values)  # type: ignore[arg-type]


def _handover(**changes: object) -> Handover:
    values: dict[str, object] = {
        "id": HANDOVER_ID,
        "term_id": TERM_ID,
        "term_code": "2029-2030",
        "term_name": "Term 2029-2030",
        "term_starts_on": date(2030, 1, 1),
        "term_ends_on": date(2030, 1, 31),
        "old_hr_user_id": OLD_HR_ID,
        "new_hr_user_id": NEW_HR_ID,
        "created_by_user_id": OLD_HR_ID,
        "created_at": NOW,
        "items": (_item(),),
    }
    values.update(changes)
    return Handover(**values)  # type: ignore[arg-type]


def _actor(user_id: UUID) -> CurrentActor:
    return CurrentActor(user_id, frozenset({OperationalRole.HR}))


_next_id_value = 20_000


def _next_uuid() -> UUID:
    global _next_id_value
    _next_id_value += 1
    return UUID(int=_next_id_value)


def _service() -> tuple[HandoverService, FakeHandoverRepository, FakeUnitOfWork, FakeExecutor]:
    repository = FakeHandoverRepository()
    unit_of_work = FakeUnitOfWork(repository)
    executor = FakeExecutor(unit_of_work)
    service = HandoverService(
        executor,
        lambda: cast(IUnitOfWork, unit_of_work),
        FixedClock(),
        id_factory=_next_uuid,
    )
    return service, repository, unit_of_work, executor
