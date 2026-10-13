"""HTTP contract tests for F11 Handover routes and synchronous CSV export."""

from __future__ import annotations

import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import cast
from uuid import UUID

from httpx import ASGITransport, AsyncClient

from ams.application.authentication import SessionService
from ams.application.handovers import HandoverCsvExport, HandoverResult, HandoverService
from ams.domain.assets import AssetCondition, CustodyStatus
from ams.domain.handovers import (
    Handover,
    HandoverConfirmationForbiddenError,
    HandoverCustodySnapshot,
    HandoverItem,
)
from ams.domain.identity import CurrentActor, OperationalRole
from ams.interface.api.app import create_app
from ams.interface.api.dependencies import get_current_actor, get_handover_service

NOW = datetime(2030, 2, 2, 3, 0, tzinfo=UTC)
TERM_ID = UUID("00000000-0000-4000-8000-000000013001")
HANDOVER_ID = UUID("00000000-0000-4000-8000-000000013002")
ITEM_ID = UUID("00000000-0000-4000-8000-000000013003")
ASSET_ID = UUID("00000000-0000-4000-8000-000000013004")
CUSTODY_ID = UUID("00000000-0000-4000-8000-000000013005")
OLD_HR_ID = UUID("00000000-0000-4000-8000-000000013006")
NEW_HR_ID = UUID("00000000-0000-4000-8000-000000013007")


class UnusedSessionServiceFactory:
    """Satisfy app composition while authentication is overridden."""

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[SessionService]:
        """Yield an unused placeholder."""

        yield cast(SessionService, object())


class FakeHandoverService:
    """Return deterministic snapshots and expose stable side errors."""

    def __init__(self) -> None:
        self.created_term_id: UUID | None = None
        self.forbid_old = False

    async def create(
        self, term_id: UUID, actor: CurrentActor, idempotency_key: str
    ) -> HandoverResult:
        self.created_term_id = term_id
        return HandoverResult(_handover())

    async def get(self, handover_id: UUID, actor: CurrentActor) -> HandoverResult:
        return HandoverResult(_handover())

    async def confirm_old(
        self, handover_id: UUID, actor: CurrentActor, idempotency_key: str
    ) -> HandoverResult:
        if self.forbid_old:
            raise HandoverConfirmationForbiddenError("Only old-side actor may confirm.")
        return HandoverResult(
            _handover(old_confirmed_by_user_id=OLD_HR_ID, old_confirmed_at=NOW)
        )

    async def confirm_new(
        self, handover_id: UUID, actor: CurrentActor, idempotency_key: str
    ) -> HandoverResult:
        return HandoverResult(
            _handover(new_confirmed_by_user_id=NEW_HR_ID, new_confirmed_at=NOW)
        )

    async def export_csv(self, handover_id: UUID, actor: CurrentActor) -> HandoverCsvExport:
        return HandoverCsvExport("handover.csv", b"asset_id,asset_name\r\n1,Camera\r\n")


class HandoverApiTests(unittest.IsolatedAsyncioTestCase):
    """Verify aliases, either-side routes, CSV headers, validation, and errors."""

    async def asyncSetUp(self) -> None:
        self.service = FakeHandoverService()
        app = create_app(UnusedSessionServiceFactory())
        app.dependency_overrides[get_current_actor] = lambda: CurrentActor(
            OLD_HR_ID, frozenset({OperationalRole.HR})
        )
        app.dependency_overrides[get_handover_service] = lambda: cast(
            HandoverService, self.service
        )
        self.client = AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="https://testserver",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_complete_route_contract_and_csv_download(self) -> None:
        headers = {"Idempotency-Key": "key"}
        created = await self.client.post(
            "/api/v1/handovers", headers=headers, json={"termId": str(TERM_ID)}
        )
        detail = await self.client.get(f"/api/v1/handovers/{HANDOVER_ID}")
        old = await self.client.post(
            f"/api/v1/handovers/{HANDOVER_ID}/confirm-old", headers=headers
        )
        new = await self.client.post(
            f"/api/v1/handovers/{HANDOVER_ID}/confirm-new", headers=headers
        )
        exported = await self.client.get(f"/api/v1/handovers/{HANDOVER_ID}/export")

        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["termId"], str(TERM_ID))
        self.assertEqual(created.json()["items"][0]["assetName"], "Camera")
        self.assertEqual(detail.json()["status"], "PENDING_CONFIRMATIONS")
        self.assertIsNotNone(old.json()["oldConfirmedAt"])
        self.assertIsNotNone(new.json()["newConfirmedAt"])
        self.assertEqual(exported.headers["content-type"], "text/csv; charset=utf-8")
        self.assertIn("attachment", exported.headers["content-disposition"])
        self.assertIn(b"Camera", exported.content)
        self.assertEqual(self.service.created_term_id, TERM_ID)

    async def test_strict_validation_and_confirmation_ownership_error(self) -> None:
        invalid = await self.client.post(
            "/api/v1/handovers",
            headers={"Idempotency-Key": "key"},
            json={"termId": str(TERM_ID), "extra": True},
        )
        self.assertEqual(invalid.status_code, 422)
        self.service.forbid_old = True
        forbidden = await self.client.post(
            f"/api/v1/handovers/{HANDOVER_ID}/confirm-old",
            headers={"Idempotency-Key": "old"},
        )
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(forbidden.json()["error"]["code"], "HANDOVER_CONFIRMATION_FORBIDDEN")


def _handover(**changes: object) -> Handover:
    item = HandoverItem(
        ITEM_ID,
        HANDOVER_ID,
        ASSET_ID,
        "Camera",
        2,
        2,
        AssetCondition.GOOD,
        (
            HandoverCustodySnapshot(
                CUSTODY_ID,
                OLD_HR_ID,
                CustodyStatus.ACTIVE,
                2,
                0,
                0,
                AssetCondition.GOOD,
                None,
            ),
        ),
        False,
        NOW,
        datetime(2030, 1, 1, tzinfo=UTC),
    )
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
        "items": (item,),
    }
    values.update(changes)
    return Handover(**values)  # type: ignore[arg-type]
