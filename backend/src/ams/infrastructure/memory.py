"""Explicitly injected in-memory adapters for unit tests and local development."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from ams.application.authentication import IRefreshSessionRepository, RefreshSession
from ams.domain.authentication import LocalCredential, UserIdentity
from ams.domain.repositories import IUserRepository


class InMemoryUserRepository(IUserRepository):
    """Mutable, injected user repository for tests and local development only."""

    def __init__(self, credentials: tuple[LocalCredential, ...] = ()) -> None:
        self._credentials: dict[UUID, LocalCredential] = {}
        self._login_index: dict[str, UUID] = {}
        for credential in credentials:
            self.upsert(credential)

    def upsert(self, credential: LocalCredential) -> None:
        """Add or replace one fixture identity and its login aliases."""

        previous = self._credentials.get(credential.user.id)
        if previous is not None:
            self._login_index.pop(previous.user.username.casefold(), None)
            self._login_index.pop(previous.user.email.casefold(), None)
        aliases = (credential.user.username.casefold(), credential.user.email.casefold())
        for alias in aliases:
            owner_id = self._login_index.get(alias)
            if owner_id is not None and owner_id != credential.user.id:
                raise ValueError("Username/email alias is already assigned.")
        self._credentials[credential.user.id] = credential
        for alias in aliases:
            self._login_index[alias] = credential.user.id

    async def get_local_credential(self, login: str) -> LocalCredential | None:
        """Find a fixture credential by case-insensitive username or email."""

        user_id = self._login_index.get(login.casefold())
        return self._credentials.get(user_id) if user_id is not None else None

    async def get_identity(self, user_id: UUID) -> UserIdentity | None:
        """Return current fixture roles, teams, and active status."""

        credential = self._credentials.get(user_id)
        return credential.user if credential is not None else None


@dataclass(slots=True)
class _StoredRefreshSession:
    session: RefreshSession
    revoked_at: datetime | None = None


class InMemoryRefreshSessionRepository(IRefreshSessionRepository):
    """Concurrency-safe non-production refresh-session store with no global state."""

    def __init__(self) -> None:
        self._records: dict[UUID, _StoredRefreshSession] = {}
        self._lock = asyncio.Lock()

    async def add(self, session: RefreshSession) -> None:
        """Store a session and reject duplicate identifiers."""

        async with self._lock:
            if session.id in self._records:
                raise ValueError("Refresh session identifier already exists.")
            self._records[session.id] = _StoredRefreshSession(session)

    async def is_active(self, session_id: UUID, token_digest: str, now: datetime) -> bool:
        """Check exact digest, revocation state, and expiration."""

        async with self._lock:
            record = self._records.get(session_id)
            return self._matches_active(record, token_digest, now)

    async def rotate(
        self,
        current_session_id: UUID,
        current_token_digest: str,
        replacement: RefreshSession,
        now: datetime,
    ) -> bool:
        """Consume one session and add one replacement under a single lock."""

        async with self._lock:
            current = self._records.get(current_session_id)
            if not self._matches_active(current, current_token_digest, now):
                return False
            if replacement.id in self._records:
                raise ValueError("Refresh session identifier already exists.")
            assert current is not None
            current.revoked_at = now
            self._records[replacement.id] = _StoredRefreshSession(replacement)
            return True

    async def revoke(self, session_id: UUID, token_digest: str, now: datetime) -> bool:
        """Revoke one exact active session once."""

        async with self._lock:
            record = self._records.get(session_id)
            if not self._matches_active(record, token_digest, now):
                return False
            assert record is not None
            record.revoked_at = now
            return True

    @staticmethod
    def _matches_active(
        record: _StoredRefreshSession | None, token_digest: str, now: datetime
    ) -> bool:
        return bool(
            record is not None
            and record.revoked_at is None
            and record.session.token_digest == token_digest
            and record.session.expires_at > now
        )
