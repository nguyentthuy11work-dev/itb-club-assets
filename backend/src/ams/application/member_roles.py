"""Member listing and RBAC role replacement for HR and system administrators."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from ams.domain.identity import (
    ELEVATED_ROLES,
    AuthorizationError,
    CurrentActor,
    OperationalRole,
    RoleAdministrationPolicy,
)


class MemberNotFoundError(Exception):
    """Target member of a role change does not exist."""

    code = "MEMBER_NOT_FOUND"


class RoleAssignmentForbiddenError(AuthorizationError):
    """Role change rejected by the anti-escalation policy."""

    code = "ROLE_ASSIGNMENT_FORBIDDEN"


@dataclass(frozen=True, slots=True)
class MemberSummary:
    """Member identity and effective roles, without credential material."""

    id: UUID
    username: str
    email: str
    display_name: str
    roles: frozenset[OperationalRole]
    is_active: bool


@dataclass(frozen=True, slots=True)
class MemberPage:
    """One page of members ordered by display name."""

    items: list[MemberSummary]
    page: int
    page_size: int
    total: int


class IMemberRoleRepository(Protocol):
    """Persistence port for member reads and audited role replacement."""

    async def list_members(self, *, offset: int, limit: int) -> tuple[list[MemberSummary], int]:
        """Return one page of members and the total member count."""

    async def get_for_update(self, member_id: UUID) -> MemberSummary | None:
        """Load and lock one member so concurrent role edits serialize."""

    async def replace_roles(
        self, member_id: UUID, roles: frozenset[OperationalRole], granted_by: UUID
    ) -> None:
        """Revoke assignments outside ``roles`` and grant the missing ones."""


class MemberRoleService:
    """Apply ``RoleAdministrationPolicy`` to member role administration."""

    def __init__(self, repository: IMemberRoleRepository) -> None:
        self._repository = repository
        self._policy = RoleAdministrationPolicy()

    async def list_members(
        self, actor: CurrentActor, *, page: int, page_size: int
    ) -> MemberPage:
        """List members for an active HR or system administrator."""

        if not actor.is_active or actor.roles.isdisjoint(ELEVATED_ROLES):
            raise AuthorizationError("HR or system administrator role is required.")
        items, total = await self._repository.list_members(
            offset=(page - 1) * page_size, limit=page_size
        )
        return MemberPage(items, page, page_size, total)

    async def replace_roles(
        self, actor: CurrentActor, member_id: UUID, roles: frozenset[OperationalRole]
    ) -> MemberSummary:
        """Replace a member's full role set when the policy allows it."""

        member = await self._repository.get_for_update(member_id)
        if member is None:
            raise MemberNotFoundError("Member is not available.")
        try:
            self._policy.assert_can_replace_roles(actor, member_id, member.roles, roles)
        except AuthorizationError as error:
            raise RoleAssignmentForbiddenError(str(error)) from error
        await self._repository.replace_roles(member_id, roles, actor.id)
        return MemberSummary(
            id=member.id,
            username=member.username,
            email=member.email,
            display_name=member.display_name,
            roles=roles,
            is_active=member.is_active,
        )
