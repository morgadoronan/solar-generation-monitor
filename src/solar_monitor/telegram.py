"""Secret-safe Telegram ``sendMessage`` adapter."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from solar_monitor.config import TelegramConfig
from solar_monitor.models import Notification


class TelegramDeliveryError(RuntimeError):
    """A sanitized delivery failure whose acceptance status is unknown."""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes


class HttpTransport(Protocol):
    def post(
        self, url: str, *, data: bytes, headers: dict[str, str], timeout: float
    ) -> HttpResponse: ...


class UrllibTransport:
    def post(
        self, url: str, *, data: bytes, headers: dict[str, str], timeout: float
    ) -> HttpResponse:
        request = Request(url, data=data, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=timeout) as response:
                return HttpResponse(response.status, response.read())
        except HTTPError as error:
            return HttpResponse(error.code, error.read())
        except (OSError, URLError):
            raise TelegramDeliveryError("Telegram delivery failed") from None


class TelegramNotifier:
    """Deliver logical events once per call; persistence owns deduplication.

    A successful return means Telegram acknowledged the request. A raised error
    deliberately does not claim whether Telegram accepted it, which preserves
    at-least-once retry semantics across the post-acceptance crash window.
    """

    def __init__(
        self,
        config: TelegramConfig,
        *,
        transport: HttpTransport | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._config = config
        self._transport = transport or UrllibTransport()
        self._timeout = timeout

    def send(self, notification: Notification) -> None:
        text = f"{notification.title}\n\n{notification.body}\n\nEvent: {notification.event_id}"
        data = urlencode(
            {"chat_id": self._config.chat_id, "text": text, "disable_web_page_preview": "true"}
        ).encode()
        url = f"https://api.telegram.org/bot{self._config.bot_token.reveal()}/sendMessage"
        try:
            response = self._transport.post(
                url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self._timeout,
            )
        except TelegramDeliveryError:
            raise
        except Exception:
            raise TelegramDeliveryError("Telegram delivery failed") from None
        if response.status != 200 or not _telegram_ok(response.body):
            raise TelegramDeliveryError("Telegram rejected the notification")


def _telegram_ok(body: bytes) -> bool:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("ok") is True
