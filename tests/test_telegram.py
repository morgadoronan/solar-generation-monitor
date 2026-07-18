from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs

import pytest

from solar_monitor.config import Secret, TelegramConfig
from solar_monitor.models import Notification, NotificationKind
from solar_monitor.telegram import HttpResponse, TelegramDeliveryError, TelegramNotifier

CANARY = "telegram-canary-secret"


class FakeTransport:
    def __init__(self, result: HttpResponse | Exception) -> None:
        self.result = result
        self.calls: list[tuple[str, bytes]] = []

    def post(
        self, url: str, *, data: bytes, headers: dict[str, str], timeout: float
    ) -> HttpResponse:
        self.calls.append((url, data))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def event() -> Notification:
    return Notification(
        "evt_v1_stable", NotificationKind.INTERRUPTION, datetime.now(UTC), "Alert", "Body"
    )


def test_send_message_preserves_service_supplied_event_id() -> None:
    transport = FakeTransport(HttpResponse(200, b'{"ok":true}'))
    notifier = TelegramNotifier(TelegramConfig(Secret(CANARY), "42"), transport=transport)

    notifier.send(event())

    query = parse_qs(transport.calls[0][1].decode())
    assert query["chat_id"] == ["42"]
    assert "evt_v1_stable" in query["text"][0]


@pytest.mark.parametrize(
    "result", [HttpResponse(500, CANARY.encode()), HttpResponse(200, b"bad"), RuntimeError(CANARY)]
)
def test_delivery_failures_are_secret_safe_and_retryable(result: HttpResponse | Exception) -> None:
    notifier = TelegramNotifier(
        TelegramConfig(Secret(CANARY), "42"), transport=FakeTransport(result)
    )

    with pytest.raises(TelegramDeliveryError) as caught:
        notifier.send(event())

    assert CANARY not in str(caught.value)
