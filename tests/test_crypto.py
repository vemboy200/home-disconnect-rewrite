import pytest

from home_disconnect.crypto import AesCodec, decode_key
from home_disconnect.errors import DecryptionError

PSK = bytes(range(32))
IV = bytes(range(16, 32))


def codec_pair() -> tuple[AesCodec, AesCodec]:
    return AesCodec(PSK, IV), AesCodec(PSK, IV, role="appliance")


def test_decode_key_accepts_missing_padding() -> None:
    assert decode_key("AAEC") == b"\x00\x01\x02"
    assert decode_key("AAE") == b"\x00\x01"
    assert decode_key("_-8") == b"\xff\xef"


@pytest.mark.parametrize("length", [0, 1, 14, 15, 16, 17, 31, 32, 100])
def test_round_trip_both_directions(length: int) -> None:
    client, appliance = codec_pair()
    message = "x" * length
    assert appliance.decrypt(client.encrypt(message)) == message
    assert client.decrypt(appliance.encrypt(message)) == message


@pytest.mark.parametrize("length", [0, 14, 15, 16, 31])
def test_frames_are_whole_blocks_plus_mac(length: int) -> None:
    client, _ = codec_pair()
    frame = client.encrypt("x" * length)
    assert (len(frame) - 16) % 16 == 0
    # The pad is never a single byte: 15 bytes of text gets a whole extra block.
    assert len(frame) - 16 >= length + 2


def test_chains_continue_across_messages() -> None:
    client, appliance = codec_pair()
    messages = ['{"sID":1}', '{"msgID":2,"resource":"/ro/values"}', "é" * 40]
    for message in messages:
        assert appliance.decrypt(client.encrypt(message)) == message


def test_same_message_encrypts_differently_each_time() -> None:
    client, _ = codec_pair()
    assert client.encrypt("same") != client.encrypt("same")


def test_out_of_order_frame_fails_authentication() -> None:
    client, appliance = codec_pair()
    first = client.encrypt("first")
    second = client.encrypt("second")
    with pytest.raises(DecryptionError):
        appliance.decrypt(second)
    assert first  # never delivered


def test_tampered_frame_fails_authentication() -> None:
    client, appliance = codec_pair()
    frame = bytearray(client.encrypt("hello"))
    frame[0] ^= 1
    with pytest.raises(DecryptionError, match="authentication"):
        appliance.decrypt(bytes(frame))


def test_frame_in_wrong_direction_fails_authentication() -> None:
    client, other_client = codec_pair()[0], AesCodec(PSK, IV)
    with pytest.raises(DecryptionError):
        other_client.decrypt(client.encrypt("hello"))


def test_wrong_key_fails_authentication() -> None:
    client = AesCodec(PSK, IV)
    appliance = AesCodec(bytes(32), IV, role="appliance")
    with pytest.raises(DecryptionError):
        appliance.decrypt(client.encrypt("hello"))


@pytest.mark.parametrize("length", [0, 15, 17, 40])
def test_badly_sized_frame_is_rejected(length: int) -> None:
    _, appliance = codec_pair()
    with pytest.raises(DecryptionError, match="blocks"):
        appliance.decrypt(bytes(length))


def test_reset_starts_new_chains() -> None:
    client, appliance = codec_pair()
    appliance.decrypt(client.encrypt("before"))
    client.reset()
    appliance.reset()
    assert appliance.decrypt(client.encrypt("after")) == "after"


def test_padding_uses_injected_random_bytes() -> None:
    client = AesCodec(PSK, IV, random_bytes=lambda n: b"\xaa" * n)
    other = AesCodec(PSK, IV, random_bytes=lambda n: b"\xaa" * n)
    assert client.encrypt("deterministic") == other.encrypt("deterministic")
