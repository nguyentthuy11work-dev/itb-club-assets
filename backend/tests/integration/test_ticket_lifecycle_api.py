"""PostgreSQL API integration proof for the complete F05 Ticket lifecycle."""

from __future__ import annotations

import os
import tempfile
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ams.application.authentication import SessionService
from ams.application.evidence import EvidenceStorageService
from ams.application.reliability import IClock
from ams.domain.identity import CurrentActor, OperationalRole
from ams.infrastructure.storage import LocalFileStorage
from ams.interface.api.app import (
    SqlAlchemyOperationalReadServiceFactory,
    SqlAlchemyTicketCreationServiceFactory,
    SqlAlchemyTicketLifecycleServiceFactory,
    SqlAlchemyTicketReadServiceFactory,
    create_app,
)
from ams.interface.api.dependencies import get_current_actor

BORROWER_ID = UUID("00000000-0000-4000-8000-000000005401")
CUSTODIAN_ID = UUID("00000000-0000-4000-8000-000000005402")
OUTSIDER_ID = UUID("00000000-0000-4000-8000-000000005403")
CATEGORY_ID = UUID("00000000-0000-4000-8000-000000005404")
ASSET_ID = UUID("00000000-0000-4000-8000-000000005405")
CUSTODY_ID = UUID("00000000-0000-4000-8000-000000005406")
CUSTODY_EVIDENCE_ID = UUID("00000000-0000-4000-8000-000000005407")
MISSING_CUSTODY_ID = UUID("00000000-0000-4000-8000-000000005408")
TEAM_ID = UUID("00000000-0000-4000-8000-000000005409")
OTHER_TEAM_ID = UUID("00000000-0000-4000-8000-000000005410")


class SteppingClock(IClock):
    """Keep reminder dates stable while preserving deterministic event ordering."""

    def __init__(self) -> None:
        self._current = datetime(2026, 10, 9, tzinfo=UTC)

    def now(self) -> datetime:
        """Return a deterministic UTC instant newer than the prior call."""

        current = self._current
        self._current += timedelta(microseconds=1)
        return current


class UnusedSessionServiceFactory:
    """Satisfy composition while tests override current actor resolution."""

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[SessionService]:
        """Yield an unused placeholder service."""

        yield cast(SessionService, object())


@unittest.skipUnless(os.getenv("AMS_DATABASE_URL"), "AMS_DATABASE_URL is required")
class TicketLifecycleApiTests(unittest.IsolatedAsyncioTestCase):
    """Exercise grouping, ownership, transitions, quantities, evidence, and replay."""

    async def asyncSetUp(self) -> None:
        """Insert one active source Custody and wire production Ticket adapters."""

        self.engine = create_async_engine(os.environ["AMS_DATABASE_URL"], pool_pre_ping=True)
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)
        self.temp_directory = tempfile.TemporaryDirectory(dir="/tmp/opencode")
        await self._cleanup()
        await self._seed()
        self.actor = self._actor(BORROWER_ID, "Borrower")
        evidence = EvidenceStorageService(LocalFileStorage(Path(self.temp_directory.name)))
        clock = SteppingClock()
        application = create_app(
            UnusedSessionServiceFactory(),
            ticket_creation_service_factory=SqlAlchemyTicketCreationServiceFactory(
                self.session_maker,
                clock,
            ),
            ticket_lifecycle_service_factory=SqlAlchemyTicketLifecycleServiceFactory(
                self.session_maker,
                evidence,
                clock,
            ),
            ticket_read_service_factory=SqlAlchemyTicketReadServiceFactory(self.session_maker),
            operational_read_service_factory=SqlAlchemyOperationalReadServiceFactory(
                self.session_maker
            ),
        )

        async def current_actor_override() -> CurrentActor:
            return self.actor

        application.dependency_overrides[get_current_actor] = current_actor_override
        self.client = AsyncClient(
            transport=ASGITransport(app=application, raise_app_exceptions=False),
            base_url="https://testserver",
        )

    async def asyncTearDown(self) -> None:
        """Close resources and delete all Ticket-owned fixture records."""

        await self.client.aclose()
        await self._cleanup()
        await self.engine.dispose()
        self.temp_directory.cleanup()

    async def test_temporary_ticket_end_to_end_is_owned_atomic_and_idempotent(self) -> None:
        """A temporary Ticket reserves once and releases only after both return actions."""

        created = await self._create("temporary-create")
        retry = await self._create("temporary-create")
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(retry.json(), created.json())
        body = created.json()
        self.assertEqual(len(body["createdTickets"]), 1)
        self.assertEqual(len(body["rejectedGroups"]), 1)
        self.assertEqual(body["rejectedGroups"][0]["reasonCode"], "SOURCE_CUSTODY_UNAVAILABLE")
        ticket_id = UUID(body["createdTickets"][0]["id"])
        self.assertEqual(await self._quantities(), (3, 2, 0))
        reminder_rows = await self._reminder_rows(ticket_id)
        self.assertEqual(
            [(row[1], row[2]) for row in reminder_rows],
            [
                ("TICKET_DUE_1_DAY", "2026-10-14T02:00:00+00:00"),
                ("TICKET_DUE_3_DAYS", "2026-10-12T02:00:00+00:00"),
                ("TICKET_OVERDUE", "2026-10-16T02:00:00+00:00"),
            ],
        )

        page = await self.client.get("/api/v1/tickets")
        filtered_page = await self.client.get(
            "/api/v1/tickets",
            params={
                "perspective": "BORROWER",
                "status": "PENDING",
                "search": "Camera",
                "asset_id": str(ASSET_ID),
            },
        )
        detail = await self.client.get(f"/api/v1/tickets/{ticket_id}")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual(page.json()["total"], 1)
        self.assertEqual(filtered_page.status_code, 200, filtered_page.text)
        self.assertEqual(filtered_page.json()["total"], 1)
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertNotIn("objectKey", detail.text)

        self.actor = self._actor(OUTSIDER_ID, "Outsider")
        hidden = await self.client.get(f"/api/v1/tickets/{ticket_id}")
        self._assert_error(hidden, 404, "TICKET_NOT_FOUND")

        self.actor = self._actor(CUSTODIAN_ID, "Custodian")
        custodian_page = await self.client.get(
            "/api/v1/tickets", params={"perspective": "CUSTODIAN"}
        )
        self.assertEqual(custodian_page.json()["total"], 1)
        approved = await self._json_action(ticket_id, "approve", "approve-key")
        self.assertEqual(approved.json()["status"], "APPROVED")
        self.assertEqual(
            [row[0] for row in await self._reminder_rows(ticket_id)],
            [row[0] for row in reminder_rows],
        )
        delivered_reminder_id = reminder_rows[0][0]
        await self._mark_reminder_delivered(delivered_reminder_id)

        self.actor = self._actor(OUTSIDER_ID, "Outsider")
        wrong_borrower = await self._evidence_action(
            ticket_id, "confirm-received", "wrong-pickup"
        )
        self._assert_error(wrong_borrower, 403, "TICKET_ACTION_FORBIDDEN")
        self.assertEqual(await self._quantities(), (3, 2, 0))

        self.actor = self._actor(BORROWER_ID, "Borrower")
        received = await self._evidence_action(ticket_id, "confirm-received", "pickup-key")
        received_retry = await self._evidence_action(
            ticket_id, "confirm-received", "pickup-key"
        )
        self.assertEqual(received.status_code, 200, received.text)
        self.assertEqual(received_retry.json(), received.json())
        self.assertEqual(received.json()["status"], "BORROWING")
        self.assertEqual(await self._quantities(), (3, 0, 2))

        returned = await self._evidence_action(ticket_id, "mark-returned", "return-key")
        self.assertEqual(returned.json()["status"], "RETURNING")
        self.assertEqual(await self._quantities(), (3, 0, 2))
        return_event_type, return_payload = await self._latest_ticket_outbox(ticket_id)
        self.assertEqual(return_event_type, "TICKET_RETURN_AWAITING_CONFIRMATION")
        self.assertEqual(return_payload["action"], "CONFIRM_RETURNED")
        return_emails = cast(list[dict[str, str]], return_payload["emails"])
        self.assertEqual(return_emails[0]["recipientId"], str(CUSTODIAN_ID))

        wrong_receiver = await self._evidence_action(
            ticket_id, "confirm-returned", "wrong-receiver"
        )
        self._assert_error(wrong_receiver, 403, "TICKET_ACTION_FORBIDDEN")
        self.assertEqual(await self._quantities(), (3, 0, 2))

        self.actor = self._actor(CUSTODIAN_ID, "Custodian")
        closed = await self._evidence_action(ticket_id, "confirm-returned", "receive-back-key")
        closed_retry = await self._evidence_action(
            ticket_id, "confirm-returned", "receive-back-key"
        )
        self.assertEqual(closed.json()["status"], "CLOSED")
        self.assertEqual(closed_retry.json(), closed.json())
        self.assertEqual(await self._quantities(), (5, 0, 0))
        self.assertEqual(await self._ticket_child_counts(ticket_id), (3, 5))
        self.assertEqual(
            [row[0] for row in await self._reminder_rows(ticket_id)],
            [delivered_reminder_id],
        )

    async def test_reject_and_cancellation_release_exactly_once(self) -> None:
        """Reject, direct cancel, and accepted approved cancel release only once."""

        ticket_id = await self._create_valid_ticket("reject-create")
        self.actor = self._actor(OUTSIDER_ID, "Outsider")
        forbidden = await self.client.post(
            f"/api/v1/tickets/{ticket_id}/reject",
            headers={"Idempotency-Key": "wrong-reject"},
            json={"reason": "No"},
        )
        self._assert_error(forbidden, 403, "TICKET_ACTION_FORBIDDEN")

        self.actor = self._actor(CUSTODIAN_ID, "Custodian")
        rejected = await self.client.post(
            f"/api/v1/tickets/{ticket_id}/reject",
            headers={"Idempotency-Key": "reject-key"},
            json={"reason": "Unavailable"},
        )
        rejected_retry = await self.client.post(
            f"/api/v1/tickets/{ticket_id}/reject",
            headers={"Idempotency-Key": "reject-key"},
            json={"reason": "Unavailable"},
        )
        self.assertEqual(rejected.json()["status"], "REJECTED")
        self.assertEqual(rejected_retry.json(), rejected.json())
        self.assertEqual(await self._quantities(), (5, 0, 0))
        self.assertEqual(await self._reminder_rows(ticket_id), [])
        rejected_type, rejected_payload = await self._latest_ticket_outbox(ticket_id)
        self.assertEqual(rejected_type, "TICKET_REJECTED")
        rejected_emails = cast(list[dict[str, str]], rejected_payload["emails"])
        self.assertEqual(rejected_emails[0]["recipientId"], str(BORROWER_ID))

        self.actor = self._actor(BORROWER_ID, "Borrower")
        direct_id = await self._create_valid_ticket("direct-create")
        direct = await self.client.post(
            f"/api/v1/tickets/{direct_id}/cancel-request",
            headers={"Idempotency-Key": "direct-cancel"},
            json={"reason": "Changed plans"},
        )
        self.assertEqual(direct.json()["status"], "CANCELLED")
        self.assertEqual(await self._quantities(), (5, 0, 0))
        self.assertEqual(await self._reminder_rows(direct_id), [])
        direct_type, direct_payload = await self._latest_ticket_outbox(direct_id)
        self.assertEqual(direct_type, "TICKET_CANCELLED")
        direct_emails = cast(list[dict[str, str]], direct_payload["emails"])
        self.assertEqual(direct_emails[0]["recipientId"], str(CUSTODIAN_ID))

        self.actor = self._actor(BORROWER_ID, "Borrower")
        approved_id = await self._create_valid_ticket("approved-create")
        self.actor = self._actor(CUSTODIAN_ID, "Custodian")
        await self._json_action(approved_id, "approve", "approved-approve")
        self.actor = self._actor(BORROWER_ID, "Borrower")
        requested = await self.client.post(
            f"/api/v1/tickets/{approved_id}/cancel-request",
            headers={"Idempotency-Key": "approved-request"},
            json={"reason": "No longer needed"},
        )
        self.assertEqual(requested.json()["status"], "CANCEL_PENDING")
        self.assertEqual(await self._quantities(), (3, 2, 0))
        requested_type, requested_payload = await self._latest_ticket_outbox(approved_id)
        self.assertEqual(requested_type, "TICKET_CANCELLATION_REQUESTED")
        self.assertEqual(requested_payload["action"], "RESOLVE_CANCELLATION")
        self.actor = self._actor(CUSTODIAN_ID, "Custodian")
        resolved = await self.client.post(
            f"/api/v1/tickets/{approved_id}/cancel-resolve",
            headers={"Idempotency-Key": "approved-resolve"},
            json={"approve": True},
        )
        self.assertEqual(resolved.json()["status"], "CANCELLED")
        self.assertEqual(await self._quantities(), (5, 0, 0))
        self.assertEqual(await self._reminder_rows(approved_id), [])
        resolved_type, resolved_payload = await self._latest_ticket_outbox(approved_id)
        self.assertEqual(resolved_type, "TICKET_CANCELLATION_APPROVED")
        resolved_emails = cast(list[dict[str, str]], resolved_payload["emails"])
        self.assertEqual(resolved_emails[0]["recipientId"], str(BORROWER_ID))
        repeated = await self.client.post(
            f"/api/v1/tickets/{approved_id}/cancel-resolve",
            headers={"Idempotency-Key": "approved-resolve-again"},
            json={"approve": True},
        )
        self._assert_error(repeated, 409, "INVALID_TICKET_TRANSITION")
        self.assertEqual(await self._quantities(), (5, 0, 0))

    async def test_create_validates_header_and_method_due_date_policy(self) -> None:
        """Transport validation rejects ambiguous retries and invalid method/date pairs."""

        missing_header = await self.client.post(
            "/api/v1/tickets/borrow", json=self._payload(include_invalid=False)
        )
        self._assert_error(missing_header, 422, "VALIDATION_ERROR")

        temporary = self._payload(include_invalid=False)
        temporary_items = cast(list[dict[str, object]], temporary["items"])
        temporary_allocations = cast(list[dict[str, object]], temporary_items[0]["allocations"])
        temporary_allocations[0]["dueDate"] = None
        invalid_temporary = await self.client.post(
            "/api/v1/tickets/borrow",
            headers={"Idempotency-Key": "invalid-temporary"},
            json=temporary,
        )
        self._assert_error(invalid_temporary, 422, "VALIDATION_ERROR")

        transfer = self._payload(include_invalid=False, method="CUSTODY_TRANSFER")
        transfer_items = cast(list[dict[str, object]], transfer["items"])
        transfer_allocations = cast(list[dict[str, object]], transfer_items[0]["allocations"])
        transfer_allocations[0]["dueDate"] = "2026-10-15"
        invalid_transfer = await self.client.post(
            "/api/v1/tickets/borrow",
            headers={"Idempotency-Key": "invalid-transfer"},
            json=transfer,
        )
        self._assert_error(invalid_transfer, 422, "VALIDATION_ERROR")
        async with self.session_maker() as session:
            count = await session.scalar(
                text("SELECT COUNT(*) FROM borrow_tickets WHERE borrower_user_id = :borrower"),
                {"borrower": BORROWER_ID},
            )
        self.assertEqual(count, 0)

    async def test_custody_transfer_moves_holding_and_closes(self) -> None:
        """Recipient evidence moves source on-hand into active target Custody."""

        payload = self._payload(include_invalid=False, method="CUSTODY_TRANSFER")
        created = await self.client.post(
            "/api/v1/tickets/borrow",
            headers={"Idempotency-Key": "transfer-create"},
            json=payload,
        )
        self.assertEqual(created.status_code, 201, created.text)
        ticket_id = UUID(created.json()["createdTickets"][0]["id"])
        self.assertEqual(await self._reminder_rows(ticket_id), [])
        self.actor = self._actor(CUSTODIAN_ID, "Custodian")
        await self._json_action(ticket_id, "approve", "transfer-approve")
        self.actor = self._actor(BORROWER_ID, "Borrower")
        closed = await self._evidence_action(ticket_id, "confirm-received", "transfer-received")
        self.assertEqual(closed.status_code, 200, closed.text)
        self.assertEqual(closed.json()["status"], "CLOSED")
        async with self.session_maker() as session:
            result = await session.execute(
                text(
                    """
                    SELECT custodian_user_id, on_hand_quantity, reserved_quantity, status
                    FROM custodies
                    WHERE asset_id = :asset_id AND status IN ('ACTIVE', 'PENDING')
                    ORDER BY custodian_user_id
                    """
                ),
                {"asset_id": ASSET_ID},
            )
            rows = result.all()
        self.assertEqual(len(rows), 2)
        holdings = {row[0]: (row[1], row[2], row[3]) for row in rows}
        self.assertEqual(holdings[CUSTODIAN_ID], (3, 0, "ACTIVE"))
        self.assertEqual(holdings[BORROWER_ID], (2, 0, "ACTIVE"))
        self.assertEqual((await self._quantities())[0], 5)

        second_payload = self._payload(include_invalid=False, method="CUSTODY_TRANSFER")
        second_items = cast(list[dict[str, object]], second_payload["items"])
        second_allocations = cast(list[dict[str, object]], second_items[0]["allocations"])
        second_allocations[0]["quantity"] = 1
        second = await self.client.post(
            "/api/v1/tickets/borrow",
            headers={"Idempotency-Key": "transfer-create-second"},
            json=second_payload,
        )
        self.assertEqual(second.status_code, 201, second.text)
        second_id = UUID(second.json()["createdTickets"][0]["id"])
        self.actor = self._actor(CUSTODIAN_ID, "Custodian")
        await self._json_action(second_id, "approve", "transfer-approve-second")
        self.actor = self._actor(BORROWER_ID, "Borrower")
        merged = await self._evidence_action(
            second_id, "confirm-received", "transfer-received-second"
        )
        self.assertEqual(merged.status_code, 200, merged.text)
        async with self.session_maker() as session:
            target_quantity = await session.scalar(
                text(
                    """
                    SELECT on_hand_quantity
                    FROM custodies
                    WHERE asset_id = :asset_id AND custodian_user_id = :borrower
                      AND status = 'ACTIVE'
                    """
                ),
                {"asset_id": ASSET_ID, "borrower": BORROWER_ID},
            )
        self.assertEqual(target_quantity, 3)

    async def test_operational_history_dashboards_and_notification_references(self) -> None:
        """Real read models enforce scope and expose typed work/event references."""

        ticket_id = await self._create_valid_ticket("operations-create")
        self.actor = self._actor(CUSTODIAN_ID, "Custodian")
        dashboard = await self.client.get("/api/v1/dashboard")
        self.assertEqual(dashboard.status_code, 200, dashboard.text)
        self.assertEqual(dashboard.json()["stats"]["pendingApprovalCount"], 1)
        self.assertEqual(dashboard.json()["todos"][0]["action"], "REVIEW_TICKET")
        self.assertEqual(dashboard.json()["todos"][0]["referenceId"], str(ticket_id))

        denied = await self.client.get(f"/api/v1/assets/{ASSET_ID}/transactions")
        self._assert_error(denied, 403, "AUTHORIZATION_DENIED")

        self.actor = self._actor(
            CUSTODIAN_ID,
            "Asset Lead",
            OperationalRole.TEAM_LEAD_ASSET,
            team_ids=frozenset({TEAM_ID}),
        )
        lead_history = await self.client.get(f"/api/v1/assets/{ASSET_ID}/transactions")
        self.assertEqual(lead_history.status_code, 200, lead_history.text)
        self.actor = self._actor(
            CUSTODIAN_ID,
            "Wrong Asset Lead",
            OperationalRole.TEAM_LEAD_ASSET,
            team_ids=frozenset({OTHER_TEAM_ID}),
        )
        wrong_lead = await self.client.get(f"/api/v1/assets/{ASSET_ID}/transactions")
        self._assert_error(wrong_lead, 403, "AUTHORIZATION_DENIED")

        self.actor = self._actor(CUSTODIAN_ID, "HR", OperationalRole.HR)
        history = await self.client.get(
            "/api/v1/hr/asset-transactions",
            params={"assetId": str(ASSET_ID), "offset": 0, "limit": 2},
        )
        asset_history = await self.client.get(f"/api/v1/assets/{ASSET_ID}/transactions")
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(len(history.json()["items"]), 2)
        self.assertGreaterEqual(history.json()["total"], 4)
        self.assertTrue(
            all(row["assetId"] == str(ASSET_ID) for row in asset_history.json()["items"])
        )
        times = [row["occurredAt"] for row in asset_history.json()["items"]]
        self.assertEqual(times, sorted(times, reverse=True))
        filtered = await self.client.get(
            "/api/v1/hr/asset-transactions",
            params={"from": "2030-01-01T00:00:00Z"},
        )
        self.assertEqual(filtered.json()["total"], 0)
        hr_dashboard = await self.client.get("/api/v1/hr/dashboard")
        self.assertEqual(hr_dashboard.status_code, 200, hr_dashboard.text)
        self.assertEqual(hr_dashboard.json()["stats"]["totalAssetQuantity"], 5)

        await self._json_action(ticket_id, "approve", "operations-approve")
        event_type, payload = await self._latest_ticket_outbox(ticket_id)
        self.assertEqual(event_type, "TICKET_APPROVED")
        self.assertEqual(payload["reference"], {"type": "TICKET", "id": str(ticket_id)})
        self.assertEqual(payload["action"], "CONFIRM_RECEIVED")
        emails = cast(list[dict[str, str]], payload["emails"])
        self.assertEqual(emails[0]["recipientId"], str(BORROWER_ID))

    async def _create(self, key: str) -> Response:
        return await self.client.post(
            "/api/v1/tickets/borrow",
            headers={"Idempotency-Key": key},
            json=self._payload(include_invalid=True),
        )

    async def _create_valid_ticket(self, key: str) -> UUID:
        response = await self.client.post(
            "/api/v1/tickets/borrow",
            headers={"Idempotency-Key": key},
            json=self._payload(include_invalid=False),
        )
        self.assertEqual(response.status_code, 201, response.text)
        return UUID(response.json()["createdTickets"][0]["id"])

    @staticmethod
    def _payload(*, include_invalid: bool, method: str = "TEMPORARY") -> dict[str, object]:
        allocations: list[dict[str, object]] = [
            {
                "sourceCustodyId": str(CUSTODY_ID),
                "quantity": 2,
                "method": method,
                "dueDate": "2026-10-15" if method == "TEMPORARY" else None,
            }
        ]
        if include_invalid:
            allocations.append(
                {
                    "sourceCustodyId": str(MISSING_CUSTODY_ID),
                    "quantity": 1,
                    "method": method,
                    "dueDate": "2026-10-15" if method == "TEMPORARY" else None,
                }
            )
        return {"items": [{"assetId": str(ASSET_ID), "allocations": allocations}]}

    async def _json_action(self, ticket_id: UUID, action: str, key: str) -> Response:
        response = await self.client.post(
            f"/api/v1/tickets/{ticket_id}/{action}",
            headers={"Idempotency-Key": key},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response

    async def _evidence_action(self, ticket_id: UUID, action: str, key: str) -> Response:
        return await self.client.post(
            f"/api/v1/tickets/{ticket_id}/{action}",
            headers={"Idempotency-Key": key},
            data={"metadata": '{"note":"API proof"}'},
            files={"files": ("proof.png", self._png(), "image/png")},
        )

    async def _quantities(self) -> tuple[int, int, int]:
        async with self.session_maker() as session:
            result = await session.execute(
                text(
                    """
                    SELECT assets.available_quantity,
                           custodies.reserved_quantity,
                           custodies.temporarily_loaned_quantity
                    FROM assets
                    JOIN custodies ON custodies.id = :custody_id
                    WHERE assets.id = :asset_id
                    """
                ),
                {"asset_id": ASSET_ID, "custody_id": CUSTODY_ID},
            )
            row = result.one()
            return int(row[0]), int(row[1]), int(row[2])

    async def _ticket_child_counts(self, ticket_id: UUID) -> tuple[int, int]:
        async with self.session_maker() as session:
            result = await session.execute(
                text(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM ticket_evidence WHERE ticket_id = :id),
                        (SELECT COUNT(*) FROM ticket_events WHERE ticket_id = :id)
                    """
                ),
                {"id": ticket_id},
            )
            row = result.one()
            return int(row[0]), int(row[1])

    async def _reminder_rows(self, ticket_id: UUID) -> list[tuple[UUID, str, str]]:
        async with self.session_maker() as session:
            result = await session.execute(
                text(
                    """
                    SELECT id, event_type, scheduled_at
                    FROM outbox_events
                    WHERE aggregate_type = 'BORROW_TICKET'
                      AND aggregate_id = :ticket_id
                      AND event_type IN (
                          'TICKET_DUE_3_DAYS',
                          'TICKET_DUE_1_DAY',
                          'TICKET_OVERDUE'
                      )
                    ORDER BY event_type
                    """
                ),
                {"ticket_id": ticket_id},
            )
            return [
                (row.id, str(row.event_type), row.scheduled_at.isoformat()) for row in result
            ]

    async def _mark_reminder_delivered(self, reminder_id: UUID) -> None:
        async with self.session_maker() as session:
            await session.execute(
                text(
                    """
                    UPDATE outbox_events
                    SET status = 'DELIVERED',
                        completed_at = scheduled_at,
                        updated_at = scheduled_at
                    WHERE id = :reminder_id
                    """
                ),
                {"reminder_id": reminder_id},
            )
            await session.commit()

    async def _latest_ticket_outbox(self, ticket_id: UUID) -> tuple[str, dict[str, object]]:
        async with self.session_maker() as session:
            result = await session.execute(
                text(
                    """
                    SELECT event_type, payload
                    FROM outbox_events
                    WHERE aggregate_type = 'BORROW_TICKET'
                      AND aggregate_id = :ticket_id
                      AND event_type NOT IN (
                          'TICKET_DUE_3_DAYS', 'TICKET_DUE_1_DAY', 'TICKET_OVERDUE'
                      )
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """
                ),
                {"ticket_id": ticket_id},
            )
            row = result.mappings().one()
            return str(row["event_type"]), dict(row["payload"])

    async def _seed(self) -> None:
        async with self.session_maker() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO users (id, username, email, display_name, password_hash)
                    VALUES
                        (:borrower, 'ticket.borrower', 'ticket.borrower@example.test',
                         'Borrower', 'hash'),
                        (:custodian, 'ticket.custodian', 'ticket.custodian@example.test',
                         'Custodian', 'hash'),
                        (:outsider, 'ticket.outsider', 'ticket.outsider@example.test',
                         'Outsider', 'hash')
                    """
                ),
                {
                    "borrower": BORROWER_ID,
                    "custodian": CUSTODIAN_ID,
                    "outsider": OUTSIDER_ID,
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO teams (id, name, code)
                    VALUES (:id, 'Ticket API Team', 'TICKET_API_TEAM')
                    """
                ),
                {"id": TEAM_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO asset_categories (id, code, name)
                    VALUES (:id, 'TICKET_API_CATEGORY', 'Ticket API Category')
                    """
                ),
                {"id": CATEGORY_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO assets (
                        id, category_id, responsible_team_id, name, source, total_quantity,
                        available_quantity, created_by_user_id
                    ) VALUES (
                        :id, :category, :team, 'Ticket Camera', 'PURCHASE', 5, 5, :custodian
                    )
                    """
                ),
                {
                    "id": ASSET_ID,
                    "category": CATEGORY_ID,
                    "team": TEAM_ID,
                    "custodian": CUSTODIAN_ID,
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO custodies (
                        id, asset_id, custodian_user_id, assigned_by_user_id, status,
                        on_hand_quantity, condition, assigned_at, confirmed_at,
                        confirmed_by_user_id
                    ) VALUES (
                        :id, :asset, :custodian, :custodian, 'ACTIVE', 5, 'GOOD',
                        '2026-10-01T00:00:00Z', '2026-10-01T01:00:00Z', :custodian
                    )
                    """
                ),
                {"id": CUSTODY_ID, "asset": ASSET_ID, "custodian": CUSTODIAN_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO custody_evidence (
                        id, custody_id, uploaded_by_user_id, evidence_type, object_key,
                        content_type, content_sha256, size_bytes
                    ) VALUES (
                        :id, :custody, :custodian, 'ASSIGNMENT_CONFIRMATION',
                        'ticket-api/source', 'image/png', :sha, 100
                    )
                    """
                ),
                {
                    "id": CUSTODY_EVIDENCE_ID,
                    "custody": CUSTODY_ID,
                    "custodian": CUSTODIAN_ID,
                    "sha": "c" * 64,
                },
            )
            await session.commit()

    async def _cleanup(self) -> None:
        async with self.session_maker() as session:
            await session.execute(
                text(
                    """
                    DELETE FROM notifications
                    WHERE outbox_event_id IN (
                        SELECT id FROM outbox_events
                        WHERE aggregate_type = 'BORROW_TICKET'
                          AND aggregate_id IN (
                              SELECT id FROM borrow_tickets
                              WHERE borrower_user_id = ANY(:actor_ids)
                          )
                    )
                    """
                ),
                {"actor_ids": [BORROWER_ID, CUSTODIAN_ID, OUTSIDER_ID]},
            )
            await session.execute(
                text(
                    """
                    DELETE FROM outbox_events
                    WHERE aggregate_type = 'BORROW_TICKET'
                      AND aggregate_id IN (
                          SELECT id FROM borrow_tickets
                          WHERE borrower_user_id = ANY(:actor_ids)
                      )
                    """
                ),
                {"actor_ids": [BORROWER_ID, CUSTODIAN_ID, OUTSIDER_ID]},
            )
            for table in ("ticket_evidence", "ticket_events", "ticket_items"):
                await session.execute(
                    text(
                        f"""
                        DELETE FROM {table}
                        WHERE ticket_id IN (
                            SELECT id FROM borrow_tickets
                            WHERE borrower_user_id = ANY(:actor_ids)
                        )
                        """
                    ),
                    {"actor_ids": [BORROWER_ID, CUSTODIAN_ID, OUTSIDER_ID]},
                )
            await session.execute(
                text("DELETE FROM borrow_tickets WHERE borrower_user_id = ANY(:actor_ids)"),
                {"actor_ids": [BORROWER_ID, CUSTODIAN_ID, OUTSIDER_ID]},
            )
            await session.execute(
                text("DELETE FROM idempotency_records WHERE actor_id = ANY(:ids)"),
                {"ids": [BORROWER_ID, CUSTODIAN_ID, OUTSIDER_ID]},
            )
            await session.execute(
                text(
                    """
                    DELETE FROM custody_evidence
                    WHERE custody_id IN (
                        SELECT id FROM custodies WHERE asset_id = :asset
                    )
                    """
                ),
                {"asset": ASSET_ID},
            )
            await session.execute(
                text("DELETE FROM custodies WHERE asset_id = :asset"), {"asset": ASSET_ID}
            )
            await session.execute(text("DELETE FROM assets WHERE id = :id"), {"id": ASSET_ID})
            await session.execute(
                text("DELETE FROM asset_categories WHERE id = :id"), {"id": CATEGORY_ID}
            )
            await session.execute(text("DELETE FROM teams WHERE id = :id"), {"id": TEAM_ID})
            await session.execute(
                text("DELETE FROM users WHERE id = ANY(:ids)"),
                {"ids": [BORROWER_ID, CUSTODIAN_ID, OUTSIDER_ID]},
            )
            await session.commit()

    @staticmethod
    def _actor(
        actor_id: UUID,
        display_name: str,
        *roles: OperationalRole,
        team_ids: frozenset[UUID] = frozenset(),
    ) -> CurrentActor:
        return CurrentActor(
            actor_id,
            frozenset(roles),
            team_ids=team_ids,
            is_active=True,
            display_name=display_name,
        )

    @staticmethod
    def _png() -> bytes:
        return b"\x89PNG\r\n\x1a\n" + b"ticket-evidence"

    def _assert_error(self, response: Response, status_code: int, code: str) -> None:
        self.assertEqual(response.status_code, status_code, response.text)
        self.assertEqual(response.json()["error"]["code"], code)
        UUID(response.json()["traceId"])
