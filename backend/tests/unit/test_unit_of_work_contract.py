"""Fake-based proof of the F03.02 transaction/outbox contract."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from types import TracebackType
from typing import cast
from uuid import UUID

from ams.application.reliability import (
    ClaimedOutboxEvent,
    IIdempotencyRepository,
    INotificationRepository,
    IOutboxRepository,
    OutboxEvent,
)
from ams.application.unit_of_work import IUnitOfWork
from ams.domain.repositories import IAssetRepository, ICustodyRepository

EVENT_ID = UUID("00000000-0000-4000-8000-000000000701")
AGGREGATE_ID = UUID("00000000-0000-4000-8000-000000000702")
NOW = datetime(2026, 10, 4, 13, 0, tzinfo=UTC)


class FakeOutboxRepository(IOutboxRepository):
    """Stage events in the owning fake Unit of Work."""

    def __init__(self, staged: list[OutboxEvent]) -> None:
        self._staged = staged

    async def add(self, event: OutboxEvent) -> None:
        """Stage one event without publishing it."""

        self._staged.append(event)

    async def claim_due(
        self,
        *,
        now: datetime,
        worker_id: str,
        lease_expires_at: datetime,
        limit: int,
    ) -> tuple[ClaimedOutboxEvent, ...]:
        """Return no worker events in this transaction-focused fake."""

        return ()

    async def mark_delivered(
        self, event_id: UUID, *, worker_id: str, completed_at: datetime
    ) -> None:
        """Accept terminal state in this transaction-focused fake."""

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
        """Accept retry state in this transaction-focused fake."""


class FakeUnitOfWork(IUnitOfWork):
    """Model commit/rollback visibility without a database."""

    def __init__(self) -> None:
        self.staged_business: list[str] = []
        self.staged_events: list[OutboxEvent] = []
        self.committed_business: list[str] = []
        self.committed_events: list[OutboxEvent] = []
        self.assets = cast(IAssetRepository, object())
        self.custodies = cast(ICustodyRepository, object())
        self.notifications = cast(INotificationRepository, object())
        self.idempotency = cast(IIdempotencyRepository, object())
        self.outbox = FakeOutboxRepository(self.staged_events)

    async def __aenter__(self) -> FakeUnitOfWork:
        """Open the fake transaction."""

        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Roll back when command processing exits with a failure."""

        if exception is not None:
            await self.rollback()

    async def commit(self) -> None:
        """Make business changes and events visible together."""

        self.committed_business.extend(self.staged_business)
        self.committed_events.extend(self.staged_events)
        self.staged_business.clear()
        self.staged_events.clear()

    async def rollback(self) -> None:
        """Suppress both business changes and event publication."""

        self.staged_business.clear()
        self.staged_events.clear()


class UnitOfWorkContractTests(unittest.IsolatedAsyncioTestCase):
    """Demonstrate the application-service transaction pattern using fakes."""

    async def test_success_commits_business_change_and_event_together(self) -> None:
        """A successful command exposes both effects after one commit."""

        unit_of_work = FakeUnitOfWork()
        await self._run_command(unit_of_work, fail=False)

        self.assertEqual(unit_of_work.committed_business, ["asset-created"])
        self.assertEqual(unit_of_work.committed_events, [self._event()])

    async def test_failure_rolls_back_business_change_and_suppresses_event(self) -> None:
        """A command failure cannot publish an event for rolled-back business data."""

        unit_of_work = FakeUnitOfWork()
        with self.assertRaises(RuntimeError):
            await self._run_command(unit_of_work, fail=True)

        self.assertEqual(unit_of_work.committed_business, [])
        self.assertEqual(unit_of_work.committed_events, [])
        self.assertEqual(unit_of_work.staged_events, [])

    async def _run_command(self, unit_of_work: FakeUnitOfWork, *, fail: bool) -> None:
        async with unit_of_work:
            unit_of_work.staged_business.append("asset-created")
            await unit_of_work.outbox.add(self._event())
            if fail:
                raise RuntimeError("simulated business failure")
            await unit_of_work.commit()

    @staticmethod
    def _event() -> OutboxEvent:
        return OutboxEvent(
            id=EVENT_ID,
            event_type="ASSET_CREATED",
            aggregate_type="ASSET",
            aggregate_id=AGGREGATE_ID,
            payload={"version": 1},
            scheduled_at=NOW,
            created_at=NOW,
        )
