"""PostgreSQL/filesystem API tests for F02.05 Custody confirmation."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast
from uuid import UUID

from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ams.application.authentication import SessionService
from ams.application.custody_confirmation import (
    ConfirmCustodyMetadata,
    CustodyConfirmationService,
)
from ams.application.evidence import EvidenceFileInput, EvidenceStorageService
from ams.application.idempotency import IdempotentCommandExecutor
from ams.domain.identity import CurrentActor
from ams.infrastructure.notifications import SystemClock
from ams.infrastructure.reliability_persistence import SqlAlchemyUnitOfWork
from ams.infrastructure.storage import LocalFileStorage
from ams.interface.api.app import (
    SqlAlchemyCustodyConfirmationServiceFactory,
    create_app,
)
from ams.interface.api.dependencies import get_current_actor

HR_ID = UUID("00000000-0000-4000-8000-000000001901")
CUSTODIAN_ID = UUID("00000000-0000-4000-8000-000000001902")
OTHER_USER_ID = UUID("00000000-0000-4000-8000-000000001903")
CATEGORY_ID = UUID("00000000-0000-4000-8000-000000001904")
ASSET_ID = UUID("00000000-0000-4000-8000-000000001905")
CUSTODY_ID = UUID("00000000-0000-4000-8000-000000001906")
PNG = b"\x89PNG\r\n\x1a\nconfirmation-png"
JPEG = b"\xff\xd8\xffconfirmation-jpeg\xff\xd9"


async def _stream(content: bytes) -> AsyncIterable[bytes]:
    yield content


class RollbackThenFailUnitOfWork(SqlAlchemyUnitOfWork):
    """Simulate a transaction failure after storage finalization."""

    async def commit(self) -> None:
        """Roll back all database work and report commit failure."""

        await self.rollback()
        raise RuntimeError("simulated commit failure")


class UnusedSessionServiceFactory:
    """Satisfy application composition while actor resolution is overridden."""

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[SessionService]:
        """Yield an unused placeholder."""

        yield cast(SessionService, object())


@unittest.skipUnless(os.getenv("AMS_DATABASE_URL"), "AMS_DATABASE_URL is required")
class CustodyConfirmationApiTests(unittest.IsolatedAsyncioTestCase):
    """Verify multipart inputs, ownership, activation, replay, and orphan cleanup."""

    async def asyncSetUp(self) -> None:
        """Create pending Custody fixtures, private storage, and production adapters."""

        self.engine = create_async_engine(os.environ["AMS_DATABASE_URL"], pool_pre_ping=True)
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)
        self.temporary = tempfile.TemporaryDirectory()
        self.storage_root = Path(self.temporary.name)
        await self._cleanup()
        async with self.session_maker() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO users (id, username, email, display_name, password_hash)
                    VALUES
                        (:hr, 'confirm.hr', 'confirm.hr@example.test', 'Confirm HR', 'hash'),
                        (:custodian, 'confirm.owner', 'confirm.owner@example.test',
                         'Confirm Owner', 'hash'),
                        (:other, 'confirm.other', 'confirm.other@example.test',
                         'Confirm Other', 'hash')
                    """
                ),
                {"hr": HR_ID, "custodian": CUSTODIAN_ID, "other": OTHER_USER_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO asset_categories (id, code, name)
                    VALUES (:id, 'CONFIRM_CATEGORY', 'Confirmation Category')
                    """
                ),
                {"id": CATEGORY_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO assets (
                        id, category_id, name, source, total_quantity,
                        available_quantity, created_by_user_id
                    ) VALUES (
                        :id, :category, 'Confirmation Camera', 'DONATION', 3, 0, :hr
                    )
                    """
                ),
                {"id": ASSET_ID, "category": CATEGORY_ID, "hr": HR_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO custodies (
                        id, asset_id, custodian_user_id, assigned_by_user_id,
                        status, on_hand_quantity, condition
                    ) VALUES (
                        :id, :asset, :custodian, :hr, 'PENDING', 2, 'GOOD'
                    )
                    """
                ),
                {
                    "id": CUSTODY_ID,
                    "asset": ASSET_ID,
                    "custodian": CUSTODIAN_ID,
                    "hr": HR_ID,
                },
            )
            await session.commit()

        self.actor = CurrentActor(CUSTODIAN_ID, frozenset(), is_active=True)
        factory = SqlAlchemyCustodyConfirmationServiceFactory(
            self.session_maker,
            EvidenceStorageService(LocalFileStorage(self.storage_root)),
        )
        application = create_app(
            UnusedSessionServiceFactory(),
            custody_confirmation_service_factory=factory,
        )

        async def current_actor_override() -> CurrentActor:
            return self.actor

        application.dependency_overrides[get_current_actor] = current_actor_override
        self.client = AsyncClient(
            transport=ASGITransport(app=application, raise_app_exceptions=False),
            base_url="https://testserver",
        )

    async def asyncTearDown(self) -> None:
        """Close resources, remove database fixtures, and delete private files."""

        await self.client.aclose()
        await self._cleanup()
        await self.engine.dispose()
        self.temporary.cleanup()

    async def test_success_retry_and_changed_file_preserve_one_effect(self) -> None:
        """Exact replay is stable; changed content adds no file or database effect."""

        first = await self._confirm("confirm-request", PNG, "image/png")
        retry = await self._confirm("confirm-request", PNG, "image/png")

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(retry.json(), first.json())
        self.assertEqual(
            first.json(),
            {
                "custodyId": str(CUSTODY_ID),
                "status": "ACTIVE",
                "confirmedAt": first.json()["confirmedAt"],
            },
        )
        self.assertEqual(await self._state(), ("ACTIVE", 2, 1, 1, 1))
        self.assertEqual(len(self._stored_files()), 1)
        self.assertNotIn("object", first.text.casefold())
        self.assertNotIn("sha256", first.text.casefold())

        conflict = await self._confirm("confirm-request", JPEG, "image/jpeg")
        self._assert_error(conflict, 409, "IDEMPOTENCY_KEY_REUSED")
        self.assertEqual(await self._state(), ("ACTIVE", 2, 1, 1, 1))
        self.assertEqual(len(self._stored_files()), 1)

    async def test_wrong_actor_is_forbidden_and_staged_file_is_removed(self) -> None:
        """A different authenticated member cannot confirm or retain an uploaded object."""

        self.actor = CurrentActor(OTHER_USER_ID, frozenset(), is_active=True)

        response = await self._confirm("wrong-actor", PNG, "image/png")

        self._assert_error(response, 403, "CUSTODY_CONFIRMATION_FORBIDDEN")
        self.assertEqual(await self._state(), ("PENDING", 0, 0, 0, 0))
        self.assertEqual(self._stored_files(), [])

    async def test_missing_or_invalid_image_is_rejected_without_orphans(self) -> None:
        """Multipart count and signature validation fail before any business state change."""

        missing = await self.client.post(
            f"/api/v1/custodies/{CUSTODY_ID}/confirm",
            headers={"Idempotency-Key": "missing-image"},
            data={"metadata": json.dumps({"note": "Received"})},
        )
        self._assert_error(missing, 422, "VALIDATION_ERROR")

        invalid_metadata = await self.client.post(
            f"/api/v1/custodies/{CUSTODY_ID}/confirm",
            headers={"Idempotency-Key": "invalid-metadata"},
            files=[
                ("metadata", (None, "not-json", "application/json")),
                ("files", ("proof", PNG, "image/png")),
            ],
        )
        self._assert_error(invalid_metadata, 422, "VALIDATION_ERROR")

        invalid = await self._confirm("invalid-image", b"not-a-png", "image/png")
        self._assert_error(invalid, 422, "INVALID_EVIDENCE")

        wrong_mime = await self._confirm("wrong-mime", PNG, "image/jpeg")
        self._assert_error(wrong_mime, 422, "INVALID_EVIDENCE")

        oversized = await self._confirm(
            "oversized-image",
            b"\x89PNG\r\n\x1a\n" + b"x" * (10 * 1024 * 1024),
            "image/png",
        )
        self._assert_error(oversized, 422, "INVALID_EVIDENCE")
        self.assertEqual(await self._state(), ("PENDING", 0, 0, 0, 0))
        self.assertEqual(self._stored_files(), [])

    async def test_transaction_failure_removes_finalized_file_and_database_work(self) -> None:
        """A post-finalization rollback leaves pending state and no private object."""

        clock = SystemClock()
        evidence_storage = EvidenceStorageService(LocalFileStorage(self.storage_root))
        executor = IdempotentCommandExecutor(
            lambda: RollbackThenFailUnitOfWork(self.session_maker),
            clock,
        )
        service = CustodyConfirmationService(executor, evidence_storage, clock)

        with self.assertRaises(RuntimeError):
            await service.confirm(
                custody_id=CUSTODY_ID,
                metadata=ConfirmCustodyMetadata("Received"),
                files=(EvidenceFileInput(_stream(PNG), "image/png"),),
                actor=self.actor,
                idempotency_key="failed-commit",
            )

        self.assertEqual(await self._state(), ("PENDING", 0, 0, 0, 0))
        self.assertEqual(self._stored_files(), [])

    async def _confirm(self, key: str, content: bytes, content_type: str) -> Response:
        return await self.client.post(
            f"/api/v1/custodies/{CUSTODY_ID}/confirm",
            headers={"Idempotency-Key": key},
            files=[
                (
                    "metadata",
                    (None, json.dumps({"note": "Received"}), "application/json"),
                ),
                ("files", ("proof", content, content_type)),
            ],
        )

    async def _state(self) -> tuple[str, int, int, int, int]:
        async with self.session_maker() as session:
            result = await session.execute(
                text(
                    """
                    SELECT custody.status,
                           asset.available_quantity,
                           (SELECT COUNT(*) FROM custody_evidence
                            WHERE custody_id = :custody),
                           (SELECT COUNT(*) FROM outbox_events
                            WHERE aggregate_id = :custody
                              AND event_type = 'CUSTODY_CONFIRMED'),
                           (SELECT COUNT(*) FROM idempotency_records
                            WHERE actor_id = :actor
                              AND command_name = 'CONFIRM_CUSTODY')
                    FROM custodies AS custody
                    JOIN assets AS asset ON asset.id = custody.asset_id
                    WHERE custody.id = :custody
                    """
                ),
                {"custody": CUSTODY_ID, "actor": CUSTODIAN_ID},
            )
            row = result.one()
            return str(row[0]), int(row[1]), int(row[2]), int(row[3]), int(row[4])

    def _stored_files(self) -> list[Path]:
        return sorted(path for path in self.storage_root.rglob("*") if path.is_file())

    async def _cleanup(self) -> None:
        async with self.session_maker() as session:
            await session.execute(
                text(
                    """
                    DELETE FROM notifications
                    WHERE outbox_event_id IN (
                        SELECT id FROM outbox_events WHERE aggregate_id = :custody
                    )
                    """
                ),
                {"custody": CUSTODY_ID},
            )
            await session.execute(
                text("DELETE FROM outbox_events WHERE aggregate_id = :custody"),
                {"custody": CUSTODY_ID},
            )
            await session.execute(
                text(
                    """
                    DELETE FROM idempotency_records
                    WHERE actor_id IN (:custodian, :other)
                      AND command_name = 'CONFIRM_CUSTODY'
                    """
                ),
                {"custodian": CUSTODIAN_ID, "other": OTHER_USER_ID},
            )
            await session.execute(
                text("DELETE FROM custody_evidence WHERE custody_id = :custody"),
                {"custody": CUSTODY_ID},
            )
            await session.execute(
                text("DELETE FROM custodies WHERE id = :custody"),
                {"custody": CUSTODY_ID},
            )
            await session.execute(
                text("DELETE FROM assets WHERE id = :asset"),
                {"asset": ASSET_ID},
            )
            await session.execute(
                text("DELETE FROM asset_categories WHERE id = :category"),
                {"category": CATEGORY_ID},
            )
            await session.execute(
                text("DELETE FROM users WHERE id IN (:hr, :custodian, :other)"),
                {"hr": HR_ID, "custodian": CUSTODIAN_ID, "other": OTHER_USER_ID},
            )
            await session.commit()

    def _assert_error(self, response: Response, status: int, code: str) -> None:
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["error"]["code"], code)
        UUID(response.json()["traceId"])
