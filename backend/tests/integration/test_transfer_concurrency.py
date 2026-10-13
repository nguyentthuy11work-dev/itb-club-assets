"""PostgreSQL race proof for F10 two-party Transfer completion."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ams.application.evidence import EvidenceFileInput, EvidenceStorageService
from ams.application.idempotency import IdempotentCommandExecutor
from ams.application.reliability import IClock
from ams.application.transfers import (
    ConfirmTransferMetadata,
    CreateTransferCommand,
    TransferService,
)
from ams.domain.identity import CurrentActor, OperationalRole
from ams.domain.transfers import TransferStatus
from ams.infrastructure.reliability_persistence import SqlAlchemyUnitOfWork
from ams.infrastructure.storage import LocalFileStorage

HR_ID = UUID("00000000-0000-4000-8000-000000011201")
CURRENT_ID = UUID("00000000-0000-4000-8000-000000011202")
TARGET_ID = UUID("00000000-0000-4000-8000-000000011203")
CATEGORY_ID = UUID("00000000-0000-4000-8000-000000011204")
ASSET_ID = UUID("00000000-0000-4000-8000-000000011205")
CUSTODY_ID = UUID("00000000-0000-4000-8000-000000011206")
NOW = datetime(2030, 2, 1, 3, 0, tzinfo=UTC)
JPEG = b"\xff\xd8\xfftransfer-confirmation\xff\xd9"


class FixedClock(IClock):
    """Return one deterministic Transfer timestamp."""

    def now(self) -> datetime:
        """Return the fixed UTC instant."""

        return NOW


@unittest.skipUnless(os.getenv("AMS_DATABASE_URL"), "AMS_DATABASE_URL is required")
class TransferConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    """Exercise production row locks while both participant confirmations race."""

    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine(os.environ["AMS_DATABASE_URL"], pool_pre_ping=True)
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)
        self.temporary = tempfile.TemporaryDirectory()
        self.storage_root = Path(self.temporary.name)
        await self._cleanup()
        await self._seed()

    async def asyncTearDown(self) -> None:
        await self._cleanup()
        await self.engine.dispose()
        self.temporary.cleanup()

    async def test_both_confirmations_create_one_target_and_preserve_balance(self) -> None:
        """Serialize on the Transfer root and complete custody exactly once."""

        service = self._service()
        created = await service.create(
            CreateTransferCommand(CUSTODY_ID, TARGET_ID, 2),
            CurrentActor(HR_ID, frozenset({OperationalRole.HR})),
            "create-transfer-race",
        )
        current, target = await asyncio.gather(
            service.confirm_current(
                created.transfer.id,
                ConfirmTransferMetadata("released"),
                (_image(),),
                CurrentActor(CURRENT_ID, frozenset()),
                "confirm-current-race",
            ),
            service.confirm_target(
                created.transfer.id,
                ConfirmTransferMetadata("received"),
                (_image(),),
                CurrentActor(TARGET_ID, frozenset()),
                "confirm-target-race",
            ),
        )
        self.assertIn(
            TransferStatus.COMPLETED,
            {current.transfer.status, target.transfer.status},
        )

        async with self.session_maker() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT custodian_user_id, status, on_hand_quantity,
                               reserved_quantity
                        FROM custodies
                        WHERE asset_id = :asset
                        ORDER BY custodian_user_id
                        """
                    ),
                    {"asset": ASSET_ID},
                )
            ).all()
            asset_available = await session.scalar(
                text("SELECT available_quantity FROM assets WHERE id = :asset"),
                {"asset": ASSET_ID},
            )
            completed_count = await session.scalar(
                text(
                    """
                    SELECT count(*) FROM custody_transfers
                    WHERE source_custody_id = :source AND status = 'COMPLETED'
                    """
                ),
                {"source": CUSTODY_ID},
            )
        self.assertEqual(len(rows), 2)
        by_user = {row.custodian_user_id: row for row in rows}
        self.assertEqual(by_user[CURRENT_ID].on_hand_quantity, 2)
        self.assertEqual(by_user[CURRENT_ID].reserved_quantity, 0)
        self.assertEqual(by_user[TARGET_ID].on_hand_quantity, 2)
        self.assertEqual(asset_available, 4)
        self.assertEqual(completed_count, 1)

    def _service(self) -> TransferService:
        def unit_of_work_factory() -> SqlAlchemyUnitOfWork:
            return SqlAlchemyUnitOfWork(self.session_maker)

        clock = FixedClock()
        return TransferService(
            IdempotentCommandExecutor(unit_of_work_factory, clock),
            unit_of_work_factory,
            EvidenceStorageService(LocalFileStorage(self.storage_root)),
            clock,
        )

    async def _seed(self) -> None:
        async with self.session_maker.begin() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO users (id, username, email, display_name, password_hash)
                    VALUES
                        (:hr, 'tr.hr', 'tr.hr@example.test', 'HR', 'hash'),
                        (:current, 'tr.current', 'tr.current@example.test', 'Current', 'hash'),
                        (:target, 'tr.target', 'tr.target@example.test', 'Target', 'hash')
                    """
                ),
                {"hr": HR_ID, "current": CURRENT_ID, "target": TARGET_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO asset_categories (id, code, name)
                    VALUES (:id, 'TRANSFER_RACE', 'Transfer race')
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
                    ) VALUES (:id, :category, 'Camera', 'PURCHASE', 4, 4, :hr)
                    """
                ),
                {"id": ASSET_ID, "category": CATEGORY_ID, "hr": HR_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO custodies (
                        id, asset_id, custodian_user_id, assigned_by_user_id, status,
                        on_hand_quantity, condition, assigned_at, confirmed_at,
                        confirmed_by_user_id
                    ) VALUES (
                        :id, :asset, :current, :hr, 'ACTIVE', 4, 'GOOD',
                        :now, :now, :current
                    )
                    """
                ),
                {
                    "id": CUSTODY_ID,
                    "asset": ASSET_ID,
                    "current": CURRENT_ID,
                    "hr": HR_ID,
                    "now": NOW,
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO custody_evidence (
                        custody_id, uploaded_by_user_id, evidence_type, object_key,
                        content_type, content_sha256, size_bytes, created_at
                    ) VALUES (
                        :custody, :current, 'ASSIGNMENT_CONFIRMATION',
                        'transfer-race/source-proof', 'image/jpeg', :sha256, 16, :now
                    )
                    """
                ),
                {
                    "custody": CUSTODY_ID,
                    "current": CURRENT_ID,
                    "sha256": "a" * 64,
                    "now": NOW,
                },
            )

    async def _cleanup(self) -> None:
        async with self.session_maker.begin() as session:
            params = {
                "hr": HR_ID,
                "current": CURRENT_ID,
                "target": TARGET_ID,
                "source": CUSTODY_ID,
                "asset": ASSET_ID,
                "category": CATEGORY_ID,
            }
            for statement in (
                "DELETE FROM custody_evidence WHERE uploaded_by_user_id IN (:current, :target)",
                "DELETE FROM custody_transfer_evidence "
                "WHERE uploaded_by_user_id IN (:current, :target)",
                "DELETE FROM idempotency_records WHERE actor_id IN (:hr, :current, :target)",
                "DELETE FROM outbox_events WHERE aggregate_id IN "
                "(SELECT id FROM custody_transfers WHERE source_custody_id = :source)",
                "DELETE FROM custody_transfers WHERE source_custody_id = :source",
                "DELETE FROM custodies WHERE asset_id = :asset",
                "DELETE FROM assets WHERE id = :asset",
                "DELETE FROM asset_categories WHERE id = :category",
                "DELETE FROM users WHERE id IN (:hr, :current, :target)",
            ):
                await session.execute(text(statement), params)


async def _image_content() -> AsyncIterator[bytes]:
    yield JPEG


def _image() -> EvidenceFileInput:
    return EvidenceFileInput(_image_content(), "image/jpeg")
