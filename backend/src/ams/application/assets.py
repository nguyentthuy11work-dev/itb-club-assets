"""Application transaction contracts for Asset and Custody use cases."""

from __future__ import annotations

from abc import ABC, abstractmethod
from types import TracebackType

from ams.domain.repositories import IAssetRepository, ICustodyRepository


class AssetPersistenceError(Exception):
    """Stable failure raised after infrastructure persistence errors are sanitized."""

    code = "ASSET_PERSISTENCE_FAILED"


class IAssetUnitOfWork(ABC):
    """Own one atomic transaction spanning Asset and Custody repositories."""

    assets: IAssetRepository
    custodies: ICustodyRepository

    @abstractmethod
    async def __aenter__(self) -> IAssetUnitOfWork:
        """Open the transaction and initialize its repositories."""

    @abstractmethod
    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Roll back uncommitted or failed work and close transaction resources."""

    @abstractmethod
    async def commit(self) -> None:
        """Atomically commit every staged aggregate change."""

    @abstractmethod
    async def rollback(self) -> None:
        """Discard every staged aggregate change."""
