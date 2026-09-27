"""A small fake appliance for tests: an AES WebSocket server that runs the handshake."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

from home_disconnect.crypto import AesCodec

PSK = bytes(range(32))
IV = bytes(range(16, 32))
PSK64 = base64.urlsafe_b64encode(PSK).decode().rstrip("=")
IV64 = base64.urlsafe_b64encode(IV).decode().rstrip("=")

SESSION_ID = 4321
FIRST_MESSAGE_ID = 1000


class FakeAppliance:
    """Answers the handshake requests; everything else is up to the test."""

    def __init__(self, services: dict[str, int] | None = None) -> None:
        self.services = services or {"ci": 2, "ei": 2, "ni": 1, "ro": 1}
        self.received: list[dict[str, Any]] = []
        self.responses: dict[str, dict[str, Any]] = {
            "/ci/info": {"data": [{"deviceID": "123", "vib": "TEST"}]},
            "/iz/info": {"data": [{"deviceID": "456", "vib": "IZTEST"}]},
            "/ci/authentication": {"data": [{"response": "x"}]},
            "/ni/info": {"data": [{"interfaceID": 0, "ipV4": {"ipAddress": "192.0.2.1"}}]},
        }
        self.connections = 0
        self.websocket: web.WebSocketResponse | None = None
        self.codec: AesCodec | None = None
        self.handshake_done = asyncio.Event()
        self.close_after_handshake = False
        self.accepting = True
        self.server: TestServer | None = None

    async def start(self) -> int:
        app = web.Application()
        app.router.add_get("/homeconnect", self._view)
        self.server = TestServer(app, host="127.0.0.1")
        await self.server.start_server()
        port = self.server.port
        assert port is not None
        return port

    async def stop(self) -> None:
        if self.websocket is not None:
            await self.websocket.close()
        if self.server is not None:
            await self.server.close()

    async def send(self, payload: dict[str, Any]) -> None:
        assert self.websocket is not None
        assert self.codec is not None
        await self.websocket.send_bytes(self.codec.encrypt(json.dumps(payload)))

    async def drop(self) -> None:
        assert self.websocket is not None
        await self.websocket.close()

    async def _view(self, request: web.Request) -> web.StreamResponse:
        if not self.accepting:
            return web.Response(status=503)
        self.connections += 1
        self.handshake_done.clear()
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        self.websocket = websocket
        self.codec = AesCodec(PSK, IV, role="appliance")
        await self.send(
            {
                "sID": SESSION_ID,
                "msgID": 1,
                "resource": "/ei/initialValues",
                "version": 2,
                "action": "POST",
                "data": [{"edMsgID": FIRST_MESSAGE_ID}],
            }
        )
        async for frame in websocket:
            message = json.loads(self.codec.decrypt(frame.data))
            self.received.append(message)
            await self._answer(message)
            if message["resource"] == "/ei/deviceReady":
                self.handshake_done.set()
                if self.close_after_handshake:
                    await websocket.close()
        return websocket

    async def _answer(self, message: dict[str, Any]) -> None:
        if message["action"] in ("NOTIFY", "RESPONSE"):
            return
        resource = message["resource"]
        reply: dict[str, Any] = {
            "sID": message["sID"],
            "msgID": message["msgID"],
            "resource": resource,
            "version": message["version"],
            "action": "RESPONSE",
        }
        if resource == "/ci/services":
            reply["data"] = [{"service": s, "version": v} for s, v in self.services.items()]
        elif resource in self.responses:
            reply.update(self.responses[resource])
        else:
            return  # unanswered: lets tests exercise request timeouts
        await self.send(reply)
