import hashlib
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet
from mcp.server.auth.provider import AuthorizationParams, OAuthClientInformationFull, RegistrationError
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.routing import Route

from oauth_provider import (
    DEFAULT_SCOPE,
    GaleOAuthProvider,
    _is_allowed_chatgpt_redirect,
    hash_login_secret,
    verify_login_secret,
)


ISSUER = "https://memory.mininicole.com/"
RESOURCE = "https://memory.mininicole.com/"
REDIRECT = "https://chatgpt.com/connector/oauth/test_callback-123"


def _client(client_id="chatgpt-test", secret="registered-client-secret"):
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret=secret,
        redirect_uris=[AnyUrl(REDIRECT)],
        token_endpoint_auth_method="client_secret_post",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=DEFAULT_SCOPE,
        client_name="ChatGPT test client",
    )


def _provider(tmp_path, login_secret="correct horse battery staple oauth"):
    return GaleOAuthProvider(
        database=tmp_path / "oauth.db",
        issuer_url=ISSUER,
        resource_url=RESOURCE,
        login_secret_hash=hash_login_secret(login_secret),
        storage_key=Fernet.generate_key().decode("ascii"),
    )


def test_login_secret_uses_scrypt_and_constant_verification():
    encoded = hash_login_secret("correct horse battery staple oauth")
    assert encoded.startswith("scrypt$")
    assert verify_login_secret("correct horse battery staple oauth", encoded)
    assert not verify_login_secret("incorrect horse battery staple oauth", encoded)
    assert not verify_login_secret("anything", "malformed")


@pytest.mark.parametrize(
    ("uri", "allowed"),
    [
        ("https://chatgpt.com/connector_platform_oauth_redirect", True),
        (REDIRECT, True),
        ("http://chatgpt.com/connector/oauth/test", False),
        ("https://evil.example/connector/oauth/test", False),
        ("https://chatgpt.com/connector/oauth/test/extra", False),
        ("https://chatgpt.com/connector/oauth/test#fragment", False),
    ],
)
def test_chatgpt_callback_allowlist(uri, allowed):
    assert _is_allowed_chatgpt_redirect(uri) is allowed


@pytest.mark.asyncio
async def test_registration_rejects_non_chatgpt_redirect(tmp_path):
    provider = _provider(tmp_path)
    client = _client()
    client.redirect_uris = [AnyUrl("https://evil.example/callback")]
    with pytest.raises(RegistrationError) as exc:
        await provider.register_client(client)
    assert exc.value.error == "invalid_redirect_uri"


@pytest.mark.asyncio
async def test_registration_limit_blocks_unbounded_client_growth(tmp_path):
    provider = _provider(tmp_path)
    provider.max_registered_clients = 1
    await provider.register_client(_client(client_id="first"))
    with pytest.raises(RegistrationError) as exc:
        await provider.register_client(_client(client_id="second"))
    assert exc.value.error == "invalid_client_metadata"


@pytest.mark.asyncio
async def test_full_consent_code_token_refresh_and_revoke_flow(tmp_path):
    login_secret = "correct horse battery staple oauth"
    provider = _provider(tmp_path, login_secret)
    client = _client()
    await provider.register_client(client)

    loaded_client = await provider.get_client(client.client_id)
    assert loaded_client is not None
    assert loaded_client.client_secret == client.client_secret
    database_bytes = (tmp_path / "oauth.db").read_bytes()
    assert client.client_secret.encode("utf-8") not in database_bytes

    challenge = hashlib.sha256(b"pkce-verifier").digest()
    challenge_text = __import__("base64").urlsafe_b64encode(challenge).decode().rstrip("=")
    consent_url = await provider.authorize(
        loaded_client,
        AuthorizationParams(
            state="opaque-state",
            scopes=[DEFAULT_SCOPE],
            code_challenge=challenge_text,
            redirect_uri=AnyUrl(REDIRECT),
            redirect_uri_provided_explicitly=True,
            resource=RESOURCE,
        ),
    )
    request_id = parse_qs(urlparse(consent_url).query)["request"][0]
    app = Starlette(
        routes=[Route("/oauth/consent", provider.consent_response, methods=["GET", "POST"])]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=ISSUER,
        follow_redirects=False,
    ) as http:
        page = await http.get("/oauth/consent", params={"request": request_id})
        assert page.status_code == 200
        assert "ChatGPT test client" in page.text
        assert page.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert (
            "form-action 'self' https://chatgpt.com"
            in page.headers["content-security-policy"]
        )

        rejected = await http.post(
            "/oauth/consent",
            data={
                "request": request_id,
                "login_secret": "wrong secret value with enough length",
                "decision": "allow",
            },
        )
        assert rejected.status_code == 401

        accepted = await http.post(
            "/oauth/consent",
            data={
                "request": request_id,
                "login_secret": login_secret,
                "decision": "allow",
            },
        )
        duplicate = await http.post(
            "/oauth/consent",
            data={
                "request": request_id,
                "login_secret": login_secret,
                "decision": "allow",
            },
        )
        unauthorized_duplicate = await http.post(
            "/oauth/consent",
            data={
                "request": request_id,
                "login_secret": "wrong secret value with enough length",
                "decision": "allow",
            },
        )
    assert accepted.status_code == 302
    assert duplicate.status_code == 302
    assert duplicate.headers["location"] == accepted.headers["location"]
    assert unauthorized_duplicate.status_code == 400
    with provider._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM oauth_codes").fetchone()[0] == 1
    callback = urlparse(accepted.headers["location"])
    assert f"{callback.scheme}://{callback.netloc}{callback.path}" == REDIRECT
    callback_query = parse_qs(callback.query)
    assert callback_query["state"] == ["opaque-state"]
    code = callback_query["code"][0]

    auth_code = await provider.load_authorization_code(loaded_client, code)
    assert auth_code is not None
    assert auth_code.resource == RESOURCE
    tokens = await provider.exchange_authorization_code(loaded_client, auth_code)
    assert tokens.token_type == "Bearer"
    assert tokens.expires_in == 3600
    access = await provider.load_access_token(tokens.access_token)
    assert access is not None
    assert access.resource == RESOURCE
    assert access.scopes == [DEFAULT_SCOPE]
    assert access.claims["aud"] == RESOURCE

    refresh = await provider.load_refresh_token(loaded_client, tokens.refresh_token)
    assert refresh is not None
    rotated = await provider.exchange_refresh_token(loaded_client, refresh, [DEFAULT_SCOPE])
    assert await provider.load_access_token(tokens.access_token) is None
    assert await provider.load_refresh_token(loaded_client, tokens.refresh_token) is None
    rotated_access = await provider.load_access_token(rotated.access_token)
    assert rotated_access is not None
    await provider.revoke_token(rotated_access)
    assert await provider.load_access_token(rotated.access_token) is None

    final_db = (tmp_path / "oauth.db").read_bytes()
    assert tokens.access_token.encode("utf-8") not in final_db
    assert tokens.refresh_token.encode("utf-8") not in final_db


@pytest.mark.asyncio
async def test_consent_locks_after_five_failures(tmp_path):
    provider = _provider(tmp_path)
    client = _client()
    await provider.register_client(client)
    url = await provider.authorize(
        client,
        AuthorizationParams(
            state=None,
            scopes=[DEFAULT_SCOPE],
            code_challenge="challenge",
            redirect_uri=AnyUrl(REDIRECT),
            redirect_uri_provided_explicitly=True,
            resource=RESOURCE,
        ),
    )
    request_id = parse_qs(urlparse(url).query)["request"][0]
    app = Starlette(
        routes=[Route("/oauth/consent", provider.consent_response, methods=["POST"])]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=ISSUER,
    ) as http:
        responses = [
            await http.post(
                "/oauth/consent",
                data={
                    "request": request_id,
                    "login_secret": "wrong secret value with enough length",
                    "decision": "allow",
                },
            )
            for _ in range(5)
        ]
    assert [response.status_code for response in responses] == [401, 401, 401, 401, 429]
