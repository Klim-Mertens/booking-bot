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
from typing import Any

import aiohttp
import certifi

API_URL = "https://platform-api2.max.ru"
log = logging.getLogger("booking.max")


def tls_context() -> ssl.SSLContext:
    """Проверка сертификата по набору корневых сертификатов certifi.

    Python с python.org на macOS не видит системные сертификаты, пока не запущен
    «Install Certificates.command», и тогда любое HTTPS-соединение падает.
    aiogram поэтому тоже берёт certifi — делаем так же, проверку не отключаем.
    """
    return ssl.create_default_context(cafile=certifi.where())


class MaxApiError(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"MAX API {status}: {code}")
        self.status, self.code = status, code


def callback_button(text: str, payload: str) -> dict[str, Any]:
    # payload — до 256 символов; как и в Telegram, это недоверенные данные
    return {"type": "callback", "text": text[:64], "payload": payload}


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

    async def send(self, user_id: int, text: str, buttons: list[list[dict]] | None = None) -> Any:
        body: dict[str, Any] = {"text": text[:4000], "format": "html", "attachments": []}
        if buttons:
            body["attachments"].append(keyboard(buttons))
        return await self._request("POST", "/messages", params={"user_id": user_id}, json=body)

    async def me(self) -> dict[str, Any]:
        """Кто я: проверка токена при запуске."""
        return await self._request("GET", "/me", timeout=15)

    async def subscriptions(self) -> list[dict[str, Any]]:
        """Webhook-подписки. Если есть хоть одна, MAX не отдаёт события через polling."""
        data = await self._request("GET", "/subscriptions", timeout=15)
        return (data or {}).get("subscriptions") or []

    async def answer_callback(self, callback_id: str, notification: str | None = None) -> Any:
        body = {"notification": notification} if notification else {}
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
