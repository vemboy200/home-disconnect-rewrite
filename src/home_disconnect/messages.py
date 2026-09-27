"""The JSON messages exchanged with an appliance, and matching responses to requests.

Every message is one JSON object (see hcpy, MIT, THIRD_PARTY_NOTICES.md):

- `sID`: the session ID. The appliance picks it in its first message, `/ei/initialValues`.
- `msgID`: numbers our messages. The first one is the `edMsgID` the appliance sends in
  `/ei/initialValues`, and it goes up by one for every message we send. A response carries
  the `msgID` of the request it answers.
- `resource`: what the message is about, e.g. `/ro/values`. The first path segment is the
  service (`ro`, `ci`, `ei`, `ni`, `iz`, ...).
- `version`: the service's version, as the appliance reports it in `/ci/services`.
- `action`: `GET`, `POST`, `DELETE`, `NOTIFY` or `RESPONSE`.
- `data`: an optional list of objects.
- `code`: set instead of data on a response that reports an error.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from .errors import HomeDisconnectError

type MessageData = list[dict[str, Any]]


class Action(StrEnum):
    """What a message does."""

    GET = "GET"
    POST = "POST"
    DELETE = "DELETE"
    NOTIFY = "NOTIFY"
    RESPONSE = "RESPONSE"


class InvalidMessageError(HomeDisconnectError):
    """A message from the appliance isn't a valid message object."""


class ResponseError(HomeDisconnectError):
    """The appliance answered a request with an error code."""

    def __init__(self, code: int, resource: str) -> None:
        """Store the error code and the resource it was for."""
        super().__init__(f"Appliance returned error {code} for {resource}")
        self.code = code
        self.resource = resource


@dataclass(frozen=True, slots=True)
class Message:
    """One message, in either direction."""

    resource: str
    action: Action = Action.GET
    data: MessageData | None = None
    version: int | None = None
    session_id: int | None = None
    message_id: int | None = None
    code: int | None = None

    @property
    def service(self) -> str:
        """The service the resource belongs to, e.g. `ro` for `/ro/values`."""
        return self.resource.split("/", 2)[1] if self.resource.startswith("/") else ""

    def to_json(self) -> str:
        """Serialize for sending. Fields that aren't set are left out."""
        payload: dict[str, Any] = {
            "sID": self.session_id,
            "msgID": self.message_id,
            "resource": self.resource,
            "version": self.version,
            "action": str(self.action),
        }
        if self.data is not None:
            payload["data"] = self.data
        if self.code is not None:
            payload["code"] = self.code
        return json.dumps(
            {k: v for k, v in payload.items() if v is not None}, separators=(",", ":")
        )

    @classmethod
    def from_json(cls, text: str) -> Message:
        """Parse a message received from the appliance."""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as err:
            msg = f"Message isn't valid JSON: {err}"
            raise InvalidMessageError(msg) from err
        if not isinstance(payload, dict):
            msg = "Message isn't a JSON object"
            raise InvalidMessageError(msg)
        try:
            action = Action(payload["action"])
            resource = payload["resource"]
        except (KeyError, ValueError) as err:
            msg = f"Message has no valid action or resource: {text[:200]}"
            raise InvalidMessageError(msg) from err
        data = payload.get("data")
        if data is not None and not (
            isinstance(data, list) and all(isinstance(item, dict) for item in data)
        ):
            msg = f"Message data isn't a list of objects: {text[:200]}"
            raise InvalidMessageError(msg)
        return cls(
            resource=str(resource),
            action=action,
            data=data,
            version=payload.get("version"),
            session_id=payload.get("sID"),
            message_id=payload.get("msgID"),
            code=payload.get("code"),
        )

    def response(self, data: MessageData | None = None) -> Message:
        """Build the response to this message, e.g. our reply to `/ei/initialValues`."""
        return replace(self, action=Action.RESPONSE, data=data, code=None)


@dataclass(slots=True)
class RequestTracker:
    """Numbers outgoing messages and pairs each response with the request it answers.

    One tracker per connection: `session_id` and `next_message_id` come from the appliance's
    `/ei/initialValues`.
    """

    session_id: int
    next_message_id: int
    service_versions: dict[str, int] = field(default_factory=dict)
    _pending: dict[int, asyncio.Future[Message]] = field(default_factory=dict)

    def prepare(self, message: Message) -> tuple[Message, asyncio.Future[Message] | None]:
        """Fill in session, message ID and version, and register a future for the response.

        Returns the message to send and a future that resolves to the response, or raises
        `ResponseError` if the appliance answers with an error code. `NOTIFY` and `RESPONSE`
        messages get no response, so their future is `None`.
        """
        version = message.version
        if version is None:
            version = self.service_versions.get(message.service, 1)
        prepared = replace(
            message,
            session_id=self.session_id,
            message_id=self.next_message_id,
            version=version,
        )
        self.next_message_id += 1
        if message.action in (Action.NOTIFY, Action.RESPONSE):
            return prepared, None
        future: asyncio.Future[Message] = asyncio.get_running_loop().create_future()
        self._pending[self.next_message_id - 1] = future
        return prepared, future

    def resolve(self, message: Message) -> bool:
        """Hand a received response to the request waiting for it.

        Returns `False` when nothing is waiting for it (not a response, an unknown message ID,
        or a request that was already given up on).
        """
        if message.action is not Action.RESPONSE or message.message_id is None:
            return False
        future = self._pending.pop(message.message_id, None)
        if future is None or future.done():
            return False
        if message.code is not None:
            future.set_exception(ResponseError(message.code, message.resource))
        else:
            future.set_result(message)
        return True

    def discard(self, message_id: int) -> None:
        """Forget a request, e.g. after the caller stopped waiting for it."""
        self._pending.pop(message_id, None)

    def fail_all(self, error: BaseException) -> None:
        """Fail every waiting request, e.g. when the connection drops."""
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(error)

    @property
    def pending_count(self) -> int:
        """How many requests are still waiting for a response."""
        return len(self._pending)
