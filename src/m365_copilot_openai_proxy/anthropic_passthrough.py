"""Forward non-m365 model requests straight to the real Anthropic API.

Default credential is the Claude Code OAuth login (`~/.claude/.credentials.json` -> `claudeAiOauth`),
so it is free (uses the subscription, not API credits). OAuth tokens require the
`anthropic-beta: oauth-2025-04-20` flag and `Authorization: Bearer`. An `M365_ANTHROPIC_KEY` override
switches to standard `x-api-key` auth (which DOES consume API credits).

Only the minimal forward is implemented (header allowlist + SSE streaming + OAuth refresh). The
sol proxy's security layer (Keycloak/jwt-gateway, tier enforcement, client_id exchange, anti-Cloudflare
TLS) is intentionally NOT reproduced.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi.responses import JSONResponse, StreamingResponse

_OAUTH_TOKEN_ENDPOINT = "https://platform.claude.com/v1/oauth/token"
# Claude Code's public OAuth client id (same one ccusage / claude-code-router use).
_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_OAUTH_BETA = "oauth-2025-04-20"
_REFRESH_BUFFER_MS = 60_000  # refresh a bit before the token actually expires


def _creds_path(settings: Any) -> Path:
    configured = (getattr(settings, "anthropic_creds_file", "") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".claude" / ".credentials.json"


def credential_available(settings: Any) -> bool:
    """True if passthrough has *some* usable credential (API-key override or an OAuth creds file)."""
    if (getattr(settings, "anthropic_key", "") or "").strip():
        return True
    try:
        return bool(
            json.loads(_creds_path(settings).read_text("utf-8"))
            .get("claudeAiOauth", {})
            .get("accessToken")
        )
    except Exception:
        return False


def _refresh_oauth(refresh_token: str) -> tuple[str, str, int]:
    resp = httpx.post(
        _OAUTH_TOKEN_ENDPOINT,
        json={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": _OAUTH_CLIENT_ID,
        },
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    expires_in = int(data.get("expires_in", 3600))
    print(
        f"   OAuth refresh: ok (status {resp.status_code}, new token valid ~{expires_in // 60}m)"
    )
    return (
        data["access_token"],
        data.get("refresh_token", refresh_token),
        int(time.time() * 1000) + expires_in * 1000,
    )


def _oauth_access_token(settings: Any) -> str:
    """Read the Claude Code OAuth access token, refreshing (and writing back) if near expiry."""
    path = _creds_path(settings)
    raw: dict = json.loads(path.read_text("utf-8"))
    oauth = raw.get("claudeAiOauth") or {}
    access = oauth.get("accessToken", "")
    refresh = oauth.get("refreshToken", "")
    expires_at = int(oauth.get("expiresAt") or 0)

    if access and (
        not expires_at or time.time() * 1000 < expires_at - _REFRESH_BUFFER_MS
    ):
        left = int((expires_at / 1000 - time.time()) / 60) if expires_at else None
        print(
            f"   OAuth token: valid ({left}m left), reusing"
            if left is not None
            else "   OAuth token: valid (no expiry), reusing"
        )
        return access
    if not refresh:
        if access:
            print(
                "   OAuth token: expired and no refresh_token — trying the stale token anyway"
            )
            return access  # no refresh token but we have an access token — try it
        raise RuntimeError("no OAuth access/refresh token in credentials file")

    print(
        "   OAuth token: near expiry/expired -> refreshing via platform.claude.com ..."
    )
    new_access, new_refresh, new_expires = _refresh_oauth(refresh)
    # Write back preserving every sibling field Claude Code manages (scopes, subscriptionType, ...).
    oauth["accessToken"], oauth["refreshToken"], oauth["expiresAt"] = (
        new_access,
        new_refresh,
        new_expires,
    )
    raw["claudeAiOauth"] = oauth
    try:
        path.write_text(json.dumps(raw), "utf-8")
        os.chmod(path, 0o600)
        print(f"   OAuth token: refreshed + written back to {path}")
    except Exception as exc:
        print(f"   OAuth token: refreshed (in-memory only, writeback failed: {exc})")
    return new_access


def _upstream_headers(settings: Any, client_headers: Any) -> dict[str, str]:
    def ch(name: str) -> str:
        return (client_headers.get(name) or "").strip()

    version = ch("anthropic-version") or settings.anthropic_version
    headers = {
        "anthropic-version": version,
        "Accept": "application/json, text/event-stream",
        "Accept-Encoding": "identity",  # no gzip -> clean SSE passthrough
        "Content-Type": "application/json",
        "User-Agent": "m365-copilot-proxy/passthrough",
    }
    api_key = (getattr(settings, "anthropic_key", "") or "").strip()
    if api_key:
        headers["x-api-key"] = api_key
        beta = ch("anthropic-beta")
        if beta:
            headers["anthropic-beta"] = beta
    else:
        token = _oauth_access_token(settings)
        headers["Authorization"] = f"Bearer {token}"
        # OAuth tokens require this beta flag; merge with any beta the client already asked for.
        beta = ch("anthropic-beta")
        headers["anthropic-beta"] = (
            f"{beta},{_OAUTH_BETA}"
            if beta and _OAUTH_BETA not in beta
            else (beta or _OAUTH_BETA)
        )
    return headers


async def forward_messages(settings: Any, body: bytes, client_headers: Any):
    """Reverse-proxy a /v1/messages request to the real Anthropic API, streaming the response back."""
    try:
        headers = _upstream_headers(settings, client_headers)
    except Exception as exc:
        return JSONResponse(
            {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": f"passthrough credential error: {exc}",
                },
            },
            status_code=502,
        )

    url = settings.anthropic_upstream.rstrip("/") + "/v1/messages"
    mode = "x-api-key" if "x-api-key" in headers else "oauth"
    client = httpx.AsyncClient(timeout=None)
    try:
        req = client.build_request("POST", url, content=body, headers=headers)
        resp = await client.send(req, stream=True)
    except Exception as exc:
        await client.aclose()
        print(f"[502] passthrough upstream error ({url}, auth={mode}): {exc}")
        return JSONResponse(
            {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": f"passthrough upstream error: {exc}",
                },
            },
            status_code=502,
        )
    print(f"   passthrough -> {url} (auth={mode}) status={resp.status_code}")

    async def body_iter():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        body_iter(),
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )
