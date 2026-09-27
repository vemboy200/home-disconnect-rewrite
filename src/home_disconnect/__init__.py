"""Local WebSocket client for Home Connect appliances."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("home-disconnect")
except PackageNotFoundError:  # pragma: no cover - running from a source tree without an install
    __version__ = "0.0.0"

__all__ = ["__version__"]
