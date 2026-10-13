"""PostgreSQL integration tests for F03.05 idempotent command execution."""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ams.application.idempotency import (
    CommandResponse,
    IdempotencyKeyReusedError,
    IdempotentCommandExecutor,
    MultipartFileFingerprint,
    canonical_json_fingerprint,
    multipart_fingerprint,
)
from ams.application.reliability import IClock, OutboxEvent
from ams.application.unit_of_work import IUnitOfWork
from ams.infrastructure.reliability_persistence import SqlAlchemyUnitOfWork

ACTOR_ID = UUID("00000000-0000-4000-8000-000000001401")
AGGREGATE_ID = UUID("00000000-0000-4000-8000-000000001402")
NOW = datetime(2026, 10, 4, 17, 0, tzinfo=UTC)


class FixedClock(IClock):
    """Return one deterministic UTC command instant."""

    def now(self) -> datetime:
        """Return the configured instant."""

        return NOW


@unittest.skipUnless(os.getenv("AMS_DATABASE_URL"), "AMS_DATABASE_URL is required")
class IdempotentCommandPostgreSqlTests(unittest.IsolatedAsyncioTestCase):
    """Prove concurrent single effect and durable JSON/multipart response replay."""

    async def asyncSetUp(self) -> None:
        """Create database infrastructure and the actor fixture."""

        self.engine = create_async_engine(os.environ["AMS_DATABASE_URL"], pool_pre_ping=True)
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)
        self.operation_calls = 0
        await self._cleanup()
        async with self.session_maker() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO users (id, username, email, display_name, password_hash)
                    VALUES (:id, 'idempotency.actor', 'idempotency@example.test',
                            'Idempotency Actor', 'hash')
                    """
                ),
                {"id": ACTOR_ID},
            )
            await session.commit()

    async def asyncTearDown(self) -> None:
        """Remove committed command fixtures and dispose the engine."""

        await self._cleanup()
        await self.engine.dispose()

    async def test_concurrent_json_retry_is_single_effect_and_changed_payload_conflicts(
        self,
    ) -> None:
        """Concurrent equal requests share output; changed reuse raises the API conflict."""

        executor = self._executor()
        fingerprint = canonical_json_fingerprint(
            {"name": "Camera", "quantity": 2, "teamId": None}
        )

        first, second = await asyncio.gather(
            executor.execute(
                actor_id=ACTOR_ID,
                command_name="CREATE_ASSET",
                idempotency_key="json-request",
                request_fingerprint=fingerprint,
                operation=self._create_effect,
            ),
            executor.execute(
                actor_id=ACTOR_ID,
                command_name="CREATE_ASSET",
                idempotency_key="json-request",
                request_fingerprint=fingerprint,
                operation=self._create_effect,
            ),
        )

        self.assertEqual(self.operation_calls, 1)
        self.assertEqual(first.body, second.body)
        self.assertEqual(sorted((first.replayed, second.replayed)), [False, True])
        self.assertEqual(await self._counts("CREATE_ASSET", "json-request"), (1, 1))

        with self.assertRaises(IdempotencyKeyReusedError):
            await executor.execute(
                actor_id=ACTOR_ID,
                command_name="CREATE_ASSET",
                idempotency_key="json-request",
                request_fingerprint=canonical_json_fingerprint(
                    {"name": "Camera", "quantity": 3, "teamId": None}
                ),
                operation=self._unexpected_effect,
            )
        self.assertEqual(await self._counts("CREATE_ASSET", "json-request"), (1, 1))

    async def test_multipart_retry_after_response_loss_replays_committed_response(self) -> None:
        """A retry after commit but lost HTTP response does not repeat multipart effects."""

        executor = self._executor()
        fingerprint = multipart_fingerprint(
            {"custodyId": str(AGGREGATE_ID), "confirmation": "received"},
            [
                MultipartFileFingerprint(
                    field_name="evidence",
                    content_type="image/png",
                    content_sha256="d" * 64,
                    size_bytes=4096,
                )
            ],
        )

        committed_response = await executor.execute(
            actor_id=ACTOR_ID,
            command_name="CONFIRM_CUSTODY",
            idempotency_key="multipart-request",
            request_fingerprint=fingerprint,
            operation=self._create_effect,
        )
        replay = await executor.execute(
            actor_id=ACTOR_ID,
            command_name="CONFIRM_CUSTODY",
            idempotency_key="multipart-request",
            request_fingerprint=fingerprint,
            operation=self._unexpected_effect,
        )

        self.assertFalse(committed_response.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.body, committed_response.body)
        self.assertEqual(self.operation_calls, 1)
        self.assertEqual(await self._counts("CONFIRM_CUSTODY", "multipart-request"), (1, 1))

    async def _create_effect(self, unit_of_work: IUnitOfWork) -> CommandResponse:
        self.operation_calls += 1
        await asyncio.sleep(0.1)
        event_id = uuid4()
        await unit_of_work.outbox.add(
            OutboxEvent(
                id=event_id,
                event_type="IDEMPOTENT_TEST_EFFECT",
                aggregate_type="TEST",
                aggregate_id=AGGREGATE_ID,
                payload={"actorId": str(ACTOR_ID)},
                scheduled_at=NOW,
                created_at=NOW,
            )
        )
        return CommandResponse(201, {"eventId": str(event_id)})

    @staticmethod
    async def _unexpected_effect(unit_of_work: IUnitOfWork) -> CommandResponse:
        raise AssertionError("A stored response replay must not execute the command.")

    def _executor(self) -> IdempotentCommandExecutor:
        return IdempotentCommandExecutor(
            lambda: SqlAlchemyUnitOfWork(self.session_maker),
            FixedClock(),
        )

    async def _counts(self, command_name: str, key: str) -> tuple[int, int]:
        async with self.session_maker() as session:
            result = await session.execute(
                text(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM idempotency_records
                         WHERE actor_id = :actor_id
                           AND command_name = :command_name
                           AND idempotency_key = :key),
                        (SELECT COUNT(*) FROM outbox_events
                         WHERE aggregate_id = :aggregate_id)
                    """
                ),
                {
                    "actor_id": ACTOR_ID,
                    "command_name": command_name,
                    "key": key,
                    "aggregate_id": AGGREGATE_ID,
                },
            )
            row = result.one()
            return int(row[0]), int(row[1])

    async def _cleanup(self) -> None:
        async with self.session_maker() as session:
            await session.execute(
                text("DELETE FROM outbox_events WHERE aggregate_id = :id"),
                {"id": AGGREGATE_ID},
            )
            await session.execute(
                text("DELETE FROM idempotency_records WHERE actor_id = :id"),
                {"id": ACTOR_ID},
            )
            await session.execute(
                text("DELETE FROM users WHERE id = :id"),
                {"id": ACTOR_ID},
            )
            await session.commit()
