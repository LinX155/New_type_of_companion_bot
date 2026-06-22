import asyncio
import json
import uuid
from datetime import datetime
from typing import Awaitable, Callable, Optional

from fastapi import WebSocket, WebSocketDisconnect


OneBotEventHandler = Callable[[dict], Awaitable[None]]


class OneBotConnectionManager:
    """OneBot reverse WebSocket connection manager.

    NapCat connects to us as a WebSocket client. Events and action responses are
    received from the same socket, and actions are sent back over that socket.
    """

    def __init__(self):
        self._websocket: Optional[WebSocket] = None
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future] = {}
        self._connected_at: Optional[datetime] = None
        self._last_event_at: Optional[datetime] = None
        self._last_heartbeat_at: Optional[datetime] = None
        self._last_error: Optional[str] = None
        self._self_id: Optional[str] = None
        self._connection_name: Optional[str] = None

    async def handle_websocket(
        self,
        websocket: WebSocket,
        event_handler: OneBotEventHandler,
        expected_token: str = "",
    ) -> None:
        if expected_token and not self._token_matches(websocket, expected_token):
            await websocket.close(code=1008)
            return

        await websocket.accept()
        previous = self._websocket
        if previous is not None and previous is not websocket:
            try:
                await previous.close(code=1000)
            except Exception:
                pass

        self._websocket = websocket
        self._connected_at = datetime.now()
        self._last_event_at = self._connected_at
        self._last_error = None
        self._connection_name = websocket.query_params.get("name")

        try:
            while True:
                payload = await self._receive_payload(websocket)
                self._last_event_at = datetime.now()
                if self._is_action_response(payload):
                    self._resolve_action_response(payload)
                    continue

                self._record_meta(payload)
                await event_handler(payload)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            self._last_error = str(exc)
        finally:
            if self._websocket is websocket:
                self._websocket = None
            self._fail_pending("onebot websocket disconnected")

    async def request(self, action: str, params: dict, timeout: float = 10.0) -> dict:
        websocket = self._websocket
        if websocket is None:
            raise RuntimeError("OneBot websocket is not connected")

        echo = f"onebot_{uuid.uuid4().hex}"
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[echo] = future

        payload = {
            "action": action,
            "params": params,
            "echo": echo,
        }
        async with self._send_lock:
            await websocket.send_json(payload)

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(echo, None)

    async def send_private_text(
        self,
        user_id: str,
        text: str,
        reply_to_message_id: Optional[str] = None,
    ) -> dict:
        return await self.send_private_message(
            user_id=user_id,
            message=[{"type": "text", "data": {"text": text}}],
            reply_to_message_id=reply_to_message_id,
        )

    async def send_private_image(
        self,
        user_id: str,
        file_uri: str,
        reply_to_message_id: Optional[str] = None,
    ) -> dict:
        return await self.send_private_message(
            user_id=user_id,
            message=[{"type": "image", "data": {"file": file_uri}}],
            reply_to_message_id=reply_to_message_id,
        )

    async def send_private_message(
        self,
        user_id: str,
        message: list[dict],
        reply_to_message_id: Optional[str] = None,
    ) -> dict:
        return await self.request(
            action="send_private_msg",
            params={
                "user_id": _coerce_int(user_id),
                "message": _with_reply_segment(message, reply_to_message_id),
            },
        )

    async def set_input_status(self, user_id: str, event_type: int) -> dict:
        return await self.request(
            action="set_input_status",
            params={
                "user_id": _coerce_int(user_id),
                "event_type": event_type,
            },
            timeout=3.0,
        )

    async def get_msg(self, message_id: str) -> dict:
        return await self.request(
            action="get_msg",
            params={"message_id": _coerce_int(message_id)},
        )

    async def get_image(self, file: str) -> dict:
        return await self.request(
            action="get_image",
            params={"file": file},
        )

    async def get_file(self, file_id: str) -> dict:
        return await self.request(
            action="get_file",
            params={"file_id": file_id},
        )

    async def download_file_stream(self, file_id: str) -> dict:
        return await self.request(
            action="download_file_stream",
            params={"file_id": file_id},
        )

    def status(self) -> dict:
        return {
            "connected": self._websocket is not None,
            "connected_at": self._connected_at.isoformat() if self._connected_at else None,
            "last_event_at": self._last_event_at.isoformat() if self._last_event_at else None,
            "last_heartbeat_at": self._last_heartbeat_at.isoformat() if self._last_heartbeat_at else None,
            "self_id": self._self_id,
            "connection_name": self._connection_name,
            "pending_actions": len(self._pending),
            "last_error": self._last_error,
        }

    def _token_matches(self, websocket: WebSocket, expected_token: str) -> bool:
        authorization = websocket.headers.get("authorization") or ""
        token = websocket.query_params.get("access_token") or websocket.query_params.get("token")
        return (
            token == expected_token
            or authorization == f"Bearer {expected_token}"
            or authorization == f"Token {expected_token}"
        )

    async def _receive_payload(self, websocket: WebSocket) -> dict:
        text = await websocket.receive_text()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return {"_raw_text": text}
        return payload if isinstance(payload, dict) else {"_raw_payload": payload}

    def _is_action_response(self, payload: dict) -> bool:
        return "echo" in payload and (
            "status" in payload
            or "retcode" in payload
            or "data" in payload
        )

    def _resolve_action_response(self, payload: dict) -> None:
        echo = str(payload.get("echo") or "")
        future = self._pending.get(echo)
        if future and not future.done():
            future.set_result(payload)

    def _record_meta(self, payload: dict) -> None:
        self_id = payload.get("self_id")
        if self_id is not None:
            self._self_id = str(self_id)
        if payload.get("post_type") == "meta_event" and payload.get("meta_event_type") == "heartbeat":
            self._last_heartbeat_at = datetime.now()

    def _fail_pending(self, error_message: str) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(RuntimeError(error_message))
        self._pending.clear()


def _coerce_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _with_reply_segment(message: list[dict], reply_to_message_id: Optional[str]) -> list[dict]:
    reply_id = str(reply_to_message_id or "").strip()
    if not reply_id:
        return list(message or [])
    segments = list(message or [])
    if segments and segments[0].get("type") == "reply":
        return segments
    return [
        {"type": "reply", "data": {"id": _coerce_int(reply_id)}},
        *segments,
    ]
