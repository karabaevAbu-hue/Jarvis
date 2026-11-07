"""Тонкий асинхронный клиент GREEN-API на httpx.

Адреса и HTTP-методы сверены с официальной библиотекой whatsapp-api-client-python 0.0.55.
"""
from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)


class GreenAPIError(Exception):
    def __init__(self, method: str, status: int | None = None, body: str = "", *, network: bool = False):
        self.method = method
        self.status = status
        self.body = body or ""
        self.network = network
        what = "нет связи или таймаут" if network else f"HTTP {status}"
        super().__init__(f"GREEN-API {method}: {what} {self.body[:200]}".strip())

    @property
    def maybe_sent(self) -> bool:
        """Ответа нет, но сообщение могло уйти: повторять только после проверки."""
        return self.network or self.status in (499, 502, 503, 504)

    @property
    def rate_limited(self) -> bool:
        return self.status == 429

    @property
    def quota(self) -> bool:
        return self.status == 466

    @property
    def bad_credentials(self) -> bool:
        return self.status in (401, 403)


class GreenAPI:
    def __init__(
        self,
        api_url: str,
        media_url: str,
        id_instance: str,
        token: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._api = api_url.rstrip("/")
        self._media = media_url.rstrip("/")
        self._id = id_instance
        self._token = token
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0), transport=transport)

    async def close(self) -> None:
        await self._http.aclose()

    def _url(self, method: str, *, media: bool = False, suffix: str = "") -> str:
        base = self._media if media else self._api
        return f"{base}/waInstance{self._id}/{method}/{self._token}{suffix}"

    async def _call(
        self,
        verb: str,
        method: str,
        *,
        media: bool = False,
        suffix: str = "",
        params: dict | None = None,
        json_body: dict | None = None,
        data: dict | None = None,
        files: dict | None = None,
        timeout: float | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {"params": params, "json": json_body, "data": data, "files": files}
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            resp = await self._http.request(verb, self._url(method, media=media, suffix=suffix), **kwargs)
        except httpx.TimeoutException:
            # `from None`: исходное исключение содержит адрес с токеном
            raise GreenAPIError(method, body="timeout", network=True) from None
        except httpx.TransportError as exc:
            raise GreenAPIError(method, body=type(exc).__name__, network=True) from None
        if resp.status_code != 200:
            raise GreenAPIError(method, resp.status_code, resp.text[:500])
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    # --- аккаунт ---
    async def get_state(self) -> str:
        data = await self._call("GET", "getStateInstance")
        return (data or {}).get("stateInstance", "unknown")

    async def get_settings(self) -> dict:
        return await self._call("GET", "getSettings") or {}

    async def set_settings(self, settings: dict) -> None:
        await self._call("POST", "setSettings", json_body=settings)

    async def reboot(self) -> None:
        await self._call("GET", "reboot")

    # --- чаты и группы ---
    async def get_groups(self) -> list[dict]:
        chats = await self._call("GET", "getChats") or []
        return [c for c in chats if c.get("type") == "group" or str(c.get("id", "")).endswith("@g.us")]

    async def get_group_data(self, group_id: str) -> dict:
        return await self._call("POST", "getGroupData", json_body={"groupId": group_id}) or {}

    # --- отправка ---
    async def send_message(self, chat_id: str, text: str) -> str:
        data = await self._call("POST", "sendMessage", json_body={"chatId": chat_id, "message": text})
        return str((data or {}).get("idMessage", ""))

    async def send_file_by_upload(self, chat_id: str, path: Path, caption: str = "") -> tuple[str, str]:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data = await self._call(
            "POST",
            "sendFileByUpload",
            media=True,
            data={"chatId": chat_id, "fileName": path.name, "caption": caption},
            files={"file": (path.name, path.read_bytes(), mime)},
            timeout=120.0,
        )
        data = data or {}
        return str(data.get("idMessage", "")), str(data.get("urlFile", ""))

    async def send_file_by_url(self, chat_id: str, url: str, file_name: str, caption: str = "") -> str:
        data = await self._call(
            "POST",
            "sendFileByUrl",
            json_body={"chatId": chat_id, "urlFile": url, "fileName": file_name, "caption": caption},
        )
        return str((data or {}).get("idMessage", ""))

    # --- уведомления (HTTP API, без вебхука) ---
    async def receive_notification(self, wait_seconds: int = 20) -> dict | None:
        return await self._call(
            "GET", "receiveNotification", params={"receiveTimeout": wait_seconds}, timeout=wait_seconds + 15
        )

    async def delete_notification(self, receipt_id: int) -> None:
        await self._call("DELETE", "deleteNotification", suffix=f"/{receipt_id}")

    # --- очередь и журнал ---
    async def show_queue(self) -> list:
        return await self._call("GET", "showMessagesQueue") or []

    async def clear_queue(self) -> None:
        await self._call("GET", "clearMessagesQueue")

    async def last_outgoing(self, minutes: int = 10) -> list[dict]:
        return await self._call("GET", "lastOutgoingMessages", params={"minutes": minutes}) or []
