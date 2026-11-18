"""Минимальный клиент MAX Bot API (https://dev.max.ru/docs-api).

Почему свой клиент, а не сторонняя библиотека: нам нужны 4 метода, а каждая
лишняя зависимость — это риск атаки через цепочку поставок. 60 строк своего
кода проще проверить, чем чужой пакет с десятком транзитивных зависимостей.

Формат запросов сверен с официальной документацией и с открытой библиотекой
maxapi 1.2.4 (MIT): токен в заголовке Authorization, базовый адрес
platform-api2.max.ru.
"""
from __future__ import annotations

import logging
import ssl
from pathlib import Path
from typing import Any

import aiohttp
import certifi

API_URL = "https://platform-api2.max.ru"
log = logging.getLogger("booking.max")


RUSSIAN_CA = Path(__file__).with_name("russiantrustedca.pem")


def tls_context() -> ssl.SSLContext:
    """TLS-контекст для MAX: проверка сертификата ВКЛЮЧЕНА.

    Сервер MAX (platform-api2.max.ru) предъявляет сертификат, выданный российским
    удостоверяющим центром Минцифры («Russian Trusted Root/Sub CA»). Его нет ни в
    certifi, ни в системе macOS, поэтому без него соединение падает с
    CERTIFICATE_VERIFY_FAILED. Мы добавляем этот центр ТОЛЬКО в контекст соединения
    с MAX (не в систему и не в Telegram-часть), остальные сертификаты берём из certifi.
    Тот же файл поставляется с официальной библиотекой maxapi.
    """
    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.load_verify_locations(cafile=str(RUSSIAN_CA))
    return ctx


class MaxApiError(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"MAX API {status}: {code}")
        self.status, self.code = status, code


def callback_button(text: str, payload: str, intent: str | None = None) -> dict[str, Any]:
    # payload — до 256 символов; как и в Telegram, это недоверенные данные.
    # intent: "positive" (зелёная) / "negative" (красная) / по умолчанию — обычная.
    b: dict[str, Any] = {"type": "callback", "text": text[:64], "payload": payload}
    if intent:
        b["intent"] = intent
    return b


def clipboard_button(text: str, payload: str) -> dict[str, Any]:
    """Кнопка «скопировать»: текст попадает в буфер обмена пользователя."""
    return {"type": "clipboard", "text": text[:64], "payload": payload[:256]}


def link_button(text: str, url: str) -> dict[str, Any]:
    return {"type": "link", "text": text[:64], "url": url}


def contact_button(text: str) -> dict[str, Any]:
    return {"type": "request_contact", "text": text[:64]}


def keyboard(rows: list[list[dict[str, Any]]]) -> dict[str, Any]:
    return {"type": "inline_keyboard", "payload": {"buttons": rows}}


class MaxClient:
    def __init__(self, token: str, session: aiohttp.ClientSession | None = None) -> None:
        self._headers = {"Authorization": token}   # токен не попадает в URL и логи веб-сервера
        self._session = session

    async def _request(self, method: str, path: str, *, params: dict | None = None,
                       json: dict | None = None, timeout: float = 35) -> Any:
        if self._session is None:
            self._session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=tls_context()))
        async with self._session.request(method, API_URL + path, params=params, json=json,
                                         headers=self._headers,
                                         timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            # Проверку TLS-сертификата НЕ отключаем (никаких ssl=False).
            data = await resp.json(content_type=None) if resp.content_length != 0 else None
            if resp.status >= 400 or (isinstance(data, dict) and data.get("success") is False):
                code = data.get("code", "unknown") if isinstance(data, dict) else "unknown"
                raise MaxApiError(resp.status, code)
            return data

    @staticmethod
    def message_body(text: str, buttons: list[list[dict]] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"text": text[:4000], "format": "html", "attachments": []}
        if buttons:
            body["attachments"].append(keyboard(buttons))
        return body

    async def send(self, user_id: int, text: str, buttons: list[list[dict]] | None = None) -> Any:
        return await self._request("POST", "/messages", params={"user_id": user_id},
                                   json=self.message_body(text, buttons))

    async def me(self) -> dict[str, Any]:
        """Кто я: проверка токена при запуске."""
        return await self._request("GET", "/me", timeout=15)

    async def subscriptions(self) -> list[dict[str, Any]]:
        """Webhook-подписки. Если есть хоть одна, MAX не отдаёт события через polling."""
        data = await self._request("GET", "/subscriptions", timeout=15)
        return (data or {}).get("subscriptions") or []

    async def answer_callback(self, callback_id: str, notification: str | None = None,
                              message: dict[str, Any] | None = None) -> Any:
        """Ответ на нажатие. message — заменить сообщение с кнопкой новым содержимым
        (экран меняется на месте, чат не засоряется), notification — всплывающий текст."""
        body: dict[str, Any] = {}
        if notification:
            body["notification"] = notification[:200]
        if message:
            body["message"] = message
        return await self._request("POST", "/answers", params={"callback_id": callback_id}, json=body)

    async def get_updates(self, marker: int | None, timeout: int = 30) -> dict[str, Any]:
        params: dict[str, Any] = {"timeout": timeout, "limit": 100,
                                  "types": "message_created,message_callback,bot_started"}
        if marker is not None:
            params["marker"] = marker
        return await self._request("GET", "/updates", params=params, timeout=timeout + 10)

    async def close(self) -> None:
        if self._session:
            await self._session.close()
