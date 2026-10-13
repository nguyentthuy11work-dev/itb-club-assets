"""Unit tests for the identity/RBAC policy."""

from __future__ import annotations

import unittest
from uuid import UUID

from ams.domain.identity import (
    AuthorizationError,
    CurrentActor,
    OperationalRole,
    RoleAdministrationPolicy,
)

ADMIN_ID = UUID("00000000-0000-4000-8000-000000000001")
HR_ID = UUID("00000000-0000-4000-8000-000000000002")
USER_ID = UUID("00000000-0000-4000-8000-000000000003")


class RoleAdministrationPolicyTests(unittest.TestCase):
    """Exercise every privileged role-management allow and deny path."""

    def setUp(self) -> None:
        """Create the policy under test."""

        self.policy = RoleAdministrationPolicy()

    def test_system_admin_can_assign_hr_to_another_user(self) -> None:
        """Only System Admin may assign an elevated HR role."""

        actor = CurrentActor(ADMIN_ID, frozenset({OperationalRole.SYSTEM_ADMIN}))
        self.policy.assert_can_replace_roles(
            actor, USER_ID, frozenset(), frozenset({OperationalRole.HR})
        )

    def test_system_admin_cannot_change_own_roles(self) -> None:
        """System Admin is also subject to the self-edit prohibition."""

        actor = CurrentActor(ADMIN_ID, frozenset({OperationalRole.SYSTEM_ADMIN}))
        with self.assertRaises(AuthorizationError):
            self.policy.assert_can_replace_roles(
                actor, ADMIN_ID, frozenset({OperationalRole.SYSTEM_ADMIN}), frozenset()
            )

    def test_hr_can_assign_non_elevated_role_to_other_user(self) -> None:
        """HR may manage a normal operational role on an ordinary user."""

        actor = CurrentActor(HR_ID, frozenset({OperationalRole.HR}))
        self.policy.assert_can_replace_roles(
            actor, USER_ID, frozenset(), frozenset({OperationalRole.TEAM_HEAD})
        )

    def test_hr_cannot_assign_elevated_role(self) -> None:
        """HR cannot grant HR or System Admin."""

        actor = CurrentActor(HR_ID, frozenset({OperationalRole.HR}))
        with self.assertRaises(AuthorizationError):
            self.policy.assert_can_replace_roles(
                actor, USER_ID, frozenset(), frozenset({OperationalRole.SYSTEM_ADMIN})
            )

    def test_hr_cannot_modify_user_with_existing_elevated_role(self) -> None:
        """HR cannot use a non-elevated replacement to modify an elevated account."""

        actor = CurrentActor(HR_ID, frozenset({OperationalRole.HR}))
        with self.assertRaises(AuthorizationError):
            self.policy.assert_can_replace_roles(
                actor,
                USER_ID,
                frozenset({OperationalRole.HR}),
                frozenset({OperationalRole.TEAM_HEAD}),
            )

    def test_hr_cannot_change_own_roles(self) -> None:
        """HR self-escalation and self-removal are both forbidden."""

        actor = CurrentActor(HR_ID, frozenset({OperationalRole.HR}))
        with self.assertRaises(AuthorizationError):
            self.policy.assert_can_replace_roles(
                actor, HR_ID, frozenset({OperationalRole.HR}), frozenset()
            )

    def test_unprivileged_user_cannot_manage_roles(self) -> None:
        """An ordinary authenticated user has no role-administration authority."""

        actor = CurrentActor(USER_ID, frozenset())
        with self.assertRaises(AuthorizationError):
            self.policy.assert_can_replace_roles(
                actor, HR_ID, frozenset({OperationalRole.HR}), frozenset()
            )

    def test_inactive_privileged_actor_is_denied(self) -> None:
        """Inactive status wins over any role assignment."""

        actor = CurrentActor(
            ADMIN_ID, frozenset({OperationalRole.SYSTEM_ADMIN}), is_active=False
        )
        with self.assertRaises(AuthorizationError):
            self.policy.assert_can_replace_roles(actor, USER_ID, frozenset(), frozenset())

    def test_custodian_cannot_be_forged_as_an_assignable_role(self) -> None:
        """Custodian is custody-derived and deliberately absent from RBAC roles."""

        with self.assertRaises(ValueError):
            OperationalRole("CUSTODIAN")
