"""Use-case-specific domain repository contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from uuid import UUID

from ams.domain.assets import Asset, Custody, CustodyEvidence
from ams.domain.authentication import LocalCredential, UserIdentity


class IUserRepository(ABC):
    """Load local credentials and current effective identity state."""

    @abstractmethod
    async def get_local_credential(self, login: str) -> LocalCredential | None:
        """Return a credential by case-insensitive username/email, or ``None``."""

    @abstractmethod
    async def get_identity(self, user_id: UUID) -> UserIdentity | None:
        """Return a user's current roles, teams, and active state, or ``None``."""


class IAssetRepository(ABC):
    """Persist and lock club-wide Asset aggregate state."""

    @abstractmethod
    async def get_by_id(self, asset_id: UUID) -> Asset | None:
        """Return an Asset without acquiring a write lock."""

    @abstractmethod
    async def get_for_update(self, asset_id: UUID) -> Asset | None:
        """Return an Asset under a PostgreSQL row lock until transaction end."""

    @abstractmethod
    async def add(self, asset: Asset) -> None:
        """Stage a new Asset without committing the transaction."""

    @abstractmethod
    async def save(self, asset: Asset) -> None:
        """Stage changes to an existing Asset without committing."""

    @abstractmethod
    async def active_category_exists(self, category_id: UUID) -> bool:
        """Return whether an Asset category exists and is active."""

    @abstractmethod
    async def active_team_exists(self, team_id: UUID) -> bool:
        """Return whether a responsible Team exists and is active."""


class ICustodyRepository(ABC):
    """Persist and lock physical Custody aggregate state."""

    @abstractmethod
    async def get_by_id(self, custody_id: UUID) -> Custody | None:
        """Return a Custody with its evidence references."""

    @abstractmethod
    async def get_for_update(self, custody_id: UUID) -> Custody | None:
        """Return a Custody under a PostgreSQL row lock until transaction end."""

    @abstractmethod
    async def get_many_for_update(self, custody_ids: tuple[UUID, ...]) -> tuple[Custody, ...]:
        """Lock and return unique Custodies in ascending UUID order."""

    @abstractmethod
    async def get_current_for_update(
        self, asset_id: UUID, custodian_user_id: UUID
    ) -> Custody | None:
        """Lock one actor's pending or active Custody for an Asset, if present."""

    @abstractmethod
    async def add(self, custody: Custody) -> None:
        """Stage a new Custody without committing the transaction."""

    @abstractmethod
    async def save(self, custody: Custody) -> None:
        """Stage changes to an existing Custody without committing."""


class ICustodyEvidenceRepository(ABC):
    """Persist private evidence metadata under its Custody owner."""

    @abstractmethod
    async def add(self, evidence: CustodyEvidence) -> None:
        """Stage one evidence metadata row without committing."""
