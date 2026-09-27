"""Exceptions raised by home-disconnect."""

from __future__ import annotations


class HomeDisconnectError(Exception):
    """Base class for every error raised by this library."""


class ConnectionFailedError(HomeDisconnectError):
    """The connection to the appliance couldn't be opened."""


class ConnectionClosedError(HomeDisconnectError):
    """The connection to the appliance closed or dropped."""

    def __init__(self, code: int | None = None) -> None:
        """Store the WebSocket close code, if the appliance sent one."""
        super().__init__(f"Connection closed (code {code})")
        self.code = code


class DecryptionError(HomeDisconnectError):
    """A message from the appliance failed authentication or decryption."""
