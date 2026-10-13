"""HTTP contract tests for F09 reconciliation routes without PostgreSQL."""

from __future__ import annotations

import json
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import cast
from uuid import UUID

from httpx import ASGITransport, AsyncClient

from ams.application.authentication import SessionService
from ams.application.reconciliations import (
    ConfirmationRequestResult,
    ConfirmReconciliationMetadata,
    ReconciliationItemPage,
    ReconciliationItemResult,
    ReconciliationResult,
    ReconciliationService,
    ReminderResult,
)
from ams.domain.assets import AssetCondition
from ams.domain.identity import CurrentActor, OperationalRole
from ams.domain.reconciliations import (
    ReconciliationCloseBlockedError,
    ReconciliationItem,
    ReconciliationItemStatus,
    ReconciliationSession,
)
from ams.interface.api.app import create_app
from ams.interface.api.dependencies import get_current_actor, get_reconciliation_service

NOW = datetime(2030, 1, 2, 3, 0, tzinfo=UTC)
HR_ID = UUID("00000000-0000-4000-8000-000000009101")
SESSION_ID = UUID("00000000-0000-4000-8000-000000009102")
ITEM_ID = UUID("00000000-0000-4000-8000-000000009103")
CUSTODY_ID = UUID("00000000-0000-4000-8000-000000009104")
ASSET_ID = UUID("00000000-0000-4000-8000-000000009105")


class UnusedSessionServiceFactory:
    """Satisfy application composition while authentication is overridden."""

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[SessionService]:
        """Yield an unused placeholder."""

        yield cast(SessionService, object())


class FakeReconciliationService:
    """Record parsed transport values and return deterministic F09 results."""

    def __init__(self) -> None:
        self.metadata: ConfirmReconciliationMetadata | None = None
        self.reject_close = False

    async def open(
        self, reconciliation_date: date, actor: CurrentActor, idempotency_key: str
    ) -> ReconciliationResult:
        return ReconciliationResult(_session())

    async def get(self, reconciliation_id: UUID, actor: CurrentActor) -> ReconciliationResult:
        return ReconciliationResult(_session())

    async def list_mine(
        self, actor: CurrentActor, *, offset: int = 0, limit: int = 20
    ) -> ReconciliationItemPage:
        return ReconciliationItemPage((_item(),), 1, offset, limit)

    async def request_confirmation(
        self, reconciliation_id: UUID, actor: CurrentActor, idempotency_key: str
    ) -> ConfirmationRequestResult:
        return ConfirmationRequestResult(SESSION_ID, NOW, 1)

    async def confirm_item(
        self,
        item_id: UUID,
        metadata: ConfirmReconciliationMetadata,
        files: object,
        actor: CurrentActor,
        idempotency_key: str,
    ) -> ReconciliationItemResult:
        self.metadata = metadata
        item = _item()
        item.confirm(
            actor_id=HR_ID,
            actual_quantity=metadata.actual_quantity,
            actual_condition=metadata.condition,
            condition_note=metadata.condition_note,
            discrepancy_reason=metadata.discrepancy_reason,
            evidence_ids=(UUID(int=1),),
            confirmed_at=NOW,
        )
        return ReconciliationItemResult(item)

    async def resolve_item(
        self,
        item_id: UUID,
        resolution_note: str,
        actor: CurrentActor,
        idempotency_key: str,
    ) -> ReconciliationItemResult:
        item = _item(status=ReconciliationItemStatus.DISCREPANCY)
        item.resolve(actor_id=HR_ID, note=resolution_note, resolved_at=NOW)
        return ReconciliationItemResult(item)

    async def remind_item(
        self, item_id: UUID, actor: CurrentActor, idempotency_key: str
    ) -> ReminderResult:
        return ReminderResult(ITEM_ID, True)

    async def close(
        self, reconciliation_id: UUID, actor: CurrentActor, idempotency_key: str
    ) -> ReconciliationResult:
        if self.reject_close:
            raise ReconciliationCloseBlockedError("Resolve all discrepancies before closing.")
        session = _session()
        session.close(actor_id=HR_ID, closed_at=NOW, unresolved_count=0)
        return ReconciliationResult(session)


class ReconciliationApiTests(unittest.IsolatedAsyncioTestCase):
    """Verify canonical aliases, multipart parsing, paging, and errors."""

    async def asyncSetUp(self) -> None:
        self.service = FakeReconciliationService()
        app = create_app(UnusedSessionServiceFactory())
        app.dependency_overrides[get_current_actor] = lambda: CurrentActor(
            HR_ID, frozenset({OperationalRole.HR})
        )
        app.dependency_overrides[get_reconciliation_service] = lambda: cast(
            ReconciliationService, self.service
        )
        self.client = AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="https://testserver",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_complete_route_contract(self) -> None:
        headers = {"Idempotency-Key": "key"}
        opened = await self.client.post(
            "/api/v1/reconciliations",
            headers=headers,
            json={"reconciliationDate": "2030-01-03"},
        )
        mine = await self.client.get("/api/v1/reconciliations/me?page_size=10")
        detail = await self.client.get(f"/api/v1/reconciliations/{SESSION_ID}")
        requested = await self.client.post(
            f"/api/v1/reconciliations/{SESSION_ID}/request-confirmation",
            headers=headers,
        )
        confirmed = await self.client.post(
            f"/api/v1/reconciliation-items/{ITEM_ID}/confirm",
            headers=headers,
            data={
                "metadata": json.dumps(
                    {"actualQuantity": 2, "condition": "GOOD", "conditionNote": "ok"}
                )
            },
            files={"files": ("proof.jpg", b"image", "image/jpeg")},
        )
        reminded = await self.client.post(
            f"/api/v1/reconciliation-items/{ITEM_ID}/remind", headers=headers
        )
        closed = await self.client.post(
            f"/api/v1/reconciliations/{SESSION_ID}/close", headers=headers
        )

        self.assertEqual(opened.status_code, 201, opened.text)
        self.assertEqual(mine.json()["pageSize"], 10)
        self.assertEqual(detail.json()["items"][0]["assetName"], "Camera")
        self.assertEqual(requested.json()["emailRecipients"], 1)
        self.assertEqual(confirmed.json()["status"], "CONFIRMED")
        assert self.service.metadata is not None
        self.assertEqual(self.service.metadata.condition_note, "ok")
        self.assertEqual(reminded.status_code, 202)
        self.assertTrue(reminded.json()["emailQueued"])
        self.assertEqual(closed.json()["status"], "CLOSED")

    async def test_strict_validation_and_close_gate_error(self) -> None:
        invalid = await self.client.post(
            "/api/v1/reconciliations",
            headers={"Idempotency-Key": "key"},
            json={"reconciliationDate": "2030-01-03", "extra": True},
        )
        self.assertEqual(invalid.status_code, 422)
        missing_header = await self.client.post(f"/api/v1/reconciliations/{SESSION_ID}/close")
        self.assertEqual(missing_header.status_code, 422)
        self.service.reject_close = True
        blocked = await self.client.post(
            f"/api/v1/reconciliations/{SESSION_ID}/close",
            headers={"Idempotency-Key": "close"},
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["error"]["code"], "RECONCILIATION_HAS_UNRESOLVED_ITEMS")


def _item(
    *, status: ReconciliationItemStatus = ReconciliationItemStatus.PENDING_CONFIRMATION
) -> ReconciliationItem:
    values: dict[str, object] = {}
    if status is ReconciliationItemStatus.DISCREPANCY:
        values = {
            "actual_quantity": 1,
            "actual_condition": AssetCondition.GOOD,
            "discrepancy_reason": "Missing",
            "confirmed_by_user_id": HR_ID,
            "confirmed_at": NOW,
        }
    elif status is ReconciliationItemStatus.CONFIRMED:
        values = {
            "actual_quantity": 2,
            "actual_condition": AssetCondition.GOOD,
            "confirmed_by_user_id": HR_ID,
            "confirmed_at": NOW,
        }
    return ReconciliationItem(
        ITEM_ID,
        SESSION_ID,
        CUSTODY_ID,
        ASSET_ID,
        HR_ID,
        "Camera",
        2,
        AssetCondition.GOOD,
        NOW,
        status=status,
        **values,  # type: ignore[arg-type]
    )


def _session() -> ReconciliationSession:
    return ReconciliationSession(
        SESSION_ID,
        date(2030, 1, 3),
        HR_ID,
        NOW,
        (_item(status=ReconciliationItemStatus.CONFIRMED),),
    )
