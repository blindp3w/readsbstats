"""Per-client bearer-token auth for the MCP server (ADR-0022, Decision 3).

The tokens file stores ``<name> <scopes> <sha256>`` lines, never a token. The
middleware hashes the presented token, compares it against every entry with
``hmac.compare_digest`` and stores the matched :class:`Client` in the ASGI
scope for tools to read. Fails closed: with no entries every request is 401.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from dataclasses import dataclass

from mcp.server.mcpserver.exceptions import ToolError

log = logging.getLogger(__name__)

SCOPES = frozenset({"read", "watchlist"})
NAME_RE = re.compile(r"^[a-z0-9-]{1,32}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")

_WARN_INTERVAL_S = 60.0
_now = time.monotonic
_last_warn = float("-inf")


@dataclass(frozen=True)
class Client:
    name: str
    scopes: frozenset[str]


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_scopes(spec: str) -> frozenset[str]:
    """``"read,watchlist"`` -> frozenset; ``watchlist`` implies ``read``."""
    parts = [p.strip() for p in spec.split(",")]
    if not spec.strip() or any(not p for p in parts):
        raise ValueError(f"empty scope in {spec!r}; use a comma-separated list of {sorted(SCOPES)}")
    unknown = [p for p in parts if p not in SCOPES]
    if unknown:
        raise ValueError(f"unknown scope {unknown[0]!r}; valid scopes: {', '.join(sorted(SCOPES))}")
    scopes = set(parts)
    if "watchlist" in scopes:
        scopes.add("read")
    return frozenset(scopes)


def parse_tokens(text: str) -> dict[str, Client]:
    """Parse the tokens file into ``{sha256: Client}``; ValueError("line N: ...")."""
    out: dict[str, Client] = {}
    names: set[str] = set()
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 3:
            raise ValueError(f"line {n}: expected '<name> <scopes> <sha256>'")
        name, spec, digest = fields
        if not NAME_RE.match(name):
            raise ValueError(f"line {n}: invalid name {name!r} (use a-z, 0-9, '-', max 32)")
        try:
            scopes = normalize_scopes(spec)
        except ValueError as exc:
            raise ValueError(f"line {n}: {exc}") from None
        if not HASH_RE.match(digest):
            raise ValueError(f"line {n}: invalid sha256 (need 64 lowercase hex characters)")
        if name in names:
            raise ValueError(f"line {n}: duplicate name {name!r}")
        if digest in out:
            raise ValueError(f"line {n}: duplicate hash")
        names.add(name)
        out[digest] = Client(name, scopes)
    return out


def load_tokens(path: str) -> dict[str, Client]:
    """Load the tokens file. Missing -> {}; group/world-writable -> ValueError."""
    try:
        f = open(path, encoding="utf-8")
    except FileNotFoundError:
        return {}
    with f:
        # fstat on the open handle: no window between the check and the read.
        if os.fstat(f.fileno()).st_mode & 0o022:
            raise ValueError(f"{path} is group- or world-writable; run chmod go-w on it")
        return parse_tokens(f.read())


def require_scope(client: Client | None, scope: str) -> None:
    if client is None or scope not in client.scopes:
        raise ToolError(f"this token lacks the '{scope}' scope")


def client_from_scope(scope: dict) -> Client | None:
    return scope.get("state", {}).get("mcp_client")


def _warn_failed_auth(scope: dict) -> None:
    """Log a rejected request at most once per minute (never the credentials)."""
    global _last_warn
    now = _now()
    if now - _last_warn < _WARN_INTERVAL_S:
        return
    _last_warn = now
    peer = scope.get("client")
    log.warning("rejected request with missing or invalid bearer token from %s",
                peer[0] if peer else "unknown")


class BearerAuthMiddleware:
    """Pure ASGI middleware; non-http scopes (lifespan) pass straight through."""

    def __init__(self, app, tokens: dict[str, Client]):
        self.app = app
        self.tokens = tokens

    def _match(self, token: str) -> Client | None:
        if not token:
            return None
        digest = hash_token(token)
        found = None
        for key, client in self.tokens.items():   # no early break: constant work
            if hmac.compare_digest(key, digest):
                found = client
        return found

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        client = None
        for k, v in scope.get("headers") or []:
            if k == b"authorization":
                scheme, sep, token = v.decode("latin-1").partition(" ")
                if sep and scheme.lower() == "bearer":
                    client = self._match(token)
                break
        if client is None:
            _warn_failed_auth(scope)
            body = json.dumps({"error": "unauthorized"}).encode()
            await send({"type": "http.response.start", "status": 401, "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b"Bearer"),
            ]})
            await send({"type": "http.response.body", "body": body})
            return
        scope.setdefault("state", {})["mcp_client"] = client
        await self.app(scope, receive, send)
