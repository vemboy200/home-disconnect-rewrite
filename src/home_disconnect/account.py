"""Sign in to a Home Connect account and fetch the appliances' profiles and keys.

This is the alternative to the Home Connect Profile Downloader: it gets the same profile
files and local keys directly from the account.

The endpoints that return the local keys and profile files (`paired-appliances`,
`encryption-information`, `iddf`) need internal scopes (`ReadAccount`, `ReadOrigApi`, ...)
that BSH's developer portal never grants to a self-registered application; only the Home
Connect app's own client gets them. So the sign-in uses that client, the same way
bruestel/homeconnect-profile-downloader does. The redirect goes to the app's `hcauth://` URL,
which a browser can't open: the user copies the URL from the address bar and pastes it back.

Flow:

1. `sign_in = SignIn.start("EU")`, then send the user to `sign_in.authorize_url`.
2. `code = sign_in.code_from_redirect(pasted_url)`.
3. `token = await sign_in.exchange(client_session, code)`.
4. `profiles = await fetch_profiles(client_session, token.access_token, "EU")`.

The code is Home Connect Local's own (vemboy200), moved from its `hc_legacy_oauth.py` and
`hc_cloud_api.py`.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, NamedTuple
from urllib.parse import parse_qs, urlencode, urlparse

from .errors import HomeDisconnectError
from .profile import ProfileError
from .profile_files import connection_from_mapping, load_profiles_from_zip

if TYPE_CHECKING:
    import aiohttp

    from .profile_files import LoadedProfile

_LOGGER = logging.getLogger(__name__)

type Region = Literal["EU", "NA", "CN"]

CLIENT_ID = "9B75AC9EC512F36C84256AC47D813E2C1DD0D6520DF774B020E1E6E2EB29B1F3"
REDIRECT_URI = "hcauth://auth/prod"
# The same scopes bruestel/homeconnect-profile-downloader asks for. The app's client is
# pre-authorized for the internal ones the profile endpoints need; without them the token has
# no rights to those endpoints.
SCOPE = (
    "Control DeleteAppliance IdentifyAppliance Images Monitor "
    "ReadAccount ReadOrigApi Settings WriteAppliance WriteOrigApi"
)
REGION_API_BASE: dict[str, str] = {
    "EU": "https://api.home-connect.com",
    "NA": "https://api-rna.home-connect.com",
    "CN": "https://api.home-connect.cn",
}
REGION_ASSET_BASE: dict[str, str] = {
    "EU": "https://eu.services.home-connect.com",
    "NA": "https://na.services.home-connect.com",
    "CN": "https://cn.services.home-connect.cn",
}
_URLENCODED = {"Content-Type": "application/x-www-form-urlencoded"}
_HTTP_OK = 200
_HTTP_UNAUTHORIZED = 401


class AccountError(HomeDisconnectError):
    """Signing in or fetching the profiles failed."""


class Token(NamedTuple):
    """An access token from the sign-in, and how long it's good for (seconds)."""

    access_token: str
    expires_in: int | None


def _random_urlsafe(length: int) -> str:
    return base64.urlsafe_b64encode(os.urandom(length)).rstrip(b"=").decode()


def _check_region(region: str) -> None:
    if region not in REGION_API_BASE:
        msg = f"Invalid region {region!r}; expected one of {', '.join(REGION_API_BASE)}"
        raise AccountError(msg)


def code_challenge(verifier: str) -> str:
    """Build the PKCE code challenge (S256) for a code verifier."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@dataclass(frozen=True, slots=True)
class SignIn:
    """One sign-in attempt: its PKCE verifier, state and the URL for the user to open."""

    region: Region
    code_verifier: str
    state: str

    @classmethod
    def start(cls, region: Region) -> SignIn:
        """Start a sign-in with a fresh verifier and state."""
        _check_region(region)
        return cls(region=region, code_verifier=_random_urlsafe(32), state=_random_urlsafe(16))

    @property
    def authorize_url(self) -> str:
        """The URL where the user signs in to their Home Connect account."""
        params = {
            "redirect_url": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "response_type": "code",
            "prompt": "login",
            "code_challenge_method": "S256",
            "code_challenge": code_challenge(self.code_verifier),
            "state": self.state,
            "nonce": _random_urlsafe(16),
            "scope": SCOPE,
        }
        return f"{REGION_API_BASE[self.region]}/security/oauth/authorize?{urlencode(params)}"

    def code_from_redirect(self, redirect_url: str) -> str:
        """Take the authorization code out of the redirect URL the user pasted back."""
        try:
            parsed = urlparse(redirect_url.strip())
            params = parse_qs(parsed.query) or parse_qs(parsed.fragment)
        except ValueError as err:
            msg = "Couldn't read the pasted URL"
            raise AccountError(msg) from err
        if "code" not in params:
            msg = "The pasted URL has no authorization code"
            raise AccountError(msg)
        if params.get("state", [None])[0] != self.state:
            msg = "The pasted URL is from a different sign-in; start again"
            raise AccountError(msg)
        return params["code"][0]

    async def exchange(self, session: aiohttp.ClientSession, code: str) -> Token:
        """Exchange the authorization code for an access token."""
        async with session.post(
            f"{REGION_API_BASE[self.region]}/security/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code_verifier": self.code_verifier,
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
            headers=_URLENCODED,
        ) as response:
            data = await response.json(content_type=None)
            if response.status != _HTTP_OK:
                msg = f"The token request failed ({response.status}): {data}"
                raise AccountError(msg)
        token = data.get("access_token") if isinstance(data, dict) else None
        if not token:
            msg = f"The token response has no access token: {data}"
            raise AccountError(msg)
        expires_in = data.get("expires_in")
        return Token(str(token), int(expires_in) if expires_in is not None else None)


def account_id_from_token(access_token: str) -> str:
    """Read the account ID (the JWT's `sub` claim) from an access token."""
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        account_id = json.loads(base64.urlsafe_b64decode(payload)).get("sub")
    except (IndexError, ValueError, AttributeError) as err:
        msg = "Couldn't read the account ID from the access token"
        raise AccountError(msg) from err
    if not account_id:
        msg = "The access token has no account ID"
        raise AccountError(msg)
    return str(account_id)


async def _get_json(
    session: aiohttp.ClientSession, url: str, headers: dict[str, str], what: str
) -> Any:  # noqa: ANN401 - JSON
    async with session.get(url, headers=headers) as response:
        if response.status != _HTTP_OK:
            msg = f"Fetching {what} failed ({response.status})"
            raise AccountError(msg)
        return await response.json(content_type=None)


async def fetch_profiles(
    session: aiohttp.ClientSession, access_token: str, region: Region
) -> list[LoadedProfile]:
    """Fetch the profile and local key of every appliance on the account.

    Demo appliances are skipped, and so is any appliance whose key or profile can't be
    fetched (with a warning), as long as at least one works.
    """
    _check_region(region)
    base = REGION_ASSET_BASE[region]
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    url = f"{base}/api/account/v2/accounts/{account_id_from_token(access_token)}/paired-appliances"
    async with session.get(url, headers=headers) as response:
        if response.status == _HTTP_UNAUTHORIZED:
            msg = f"Not authorized to list the appliances (wrong region {region!r}?)"
            raise AccountError(msg)
        if response.status != _HTTP_OK:
            msg = f"Listing the paired appliances failed ({response.status})"
            raise AccountError(msg)
        data = await response.json(content_type=None)

    appliances = [a for a in data.get("appliances", []) if not a.get("isDemo")]
    if not appliances:
        msg = "There are no appliances on this account"
        raise AccountError(msg)

    profiles: list[LoadedProfile] = []
    for appliance in appliances:
        try:
            profiles.append(await _fetch_one(session, base, headers, appliance))
        except (AccountError, ProfileError) as err:
            _LOGGER.warning("Skipping appliance %s: %s", appliance.get("haId"), err)
    if not profiles:
        msg = "Found appliances, but couldn't fetch any of their profiles"
        raise AccountError(msg)
    return profiles


async def _fetch_one(
    session: aiohttp.ClientSession,
    base: str,
    headers: dict[str, str],
    appliance: dict[str, Any],
) -> LoadedProfile:
    ha_id = str(appliance.get("haId") or "")
    if not ha_id:
        msg = "An appliance on the account has no haId"
        raise AccountError(msg)

    encryption = await _get_json(
        session,
        f"{base}/api/appliance/v2/appliances/{ha_id}/encryption-information",
        headers,
        f"the key for {ha_id}",
    )
    if encryption.get("tls", {}).get("key"):
        connection_type, key, iv = "TLS", encryption["tls"]["key"], None
    elif encryption.get("aes", {}).get("key"):
        connection_type, key, iv = "AES", encryption["aes"]["key"], encryption["aes"].get("iv")
    else:
        msg = f"No usable key for {ha_id}"
        raise AccountError(msg)

    async with session.get(
        f"{base}/api/iddf/v1/iddf/{ha_id}",
        headers={"Authorization": headers["Authorization"]},
    ) as response:
        if response.status != _HTTP_OK:
            msg = f"Fetching the profile for {ha_id} failed ({response.status})"
            raise AccountError(msg)
        archive = await response.read()

    raw: dict[str, Any] = {
        "haId": ha_id,
        "brand": (appliance.get("brand") or "").upper(),
        "vib": appliance.get("vib", ""),
        "mac": appliance.get("mac", ha_id.rsplit("-", maxsplit=1)[-1]),
        "type": appliance.get("haType") or appliance.get("type", ""),
        "connectionType": connection_type,
        "key": key,
    }
    if iv:
        raw["iv"] = iv
    connection = connection_from_mapping(raw, f"the key for {ha_id}")
    try:
        (loaded, *_) = load_profiles_from_zip(archive)
    except ProfileError as err:
        msg = f"The profile archive for {ha_id} isn't usable: {err}"
        raise AccountError(msg) from err
    return dataclasses.replace(loaded, connection=connection)
