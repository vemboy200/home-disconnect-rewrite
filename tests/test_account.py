import base64
import io
import json
import logging
import zipfile
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from home_disconnect import account
from home_disconnect.account import (
    CLIENT_ID,
    REDIRECT_URI,
    SCOPE,
    AccountError,
    SignIn,
    account_id_from_token,
    code_challenge,
    fetch_profiles,
)

from .profile_fixtures import DESCRIPTION, FEATURE_MAPPING, IV64, KEY64


def jwt(claims: dict[str, Any]) -> str:
    def part(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.signature"


TOKEN = jwt({"sub": "account-1"})


def iddf_zip(prefix: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{prefix}_DeviceDescription.xml", DESCRIPTION)
        archive.writestr(f"{prefix}_FeatureMapping.xml", FEATURE_MAPPING)
    return buffer.getvalue()


class FakeAccount:
    def __init__(self) -> None:
        self.appliances: list[dict[str, Any]] = [
            {"haId": "THERMADOR-DW-68A40E000001", "brand": "Thermador", "vib": "DW1",
             "haType": "Dishwasher"},
            {"haId": "BOSCH-OVEN-68A40E000002", "brand": "Bosch", "vib": "OV1", "type": "Oven",
             "mac": "68-A4-0E-00-00-02"},
            {"haId": "DEMO-1", "isDemo": True},
        ]  # fmt: skip
        self.keys: dict[str, Any] = {
            "THERMADOR-DW-68A40E000001": {"aes": {"key": KEY64, "iv": IV64}},
            "BOSCH-OVEN-68A40E000002": {"tls": {"key": KEY64}},
        }
        self.paired_status = 200
        self.token_response: tuple[int, dict[str, Any]] = (
            200,
            {"access_token": TOKEN, "expires_in": 3600},
        )
        self.token_requests: list[dict[str, str]] = []
        self.server: TestServer | None = None

    async def start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = web.Application()
        app.router.add_post("/security/oauth/token", self._token)
        app.router.add_get("/api/account/v2/accounts/{account}/paired-appliances", self._paired)
        app.router.add_get("/api/appliance/v2/appliances/{ha_id}/encryption-information", self._key)
        app.router.add_get("/api/iddf/v1/iddf/{ha_id}", self._iddf)
        self.server = TestServer(app, host="127.0.0.1")
        await self.server.start_server()
        base = f"http://127.0.0.1:{self.server.port}"
        monkeypatch.setitem(account.REGION_API_BASE, "EU", base)
        monkeypatch.setitem(account.REGION_ASSET_BASE, "EU", base)

    async def _token(self, request: web.Request) -> web.Response:
        self.token_requests.append(dict(await request.post()))  # type: ignore[arg-type]
        status, body = self.token_response
        return web.json_response(body, status=status)

    async def _paired(self, request: web.Request) -> web.Response:
        assert request.match_info["account"] == "account-1"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        if self.paired_status != 200:  # noqa: PLR2004
            return web.Response(status=self.paired_status)
        return web.json_response({"appliances": self.appliances})

    async def _key(self, request: web.Request) -> web.Response:
        key = self.keys.get(request.match_info["ha_id"])
        if key is None:
            return web.Response(status=404)
        return web.json_response(key)

    async def _iddf(self, request: web.Request) -> web.Response:
        ha_id = request.match_info["ha_id"]
        if ha_id.startswith("BROKEN"):
            return web.Response(body=b"not a zip")
        return web.Response(body=iddf_zip(ha_id))


@pytest.fixture
async def client_session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


@pytest.fixture
async def fake(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeAccount]:
    server = FakeAccount()
    await server.start(monkeypatch)
    yield server
    assert server.server is not None
    await server.server.close()


def test_sign_in_url() -> None:
    sign_in = SignIn.start("NA")
    url = urlparse(sign_in.authorize_url)
    params = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert f"{url.scheme}://{url.netloc}{url.path}" == (
        "https://api-rna.home-connect.com/security/oauth/authorize"
    )
    assert params["client_id"] == CLIENT_ID
    assert params["redirect_url"] == REDIRECT_URI
    assert params["scope"] == SCOPE
    assert params["response_type"] == "code"
    assert params["prompt"] == "login"
    assert params["code_challenge_method"] == "S256"
    assert params["code_challenge"] == code_challenge(sign_in.code_verifier)
    assert params["state"] == sign_in.state
    assert params["nonce"]
    other = SignIn.start("NA")
    assert (other.state, other.code_verifier) != (sign_in.state, sign_in.code_verifier)


def test_code_challenge_is_rfc7636() -> None:
    # The example from RFC 7636, appendix B.
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert code_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_invalid_region() -> None:
    with pytest.raises(AccountError, match="Invalid region"):
        SignIn.start("XX")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "url",
    [
        "hcauth://auth/prod?code=abc&state={state}",
        "hcauth://auth/prod#code=abc&state={state}",
        "  hcauth://auth/prod?state={state}&code=abc  ",
    ],
)
def test_code_from_redirect(url: str) -> None:
    sign_in = SignIn.start("EU")
    assert sign_in.code_from_redirect(url.format(state=sign_in.state)) == "abc"


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("hcauth://auth/prod?state=x", "no authorization code"),
        ("hcauth://auth/prod?code=abc&state=other", "different sign-in"),
        ("hcauth://auth/prod?code=abc", "different sign-in"),
    ],
)
def test_bad_redirects(url: str, message: str) -> None:
    with pytest.raises(AccountError, match=message):
        SignIn.start("EU").code_from_redirect(url)


async def test_exchange(client_session: aiohttp.ClientSession, fake: FakeAccount) -> None:
    sign_in = SignIn.start("EU")
    token = await sign_in.exchange(client_session, "the-code")
    assert token.access_token == TOKEN
    assert token.expires_in == 3600  # noqa: PLR2004
    assert fake.token_requests == [
        {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code_verifier": sign_in.code_verifier,
            "code": "the-code",
            "redirect_uri": REDIRECT_URI,
        }
    ]


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ((400, {"error": "invalid_grant"}), r"token request failed \(400\)"),
        ((200, {"token_type": "bearer"}), "no access token"),
    ],
)
async def test_exchange_failures(
    client_session: aiohttp.ClientSession,
    fake: FakeAccount,
    response: tuple[int, dict[str, Any]],
    message: str,
) -> None:
    fake.token_response = response
    with pytest.raises(AccountError, match=message):
        await SignIn.start("EU").exchange(client_session, "the-code")


def test_account_id_from_token() -> None:
    assert account_id_from_token(TOKEN) == "account-1"
    with pytest.raises(AccountError, match="Couldn't read"):
        account_id_from_token("not-a-jwt")
    with pytest.raises(AccountError, match="no account ID"):
        account_id_from_token(jwt({"iss": "x"}))


@pytest.mark.usefixtures("fake")
async def test_fetch_profiles(client_session: aiohttp.ClientSession) -> None:
    profiles = await fetch_profiles(client_session, TOKEN, "EU")
    by_id = {p.connection.ha_id: p for p in profiles if p.connection}
    assert set(by_id) == {"THERMADOR-DW-68A40E000001", "BOSCH-OVEN-68A40E000002"}  # no demo
    dishwasher = by_id["THERMADOR-DW-68A40E000001"].connection
    assert dishwasher is not None
    assert (dishwasher.connection_type, dishwasher.psk64, dishwasher.iv64) == ("AES", KEY64, IV64)
    assert dishwasher.raw["brand"] == "THERMADOR"
    assert dishwasher.raw["type"] == "Dishwasher"
    assert dishwasher.raw["mac"] == "68A40E000001"
    oven = by_id["BOSCH-OVEN-68A40E000002"]
    assert oven.connection is not None
    assert (oven.connection.connection_type, oven.connection.iv64) == ("TLS", None)
    assert oven.connection.raw["mac"] == "68-A4-0E-00-00-02"
    # The original XML is kept, so it can be stored and exported unchanged.
    assert oven.description_xml == DESCRIPTION.encode()
    assert oven.profile.info.model == "DW100"


async def test_unusable_appliances_are_skipped(
    client_session: aiohttp.ClientSession,
    fake: FakeAccount,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake.appliances.append({"haId": "NOKEY-1"})
    fake.appliances.append({"haId": "BROKEN-2"})
    fake.keys["BROKEN-2"] = {"aes": {"key": KEY64, "iv": IV64}}
    fake.appliances.append({"haId": "SHORTKEY-3"})
    fake.keys["SHORTKEY-3"] = {"tls": {"key": "short"}}
    with caplog.at_level(logging.WARNING):
        profiles = await fetch_profiles(client_session, TOKEN, "EU")
    assert len(profiles) == 2  # noqa: PLR2004
    assert "Skipping appliance NOKEY-1" in caplog.text
    assert "Skipping appliance BROKEN-2" in caplog.text
    assert "Skipping appliance SHORTKEY-3" in caplog.text


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ("wrong_region", "wrong region 'EU'"),
        ("server_error", r"Listing the paired appliances failed \(500\)"),
        ("empty", "no appliances on this account"),
        ("none_usable", "couldn't fetch any of their profiles"),
    ],
)
async def test_fetch_failures(
    client_session: aiohttp.ClientSession, fake: FakeAccount, setup: str, message: str
) -> None:
    if setup == "wrong_region":
        fake.paired_status = 401
    elif setup == "server_error":
        fake.paired_status = 500
    elif setup == "empty":
        fake.appliances = [{"haId": "DEMO-1", "isDemo": True}]
    else:
        fake.keys = {}
    with pytest.raises(AccountError, match=message):
        await fetch_profiles(client_session, TOKEN, "EU")
