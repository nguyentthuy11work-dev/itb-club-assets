"""HTTP contract tests for F10 standalone Custody Transfer routes."""

from __future__ import annotations

import json
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from httpx import ASGITransport, AsyncClient

from ams.application.authentication import SessionService
from ams.application.transfers import (
    ConfirmTransferMetadata,
    CreateTransferCommand,
    TransferPage,
    TransferResult,
    TransferService,
)
from ams.domain.assets import AssetCondition
from ams.domain.identity import CurrentActor, OperationalRole
from ams.domain.transfers import (
    CustodyTransfer,
    TransferConfirmationForbiddenError,
    TransferStatus,
)
from ams.interface.api.app import create_app
from ams.interface.api.dependencies import get_current_actor, get_transfer_service

NOW = datetime(2030, 2, 1, 3, 0, tzinfo=UTC)
TRANSFER_ID = UUID("00000000-0000-4000-8000-000000011001")
SOURCE_ID = UUID("00000000-0000-4000-8000-000000011002")
ASSET_ID = UUID("00000000-0000-4000-8000-000000011003")
CURRENT_ID = UUID("00000000-0000-4000-8000-000000011004")
TARGET_ID = UUID("00000000-0000-4000-8000-000000011005")
HR_ID = UUID("00000000-0000-4000-8000-000000011006")
ITEM_ID = UUID("00000000-0000-4000-8000-000000011007")


class UnusedSessionServiceFactory:
    """Satisfy application composition while authentication is overridden."""

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[SessionService]:
        """Yield an unused placeholder."""

        yield cast(SessionService, object())


class FakeTransferService:
    """Record strict transport values and return deterministic F10 results."""

    def __init__(self) -> None:
        self.created: CreateTransferCommand | None = None
        self.metadata: ConfirmTransferMetadata | None = None
        self.forbid_current = False

    async def create(
        self,
        command: CreateTransferCommand,
        actor: CurrentActor,
        idempotency_key: str,
    ) -> TransferResult:
        """Record one standalone or Reconciliation-linked create request."""

        self.created = command
        return TransferResult(_transfer(reconciliation_item_id=command.reconciliation_item_id))

    async def list(
        self,
        actor: CurrentActor,
        *,
        status: TransferStatus | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> TransferPage:
        """Return one deterministic visible Transfer page."""

        return TransferPage((_transfer(),), 1, offset, limit)

    async def confirm_current(
        self,
        transfer_id: UUID,
        metadata: ConfirmTransferMetadata,
        files: object,
        actor: CurrentActor,
        idempotency_key: str,
    ) -> TransferResult:
        """Record metadata and expose standardized authorization errors."""

        if self.forbid_current:
            raise TransferConfirmationForbiddenError("Only current custodian may confirm.")
        self.metadata = metadata
        return TransferResult(
            _transfer(
                status=TransferStatus.PENDING_TARGET_CONFIRMATION,
                current_confirmed_by_user_id=CURRENT_ID,
                current_confirmed_at=NOW,
            )
        )

    async def confirm_target(
        self,
        transfer_id: UUID,
        metadata: ConfirmTransferMetadata,
        files: object,
        actor: CurrentActor,
        idempotency_key: str,
    ) -> TransferResult:
        """Return a valid target-first state without requiring current confirmation."""

        self.metadata = metadata
        return TransferResult(
            _transfer(
                status=TransferStatus.PENDING_CURRENT_CONFIRMATION,
                target_confirmed_by_user_id=TARGET_ID,
                target_confirmed_at=NOW,
            )
        )


class TransferApiTests(unittest.IsolatedAsyncioTestCase):
    """Verify aliases, linked creation, either-order confirmation, and errors."""

    async def asyncSetUp(self) -> None:
        self.service = FakeTransferService()
        app = create_app(UnusedSessionServiceFactory())
        app.dependency_overrides[get_current_actor] = lambda: CurrentActor(
            HR_ID, frozenset({OperationalRole.HR})
        )
        app.dependency_overrides[get_transfer_service] = lambda: cast(
            TransferService, self.service
        )
        self.client = AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="https://testserver",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_complete_route_contract_and_target_first(self) -> None:
        """Expose canonical response aliases and allow target to confirm first."""

        headers = {"Idempotency-Key": "key"}
        created = await self.client.post(
            "/api/v1/transfers",
            headers=headers,
            json={
                "sourceCustodyId": str(SOURCE_ID),
                "targetUserId": str(TARGET_ID),
                "quantity": 2,
            },
        )
        listed = await self.client.get("/api/v1/transfers?page_size=10")
        linked = await self.client.post(
            f"/api/v1/reconciliation-items/{ITEM_ID}/transfer",
            headers=headers,
            json={
                "sourceCustodyId": str(SOURCE_ID),
                "targetUserId": str(TARGET_ID),
                "quantity": 1,
            },
        )
        target = await self.client.post(
            f"/api/v1/transfers/{TRANSFER_ID}/confirm-target",
            headers=headers,
            data={"metadata": json.dumps({"note": "received"})},
            files={"files": ("proof.jpg", b"image", "image/jpeg")},
        )

        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["sourceCustodyId"], str(SOURCE_ID))
        self.assertEqual(listed.json()["pageSize"], 10)
        self.assertEqual(linked.json()["reconciliationItemId"], str(ITEM_ID))
        assert self.service.created is not None
        self.assertEqual(self.service.created.reconciliation_item_id, ITEM_ID)
        self.assertEqual(target.json()["status"], "PENDING_CURRENT_CONFIRMATION")
        assert self.service.metadata is not None
        self.assertEqual(self.service.metadata.note, "received")

    async def test_strict_validation_and_forbidden_mapping(self) -> None:
        """Reject unknown JSON fields and map participant ownership to 403."""

        invalid = await self.client.post(
            "/api/v1/transfers",
            headers={"Idempotency-Key": "key"},
            json={
                "sourceCustodyId": str(SOURCE_ID),
                "targetUserId": str(TARGET_ID),
                "quantity": 1,
                "extra": True,
            },
        )
        self.assertEqual(invalid.status_code, 422)
        self.service.forbid_current = True
        forbidden = await self.client.post(
            f"/api/v1/transfers/{TRANSFER_ID}/confirm-current",
            headers={"Idempotency-Key": "confirm"},
            data={"metadata": "{}"},
            files={"files": ("proof.jpg", b"image", "image/jpeg")},
        )
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(forbidden.json()["error"]["code"], "TRANSFER_CONFIRMATION_FORBIDDEN")


def _transfer(**changes: object) -> CustodyTransfer:
    values: dict[str, object] = {
        "id": TRANSFER_ID,
        "source_custody_id": SOURCE_ID,
        "asset_id": ASSET_ID,
        "current_custodian_user_id": CURRENT_ID,
        "target_custodian_user_id": TARGET_ID,
        "quantity": 2,
        "condition_snapshot": AssetCondition.GOOD,
        "created_by_user_id": HR_ID,
        "created_at": NOW,
    }
    values.update(changes)
    return CustodyTransfer(**values)  # type: ignore[arg-type]
