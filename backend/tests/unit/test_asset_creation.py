"""Unit tests for the F02.04 Asset creation application service."""

from __future__ import annotations

import unittest
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import cast
from uuid import UUID

from ams.application.asset_creation import (
    AssetCreationResultError,
    AssetCreationService,
    AssetReferenceNotFoundError,
    CreateAssetAllocationCommand,
    CreateAssetCommand,
)
from ams.application.idempotency import (
    IdempotentCommandResult,
    IdempotentOperation,
)
from ams.application.reliability import (
    ClaimedOutboxEvent,
    IClock,
    IIdempotencyRepository,
    INotificationRepository,
    IOutboxRepository,
    OutboxEvent,
)
from ams.application.unit_of_work import IUnitOfWork
from ams.domain.assets import (
    AllocationQuantityMismatchError,
    Asset,
    AssetCondition,
    Custody,
)
from ams.domain.authentication import UserIdentity
from ams.domain.identity import AuthorizationError, CurrentActor, OperationalRole
from ams.domain.repositories import IAssetRepository, ICustodyRepository, IUserRepository

HR_ID = UUID("00000000-0000-4000-8000-000000001501")
CUSTODIAN_ID = UUID("00000000-0000-4000-8000-000000001502")
OTHER_CUSTODIAN_ID = UUID("00000000-0000-4000-8000-000000001503")
CATEGORY_ID = UUID("00000000-0000-4000-8000-000000001504")
TEAM_ID = UUID("00000000-0000-4000-8000-000000001505")
ASSET_ID = UUID("00000000-0000-4000-8000-000000001506")
CUSTODY_ID = UUID("00000000-0000-4000-8000-000000001507")
OTHER_CUSTODY_ID = UUID("00000000-0000-4000-8000-000000001508")
EVENT_ID = UUID("00000000-0000-4000-8000-000000001509")
REMINDER_ID = UUID("00000000-0000-4000-8000-000000001510")
OTHER_REMINDER_ID = UUID("00000000-0000-4000-8000-000000001511")
NOW = datetime(2026, 10, 4, 18, 0, tzinfo=UTC)


class FixedClock(IClock):
    """Return a deterministic assignment time."""

    def now(self) -> datetime:
        """Return the configured UTC instant."""

        return NOW


class FakeAssetRepository(IAssetRepository):
    """Capture new Assets and expose configurable active references."""

    def __init__(self) -> None:
        self.category_exists = True
        self.team_exists = True
        self.added: list[Asset] = []

    async def get_by_id(self, asset_id: UUID) -> Asset | None:
        """Return no pre-existing Asset."""

        return None

    async def get_for_update(self, asset_id: UUID) -> Asset | None:
        """Return no pre-existing Asset."""

        return None

    async def add(self, asset: Asset) -> None:
        """Capture one staged Asset."""

        self.added.append(asset)

    async def save(self, asset: Asset) -> None:
        """Reject unused update behavior."""

        raise AssertionError("not used")

    async def active_category_exists(self, category_id: UUID) -> bool:
        """Return configured category state."""

        return self.category_exists

    async def active_team_exists(self, team_id: UUID) -> bool:
        """Return configured Team state."""

        return self.team_exists


class FakeCustodyRepository(ICustodyRepository):
    """Capture pending Custodies staged by the command."""

    def __init__(self) -> None:
        self.added: list[Custody] = []

    async def get_by_id(self, custody_id: UUID) -> Custody | None:
        """Return no pre-existing Custody."""

        return None

    async def get_for_update(self, custody_id: UUID) -> Custody | None:
        """Return no pre-existing Custody."""

        return None

    async def get_many_for_update(self, custody_ids: tuple[UUID, ...]) -> tuple[Custody, ...]:
        """Reject unused bulk locking in Asset creation."""

        raise AssertionError("not used")

    async def get_current_for_update(
        self, asset_id: UUID, custodian_user_id: UUID
    ) -> Custody | None:
        """Reject unused current-Custody locking in Asset creation."""

        raise AssertionError("not used")

    async def add(self, custody: Custody) -> None:
        """Capture one staged pending Custody."""

        self.added.append(custody)

    async def save(self, custody: Custody) -> None:
        """Reject unused update behavior."""

        raise AssertionError("not used")


class FakeOutboxRepository(IOutboxRepository):
    """Capture the transactional assignment event."""

    def __init__(self) -> None:
        self.added: list[OutboxEvent] = []

    async def add(self, event: OutboxEvent) -> None:
        """Capture one staged event."""

        self.added.append(event)

    async def claim_due(
        self,
        *,
        now: datetime,
        worker_id: str,
        lease_expires_at: datetime,
        limit: int,
    ) -> tuple[ClaimedOutboxEvent, ...]:
        """Reject worker behavior in this command fake."""

        raise AssertionError("not used")

    async def mark_delivered(
        self, event_id: UUID, *, worker_id: str, completed_at: datetime
    ) -> None:
        """Reject worker behavior in this command fake."""

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
        """Reject worker behavior in this command fake."""

        raise AssertionError("not used")


class FakeUserRepository(IUserRepository):
    """Resolve configured assignee identities."""

    def __init__(self, users: Mapping[UUID, UserIdentity]) -> None:
        self._users = users

    async def get_local_credential(self, login: str) -> None:
        """Reject credential lookup in this command fake."""

        raise AssertionError("not used")

    async def get_identity(self, user_id: UUID) -> UserIdentity | None:
        """Return the configured assignee identity."""

        return self._users.get(user_id)


class FakeUnitOfWork(IUnitOfWork):
    """Expose command repositories without owning fake commit behavior."""

    def __init__(self, users: Mapping[UUID, UserIdentity]) -> None:
        self.assets: FakeAssetRepository = FakeAssetRepository()
        self.custodies: FakeCustodyRepository = FakeCustodyRepository()
        self.outbox: FakeOutboxRepository = FakeOutboxRepository()
        self.users: FakeUserRepository = FakeUserRepository(users)
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
    """Run an operation immediately or return a configured stored response."""

    def __init__(
        self,
        unit_of_work: FakeUnitOfWork,
        stored_body: Mapping[str, object] | None = None,
    ) -> None:
        self.unit_of_work = unit_of_work
        self.stored_body = stored_body
        self.request_fingerprint: str | None = None
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
        """Capture command identity and execute once unless replay is configured."""

        self.calls += 1
        self.request_fingerprint = request_fingerprint
        if self.stored_body is not None:
            return IdempotentCommandResult(201, self.stored_body, True)
        response = await operation(self.unit_of_work)
        return IdempotentCommandResult(response.status_code, response.body, False)


class AssetCreationServiceTests(unittest.IsolatedAsyncioTestCase):
    """Verify authorization, inputs, outputs, processing, publication, and replay."""

    async def test_creation_schedules_assignment_and_per_custody_reminders(self) -> None:
        """Each pending Custody gets a durable two-recipient reminder at 24 hours."""

        unit_of_work = FakeUnitOfWork(self._users())
        executor = FakeIdempotentExecutor(unit_of_work)
        service = self._service(executor)

        result = await service.create(self._command(), self._hr(), "request-1")

        self.assertEqual(result.asset_id, ASSET_ID)
        self.assertFalse(result.replayed)
        self.assertEqual([item.id for item in result.custodies], [CUSTODY_ID, OTHER_CUSTODY_ID])
        self.assertEqual(len(unit_of_work.assets.added), 1)
        self.assertEqual(len(unit_of_work.custodies.added), 2)
        self.assertEqual(len(unit_of_work.outbox.added), 3)
        assignment = unit_of_work.outbox.added[0]
        self.assertEqual(assignment.id, EVENT_ID)
        self.assertEqual(assignment.event_type, "CUSTODY_ASSIGNED")
        emails = assignment.payload["emails"]
        assert isinstance(emails, list)
        self.assertEqual(len(emails), 2)
        self.assertNotIn("Custodian One", str(emails))
        for reminder, custody in zip(
            unit_of_work.outbox.added[1:],
            unit_of_work.custodies.added,
            strict=True,
        ):
            self.assertEqual(reminder.event_type, "CUSTODY_PENDING_REMINDER")
            self.assertEqual(reminder.aggregate_id, custody.id)
            self.assertEqual(reminder.scheduled_at, NOW + timedelta(hours=24))
            recipients = reminder.payload["emails"]
            assert isinstance(recipients, list)
            self.assertEqual(
                {item["recipientId"] for item in recipients},
                {str(HR_ID), str(custody.custodian_user_id)},
            )
        self.assertEqual(len(executor.request_fingerprint or ""), 64)

    async def test_exact_retry_maps_stored_response_without_staging_effects(self) -> None:
        """A replay reconstructs typed output and does not execute creation processing."""

        body: Mapping[str, object] = {
            "assetId": str(ASSET_ID),
            "custodies": [
                {
                    "id": str(CUSTODY_ID),
                    "custodianUserId": str(CUSTODIAN_ID),
                    "quantity": 2,
                }
            ],
        }
        unit_of_work = FakeUnitOfWork(self._users())
        service = self._service(FakeIdempotentExecutor(unit_of_work, body))

        result = await service.create(self._command(single=True), self._hr(), "request-1")

        self.assertTrue(result.replayed)
        self.assertEqual(result.asset_id, ASSET_ID)
        self.assertEqual(unit_of_work.assets.added, [])
        self.assertEqual(unit_of_work.custodies.added, [])
        self.assertEqual(unit_of_work.outbox.added, [])

    async def test_non_hr_or_inactive_actor_is_rejected_before_idempotency(self) -> None:
        """Application authorization protects non-HTTP consumers as well as the route."""

        for actor in (
            CurrentActor(HR_ID, frozenset(), is_active=True),
            CurrentActor(HR_ID, frozenset({OperationalRole.HR}), is_active=False),
        ):
            unit_of_work = FakeUnitOfWork(self._users())
            executor = FakeIdempotentExecutor(unit_of_work)
            with self.subTest(actor=actor), self.assertRaises(AuthorizationError):
                await self._service(executor).create(self._command(), actor, "request-1")
            self.assertEqual(executor.calls, 0)

    async def test_invalid_allocation_is_rejected_without_staged_rows(self) -> None:
        """Domain quantity validation runs before repositories receive any aggregate."""

        unit_of_work = FakeUnitOfWork(self._users())
        executor = FakeIdempotentExecutor(unit_of_work)
        command = self._command(single=True, total_quantity=1, allocation_quantity=2)
        with self.assertRaises(AllocationQuantityMismatchError):
            await self._service(executor).create(command, self._hr(), "request-1")
        self.assertEqual(unit_of_work.assets.added, [])

    async def test_missing_or_inactive_references_are_rejected(self) -> None:
        """Category, Team, and assignee references must resolve as active."""

        category_uow = FakeUnitOfWork(self._users())
        category_uow.assets.category_exists = False
        team_uow = FakeUnitOfWork(self._users())
        team_uow.assets.team_exists = False
        missing_assigner_uow = FakeUnitOfWork({})
        missing_user_uow = FakeUnitOfWork({HR_ID: self._identity(HR_ID)})
        inactive_user_uow = FakeUnitOfWork(
            {
                HR_ID: self._identity(HR_ID),
                CUSTODIAN_ID: self._identity(CUSTODIAN_ID, active=False),
            }
        )
        cases = (
            (category_uow, self._command(single=True)),
            (team_uow, self._command(single=True)),
            (missing_assigner_uow, self._command(single=True, responsible_team_id=None)),
            (missing_user_uow, self._command(single=True, responsible_team_id=None)),
            (inactive_user_uow, self._command(single=True, responsible_team_id=None)),
        )
        for unit_of_work, command in cases:
            with (
                self.subTest(unit_of_work=unit_of_work),
                self.assertRaises(AssetReferenceNotFoundError),
            ):
                await self._service(FakeIdempotentExecutor(unit_of_work)).create(
                    command, self._hr(), "request-1"
                )
            self.assertEqual(unit_of_work.assets.added, [])

    async def test_invalid_stored_response_is_rejected(self) -> None:
        """Corrupt replay JSON cannot escape as an incorrect successful API result."""

        invalid_bodies: tuple[Mapping[str, object], ...] = (
            {},
            {"assetId": "bad", "custodies": []},
            {"assetId": str(ASSET_ID), "custodies": "bad"},
            {"assetId": str(ASSET_ID), "custodies": ["bad"]},
            {
                "assetId": str(ASSET_ID),
                "custodies": [
                    {
                        "id": str(CUSTODY_ID),
                        "custodianUserId": str(CUSTODIAN_ID),
                        "quantity": True,
                    }
                ],
            },
        )
        for body in invalid_bodies:
            unit_of_work = FakeUnitOfWork(self._users())
            with self.subTest(body=body), self.assertRaises(AssetCreationResultError):
                await self._service(FakeIdempotentExecutor(unit_of_work, body)).create(
                    self._command(single=True), self._hr(), "request-1"
                )

    def test_fingerprint_payload_sorts_allocations_and_preserves_nullable_team(self) -> None:
        """Allocation transport order does not change semantic request identity."""

        command = self._command(responsible_team_id=None)
        reversed_command = CreateAssetCommand(
            category_id=command.category_id,
            responsible_team_id=None,
            name=command.name,
            source=command.source,
            total_quantity=command.total_quantity,
            allocations=tuple(reversed(command.allocations)),
            description=command.description,
        )
        self.assertEqual(command.fingerprint_payload(), reversed_command.fingerprint_payload())
        self.assertIsNone(command.fingerprint_payload()["responsibleTeamId"])

    @staticmethod
    def _service(executor: FakeIdempotentExecutor) -> AssetCreationService:
        ids = iter(
            (
                ASSET_ID,
                CUSTODY_ID,
                OTHER_CUSTODY_ID,
                EVENT_ID,
                REMINDER_ID,
                OTHER_REMINDER_ID,
            )
        )
        return AssetCreationService(executor, FixedClock(), id_factory=lambda: next(ids))

    @staticmethod
    def _hr() -> CurrentActor:
        return CurrentActor(HR_ID, frozenset({OperationalRole.HR}), is_active=True)

    def _users(self) -> Mapping[UUID, UserIdentity]:
        return {
            HR_ID: self._identity(HR_ID),
            CUSTODIAN_ID: self._identity(CUSTODIAN_ID),
            OTHER_CUSTODIAN_ID: self._identity(OTHER_CUSTODIAN_ID),
        }

    @staticmethod
    def _identity(user_id: UUID, *, active: bool = True) -> UserIdentity:
        suffix = str(user_id)[-4:]
        return UserIdentity(
            id=user_id,
            username=f"custodian.{suffix}",
            email=f"custodian.{suffix}@example.test",
            display_name="Custodian One",
            roles=frozenset(),
            team_ids=frozenset(),
            is_active=active,
        )

    @staticmethod
    def _command(
        *,
        single: bool = False,
        total_quantity: int = 3,
        allocation_quantity: int = 2,
        responsible_team_id: UUID | None = TEAM_ID,
    ) -> CreateAssetCommand:
        allocations = [
            CreateAssetAllocationCommand(
                CUSTODIAN_ID,
                allocation_quantity,
                AssetCondition.GOOD,
                "New",
            )
        ]
        if not single:
            allocations.append(
                CreateAssetAllocationCommand(
                    OTHER_CUSTODIAN_ID,
                    1,
                    AssetCondition.DAMAGED,
                    "Scratched case",
                )
            )
        return CreateAssetCommand(
            category_id=CATEGORY_ID,
            responsible_team_id=responsible_team_id,
            name="Camera",
            description="Club camera",
            source="DONATION",
            total_quantity=total_quantity,
            allocations=tuple(allocations),
        )
