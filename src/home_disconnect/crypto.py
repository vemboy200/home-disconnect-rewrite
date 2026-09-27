"""Message encryption for appliances that use the AES scheme (plain WebSocket on port 80).

The scheme follows hcpy (MIT, see THIRD_PARTY_NOTICES.md):

- The encryption key is HMAC-SHA256(psk, "ENC") and the MAC key is HMAC-SHA256(psk, "MAC").
- Messages are AES-256-CBC encrypted with one cipher context per direction that lives for the
  whole connection, so each message continues the CBC chain of the previous one.
- Plaintext is padded with a zero byte, random bytes and a final byte holding the pad length.
  The pad is never a single byte; that case gets a whole extra block.
- Each frame carries the first 16 bytes of HMAC-SHA256(mac_key, iv + direction + previous MAC +
  ciphertext), so the MACs also chain. The client sends with direction "E" and receives "C".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from typing import TYPE_CHECKING, Literal

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .errors import DecryptionError

if TYPE_CHECKING:
    from collections.abc import Callable

    from cryptography.hazmat.primitives.ciphers import CipherContext

BLOCK_SIZE = 16
MAC_SIZE = 16

_DIRECTION_TO_APPLIANCE = b"E"
_DIRECTION_FROM_APPLIANCE = b"C"


def decode_key(value: str) -> bytes:
    """Decode a base64url value from an appliance profile, with or without padding."""
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _hmac_sha256(key: bytes, message: bytes) -> bytes:
    return hmac.new(key, message, hashlib.sha256).digest()


class AesCodec:
    """Encrypts outgoing and decrypts incoming messages for one connection.

    Create a new codec (or call `reset`) for every connection: both the CBC chain and the MAC
    chain start over when the appliance accepts a new WebSocket.
    """

    def __init__(
        self,
        psk: bytes,
        iv: bytes,
        *,
        role: Literal["client", "appliance"] = "client",
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        """Set up the keys. `role="appliance"` swaps the directions, for simulators and tests."""
        self._iv = iv
        self._enc_key = _hmac_sha256(psk, b"ENC")
        self._mac_key = _hmac_sha256(psk, b"MAC")
        self._random_bytes = random_bytes
        if role == "client":
            self._send_direction = _DIRECTION_TO_APPLIANCE
            self._receive_direction = _DIRECTION_FROM_APPLIANCE
        else:
            self._send_direction = _DIRECTION_FROM_APPLIANCE
            self._receive_direction = _DIRECTION_TO_APPLIANCE
        self.reset()

    def reset(self) -> None:
        """Start fresh CBC and MAC chains, as a new connection does."""
        cipher = Cipher(algorithms.AES(self._enc_key), modes.CBC(self._iv))
        self._encryptor: CipherContext = cipher.encryptor()
        self._decryptor: CipherContext = cipher.decryptor()
        self._last_sent_mac = bytes(MAC_SIZE)
        self._last_received_mac = bytes(MAC_SIZE)

    def _mac(self, direction: bytes, previous_mac: bytes, ciphertext: bytes) -> bytes:
        return _hmac_sha256(self._mac_key, self._iv + direction + previous_mac + ciphertext)[
            :MAC_SIZE
        ]

    def encrypt(self, message: str) -> bytes:
        """Encrypt one message and append its MAC."""
        plaintext = message.encode()
        pad_length = BLOCK_SIZE - len(plaintext) % BLOCK_SIZE
        if pad_length == 1:
            pad_length += BLOCK_SIZE
        padding = b"\x00" + self._random_bytes(pad_length - 2) + bytes([pad_length])
        ciphertext = self._encryptor.update(plaintext + padding)
        mac = self._mac(self._send_direction, self._last_sent_mac, ciphertext)
        self._last_sent_mac = mac
        return ciphertext + mac

    def decrypt(self, frame: bytes) -> str:
        """Check the MAC of one frame and decrypt it."""
        if len(frame) < BLOCK_SIZE + MAC_SIZE or (len(frame) - MAC_SIZE) % BLOCK_SIZE:
            msg = f"Frame of {len(frame)} bytes isn't a whole number of blocks plus a MAC"
            raise DecryptionError(msg)
        ciphertext, received_mac = frame[:-MAC_SIZE], frame[-MAC_SIZE:]
        expected_mac = self._mac(self._receive_direction, self._last_received_mac, ciphertext)
        if not hmac.compare_digest(received_mac, expected_mac):
            msg = "Message authentication failed"
            raise DecryptionError(msg)
        self._last_received_mac = received_mac
        plaintext = self._decryptor.update(ciphertext)
        pad_length = plaintext[-1]
        if not 2 <= pad_length <= len(plaintext):  # noqa: PLR2004 - the pad is never one byte
            msg = f"Invalid padding length {pad_length}"
            raise DecryptionError(msg)
        return plaintext[:-pad_length].decode()
