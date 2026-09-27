"""Local WebSocket client for Home Connect appliances."""

from importlib.metadata import PackageNotFoundError, version

from .appliance import Appliance
from .entities import (
    Access,
    AccessError,
    ActiveProgram,
    Command,
    Entities,
    Entity,
    Event,
    Execution,
    InvalidValueError,
    Option,
    Program,
    SelectedProgram,
    Setting,
    Status,
)
from .errors import (
    AlreadyConnectedError,
    AuthenticationError,
    ConnectionClosedError,
    ConnectionFailedError,
    DecryptionError,
    HomeDisconnectError,
)
from .messages import Action, Message, ResponseError
from .profile import DeviceProfile, ProfileError, parse_profile
from .profile_files import LoadedProfile, load_profiles, load_profiles_from_zip
from .session import ConnectionState, HandshakeError, Session

try:
    __version__ = version("home-disconnect")
except PackageNotFoundError:  # pragma: no cover - running from a source tree without an install
    __version__ = "0.0.0"

__all__ = [
    "Access",
    "AccessError",
    "Action",
    "ActiveProgram",
    "AlreadyConnectedError",
    "Appliance",
    "AuthenticationError",
    "Command",
    "ConnectionClosedError",
    "ConnectionFailedError",
    "ConnectionState",
    "DecryptionError",
    "DeviceProfile",
    "Entities",
    "Entity",
    "Event",
    "Execution",
    "HandshakeError",
    "HomeDisconnectError",
    "InvalidValueError",
    "LoadedProfile",
    "Message",
    "Option",
    "ProfileError",
    "Program",
    "ResponseError",
    "SelectedProgram",
    "Session",
    "Setting",
    "Status",
    "__version__",
    "load_profiles",
    "load_profiles_from_zip",
    "parse_profile",
]
