import json

import pytest

from home_disconnect.errors import ConnectionClosedError
from home_disconnect.messages import (
    Action,
    InvalidMessageError,
    Message,
    RequestTracker,
    ResponseError,
)

INITIAL_VALUES = (
    '{"sID":1234,"msgID":5678,"resource":"/ei/initialValues","version":2,"action":"POST",'
    '"data":[{"edMsgID":100}]}'
)


def test_parse_initial_values() -> None:
    message = Message.from_json(INITIAL_VALUES)
    assert message.resource == "/ei/initialValues"
    assert message.action is Action.POST
    assert message.version == 2  # noqa: PLR2004
    assert message.session_id == 1234  # noqa: PLR2004
    assert message.message_id == 5678  # noqa: PLR2004
    assert message.data == [{"edMsgID": 100}]
    assert message.service == "ei"


def test_serialize_leaves_out_unset_fields() -> None:
    message = Message("/ci/services", session_id=1, message_id=2, version=1)
    assert json.loads(message.to_json()) == {
        "sID": 1,
        "msgID": 2,
        "resource": "/ci/services",
        "version": 1,
        "action": "GET",
    }
    assert " " not in message.to_json()


def test_serialize_round_trip() -> None:
    message = Message(
        "/ro/values",
        Action.POST,
        data=[{"uid": 539, "value": 2}],
        version=1,
        session_id=1,
        message_id=2,
    )
    assert Message.from_json(message.to_json()) == message


def test_response_echoes_the_request() -> None:
    request = Message.from_json(INITIAL_VALUES)
    reply = request.response([{"deviceType": "Application"}])
    assert reply.action is Action.RESPONSE
    assert (reply.session_id, reply.message_id, reply.resource, reply.version) == (
        1234,
        5678,
        "/ei/initialValues",
        2,
    )
    assert reply.data == [{"deviceType": "Application"}]


def test_error_response_carries_code() -> None:
    message = Message.from_json(
        '{"sID":1,"msgID":7,"resource":"/ro/values","version":1,"action":"RESPONSE","code":400}'
    )
    assert message.code == 400  # noqa: PLR2004
    assert message.data is None


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[1, 2]",
        '{"resource":"/ro/values"}',
        '{"action":"GET"}',
        '{"resource":"/ro/values","action":"PATCH"}',
        '{"resource":"/ro/values","action":"NOTIFY","data":{"uid":1}}',
        '{"resource":"/ro/values","action":"NOTIFY","data":[1,2]}',
    ],
)
def test_invalid_messages_are_rejected(text: str) -> None:
    with pytest.raises(InvalidMessageError):
        Message.from_json(text)


def test_service_of_odd_resources() -> None:
    assert Message("/ro").service == "ro"
    assert Message("ro/values").service == ""


async def test_tracker_numbers_messages_and_uses_service_versions() -> None:
    tracker = RequestTracker(session_id=9, next_message_id=100, service_versions={"ro": 2})
    first, _ = tracker.prepare(Message("/ro/allMandatoryValues"))
    second, _ = tracker.prepare(Message("/ci/info"))
    explicit, _ = tracker.prepare(Message("/ci/authentication", version=2))
    assert (first.session_id, first.message_id, first.version) == (9, 100, 2)
    assert (second.message_id, second.version) == (101, 1)
    assert explicit.version == 2  # noqa: PLR2004
    assert tracker.next_message_id == 103  # noqa: PLR2004


async def test_tracker_resolves_the_matching_request() -> None:
    tracker = RequestTracker(session_id=1, next_message_id=10)
    first, first_future = tracker.prepare(Message("/ci/services"))
    _, second_future = tracker.prepare(Message("/ci/info"))
    assert first_future is not None
    assert second_future is not None
    response = Message(
        "/ci/services", Action.RESPONSE, data=[{"service": "ro", "version": 1}], message_id=10
    )
    assert tracker.resolve(response)
    assert await first_future == response
    assert not second_future.done()
    assert first.message_id == 10  # noqa: PLR2004
    assert tracker.pending_count == 1


async def test_tracker_turns_error_codes_into_exceptions() -> None:
    tracker = RequestTracker(session_id=1, next_message_id=10)
    _, future = tracker.prepare(Message("/ro/values", Action.POST, data=[{"uid": 1, "value": 0}]))
    assert future is not None
    tracker.resolve(Message("/ro/values", Action.RESPONSE, message_id=10, code=400))
    with pytest.raises(ResponseError) as raised:
        await future
    assert raised.value.code == 400  # noqa: PLR2004
    assert raised.value.resource == "/ro/values"


async def test_tracker_ignores_what_it_isnt_waiting_for() -> None:
    tracker = RequestTracker(session_id=1, next_message_id=10)
    tracker.prepare(Message("/ci/services"))
    assert not tracker.resolve(Message("/ro/values", Action.NOTIFY, data=[], message_id=10))
    assert not tracker.resolve(Message("/ci/info", Action.RESPONSE, message_id=99))
    assert not tracker.resolve(Message("/ci/info", Action.RESPONSE))
    assert tracker.pending_count == 1


async def test_notify_and_response_expect_no_answer() -> None:
    tracker = RequestTracker(session_id=1, next_message_id=10)
    _, notify_future = tracker.prepare(Message("/ei/deviceReady", Action.NOTIFY, version=2))
    _, reply_future = tracker.prepare(Message("/ei/initialValues", Action.RESPONSE))
    assert notify_future is None
    assert reply_future is None
    assert tracker.pending_count == 0
    assert tracker.next_message_id == 12  # noqa: PLR2004


async def test_discard_and_fail_all() -> None:
    tracker = RequestTracker(session_id=1, next_message_id=10)
    _, discarded = tracker.prepare(Message("/ci/services"))
    _, waiting = tracker.prepare(Message("/ci/info"))
    assert discarded is not None
    assert waiting is not None
    tracker.discard(10)
    assert not tracker.resolve(Message("/ci/services", Action.RESPONSE, message_id=10))
    tracker.fail_all(ConnectionClosedError(1006))
    with pytest.raises(ConnectionClosedError):
        await waiting
    assert tracker.pending_count == 0
    discarded.cancel()
