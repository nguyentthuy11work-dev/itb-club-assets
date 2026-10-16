"""PostgreSQL tests for member listing and audited role replacement."""

from __future__ import annotations

import os
import unittest
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ams.domain.identity import OperationalRole
from ams.infrastructure.persistence import SqlAlchemyMemberRoleRepository

ADMIN_ID = UUID("00000000-0000-4000-8000-00000000a001")
MEMBER_ID = UUID("00000000-0000-4000-8000-00000000a002")


@unittest.skipUnless(os.getenv("AMS_DATABASE_URL"), "AMS_DATABASE_URL is required")
class MemberRoleRepositoryTests(unittest.IsolatedAsyncioTestCase):
    """Role replacement closes assignments instead of deleting history."""

    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine(os.environ["AMS_DATABASE_URL"], pool_pre_ping=True)
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)
        await self._cleanup()
        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO users (id, username, email, display_name, password_hash)
                    VALUES
                        (:admin, 'roles.admin', 'roles.admin@example.test',
                         'Roles Admin', 'h'),
                        (:member, 'roles.member', 'roles.member@example.test',
                         'Roles Member', 'h')
                    """
                ),
                {"admin": ADMIN_ID, "member": MEMBER_ID},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO user_roles (user_id, role_id, granted_by_user_id)
                    SELECT :member, id, :admin FROM roles WHERE code = 'SHIFT_LEAD'
                    """
                ),
                {"member": MEMBER_ID, "admin": ADMIN_ID},
            )

    async def asyncTearDown(self) -> None:
        await self._cleanup()
        await self.engine.dispose()

    async def _cleanup(self) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM user_roles WHERE user_id IN (:a, :b)"),
                {"a": ADMIN_ID, "b": MEMBER_ID},
            )
            await connection.execute(
                text("DELETE FROM users WHERE id IN (:a, :b)"), {"a": ADMIN_ID, "b": MEMBER_ID}
            )

    async def test_replace_roles_keeps_history_and_lists_current_roles(self) -> None:
        async with self.maker() as session:
            repository = SqlAlchemyMemberRoleRepository(session)
            member = await repository.get_for_update(MEMBER_ID)
            assert member is not None
            self.assertEqual(member.roles, frozenset({OperationalRole.SHIFT_LEAD}))
            await repository.replace_roles(
                MEMBER_ID, frozenset({OperationalRole.HR, OperationalRole.TEAM_HEAD}), ADMIN_ID
            )
            await session.commit()

        async with self.maker() as session:
            repository = SqlAlchemyMemberRoleRepository(session)
            items, total = await repository.list_members(offset=0, limit=100)
            listed = {item.id: item for item in items}
            history = await session.execute(
                text(
                    """
                    SELECT roles.code, user_roles.revoked_by_user_id
                    FROM user_roles JOIN roles ON roles.id = user_roles.role_id
                    WHERE user_roles.user_id = :member
                    ORDER BY roles.code
                    """
                ),
                {"member": MEMBER_ID},
            )

        self.assertGreaterEqual(total, 2)
        self.assertEqual(
            listed[MEMBER_ID].roles,
            frozenset({OperationalRole.HR, OperationalRole.TEAM_HEAD}),
        )
        self.assertEqual(listed[ADMIN_ID].roles, frozenset())
        self.assertEqual(
            [tuple(row) for row in history],
            [("HR", None), ("SHIFT_LEAD", ADMIN_ID), ("TEAM_HEAD", None)],
        )

    async def test_unknown_member_is_none(self) -> None:
        async with self.maker() as session:
            missing = await SqlAlchemyMemberRoleRepository(session).get_for_update(
                UUID("00000000-0000-4000-8000-00000000a099")
            )

        self.assertIsNone(missing)
