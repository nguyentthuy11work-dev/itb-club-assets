"""Unit coverage for grouped Ticket creation, lifecycle commands, and queries."""

from __future__ import annotations

import unittest
from collections.abc import AsyncIterable, Callable, Mapping
from datetime import UTC, date, datetime
from typing import cast
from uuid import UUID

from ams.application.evidence import EvidenceFileInput, EvidenceStorageService
from ams.application.idempotency import (
    IdempotentCommandResult,
    IdempotentOperation,
    IIdempotentCommandExecutor,
)
from ams.application.reliability import IClock, IFileStorage, OutboxEvent, StagedFile
from ams.application.ticket_commands import (
    TicketCommandResultError,
    TicketEvidenceMetadata,
    TicketLifecycleService,
    TicketResourceNotFoundError,
)
from ams.application.ticket_queries import TicketReadNotFoundError, TicketReadService
from ams.application.ticket_reminders import TicketReminderScheduler
from ams.application.tickets import (
    CreateTicketAllocationCommand,
    CreateTicketsCommand,
    TicketCreationResultError,
    TicketCreationService,
)
from ams.application.unit_of_work import IUnitOfWork
from ams.domain.assets import Asset, AssetCondition, Custody, CustodyStatus
from ams.domain.authentication import UserIdentity
from ams.domain.identity import AuthorizationError, CurrentActor
from ams.domain.ticket_views import (
    ITicketReadRepository,
    TicketDetailView,
    TicketListItemView,
    TicketPerspective,
)
from ams.domain.tickets import (
    BorrowTicket,
    InvalidTicketTransitionError,
    TicketEvent,
    TicketEvidence,
    TicketItem,
    TicketMethod,
    TicketStatus,
)

BORROWER_ID = UUID("00000000-0000-4000-8000-000000005501")
CUSTODIAN_ID = UUID("00000000-0000-4000-8000-000000005502")
ASSET_ID = UUID("00000000-0000-4000-8000-000000005503")
CUSTODY_ID = UUID("00000000-0000-4000-8000-000000005504")
TICKET_ID = UUID("00000000-0000-4000-8000-000000005505")
EVIDENCE_ID = UUID("00000000-0000-4000-8000-000000005506")
NOW = datetime(2026, 10, 5, 4, 0, tzinfo=UTC)


class FixedClock(IClock):
    """Return one deterministic UTC timestamp."""

    def now(self) -> datetime:
        """Return the fixed test instant."""

        return NOW


class FakeExecutor(IIdempotentCommandExecutor):
    """Invoke operations against one fake UoW or replay a stored body."""

    def __init__(
        self,
        unit_of_work: FakeUnitOfWork,
        stored_body: Mapping[str, object] | None = None,
    ) -> None:
        self.unit_of_work = unit_of_work
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
        """Return replay data or run one operation without transaction mechanics."""

        del actor_id, command_name, idempotency_key, request_fingerprint
        self.calls += 1
        if self.stored_body is not None:
            return IdempotentCommandResult(200, self.stored_body, True)
        response = await operation(cast(IUnitOfWork, self.unit_of_work))
        return IdempotentCommandResult(response.status_code, response.body, False)


class FakeTicketRepository:
    """Store one aggregate plus staged event and evidence children."""

    def __init__(self, ticket: BorrowTicket | None = None) -> None:
        self.ticket = ticket
        self.added: list[BorrowTicket] = []
        self.events: list[TicketEvent] = []
        self.evidence: list[TicketEvidence] = []

    async def add(self, ticket: BorrowTicket, *, created_at: datetime) -> None:
        del created_at
        self.ticket = ticket
        self.added.append(ticket)

    async def get_for_update(self, ticket_id: UUID) -> BorrowTicket | None:
        if self.ticket is not None and self.ticket.id == ticket_id:
            return self.ticket
        return None

    async def save(self, ticket: BorrowTicket, *, updated_at: datetime) -> None:
        del updated_at
        self.ticket = ticket

    async def add_event(self, event: TicketEvent) -> None:
        self.events.append(event)

    async def add_evidence(self, evidence: TicketEvidence) -> None:
        self.evidence.append(evidence)


class FakeCustodyRepository:
    """Lock and persist source and optional target Custody entities."""

    def __init__(self, source: Custody | None = None) -> None:
        self.rows: dict[UUID, Custody] = {}
        if source is not None:
            self.rows[source.id] = source
        self.target: Custody | None = None

    async def get_many_for_update(self, ids: tuple[UUID, ...]) -> tuple[Custody, ...]:
        return tuple(
            self.rows[value] for value in sorted(set(ids), key=str) if value in self.rows
        )

    async def get_current_for_update(
        self, asset_id: UUID, custodian_user_id: UUID
    ) -> Custody | None:
        if (
            self.target is not None
            and self.target.asset_id == asset_id
            and self.target.custodian_user_id == custodian_user_id
        ):
            return self.target
        return None

    async def save(self, custody: Custody) -> None:
        self.rows[custody.id] = custody
        if custody.custodian_user_id == BORROWER_ID:
            self.target = custody

    async def add(self, custody: Custody) -> None:
        await self.save(custody)


class FakeAssetRepository:
    """Lock and store one Asset aggregate."""

    def __init__(self, asset: Asset | None) -> None:
        self.asset = asset

    async def get_for_update(self, asset_id: UUID) -> Asset | None:
        if self.asset is not None and self.asset.id == asset_id:
            return self.asset
        return None

    async def save(self, asset: Asset) -> None:
        self.asset = asset


class FakeUserRepository:
    """Resolve known Ticket participants."""

    def __init__(self, *, include_custodian: bool = True) -> None:
        self.users = {
            BORROWER_ID: _identity(BORROWER_ID, "Borrower"),
        }
        if include_custodian:
            self.users[CUSTODIAN_ID] = _identity(CUSTODIAN_ID, "Custodian")

    async def get_identity(self, user_id: UUID) -> UserIdentity | None:
        return self.users.get(user_id)


class FakeOutboxRepository:
    """Capture staged notification events."""

    def __init__(self) -> None:
        self.events: list[OutboxEvent] = []

    async def add(self, event: OutboxEvent) -> None:
        self.events.append(event)


class FakeTicketReminderRepository:
    """Capture synchronized and cancelled Ticket reminder slots."""

    def __init__(self) -> None:
        self.reminders: tuple[OutboxEvent, ...] = ()
        self.cancelled: list[UUID] = []

    async def replace_unattempted(
        self, ticket_id: UUID, reminders: tuple[OutboxEvent, ...]
    ) -> None:
        del ticket_id
        self.reminders = reminders

    async def cancel_unattempted(self, ticket_id: UUID) -> None:
        self.cancelled.append(ticket_id)
        self.reminders = ()


class FakeUnitOfWork:
    """Minimal repository surface used by Ticket services."""

    def __init__(
        self,
        ticket: BorrowTicket | None = None,
        *,
        source: Custody | None = None,
        asset: Asset | None = None,
        include_custodian: bool = True,
    ) -> None:
        self.tickets = FakeTicketRepository(ticket)
        self.custodies = FakeCustodyRepository(source)
        self.assets = FakeAssetRepository(asset)
        self.users = FakeUserRepository(include_custodian=include_custodian)
        self.outbox = FakeOutboxRepository()
        self.ticket_reminders = FakeTicketReminderRepository()


class FakeFileStorage(IFileStorage):
    """Track staged, finalized, and deleted private object keys."""

    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.finalized: list[str] = []

    async def stage(
        self, content: AsyncIterable[bytes], declared_content_type: str
    ) -> StagedFile:
        data = b"".join([chunk async for chunk in content])
        return StagedFile("staging/one", declared_content_type, "a" * 64, len(data))

    async def finalize(self, staging_key: str, owner_type: str, owner_id: UUID) -> str:
        key = f"{owner_type}/{owner_id}/one"
        self.finalized.append(key)
        return key

    async def delete(self, object_key: str) -> None:
        self.deleted.append(object_key)


class FakeTicketReadRepository(ITicketReadRepository):
    """Return configured Ticket query projections and capture filters."""

    def __init__(self, detail: TicketDetailView | None) -> None:
        self.detail = detail
        self.search: str | None = None

    async def list_for_actor(
        self,
        actor_id: UUID,
        *,
        perspective: TicketPerspective,
        status: TicketStatus | None,
        search: str | None,
        asset_id: UUID | None,
        offset: int,
        limit: int,
    ) -> tuple[tuple[TicketListItemView, ...], int]:
        del actor_id, perspective, status, asset_id, offset, limit
        self.search = search
        return (
            () if self.detail is None else (self.detail.summary,),
            int(self.detail is not None),
        )

    async def get_for_actor(self, ticket_id: UUID, actor_id: UUID) -> TicketDetailView | None:
        del ticket_id, actor_id
        return self.detail


class TicketCreationServiceTests(unittest.IsolatedAsyncioTestCase):
    """Cover partial creation, validation reasons, replay, and authorization."""

    async def test_create_groups_reserves_and_reports_missing_source(self) -> None:
        source = _source()
        uow = FakeUnitOfWork(source=source, asset=_asset())
        service = TicketCreationService(
            FakeExecutor(uow), FixedClock(), _scheduler(), id_factory=_ids()
        )
        command = CreateTicketsCommand(
            (
                _allocation(),
                _allocation(quantity=1),
                CreateTicketAllocationCommand(
                    ASSET_ID,
                    UUID("00000000-0000-4000-8000-000000005599"),
                    1,
                    TicketMethod.TEMPORARY,
                    date(2026, 10, 15),
                ),
            )
        )

        result = await service.create(command, _actor(), "create-key")

        self.assertEqual(len(result.created_tickets), 1)
        self.assertEqual(result.rejected_groups[0].reason_code, "SOURCE_CUSTODY_UNAVAILABLE")
        self.assertEqual(source.reserved_quantity, 3)
        self.assertEqual(uow.assets.asset.available_quantity if uow.assets.asset else None, 2)
        self.assertEqual(len(uow.tickets.events), 1)
        self.assertEqual(len(uow.outbox.events), 1)
        self.assertFalse(result.replayed)

    async def test_create_reports_asset_quantity_and_identity_failures(self) -> None:
        cases = (
            (FakeUnitOfWork(source=_source(), asset=None), "ASSET_UNAVAILABLE"),
            (
                FakeUnitOfWork(source=_source(), asset=_asset(available=1)),
                "INSUFFICIENT_LENDABLE_QUANTITY",
            ),
            (
                FakeUnitOfWork(source=_source(), asset=_asset(), include_custodian=False),
                "CUSTODIAN_UNAVAILABLE",
            ),
        )
        for uow, reason in cases:
            with self.subTest(reason=reason):
                result = await TicketCreationService(
                    FakeExecutor(uow), FixedClock(), _scheduler(), id_factory=_ids()
                ).create(CreateTicketsCommand((_allocation(),)), _actor(), reason)
                self.assertEqual(result.rejected_groups[0].reason_code, reason)
                self.assertFalse(result.created_tickets)

        low_source = _source()
        low_source.reserve(4)
        result = await TicketCreationService(
            FakeExecutor(FakeUnitOfWork(source=low_source, asset=_asset())),
            FixedClock(),
            _scheduler(),
            id_factory=_ids(),
        ).create(CreateTicketsCommand((_allocation(),)), _actor(), "low-source")
        self.assertEqual(
            result.rejected_groups[0].reason_code, "INSUFFICIENT_LENDABLE_QUANTITY"
        )

    async def test_create_replay_validation_and_inactive_actor(self) -> None:
        valid: Mapping[str, object] = {
            "createdTickets": [{"id": str(TICKET_ID), "status": "PENDING"}],
            "rejectedGroups": [],
        }
        result = await TicketCreationService(
            FakeExecutor(FakeUnitOfWork(), valid), FixedClock(), _scheduler()
        ).create(CreateTicketsCommand((_allocation(),)), _actor(), "replay")
        self.assertTrue(result.replayed)
        self.assertEqual(result.created_tickets[0].id, TICKET_ID)

        for body in ({}, {"createdTickets": "bad", "rejectedGroups": []}):
            with self.subTest(body=body), self.assertRaises(TicketCreationResultError):
                await TicketCreationService(
                    FakeExecutor(FakeUnitOfWork(), body), FixedClock(), _scheduler()
                ).create(CreateTicketsCommand((_allocation(),)), _actor(), "bad")

        executor = FakeExecutor(FakeUnitOfWork())
        with self.assertRaises(AuthorizationError):
            await TicketCreationService(executor, FixedClock(), _scheduler()).create(
                CreateTicketsCommand((_allocation(),)),
                CurrentActor(BORROWER_ID, frozenset(), is_active=False),
                "inactive",
            )
        self.assertEqual(executor.calls, 0)


class TicketLifecycleServiceTests(unittest.IsolatedAsyncioTestCase):
    """Cover all command branches, quantity effects, cleanup, and replay parsing."""

    async def test_json_transitions_and_release_paths(self) -> None:
        approve_uow = _uow(TicketStatus.PENDING)
        approved = await _service(approve_uow).approve(TICKET_ID, _custodian_actor(), "a")
        self.assertEqual(approved.status, TicketStatus.APPROVED)

        reject_uow = _uow(TicketStatus.PENDING)
        rejected = await _service(reject_uow).reject(
            TICKET_ID, "Unavailable", _custodian_actor(), "r"
        )
        self.assertEqual(rejected.status, TicketStatus.REJECTED)
        self.assertEqual(_source_from(reject_uow).reserved_quantity, 0)

        cancel_uow = _uow(TicketStatus.PENDING)
        cancelled = await _service(cancel_uow).request_cancellation(
            TICKET_ID, "Plans changed", _actor(), "c"
        )
        self.assertEqual(cancelled.status, TicketStatus.CANCELLED)

        request_uow = _uow(TicketStatus.APPROVED)
        requested = await _service(request_uow).request_cancellation(
            TICKET_ID, "Plans changed", _actor(), "cp"
        )
        self.assertEqual(requested.status, TicketStatus.CANCEL_PENDING)
        restored = await _service(request_uow).resolve_cancellation(
            TICKET_ID, False, _custodian_actor(), "restore"
        )
        self.assertEqual(restored.status, TicketStatus.APPROVED)

        request_uow.tickets.ticket.status = TicketStatus.CANCEL_PENDING  # type: ignore[union-attr]
        accepted = await _service(request_uow).resolve_cancellation(
            TICKET_ID, True, _custodian_actor(), "accept"
        )
        self.assertEqual(accepted.status, TicketStatus.CANCELLED)
        self.assertEqual(_source_from(request_uow).reserved_quantity, 0)

    async def test_temporary_evidence_actions_move_each_quantity_partition(self) -> None:
        storage = FakeFileStorage()
        uow = _uow(TicketStatus.APPROVED)
        service = _service(uow, storage)
        received = await service.confirm_received(
            TICKET_ID, TicketEvidenceMetadata("pickup"), _files(), _actor(), "pickup"
        )
        self.assertEqual(received.status, TicketStatus.BORROWING)
        self.assertEqual(_source_from(uow).temporarily_loaned_quantity, 2)

        returned = await service.mark_returned(
            TICKET_ID, TicketEvidenceMetadata(), _files(), _actor(), "return"
        )
        self.assertEqual(returned.status, TicketStatus.RETURNING)
        self.assertEqual(_source_from(uow).temporarily_loaned_quantity, 2)

        closed = await service.confirm_returned(
            TICKET_ID,
            TicketEvidenceMetadata("received"),
            _files(),
            _custodian_actor(),
            "closed",
        )
        self.assertEqual(closed.status, TicketStatus.CLOSED)
        self.assertEqual(_source_from(uow).temporarily_loaned_quantity, 0)
        self.assertEqual(uow.assets.asset.available_quantity if uow.assets.asset else None, 5)
        self.assertEqual(len(uow.tickets.evidence), 3)

    async def test_transfer_creates_or_merges_target_custody(self) -> None:
        created_uow = _uow(TicketStatus.APPROVED, method=TicketMethod.CUSTODY_TRANSFER)
        result = await _service(created_uow).confirm_received(
            TICKET_ID, TicketEvidenceMetadata(), _files(), _actor(), "transfer"
        )
        self.assertEqual(result.status, TicketStatus.CLOSED)
        self.assertIsNotNone(created_uow.custodies.target)
        self.assertEqual(created_uow.custodies.target.on_hand_quantity, 2)  # type: ignore[union-attr]

        merged_uow = _uow(TicketStatus.APPROVED, method=TicketMethod.CUSTODY_TRANSFER)
        merged_uow.custodies.target = _target()
        await _service(merged_uow).confirm_received(
            TICKET_ID, TicketEvidenceMetadata(), _files(), _actor(), "merge"
        )
        self.assertEqual(merged_uow.custodies.target.on_hand_quantity, 3)

        pending_uow = _uow(TicketStatus.APPROVED, method=TicketMethod.CUSTODY_TRANSFER)
        pending_uow.custodies.target = Custody.create_pending(
            custody_id=EVIDENCE_ID,
            asset_id=ASSET_ID,
            custodian_user_id=BORROWER_ID,
            assigned_by_user_id=CUSTODIAN_ID,
            quantity=1,
            assigned_at=NOW,
        )
        with self.assertRaisesRegex(Exception, "Target Custody"):
            await _service(pending_uow).confirm_received(
                TICKET_ID, TicketEvidenceMetadata(), _files(), _actor(), "pending"
            )

    async def test_command_missing_dependencies_cleanup_replay_and_inactive(self) -> None:
        with self.assertRaises(TicketResourceNotFoundError):
            await _service(FakeUnitOfWork()).approve(TICKET_ID, _custodian_actor(), "none")

        missing_source = _uow(TicketStatus.PENDING)
        missing_source.custodies.rows.clear()
        with self.assertRaises(TicketResourceNotFoundError):
            await _service(missing_source).reject(TICKET_ID, "No", _custodian_actor(), "source")

        missing_asset = _uow(TicketStatus.PENDING)
        missing_asset.assets.asset = None
        with self.assertRaises(TicketResourceNotFoundError):
            await _service(missing_asset).reject(TICKET_ID, "No", _custodian_actor(), "asset")

        missing_user = _uow(TicketStatus.PENDING)
        missing_user.users.users.pop(BORROWER_ID)
        with self.assertRaises(TicketResourceNotFoundError):
            await _service(missing_user).reject(TICKET_ID, "No", _custodian_actor(), "user")

        inactive_executor = FakeExecutor(_uow(TicketStatus.PENDING))
        inactive = TicketLifecycleService(
            inactive_executor,
            EvidenceStorageService(FakeFileStorage()),
            FixedClock(),
            _scheduler(),
        )
        with self.assertRaises(AuthorizationError):
            await inactive.approve(
                TICKET_ID,
                CurrentActor(CUSTODIAN_ID, frozenset(), is_active=False),
                "inactive",
            )
        self.assertEqual(inactive_executor.calls, 0)

        replay_body: Mapping[str, object] = {
            "ticketId": str(TICKET_ID),
            "status": "APPROVED",
        }
        replay = TicketLifecycleService(
            FakeExecutor(FakeUnitOfWork(), replay_body),
            EvidenceStorageService(FakeFileStorage()),
            FixedClock(),
            _scheduler(),
        )
        self.assertTrue(
            (await replay.approve(TICKET_ID, _custodian_actor(), "replay")).replayed
        )
        replay_storage = FakeFileStorage()
        evidence_replay = TicketLifecycleService(
            FakeExecutor(
                FakeUnitOfWork(),
                {"ticketId": str(TICKET_ID), "status": "BORROWING"},
            ),
            EvidenceStorageService(replay_storage),
            FixedClock(),
            _scheduler(),
        )
        self.assertTrue(
            (
                await evidence_replay.confirm_received(
                    TICKET_ID,
                    TicketEvidenceMetadata(),
                    _files(),
                    _actor(),
                    "evidence-replay",
                )
            ).replayed
        )
        self.assertIn("staging/one", replay_storage.deleted)
        with self.assertRaises(TicketCommandResultError):
            await TicketLifecycleService(
                FakeExecutor(FakeUnitOfWork(), {}),
                EvidenceStorageService(FakeFileStorage()),
                FixedClock(),
                _scheduler(),
            ).approve(TICKET_ID, _custodian_actor(), "bad")

        cleanup_storage = FakeFileStorage()
        with self.assertRaises(InvalidTicketTransitionError):
            await _service(_uow(TicketStatus.PENDING), cleanup_storage).confirm_received(
                TICKET_ID, TicketEvidenceMetadata(), _files(), _actor(), "wrong-state"
            )
        self.assertTrue(cleanup_storage.deleted)


class TicketReadServiceTests(unittest.IsolatedAsyncioTestCase):
    """Cover query normalization, not-found behavior, and active-account policy."""

    async def test_list_detail_not_found_and_inactive(self) -> None:
        detail = TicketDetailView(_summary(), (), (), ())
        repository = FakeTicketReadRepository(detail)
        service = TicketReadService(repository)
        page = await service.list_tickets(
            _actor(),
            perspective=TicketPerspective.ALL,
            status=None,
            search="  camera  ",
            asset_id=None,
            offset=0,
            limit=20,
        )
        self.assertEqual(page.total, 1)
        self.assertEqual(repository.search, "camera")
        self.assertEqual((await service.get_ticket(_actor(), TICKET_ID)).summary.id, TICKET_ID)

        missing = TicketReadService(FakeTicketReadRepository(None))
        with self.assertRaises(TicketReadNotFoundError):
            await missing.get_ticket(_actor(), TICKET_ID)
        inactive = CurrentActor(BORROWER_ID, frozenset(), is_active=False)
        with self.assertRaises(AuthorizationError):
            await missing.list_tickets(
                inactive,
                perspective=TicketPerspective.ALL,
                status=None,
                search=None,
                asset_id=None,
                offset=0,
                limit=20,
            )


def _identity(user_id: UUID, name: str) -> UserIdentity:
    return UserIdentity(
        id=user_id,
        username=name.lower(),
        email=f"{name.lower()}@example.test",
        display_name=name,
        roles=frozenset(),
        team_ids=frozenset(),
        is_active=True,
    )


def _actor() -> CurrentActor:
    return CurrentActor(BORROWER_ID, frozenset(), is_active=True)


def _custodian_actor() -> CurrentActor:
    return CurrentActor(CUSTODIAN_ID, frozenset(), is_active=True)


def _asset(*, available: int = 5) -> Asset:
    return Asset(
        id=ASSET_ID,
        category_id=EVIDENCE_ID,
        responsible_team_id=None,
        name="Camera",
        source="Purchase",
        total_quantity=5,
        available_quantity=available,
        created_by_user_id=CUSTODIAN_ID,
        created_at=NOW,
    )


def _source() -> Custody:
    return Custody(
        id=CUSTODY_ID,
        asset_id=ASSET_ID,
        custodian_user_id=CUSTODIAN_ID,
        assigned_by_user_id=CUSTODIAN_ID,
        status=CustodyStatus.ACTIVE,
        on_hand_quantity=5,
        reserved_quantity=0,
        temporarily_loaned_quantity=0,
        condition=AssetCondition.GOOD,
        assigned_at=NOW,
        confirmed_at=NOW,
        confirmed_by_user_id=CUSTODIAN_ID,
        evidence_ids=(EVIDENCE_ID,),
    )


def _target() -> Custody:
    target = _source()
    target.id = EVIDENCE_ID
    target.custodian_user_id = BORROWER_ID
    target.on_hand_quantity = 1
    return target


def _ticket(
    status: TicketStatus, *, method: TicketMethod = TicketMethod.TEMPORARY
) -> BorrowTicket:
    return BorrowTicket(
        id=TICKET_ID,
        borrower_user_id=BORROWER_ID,
        source_custodian_user_id=CUSTODIAN_ID,
        method=method,
        status=status,
        due_date=date(2026, 10, 15) if method is TicketMethod.TEMPORARY else None,
        items=(TicketItem(ASSET_ID, CUSTODY_ID, 2),),
    )


def _uow(
    status: TicketStatus, *, method: TicketMethod = TicketMethod.TEMPORARY
) -> FakeUnitOfWork:
    source = _source()
    source.reserve(2)
    return FakeUnitOfWork(
        _ticket(status, method=method), source=source, asset=_asset(available=3)
    )


def _source_from(unit_of_work: FakeUnitOfWork) -> Custody:
    return unit_of_work.custodies.rows[CUSTODY_ID]


def _allocation(quantity: int = 2) -> CreateTicketAllocationCommand:
    return CreateTicketAllocationCommand(
        ASSET_ID,
        CUSTODY_ID,
        quantity,
        TicketMethod.TEMPORARY,
        date(2026, 10, 15),
    )


async def _content() -> AsyncIterable[bytes]:
    yield b"image"


def _files() -> tuple[EvidenceFileInput, ...]:
    return (EvidenceFileInput(_content(), "image/png"),)


def _service(
    unit_of_work: FakeUnitOfWork, storage: FakeFileStorage | None = None
) -> TicketLifecycleService:
    return TicketLifecycleService(
        FakeExecutor(unit_of_work),
        EvidenceStorageService(storage or FakeFileStorage()),
        FixedClock(),
        _scheduler(),
        id_factory=_ids(),
    )


def _scheduler() -> TicketReminderScheduler:
    return TicketReminderScheduler(FixedClock(), id_factory=_ids())


def _ids() -> Callable[[], UUID]:
    values = iter(UUID(int=value) for value in range(100, 1000))
    return lambda: next(values)


def _summary() -> TicketListItemView:
    return TicketListItemView(
        id=TICKET_ID,
        borrower_user_id=BORROWER_ID,
        borrower_name="Borrower",
        source_custodian_user_id=CUSTODIAN_ID,
        source_custodian_name="Custodian",
        method=TicketMethod.TEMPORARY,
        status=TicketStatus.PENDING,
        due_date=date(2026, 10, 15),
        total_quantity=2,
        asset_names=("Camera",),
        created_at=NOW,
        updated_at=NOW,
    )
