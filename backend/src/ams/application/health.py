"""Application contracts for operational dependency health checks."""

from __future__ import annotations

from abc import ABC, abstractmethod


class ServiceUnavailableError(Exception):
    """Raised when a required runtime dependency is unavailable."""


class IReadinessProbe(ABC):
    """Check whether dependencies required to serve traffic are available."""

    @abstractmethod
    async def check(self) -> None:
        """Raise ``ServiceUnavailableError`` when the application is not ready."""
