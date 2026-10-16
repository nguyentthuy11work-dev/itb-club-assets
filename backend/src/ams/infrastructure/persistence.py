"""SQLAlchemy persistence adapters for identity and refresh sessions."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ams.application.authentication import IRefreshSessionRepository, RefreshSession
from ams.application.member_roles import IMemberRoleRepository, MemberSummary
from ams.domain.authentication import LocalCredential, UserIdentity
from ams.domain.identity import OperationalRole
from ams.domain.repositories import IUserRepository


class SqlAlchemyUserRepository(IUserRepository):
    """Read current identity state from the SQL-first PostgreSQL schema."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_local_credential(self, login: str) -> LocalCredential | None:
        """Load one unambiguous case-insensitive username/email credential."""

        result = await self._session.execute(
            text(
                """
                SELECT id, username, email, display_name, password_hash, is_active
                FROM users
                WHERE lower(username) = lower(:login)
                   OR lower(email) = lower(:login)
                LIMIT 2
                """
            ),
            {"login": login},
        )
        rows = result.mappings().all()
        if len(rows) != 1:
            return None
        row = rows[0]
        user_id = UUID(str(row["id"]))
        identity = await self._map_identity(
            user_id=user_id,
            username=str(row["username"]),
            email=str(row["email"]),
            display_name=str(row["display_name"]),
            is_active=bool(row["is_active"]),
        )
        return LocalCredential(user=identity, password_hash=str(row["password_hash"]))

    async def get_identity(self, user_id: UUID) -> UserIdentity | None:
        """Load a user with current active roles and team memberships."""

        result = await self._session.execute(
            text(
                """
                SELECT id, username, email, display_name, is_active
                FROM users
                WHERE id = :user_id
                """
            ),
            {"user_id": user_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return await self._map_identity(
            user_id=UUID(str(row["id"])),
            username=str(row["username"]),
            email=str(row["email"]),
            display_name=str(row["display_name"]),
            is_active=bool(row["is_active"]),
        )

    async def _map_identity(
        self,
        user_id: UUID,
        username: str,
        email: str,
        display_name: str,
        is_active: bool,
    ) -> UserIdentity:
        roles_result = await self._session.execute(
            text(
                """
                SELECT roles.code
                FROM user_roles
                JOIN roles ON roles.id = user_roles.role_id
                WHERE user_roles.user_id = :user_id
                  AND user_roles.revoked_at IS NULL
                ORDER BY roles.code
                """
            ),
            {"user_id": user_id},
        )
        team_result = await self._session.execute(
            text(
                """
                SELECT team_members.team_id
                FROM team_members
                JOIN teams ON teams.id = team_members.team_id
                WHERE team_members.user_id = :user_id
                  AND team_members.left_at IS NULL
                  AND teams.is_active
                ORDER BY team_members.team_id
                """
            ),
            {"user_id": user_id},
        )
        roles = frozenset(OperationalRole(str(code)) for code in roles_result.scalars())
        team_ids = frozenset(UUID(str(team_id)) for team_id in team_result.scalars())
        return UserIdentity(
            id=user_id,
            username=username,
            email=email,
            display_name=display_name,
            roles=roles,
            team_ids=team_ids,
            is_active=is_active,
        )


class SqlAlchemyMemberRoleRepository(IMemberRoleRepository):
    """Read members and replace roles as append-only ``user_roles`` history."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_members(self, *, offset: int, limit: int) -> tuple[list[MemberSummary], int]:
        """Return members with active roles, ordered by display name."""

        total = (await self._session.execute(text("SELECT count(*) FROM users"))).scalar_one()
        result = await self._session.execute(
            text(
                """
                SELECT users.id, users.username, users.email, users.display_name,
                       users.is_active,
                       array_remove(array_agg(roles.code ORDER BY roles.code), NULL) AS roles
                FROM users
                LEFT JOIN user_roles
                       ON user_roles.user_id = users.id AND user_roles.revoked_at IS NULL
                LEFT JOIN roles ON roles.id = user_roles.role_id
                GROUP BY users.id
                ORDER BY lower(users.display_name), users.id
                OFFSET :offset LIMIT :limit
                """
            ),
            {"offset": offset, "limit": limit},
        )
        items = [
            MemberSummary(
                id=UUID(str(row["id"])),
                username=str(row["username"]),
                email=str(row["email"]),
                display_name=str(row["display_name"]),
                roles=frozenset(OperationalRole(str(code)) for code in row["roles"]),
                is_active=bool(row["is_active"]),
            )
            for row in result.mappings()
        ]
        return items, int(total)

    async def get_for_update(self, member_id: UUID) -> MemberSummary | None:
        """Lock the member row, then load current roles."""

        locked = await self._session.execute(
            text("SELECT id FROM users WHERE id = :member_id FOR UPDATE"),
            {"member_id": member_id},
        )
        if locked.scalar_one_or_none() is None:
            return None
        identity = await SqlAlchemyUserRepository(self._session).get_identity(member_id)
        assert identity is not None
        return MemberSummary(
            id=identity.id,
            username=identity.username,
            email=identity.email,
            display_name=identity.display_name,
            roles=identity.roles,
            is_active=identity.is_active,
        )

    async def replace_roles(
        self, member_id: UUID, roles: frozenset[OperationalRole], granted_by: UUID
    ) -> None:
        """Close removed assignments and insert new ones; never delete history."""

        codes = sorted(str(role) for role in roles)
        await self._session.execute(
            text(
                """
                UPDATE user_roles
                SET revoked_at = CURRENT_TIMESTAMP, revoked_by_user_id = :actor_id
                WHERE user_id = :member_id
                  AND revoked_at IS NULL
                  AND role_id IN (
                      SELECT id FROM roles WHERE NOT (code = ANY(CAST(:codes AS varchar[])))
                  )
                """
            ),
            {"member_id": member_id, "actor_id": granted_by, "codes": codes},
        )
        await self._session.execute(
            text(
                """
                INSERT INTO user_roles (user_id, role_id, granted_by_user_id)
                SELECT :member_id, roles.id, :actor_id
                FROM roles
                WHERE roles.code = ANY(CAST(:codes AS varchar[]))
                  AND NOT EXISTS (
                      SELECT 1 FROM user_roles
                      WHERE user_roles.user_id = :member_id
                        AND user_roles.role_id = roles.id
                        AND user_roles.revoked_at IS NULL
                  )
                """
            ),
            {"member_id": member_id, "actor_id": granted_by, "codes": codes},
        )


class SqlAlchemyRefreshSessionRepository(IRefreshSessionRepository):
    """Persist digest-only refresh sessions without committing transactions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, session: RefreshSession) -> None:
        """Insert a newly issued refresh session."""

        await self._insert(session)

    async def is_active(self, session_id: UUID, token_digest: str, now: datetime) -> bool:
        """Check whether an exact refresh session remains active and unexpired."""

        result = await self._session.execute(
            text(
                """
                SELECT 1
                FROM refresh_sessions
                WHERE id = :session_id
                  AND token_digest = :token_digest
                  AND revoked_at IS NULL
                  AND expires_at > :now
                """
            ),
            {
                "session_id": session_id,
                "token_digest": token_digest,
                "now": now,
            },
        )
        return result.scalar_one_or_none() is not None

    async def rotate(
        self,
        current_session_id: UUID,
        current_token_digest: str,
        replacement: RefreshSession,
        now: datetime,
    ) -> bool:
        """Atomically consume one active session and insert its replacement."""

        result = await self._session.execute(
            text(
                """
                UPDATE refresh_sessions
                SET revoked_at = :now
                WHERE id = :session_id
                  AND token_digest = :token_digest
                  AND revoked_at IS NULL
                  AND expires_at > :now
                RETURNING id
                """
            ),
            {
                "session_id": current_session_id,
                "token_digest": current_token_digest,
                "now": now,
            },
        )
        if result.scalar_one_or_none() is None:
            return False
        await self._insert(replacement)
        return True

    async def revoke(self, session_id: UUID, token_digest: str, now: datetime) -> bool:
        """Revoke one exact active refresh session once."""

        result = await self._session.execute(
            text(
                """
                UPDATE refresh_sessions
                SET revoked_at = :now
                WHERE id = :session_id
                  AND token_digest = :token_digest
                  AND revoked_at IS NULL
                  AND expires_at > :now
                RETURNING id
                """
            ),
            {
                "session_id": session_id,
                "token_digest": token_digest,
                "now": now,
            },
        )
        return result.scalar_one_or_none() is not None

    async def _insert(self, session: RefreshSession) -> None:
        await self._session.execute(
            text(
                """
                INSERT INTO refresh_sessions (
                    id, user_id, token_digest, expires_at, created_at
                ) VALUES (
                    :id, :user_id, :token_digest, :expires_at, :created_at
                )
                """
            ),
            {
                "id": session.id,
                "user_id": session.user_id,
                "token_digest": session.token_digest,
                "expires_at": session.expires_at,
                "created_at": session.created_at,
            },
        )
