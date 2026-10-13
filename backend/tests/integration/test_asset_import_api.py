"""PostgreSQL API tests for F02.08 baseline Asset import preview and confirmation."""

from __future__ import annotations

import csv
import io
import os
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast
from uuid import UUID

from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ams.application.authentication import SessionService
from ams.domain.identity import CurrentActor, OperationalRole
from ams.infrastructure.asset_import_parser import TEMPLATE_COLUMNS
from ams.interface.api.app import SqlAlchemyAssetImportServiceFactory, create_app
from ams.interface.api.dependencies import get_current_actor

HR_ID = UUID("00000000-0000-4000-8000-000000002601")
CUSTODIAN_ID = UUID("00000000-0000-4000-8000-000000002602")
TEAM_ID = UUID("00000000-0000-4000-8000-000000002603")
CATEGORY_ID = UUID("00000000-0000-4000-8000-000000002604")


class UnusedSessionServiceFactory:
    """Satisfy application composition while actor resolution is overridden."""

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[SessionService]:
        """Yield an unused placeholder."""

        yield cast(SessionService, object())


@unittest.skipUnless(os.getenv("AMS_DATABASE_URL"), "AMS_DATABASE_URL is required")
class AssetImportApiTests(unittest.IsolatedAsyncioTestCase):
    """Prove preview isolation, valid-only confirmation, replay, and file rejection."""

    async def asyncSetUp(self) -> None:
        """Create import references and a production-adapter API."""

        self.engine = create_async_engine(os.environ["AMS_DATABASE_URL"], pool_pre_ping=True)
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)
        await self._cleanup()
        async with self.session_maker() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO users (id, username, email, display_name, password_hash)
                    VALUES
                        (:hr, 'import.hr', 'import.hr@example.test', 'Import HR', 'hash'),
                        (:custodian, 'import.owner', 'import.owner@example.test',
                         'Import Owner', 'hash')
                    """
                ),
                {"hr": HR_ID, "custodian": CUSTODIAN_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO teams (id, name, code)
                    VALUES (:id, 'Import Team', 'IMPORT_TEAM')
                    """
                ),
                {"id": TEAM_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO asset_categories (id, code, name)
                    VALUES (:id, 'IMPORT_CATEGORY', 'Import Category')
                    """
                ),
                {"id": CATEGORY_ID},
            )
            await session.commit()

        self.actor = CurrentActor(
            HR_ID,
            frozenset({OperationalRole.HR}),
            is_active=True,
        )
        application = create_app(
            UnusedSessionServiceFactory(),
            asset_import_service_factory=SqlAlchemyAssetImportServiceFactory(
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
        """Close resources and delete all import effects."""

        await self.client.aclose()
        await self._cleanup()
        await self.engine.dispose()

    async def test_preview_then_confirm_imports_only_valid_rows_once(self) -> None:
        """Preview writes no Assets; confirmation atomically writes valid rows and events."""

        content = self._csv_content()
        first = await self._preview("preview-key", content)
        retry = await self._preview("preview-key", content)

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(retry.json(), first.json())
        body = first.json()
        self.assertEqual(body["status"], "PREVIEWED")
        self.assertEqual((body["validRowCount"], body["invalidRowCount"]), (1, 1))
        self.assertEqual(await self._effect_counts(), (1, 0, 0, 0, 1))

        changed = await self._preview("preview-key", content + b"\n")
        self._assert_error(changed, 409, "IDEMPOTENCY_KEY_REUSED")

        import_id = UUID(body["importId"])
        confirmed = await self._confirm(import_id, "confirm-key")
        confirm_retry = await self._confirm(import_id, "confirm-key")

        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirm_retry.json(), confirmed.json())
        result = confirmed.json()
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["importedAssetCount"], 1)
        self.assertEqual(result["rejectedRows"][0]["rowNumber"], 3)
        self.assertEqual(await self._effect_counts(), (1, 1, 1, 2, 2))
        self.assertEqual(await self._imported_team(), TEAM_ID)

    async def test_bad_file_and_non_hr_are_rejected_without_preview(self) -> None:
        """Unsupported source and role denial cannot create preview or business rows."""

        bad = await self.client.post(
            "/api/v1/hr/assets/imports",
            headers={"Idempotency-Key": "bad-file"},
            files={"file": ("baseline.txt", b"bad", "text/plain")},
        )
        self._assert_error(bad, 422, "UNSUPPORTED_IMPORT_FILE")

        self.actor = CurrentActor(HR_ID, frozenset(), is_active=True)
        denied = await self._preview("denied", self._csv_content())
        self._assert_error(denied, 403, "AUTHORIZATION_DENIED")
        self.assertEqual(await self._effect_counts(), (0, 0, 0, 0, 0))

    async def test_reference_change_after_preview_rejects_confirmation_atomically(self) -> None:
        """A reviewed row cannot import after its category becomes inactive."""

        preview = await self._preview("stale-preview", self._csv_content(valid_only=True))
        import_id = UUID(preview.json()["importId"])
        async with self.session_maker() as session:
            await session.execute(
                text("UPDATE asset_categories SET is_active = FALSE WHERE id = :id"),
                {"id": CATEGORY_ID},
            )
            await session.commit()

        response = await self._confirm(import_id, "stale-confirm")

        self._assert_error(response, 409, "IMPORT_PREVIEW_EXPIRED")
        self.assertEqual(await self._effect_counts(), (1, 0, 0, 0, 1))

    async def _preview(self, key: str, content: bytes) -> Response:
        return await self.client.post(
            "/api/v1/hr/assets/imports",
            headers={"Idempotency-Key": key},
            files={"file": ("baseline.csv", content, "text/csv")},
        )

    async def _confirm(self, import_id: UUID, key: str) -> Response:
        return await self.client.post(
            f"/api/v1/hr/assets/imports/{import_id}/confirm",
            headers={"Idempotency-Key": key},
        )

    @staticmethod
    def _csv_content(*, valid_only: bool = False) -> bytes:
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(TEMPLATE_COLUMNS)
        writer.writerow(
            (
                "Imported Camera",
                "Baseline camera",
                "DONATION",
                2,
                CATEGORY_ID,
                TEAM_ID,
                CUSTODIAN_ID,
                "GOOD",
                "Reviewed",
            )
        )
        if not valid_only:
            writer.writerow(("", "", "", 0, "bad", "", "bad", "BROKEN", ""))
        return stream.getvalue().encode()

    async def _effect_counts(self) -> tuple[int, int, int, int, int]:
        async with self.session_maker() as session:
            result = await session.execute(
                text(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM asset_imports
                         WHERE created_by_user_id = :hr),
                        (SELECT COUNT(*) FROM assets WHERE created_by_user_id = :hr),
                        (SELECT COUNT(*) FROM custodies WHERE assigned_by_user_id = :hr),
                        (SELECT COUNT(*) FROM outbox_events
                         WHERE (aggregate_type = 'ASSET' AND aggregate_id IN (
                             SELECT id FROM assets WHERE created_by_user_id = :hr
                         )) OR (aggregate_type = 'CUSTODY' AND aggregate_id IN (
                             SELECT id FROM custodies WHERE assigned_by_user_id = :hr
                         ))),
                        (SELECT COUNT(*) FROM idempotency_records WHERE actor_id = :hr)
                    """
                ),
                {"hr": HR_ID},
            )
            row = result.one()
            return tuple(int(value) for value in row)  # type: ignore[return-value]

    async def _imported_team(self) -> UUID | None:
        async with self.session_maker() as session:
            result = await session.execute(
                text(
                    """
                    SELECT responsible_team_id
                    FROM assets
                    WHERE created_by_user_id = :hr
                    """
                ),
                {"hr": HR_ID},
            )
            return result.scalar_one_or_none()

    async def _cleanup(self) -> None:
        async with self.session_maker() as session:
            await session.execute(
                text(
                    """
                    DELETE FROM notifications WHERE outbox_event_id IN (
                        SELECT id FROM outbox_events
                        WHERE (aggregate_type = 'ASSET' AND aggregate_id IN (
                            SELECT id FROM assets WHERE created_by_user_id = :hr
                        )) OR (aggregate_type = 'CUSTODY' AND aggregate_id IN (
                            SELECT id FROM custodies WHERE assigned_by_user_id = :hr
                        ))
                    )
                    """
                ),
                {"hr": HR_ID},
            )
            await session.execute(
                text(
                    """
                    DELETE FROM outbox_events
                    WHERE (aggregate_type = 'ASSET' AND aggregate_id IN (
                        SELECT id FROM assets WHERE created_by_user_id = :hr
                    )) OR (aggregate_type = 'CUSTODY' AND aggregate_id IN (
                        SELECT id FROM custodies WHERE assigned_by_user_id = :hr
                    ))
                    """
                ),
                {"hr": HR_ID},
            )
            await session.execute(
                text("DELETE FROM idempotency_records WHERE actor_id = :hr"),
                {"hr": HR_ID},
            )
            await session.execute(
                text(
                    """
                    DELETE FROM custody_evidence
                    WHERE custody_id IN (
                        SELECT id FROM custodies WHERE assigned_by_user_id = :hr
                    )
                    """
                ),
                {"hr": HR_ID},
            )
            await session.execute(
                text("DELETE FROM custodies WHERE assigned_by_user_id = :hr"),
                {"hr": HR_ID},
            )
            await session.execute(
                text("DELETE FROM assets WHERE created_by_user_id = :hr"),
                {"hr": HR_ID},
            )
            await session.execute(
                text("DELETE FROM asset_imports WHERE created_by_user_id = :hr"),
                {"hr": HR_ID},
            )
            await session.execute(
                text("DELETE FROM asset_categories WHERE id = :id"),
                {"id": CATEGORY_ID},
            )
            await session.execute(
                text("DELETE FROM teams WHERE id = :id"),
                {"id": TEAM_ID},
            )
            await session.execute(
                text("DELETE FROM users WHERE id IN (:hr, :custodian)"),
                {"hr": HR_ID, "custodian": CUSTODIAN_ID},
            )
            await session.commit()

    def _assert_error(self, response: Response, status: int, code: str) -> None:
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["error"]["code"], code)
        UUID(response.json()["traceId"])
