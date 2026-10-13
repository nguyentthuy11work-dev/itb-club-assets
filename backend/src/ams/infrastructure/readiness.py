"""Readiness probe adapters for runtime dependencies."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from ams.application.health import IReadinessProbe, ServiceUnavailableError


class PostgreSqlReadinessProbe(IReadinessProbe):
    """Verify that PostgreSQL accepts a minimal query."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def check(self) -> None:
        """Execute a side-effect-free query and hide driver details on failure."""

        try:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except SQLAlchemyError as exception:
            raise ServiceUnavailableError(
                "A required service is temporarily unavailable."
            ) from exception


class AlwaysReadyProbe(IReadinessProbe):
    """Readiness adapter for isolated tests with no external dependencies."""

    async def check(self) -> None:
        """Report ready without performing external I/O."""
