"""Minimal single-user OAuth 2.1 provider for the hosted Gale Memory MCP.

The MCP SDK owns protocol validation, DCR, PKCE verification, metadata, and the
Bearer challenge.  This module owns Gale-specific consent and durable storage.
Client secrets are encrypted at rest; authorization codes and tokens are only
stored as SHA-256 digests.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    OAuthClientInformationFull,
    OAuthToken,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response


DEFAULT_SCOPE = "gale.memory"
_CALLBACK_ID_PATH = re.compile(r"^/connector/oauth/[A-Za-z0-9_-]+$")
_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_login_secret(secret: str) -> str:
    """Return a portable scrypt hash without logging the input."""
    if len(secret) < 24:
        raise ValueError("OAuth login secret must contain at least 24 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        secret.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=32,
    )
    return "$".join(
        (
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            base64.urlsafe_b64encode(salt).decode("ascii").rstrip("="),
            base64.urlsafe_b64encode(digest).decode("ascii").rstrip("="),
        )
    )


def verify_login_secret(secret: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt_text, expected_text = encoded.split("$", 5)
        if scheme != "scrypt":
            return False
        pad = lambda value: value + "=" * (-len(value) % 4)
        salt = base64.urlsafe_b64decode(pad(salt_text))
        expected = base64.urlsafe_b64decode(pad(expected_text))
        actual = hashlib.scrypt(
            secret.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _is_allowed_chatgpt_redirect(uri: str) -> bool:
    parsed = urlparse(uri)
    if parsed.scheme != "https" or parsed.hostname != "chatgpt.com":
        return False
    if parsed.port not in (None, 443) or parsed.fragment or parsed.username or parsed.password:
        return False
    return parsed.path == "/connector_platform_oauth_redirect" or bool(
        _CALLBACK_ID_PATH.fullmatch(parsed.path)
    )


@dataclass(frozen=True)
class OAuthRuntime:
    provider: "GaleOAuthProvider"
    settings: AuthSettings


class GaleOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """SQLite-backed OAuth provider limited to ChatGPT hosted callbacks."""

    def __init__(
        self,
        *,
        database: Path,
        issuer_url: str,
        resource_url: str,
        login_secret_hash: str,
        storage_key: str,
        scope: str = DEFAULT_SCOPE,
        access_token_ttl: int = 3600,
        refresh_token_ttl: int = 30 * 24 * 3600,
        authorization_code_ttl: int = 300,
        consent_request_ttl: int = 600,
        max_registered_clients: int = 32,
    ) -> None:
        self.database = database
        # Pydantic's AnyHttpUrl canonicalizes an origin-only URL with a trailing
        # slash.  Keep the provider's exact comparisons on the same canonical
        # identifier because OAuth resource and issuer matching is byte-exact.
        self.issuer_url = issuer_url.rstrip("/") + "/"
        self.resource_url = resource_url.rstrip("/") + "/"
        self.login_secret_hash = login_secret_hash
        self.scope = scope
        self.access_token_ttl = access_token_ttl
        self.refresh_token_ttl = refresh_token_ttl
        self.authorization_code_ttl = authorization_code_ttl
        self.consent_request_ttl = consent_request_ttl
        self.max_registered_clients = max_registered_clients
        # Browsers can submit the same consent form twice before the first 302
        # navigation wins.  Keep the successful redirect briefly in process so
        # an authenticated duplicate gets the same result without minting a
        # second authorization code or persisting the plaintext code.
        self._consent_redirect_replays: dict[str, tuple[int, str]] = {}
        try:
            self._fernet = Fernet(storage_key.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise ValueError("OMBRE_OAUTH_STORAGE_KEY is not a valid Fernet key") from exc
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS oauth_clients (
                    client_id TEXT PRIMARY KEY,
                    metadata_json TEXT NOT NULL,
                    secret_ciphertext TEXT,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_pending (
                    request_hash TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    state TEXT,
                    code_challenge TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    redirect_uri_explicit INTEGER NOT NULL,
                    resource TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS oauth_codes (
                    code_hash TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    code_challenge TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    redirect_uri_explicit INTEGER NOT NULL,
                    resource TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS oauth_tokens (
                    token_hash TEXT PRIMARY KEY,
                    token_kind TEXT NOT NULL CHECK(token_kind IN ('access', 'refresh')),
                    family_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_oauth_tokens_family
                    ON oauth_tokens(family_id);
                """
            )

    def _cleanup(self, connection: sqlite3.Connection) -> None:
        now = int(time.time())
        connection.execute("DELETE FROM oauth_pending WHERE expires_at < ?", (now,))
        connection.execute("DELETE FROM oauth_codes WHERE expires_at < ? OR used = 1", (now,))
        connection.execute("DELETE FROM oauth_tokens WHERE expires_at < ? OR revoked = 1", (now,))

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT metadata_json, secret_ciphertext FROM oauth_clients WHERE client_id = ?",
                (client_id,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["metadata_json"])
        encrypted = row["secret_ciphertext"]
        if encrypted:
            try:
                payload["client_secret"] = self._fernet.decrypt(
                    encrypted.encode("ascii")
                ).decode("utf-8")
            except InvalidToken:
                return None
        return OAuthClientInformationFull.model_validate(payload)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise RegistrationError("invalid_client_metadata", "client_id is required")
        redirects = [str(uri) for uri in (client_info.redirect_uris or [])]
        if not redirects or not all(_is_allowed_chatgpt_redirect(uri) for uri in redirects):
            raise RegistrationError(
                "invalid_redirect_uri",
                "Only documented ChatGPT hosted connector callbacks are allowed",
            )
        if client_info.token_endpoint_auth_method not in (
            "client_secret_post",
            "client_secret_basic",
        ):
            raise RegistrationError(
                "invalid_client_metadata",
                "token_endpoint_auth_method must use a confidential DCR client",
            )
        metadata = client_info.model_dump(mode="json")
        client_secret = metadata.pop("client_secret", None)
        if not client_secret:
            raise RegistrationError("invalid_client_metadata", "client_secret is required")
        ciphertext = self._fernet.encrypt(client_secret.encode("utf-8")).decode("ascii")
        with self._connect() as connection:
            self._cleanup(connection)
            exists = connection.execute(
                "SELECT 1 FROM oauth_clients WHERE client_id = ?",
                (client_info.client_id,),
            ).fetchone()
            count = connection.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0]
            if exists is None and count >= self.max_registered_clients:
                raise RegistrationError(
                    "invalid_client_metadata",
                    "OAuth client registration limit reached",
                )
            connection.execute(
                """
                INSERT INTO oauth_clients(client_id, metadata_json, secret_ciphertext, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(client_id) DO UPDATE SET
                    metadata_json=excluded.metadata_json,
                    secret_ciphertext=excluded.secret_ciphertext
                """,
                (
                    client_info.client_id,
                    json.dumps(metadata, separators=(",", ":"), sort_keys=True),
                    ciphertext,
                    int(time.time()),
                ),
            )

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        if params.resource != self.resource_url:
            from mcp.server.auth.provider import AuthorizeError

            raise AuthorizeError("invalid_request", "resource does not match Gale Memory")
        scopes = params.scopes or [self.scope]
        if set(scopes) != {self.scope}:
            from mcp.server.auth.provider import AuthorizeError

            raise AuthorizeError("invalid_scope", "unsupported scope")
        request_id = secrets.token_urlsafe(32)
        with self._connect() as connection:
            self._cleanup(connection)
            connection.execute(
                """
                INSERT INTO oauth_pending(
                    request_hash, client_id, scopes_json, state, code_challenge,
                    redirect_uri, redirect_uri_explicit, resource, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _digest(request_id),
                    client.client_id,
                    json.dumps(scopes),
                    params.state,
                    params.code_challenge,
                    str(params.redirect_uri),
                    int(params.redirect_uri_provided_explicitly),
                    params.resource,
                    int(time.time()) + self.consent_request_ttl,
                ),
            )
        return f"{self.issuer_url.rstrip('/')}/oauth/consent?request={request_id}"

    def _load_pending(self, request_id: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            self._cleanup(connection)
            return connection.execute(
                "SELECT * FROM oauth_pending WHERE request_hash = ?",
                (_digest(request_id),),
            ).fetchone()

    def _remember_consent_redirect(self, request_id: str, location: str) -> None:
        now = int(time.time())
        self._consent_redirect_replays = {
            key: value
            for key, value in self._consent_redirect_replays.items()
            if value[0] >= now
        }
        self._consent_redirect_replays[_digest(request_id)] = (
            now + self.authorization_code_ttl,
            location,
        )

    def _load_consent_redirect(self, request_id: str) -> str | None:
        now = int(time.time())
        request_hash = _digest(request_id)
        replay = self._consent_redirect_replays.get(request_hash)
        if replay is None:
            return None
        expires_at, location = replay
        if expires_at < now:
            self._consent_redirect_replays.pop(request_hash, None)
            return None
        return location

    def _consent_headers(self) -> dict[str, str]:
        return {
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; "
                # The form posts to self, then OAuth redirects to the registered
                # ChatGPT callback. Browsers enforce form-action across that
                # redirect chain, so the callback origin must be explicit. DCR
                # still restricts the exact allowed callback paths separately.
                "form-action 'self' https://chatgpt.com; "
                "base-uri 'none'; frame-ancestors 'none'"
            ),
        }

    def _render_consent(self, request_id: str, client_name: str, error: str = "") -> str:
        error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>连接 Gale Memory</title><style>
body{{font-family:system-ui,sans-serif;max-width:34rem;margin:8vh auto;padding:1.5rem;color:#202124}}
.card{{border:1px solid #ddd;border-radius:16px;padding:1.5rem;box-shadow:0 6px 24px #0001}}
input{{box-sizing:border-box;width:100%;padding:.8rem;margin:.7rem 0;border:1px solid #aaa;border-radius:8px}}
button{{padding:.75rem 1.1rem;border:0;border-radius:8px;background:#222;color:#fff;margin-right:.5rem}}
button.deny{{background:#eee;color:#222}} .error{{color:#b00020}}
</style></head><body><div class="card"><h1>连接 Gale Memory</h1>
<p><strong>{html.escape(client_name or 'ChatGPT')}</strong> 请求访问你的 Gale Memory。</p>
<p>授权后可通过 MCP 使用当前已发布的记忆工具。请确认这是你刚刚发起的连接。</p>{error_html}
<form method="post" action="/oauth/consent">
<input type="hidden" name="request" value="{html.escape(request_id, quote=True)}">
<label>Gale Memory 连接口令<input type="password" name="login_secret" autocomplete="current-password" required></label>
<button type="submit" name="decision" value="allow">允许连接</button>
<button type="submit" class="deny" name="decision" value="deny" formnovalidate>拒绝</button>
</form></div></body></html>"""

    async def consent_response(self, request: Request) -> Response:
        if request.method == "GET":
            request_id = request.query_params.get("request", "")
            row = self._load_pending(request_id) if request_id else None
            if row is None:
                return HTMLResponse(
                    "Authorization request is missing or expired.",
                    status_code=400,
                    headers=self._consent_headers(),
                )
            client = await self.get_client(row["client_id"])
            return HTMLResponse(
                self._render_consent(request_id, client.client_name if client else "ChatGPT"),
                headers=self._consent_headers(),
            )

        form = await request.form()
        request_id = str(form.get("request", ""))
        row = self._load_pending(request_id) if request_id else None
        if row is None:
            replay = self._load_consent_redirect(request_id) if request_id else None
            supplied = str(form.get("login_secret", ""))
            if (
                replay is not None
                and form.get("decision") == "allow"
                and verify_login_secret(supplied, self.login_secret_hash)
            ):
                return RedirectResponse(
                    replay,
                    status_code=302,
                    headers={"Cache-Control": "no-store"},
                )
            return HTMLResponse(
                "Authorization request is missing or expired.",
                status_code=400,
                headers=self._consent_headers(),
            )
        if form.get("decision") == "deny":
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM oauth_pending WHERE request_hash = ?",
                    (_digest(request_id),),
                )
            return RedirectResponse(
                construct_redirect_uri(
                    row["redirect_uri"],
                    error="access_denied",
                    state=row["state"],
                ),
                status_code=302,
                headers={"Cache-Control": "no-store"},
            )

        supplied = str(form.get("login_secret", ""))
        if not verify_login_secret(supplied, self.login_secret_hash):
            with self._connect() as connection:
                connection.execute(
                    "UPDATE oauth_pending SET attempts = attempts + 1 WHERE request_hash = ?",
                    (_digest(request_id),),
                )
                attempts = connection.execute(
                    "SELECT attempts FROM oauth_pending WHERE request_hash = ?",
                    (_digest(request_id),),
                ).fetchone()[0]
                if attempts >= 5:
                    connection.execute(
                        "DELETE FROM oauth_pending WHERE request_hash = ?",
                        (_digest(request_id),),
                    )
            if attempts >= 5:
                return HTMLResponse(
                    "Too many failed attempts; start the connection again.",
                    status_code=429,
                    headers=self._consent_headers(),
                )
            client = await self.get_client(row["client_id"])
            return HTMLResponse(
                self._render_consent(
                    request_id,
                    client.client_name if client else "ChatGPT",
                    "连接口令不正确。",
                ),
                status_code=401,
                headers=self._consent_headers(),
            )

        code = secrets.token_urlsafe(32)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            deleted = connection.execute(
                "DELETE FROM oauth_pending WHERE request_hash = ?",
                (_digest(request_id),),
            ).rowcount
            if deleted != 1:
                connection.rollback()
                return HTMLResponse(
                    "Authorization request was already used.",
                    status_code=400,
                    headers=self._consent_headers(),
                )
            connection.execute(
                """
                INSERT INTO oauth_codes(
                    code_hash, client_id, scopes_json, code_challenge,
                    redirect_uri, redirect_uri_explicit, resource, subject, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _digest(code),
                    row["client_id"],
                    row["scopes_json"],
                    row["code_challenge"],
                    row["redirect_uri"],
                    row["redirect_uri_explicit"],
                    row["resource"],
                    "gale-owner",
                    int(time.time()) + self.authorization_code_ttl,
                ),
            )
        redirect_location = construct_redirect_uri(
            row["redirect_uri"],
            code=code,
            state=row["state"],
        )
        self._remember_consent_redirect(request_id, redirect_location)
        return RedirectResponse(
            redirect_location,
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM oauth_codes WHERE code_hash = ? AND used = 0",
                (_digest(authorization_code),),
            ).fetchone()
        if row is None or row["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=json.loads(row["scopes_json"]),
            expires_at=row["expires_at"],
            client_id=row["client_id"],
            code_challenge=row["code_challenge"],
            redirect_uri=row["redirect_uri"],
            redirect_uri_provided_explicitly=bool(row["redirect_uri_explicit"]),
            resource=row["resource"],
            subject=row["subject"],
        )

    def _issue_tokens(
        self,
        connection: sqlite3.Connection,
        *,
        client_id: str,
        scopes: list[str],
        resource: str,
        subject: str,
        family_id: str | None = None,
    ) -> OAuthToken:
        now = int(time.time())
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(48)
        family = family_id or secrets.token_hex(16)
        rows = (
            (
                _digest(access),
                "access",
                family,
                client_id,
                json.dumps(scopes),
                resource,
                subject,
                now + self.access_token_ttl,
                now,
            ),
            (
                _digest(refresh),
                "refresh",
                family,
                client_id,
                json.dumps(scopes),
                resource,
                subject,
                now + self.refresh_token_ttl,
                now,
            ),
        )
        connection.executemany(
            """
            INSERT INTO oauth_tokens(
                token_hash, token_kind, family_id, client_id, scopes_json,
                resource, subject, expires_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self.access_token_ttl,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE oauth_codes SET used = 1
                WHERE code_hash = ? AND client_id = ? AND used = 0
                """,
                (_digest(authorization_code.code), client.client_id),
            ).rowcount
            if changed != 1:
                raise TokenError("invalid_grant", "authorization code was already used")
            return self._issue_tokens(
                connection,
                client_id=client.client_id,
                scopes=authorization_code.scopes,
                resource=authorization_code.resource or self.resource_url,
                subject=authorization_code.subject or "gale-owner",
            )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM oauth_tokens
                WHERE token_hash = ? AND token_kind = 'refresh' AND revoked = 0
                """,
                (_digest(refresh_token),),
            ).fetchone()
        if row is None or row["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes_json"]),
            expires_at=row["expires_at"],
            subject=row["subject"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM oauth_tokens
                WHERE token_hash = ? AND token_kind = 'refresh' AND revoked = 0
                """,
                (_digest(refresh_token.token),),
            ).fetchone()
            if row is None:
                raise TokenError("invalid_grant", "refresh token was already used")
            connection.execute(
                "UPDATE oauth_tokens SET revoked = 1 WHERE family_id = ?",
                (row["family_id"],),
            )
            return self._issue_tokens(
                connection,
                client_id=client.client_id,
                scopes=scopes,
                resource=row["resource"],
                subject=row["subject"],
                family_id=row["family_id"],
            )

    async def load_access_token(self, token: str) -> AccessToken | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM oauth_tokens
                WHERE token_hash = ? AND token_kind = 'access' AND revoked = 0
                """,
                (_digest(token),),
            ).fetchone()
        if row is None or row["resource"] != self.resource_url:
            return None
        return AccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes_json"]),
            expires_at=row["expires_at"],
            resource=row["resource"],
            subject=row["subject"],
            claims={
                "iss": self.issuer_url,
                "aud": row["resource"],
                "sub": row["subject"],
            },
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT family_id FROM oauth_tokens WHERE token_hash = ?",
                (_digest(token.token),),
            ).fetchone()
            if row is not None:
                connection.execute(
                    "UPDATE oauth_tokens SET revoked = 1 WHERE family_id = ?",
                    (row["family_id"],),
                )


def build_oauth_runtime_from_env() -> OAuthRuntime | None:
    if os.environ.get("OMBRE_OAUTH_ENABLED", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return None
    issuer = os.environ.get("OMBRE_OAUTH_ISSUER_URL", "").strip().rstrip("/")
    resource = os.environ.get("OMBRE_OAUTH_RESOURCE_URL", "").strip().rstrip("/")
    database = os.environ.get("OMBRE_OAUTH_DB_PATH", "").strip()
    login_hash = os.environ.get("OMBRE_OAUTH_LOGIN_SECRET_HASH", "").strip()
    storage_key = os.environ.get("OMBRE_OAUTH_STORAGE_KEY", "").strip()
    if not all((issuer, resource, database, login_hash, storage_key)):
        raise RuntimeError("OAuth is enabled but one or more OMBRE_OAUTH_* values are missing")
    if not issuer.startswith("https://") or not resource.startswith("https://"):
        raise RuntimeError("OAuth issuer and resource URLs must use HTTPS")
    provider = GaleOAuthProvider(
        database=Path(database),
        issuer_url=issuer,
        resource_url=resource,
        login_secret_hash=login_hash,
        storage_key=storage_key,
    )
    settings = AuthSettings(
        issuer_url=AnyHttpUrl(issuer),
        resource_server_url=AnyHttpUrl(resource),
        required_scopes=[DEFAULT_SCOPE],
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=[DEFAULT_SCOPE],
            default_scopes=[DEFAULT_SCOPE],
        ),
        revocation_options=RevocationOptions(enabled=True),
    )
    return OAuthRuntime(
        provider=provider,
        settings=settings,
    )
