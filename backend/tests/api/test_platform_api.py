"""API tests for the F01.05 operational and error-handling baseline."""

from __future__ import annotations

import unittest
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import cast
from uuid import UUID

from httpx import ASGITransport, AsyncClient, Response

from ams.application.authentication import SessionService
from ams.application.health import IReadinessProbe, ServiceUnavailableError
from ams.application.idempotency import IdempotencyKeyReusedError
from ams.interface.api.app import create_app


class UnusedSessionServiceFactory:
    """Satisfy composition without creating authentication infrastructure."""

    def __call__(self) -> AbstractAsyncContextManager[SessionService]:
        """Return a context that fails if an auth route unexpectedly consumes it."""

        return self._context()

    @asynccontextmanager
    async def _context(self) -> AsyncIterator[SessionService]:
        yield cast(SessionService, object())


class FailingReadinessProbe(IReadinessProbe):
    """Deterministic unavailable dependency for API tests."""

    async def check(self) -> None:
        """Report a sanitized dependency failure."""

        raise ServiceUnavailableError("postgresql password=secret table=users")


class PlatformApiTests(unittest.IsolatedAsyncioTestCase):
    """Verify inputs, outputs, processing, and information-safe failures."""

    async def asyncSetUp(self) -> None:
        """Create an isolated application with one explicitly allowed browser origin."""

        self.application = create_app(
            UnusedSessionServiceFactory(),
            cors_allowed_origins=("https://frontend.example.test",),
        )

        @self.application.get("/api/v1/test/failure")
        async def unexpected_failure() -> None:
            """Raise a test-only implementation failure."""

            raise RuntimeError("password=secret SELECT * FROM users")

        @self.application.post("/api/v1/test/idempotency-conflict")
        async def idempotency_conflict() -> None:
            """Raise a test-only idempotency key reuse error."""

            raise IdempotencyKeyReusedError(
                "The idempotency key was already used for another request payload."
            )

        self.client = AsyncClient(
            transport=ASGITransport(app=self.application, raise_app_exceptions=False),
            base_url="https://testserver",
        )

    async def asyncTearDown(self) -> None:
        """Close the asynchronous client."""

        await self.client.aclose()

    async def test_health_and_readiness_return_distinct_success_states(self) -> None:
        """Liveness and readiness expose minimal successful output contracts."""

        health = await self.client.get("/health")
        ready = await self.client.get("/ready")

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"status": "UP"})
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json(), {"status": "READY"})
        self._assert_trace(health)
        self._assert_trace(ready)

    async def test_readiness_failure_is_sanitized_and_liveness_stays_up(self) -> None:
        """A dependency outage returns 503 without turning liveness into a DB check."""

        self.application.state.readiness_probe = FailingReadinessProbe()

        health = await self.client.get("/health")
        ready = await self.client.get("/ready")

        self.assertEqual(health.status_code, 200)
        self._assert_error(ready, 503, "SERVICE_UNAVAILABLE")
        self.assertNotIn("password", ready.text)
        self.assertNotIn("users", ready.text)

    async def test_validation_and_unknown_routes_use_standard_error_envelope(self) -> None:
        """Framework-generated 422 and 404 responses follow the AMS contract."""

        validation = await self.client.post(
            "/api/v1/auth/login",
            json={"username": "member", "password": "value", "unexpected": True},
        )
        missing = await self.client.get("/api/v1/does-not-exist")

        self._assert_error(validation, 422, "VALIDATION_ERROR")
        self.assertEqual(
            validation.json()["error"]["details"]["issues"][0]["location"],
            ["body", "unexpected"],
        )
        self._assert_error(missing, 404, "RESOURCE_NOT_FOUND")

    async def test_unexpected_failure_never_exposes_implementation_details(self) -> None:
        """Unhandled exceptions produce a stable generic 500 response."""

        response = await self.client.get("/api/v1/test/failure")

        self._assert_error(response, 500, "INTERNAL_SERVER_ERROR")
        self.assertEqual(response.json()["error"]["message"], "An unexpected error occurred.")
        for secret in ("password", "SELECT", "users", "RuntimeError"):
            self.assertNotIn(secret, response.text)

    async def test_idempotency_key_reuse_returns_standard_conflict(self) -> None:
        """Changed payload reuse maps to the stable API 409 error envelope."""

        response = await self.client.post("/api/v1/test/idempotency-conflict")

        self._assert_error(response, 409, "IDEMPOTENCY_KEY_REUSED")

    async def test_cors_allows_only_configured_credentialed_origin(self) -> None:
        """Preflight accepts explicit origins/headers but never grants an unknown origin."""

        allowed = await self.client.options(
            "/api/v1/auth/login",
            headers={
                "Origin": "https://frontend.example.test",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Authorization,Idempotency-Key",
            },
        )
        denied = await self.client.options(
            "/api/v1/auth/login",
            headers={
                "Origin": "https://attacker.example.test",
                "Access-Control-Request-Method": "POST",
            },
        )

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(
            allowed.headers["access-control-allow-origin"],
            "https://frontend.example.test",
        )
        self.assertEqual(allowed.headers["access-control-allow-credentials"], "true")
        self.assertIn(
            "idempotency-key", allowed.headers["access-control-allow-headers"].lower()
        )
        self.assertNotIn("access-control-allow-origin", denied.headers)

    def _assert_error(self, response: Response, status: int, code: str) -> None:
        self.assertEqual(response.status_code, status)
        self.assertEqual(response.json()["error"]["code"], code)
        self._assert_trace(response)

    def _assert_trace(self, response: Response) -> None:
        trace_id = response.headers["x-trace-id"]
        UUID(trace_id)
        if response.status_code >= 400:
            self.assertEqual(response.json()["traceId"], trace_id)
