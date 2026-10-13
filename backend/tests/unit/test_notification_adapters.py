"""Unit tests for clock, metrics, and provider email infrastructure adapters."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from uuid import UUID

from ams.application.notifications import EmailMessage
from ams.infrastructure.notifications import (
    IEmailProviderClient,
    InMemoryWorkerMetrics,
    ProviderEmailSender,
    SystemClock,
)

RECIPIENT_ID = UUID("00000000-0000-4000-8000-000000001201")


class FakeEmailProviderClient(IEmailProviderClient):
    """Capture the provider adapter's primitive email fields."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def send_email(
        self,
        *,
        recipient_address: str,
        subject: str,
        body: str,
    ) -> str:
        """Record one provider request and return its external identifier."""

        self.calls.append((recipient_address, subject, body))
        return "external-message-1"


class NotificationAdapterTests(unittest.IsolatedAsyncioTestCase):
    """Verify provider mapping, UTC time, and inspectable worker counters."""

    async def test_provider_sender_maps_message_and_returns_provider_id(self) -> None:
        """The adapter forwards only provider primitives and preserves the result ID."""

        client = FakeEmailProviderClient()
        sender = ProviderEmailSender(client)
        message = EmailMessage(
            RECIPIENT_ID,
            "recipient@example.test",
            "Pending custody",
            "Please confirm receipt.",
        )

        provider_id = await sender.send(message)

        self.assertEqual(provider_id, "external-message-1")
        self.assertEqual(
            client.calls,
            [
                (
                    "recipient@example.test",
                    "Pending custody",
                    "Please confirm receipt.",
                )
            ],
        )

    async def test_clock_and_metrics_expose_operational_outputs(self) -> None:
        """Production time is UTC-aware and counters start at zero then increment."""

        before = datetime.now(UTC)
        current = SystemClock().now()
        after = datetime.now(UTC)
        self.assertLessEqual(before, current)
        self.assertLessEqual(current, after)
        self.assertEqual(current.utcoffset(), UTC.utcoffset(current))

        metrics = InMemoryWorkerMetrics()
        self.assertEqual(metrics.value("outbox_claimed"), 0)
        metrics.increment("outbox_claimed")
        metrics.increment("outbox_claimed")
        self.assertEqual(metrics.value("outbox_claimed"), 2)
