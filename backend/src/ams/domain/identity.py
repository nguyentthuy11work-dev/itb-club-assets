"""Identity and RBAC domain policy independent of HTTP or persistence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class AuthorizationError(Exception):
    """Raised when an authenticated actor is not allowed to perform an action."""


class OperationalRole(StrEnum):
    """Roles that may be assigned through the AMS role-administration surface."""

    SYSTEM_ADMIN = "SYSTEM_ADMIN"
    HR = "HR"
    TEAM_LEAD_ASSET = "TEAM_LEAD_ASSET"
    SHIFT_LEAD = "SHIFT_LEAD"
    TEAM_HEAD = "TEAM_HEAD"


ELEVATED_ROLES = frozenset({OperationalRole.SYSTEM_ADMIN, OperationalRole.HR})
NON_ELEVATED_ROLES = frozenset(set(OperationalRole) - ELEVATED_ROLES)


@dataclass(frozen=True, slots=True)
class CurrentActor:
    """The authenticated actor used by application authorization boundaries."""

    id: UUID
    roles: frozenset[OperationalRole]
    team_ids: frozenset[UUID] = frozenset()
    is_active: bool = True
    display_name: str = ""

    def has_role(self, role: OperationalRole) -> bool:
        """Return whether this actor currently holds an effective role."""

        return role in self.roles


class RoleAdministrationPolicy:
    """Enforce approved role-management authority and anti-escalation rules."""

    def assert_can_replace_roles(
        self,
        actor: CurrentActor,
        target_user_id: UUID,
        current_roles: frozenset[OperationalRole],
        desired_roles: frozenset[OperationalRole],
    ) -> None:
        """Allow only approved role replacements or raise ``AuthorizationError``.

        System administrators may manage other users' roles. HR may manage only
        non-elevated roles on a user that has no elevated role. No actor may
        modify their own roles; Custodian is deliberately absent from the enum
        because custody, rather than an RBAC assignment, determines it.
        """

        self._assert_active(actor)
        self._assert_not_self(actor, target_user_id)

        if actor.has_role(OperationalRole.SYSTEM_ADMIN):
            return

        if actor.has_role(OperationalRole.HR):
            if current_roles & ELEVATED_ROLES or desired_roles & ELEVATED_ROLES:
                raise AuthorizationError("HR cannot alter elevated role assignments.")
            if desired_roles <= NON_ELEVATED_ROLES:
                return

        raise AuthorizationError("Actor cannot manage roles for this user.")

    @staticmethod
    def _assert_active(actor: CurrentActor) -> None:
        """Reject inactive accounts before role checks."""

        if not actor.is_active:
            raise AuthorizationError("Inactive accounts cannot manage roles.")

    @staticmethod
    def _assert_not_self(actor: CurrentActor, target_user_id: UUID) -> None:
        """Reject any self-role edit, including removal by a system administrator."""

        if actor.id == target_user_id:
            raise AuthorizationError("Actors cannot alter their own roles.")
