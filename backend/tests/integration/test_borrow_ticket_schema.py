"""PostgreSQL integration tests for the F05.01 Borrow Ticket migration."""

from __future__ import annotations

import os
import unittest
from datetime import date
from uuid import UUID

import asyncpg

BORROWER_ID = UUID("00000000-0000-4000-8000-000000005101")
CUSTODIAN_ID = UUID("00000000-0000-4000-8000-000000005102")
CATEGORY_ID = UUID("00000000-0000-4000-8000-000000005103")
ASSET_ID = UUID("00000000-0000-4000-8000-000000005104")
OTHER_ASSET_ID = UUID("00000000-0000-4000-8000-000000005105")
CUSTODY_ID = UUID("00000000-0000-4000-8000-000000005106")
TICKET_ID = UUID("00000000-0000-4000-8000-000000005107")


def _database_url() -> str | None:
    value = os.getenv("AMS_DATABASE_URL")
    if value is None:
        return None
    return value.replace("postgresql+asyncpg://", "postgresql://", 1)


@unittest.skipUnless(_database_url(), "AMS_DATABASE_URL is required for PostgreSQL tests")
class BorrowTicketSchemaTests(unittest.IsolatedAsyncioTestCase):
    """Prove Ticket policy constraints, ownership, history, and access indexes."""

    async def asyncSetUp(self) -> None:
        """Open a rollback-only transaction and create valid source-Custody fixtures."""

        database_url = _database_url()
        assert database_url is not None
        self.connection = await asyncpg.connect(database_url)
        self.transaction = self.connection.transaction()
        await self.transaction.start()
        await self.connection.executemany(
            """
            INSERT INTO users (id, username, email, display_name, password_hash)
            VALUES ($1, $2, $3, $4, 'hash')
            """,
            [
                (
                    BORROWER_ID,
                    "ticket.borrower",
                    "ticket.borrower@example.test",
                    "Ticket Borrower",
                ),
                (
                    CUSTODIAN_ID,
                    "ticket.custodian",
                    "ticket.custodian@example.test",
                    "Ticket Custodian",
                ),
            ],
        )
        await self.connection.execute(
            """
            INSERT INTO asset_categories (id, code, name)
            VALUES ($1, 'TICKET_CATEGORY', 'Ticket Category')
            """,
            CATEGORY_ID,
        )
        await self.connection.executemany(
            """
            INSERT INTO assets (
                id, category_id, name, source, total_quantity,
                available_quantity, created_by_user_id
            ) VALUES ($1, $2, $3, 'PURCHASE', 5, 5, $4)
            """,
            [
                (ASSET_ID, CATEGORY_ID, "Ticket Camera", BORROWER_ID),
                (OTHER_ASSET_ID, CATEGORY_ID, "Other Ticket Camera", BORROWER_ID),
            ],
        )
        await self.connection.execute(
            """
            INSERT INTO custodies (
                id, asset_id, custodian_user_id, assigned_by_user_id, status,
                on_hand_quantity, condition, assigned_at, confirmed_at,
                confirmed_by_user_id
            ) VALUES (
                $1, $2, $3, $4, 'ACTIVE', 5, 'GOOD',
                '2026-10-01T00:00:00Z', '2026-10-01T01:00:00Z', $3
            )
            """,
            CUSTODY_ID,
            ASSET_ID,
            CUSTODIAN_ID,
            BORROWER_ID,
        )

    async def asyncTearDown(self) -> None:
        """Roll back all schema fixtures and close the connection."""

        await self.transaction.rollback()
        await self.connection.close()

    async def test_valid_grouped_ticket_item_evidence_and_event_persist(self) -> None:
        """Canonical temporary Ticket history persists with explicit ownership IDs."""

        await self._insert_ticket("TEMPORARY", date(2026, 10, 15))
        await self._insert_item(ASSET_ID, quantity=2)
        await self.connection.execute(
            """
            INSERT INTO ticket_evidence (
                ticket_id, uploaded_by_user_id, evidence_type, object_key,
                content_type, content_sha256, size_bytes, note
            ) VALUES ($1, $2, 'PICKUP', 'ticket/private/object',
                      'image/webp', $3, 1024, 'Pickup proof')
            """,
            TICKET_ID,
            BORROWER_ID,
            "a" * 64,
        )
        await self.connection.execute(
            """
            INSERT INTO ticket_events (
                ticket_id, actor_user_id, event_type, from_status, to_status
            ) VALUES ($1, $2, 'CREATED', NULL, 'PENDING')
            """,
            TICKET_ID,
            BORROWER_ID,
        )

        row = await self.connection.fetchrow(
            """
            SELECT borrow_tickets.method, borrow_tickets.status, borrow_tickets.due_date,
                   ticket_items.asset_id, ticket_items.source_custody_id,
                   ticket_items.quantity
            FROM borrow_tickets
            JOIN ticket_items ON ticket_items.ticket_id = borrow_tickets.id
            WHERE borrow_tickets.id = $1
            """,
            TICKET_ID,
        )
        assert row is not None
        self.assertEqual(
            tuple(row),
            ("TEMPORARY", "PENDING", date(2026, 10, 15), ASSET_ID, CUSTODY_ID, 2),
        )

    async def test_due_date_policy_and_enum_values_are_enforced(self) -> None:
        """Temporary needs a due date, transfer forbids it, and enums reject unknown values."""

        invalid_rows = (
            ("TEMPORARY", None, "PENDING"),
            ("CUSTODY_TRANSFER", date(2026, 10, 15), "PENDING"),
        )
        for method, due_date, status in invalid_rows:
            with self.subTest(method=method), self.assertRaises(asyncpg.CheckViolationError):
                async with self.connection.transaction():
                    await self._insert_ticket(method, due_date, status)

        with self.assertRaises(asyncpg.InvalidTextRepresentationError):
            async with self.connection.transaction():
                await self._insert_ticket("UNKNOWN", None)
        with self.assertRaises(asyncpg.InvalidTextRepresentationError):
            async with self.connection.transaction():
                await self._insert_ticket("TEMPORARY", date(2026, 10, 15), "UNKNOWN")

    async def test_item_quantity_source_asset_and_uniqueness_are_enforced(self) -> None:
        """Items use positive quantities and a source Custody belonging to their Asset."""

        await self._insert_ticket("TEMPORARY", date(2026, 10, 15))
        for asset_id, quantity, error_type in (
            (ASSET_ID, 0, asyncpg.CheckViolationError),
            (OTHER_ASSET_ID, 1, asyncpg.ForeignKeyViolationError),
        ):
            with (
                self.subTest(asset_id=asset_id, quantity=quantity),
                self.assertRaises(error_type),
            ):
                async with self.connection.transaction():
                    await self._insert_item(asset_id, quantity)

        await self._insert_item(ASSET_ID, quantity=1)
        with self.assertRaises(asyncpg.UniqueViolationError):
            async with self.connection.transaction():
                await self._insert_item(ASSET_ID, quantity=1)

    async def test_evidence_policy_creation_event_and_history_deletion_are_protected(
        self,
    ) -> None:
        """Evidence is bounded, creation is explicit, and deletes do not cascade."""

        await self._insert_ticket("TEMPORARY", date(2026, 10, 15))
        await self._insert_item(ASSET_ID, quantity=1)

        with self.assertRaises(asyncpg.CheckViolationError):
            async with self.connection.transaction():
                await self.connection.execute(
                    """
                    INSERT INTO ticket_evidence (
                        ticket_id, uploaded_by_user_id, evidence_type, object_key,
                        content_type, content_sha256, size_bytes
                    ) VALUES ($1, $2, 'PICKUP', 'ticket/invalid',
                              'application/pdf', $3, 100)
                    """,
                    TICKET_ID,
                    BORROWER_ID,
                    "b" * 64,
                )
        with self.assertRaises(asyncpg.CheckViolationError):
            async with self.connection.transaction():
                await self.connection.execute(
                    """
                    INSERT INTO ticket_events (
                        ticket_id, actor_user_id, event_type, from_status, to_status
                    ) VALUES ($1, $2, 'NOT_CREATED', NULL, 'APPROVED')
                    """,
                    TICKET_ID,
                    BORROWER_ID,
                )
        with self.assertRaises(asyncpg.ForeignKeyViolationError):
            async with self.connection.transaction():
                await self.connection.execute(
                    "DELETE FROM borrow_tickets WHERE id = $1", TICKET_ID
                )

    async def test_required_access_indexes_and_non_cascading_foreign_keys_exist(self) -> None:
        """Borrower/custodian/status, Asset history, and child history paths are indexed."""

        rows = await self.connection.fetch(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename IN (
                  'borrow_tickets', 'ticket_items', 'ticket_evidence', 'ticket_events'
              )
            """
        )
        names = {str(row["indexname"]) for row in rows}
        self.assertTrue(
            {
                "ix_borrow_tickets_borrower_status",
                "ix_borrow_tickets_custodian_status",
                "ix_borrow_tickets_status_created",
                "ix_borrow_tickets_temporary_due",
                "ix_ticket_items_asset_history",
                "ix_ticket_items_source_custody",
                "ix_ticket_evidence_ticket_created",
                "ix_ticket_events_ticket_history",
                "ix_ticket_events_actor_history",
            }.issubset(names)
        )
        delete_actions = await self.connection.fetch(
            """
            SELECT constraint_name, delete_rule
            FROM information_schema.referential_constraints
            WHERE constraint_schema = 'public'
              AND constraint_name LIKE ANY(ARRAY['%ticket%', 'fk_ticket%'])
            """
        )
        self.assertTrue(delete_actions)
        self.assertEqual({str(row["delete_rule"]) for row in delete_actions}, {"NO ACTION"})

    async def _insert_ticket(
        self,
        method: str,
        due_date: date | None,
        status: str = "PENDING",
    ) -> None:
        await self.connection.execute(
            """
            INSERT INTO borrow_tickets (
                id, borrower_user_id, source_custodian_user_id, method, status, due_date
            ) VALUES ($1, $2, $3, $4, $5, $6)
            """,
            TICKET_ID,
            BORROWER_ID,
            CUSTODIAN_ID,
            method,
            status,
            due_date,
        )

    async def _insert_item(self, asset_id: UUID, quantity: int) -> None:
        await self.connection.execute(
            """
            INSERT INTO ticket_items (
                ticket_id, asset_id, source_custody_id, quantity
            ) VALUES ($1, $2, $3, $4)
            """,
            TICKET_ID,
            asset_id,
            CUSTODY_ID,
            quantity,
        )
