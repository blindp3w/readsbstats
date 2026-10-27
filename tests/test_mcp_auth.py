"""Tests for readsbstats.mcp_server.auth and the MCP_* config section (ADR-0022)."""

import hashlib
import importlib
import logging
import os
import re
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from readsbstats.mcp_server import auth
from readsbstats.mcp_server.auth import (
    BearerAuthMiddleware, Client, client_from_scope, hash_token, load_tokens,
    normalize_scopes, parse_tokens, require_scope,
)

H1 = hashlib.sha256(b"tok-one").hexdigest()
H2 = hashlib.sha256(b"tok-two").hexdigest()
PKG = Path(auth.__file__).parent


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    # Reset the failed-auth log throttle between tests.
    monkeypatch.setattr(auth, "_last_warn", float("-inf"))
    yield


# ---- hashing / scopes --------------------------------------------------------
class TestHashAndScopes:
    def test_hash_token(self):
        assert hash_token("tok-one") == H1
        assert re.fullmatch(r"[0-9a-f]{64}", hash_token("é"))

    def test_normalize_scopes(self):
        assert normalize_scopes("read") == frozenset({"read"})
        assert normalize_scopes("read,watchlist") == frozenset({"read", "watchlist"})

    def test_watchlist_implies_read(self):
        assert normalize_scopes("watchlist") == frozenset({"read", "watchlist"})

    @pytest.mark.parametrize("spec", ["", "admin", "read,admin", ",", "read,,"])
    def test_bad_scopes(self, spec):
        with pytest.raises(ValueError):
            normalize_scopes(spec)

    def test_scopes_const(self):
        assert auth.SCOPES == frozenset({"read", "watchlist"})


# ---- parse_tokens ------------------------------------------------------------
class TestParseTokens:
    def test_happy_path_with_comments_and_blanks(self):
        text = f"# header\n\nalice read {H1}\n  # indented comment\nbob watchlist {H2}\n"
        out = parse_tokens(text)
        assert out[H1] == Client("alice", frozenset({"read"}))
        assert out[H2] == Client("bob", frozenset({"read", "watchlist"}))

    def test_empty(self):
        assert parse_tokens("") == {}
        assert parse_tokens("# only a comment\n") == {}

    @pytest.mark.parametrize("line", [
        "alice read",                       # too few fields
        f"alice read {H1} extra",           # too many
        f"Alice read {H1}",                 # uppercase name
        f"a_b read {H1}",                   # underscore
        f"{'x' * 33} read {H1}",            # name too long
        f"alice admin {H1}",                # bad scope
        f"alice read {H1.upper()}",         # uppercase hash
        "alice read deadbeef",              # short hash
    ])
    def test_malformed_line_reports_line_number(self, line):
        with pytest.raises(ValueError, match=r"line 3: "):
            parse_tokens(f"# c\n\n{line}\n")

    def test_duplicate_name(self):
        with pytest.raises(ValueError, match=r"line 2: .*duplicate"):
            parse_tokens(f"alice read {H1}\nalice read {H2}\n")

    def test_duplicate_hash(self):
        with pytest.raises(ValueError, match=r"line 2: .*duplicate"):
            parse_tokens(f"alice read {H1}\nbob read {H1}\n")


# ---- load_tokens -------------------------------------------------------------
class TestLoadTokens:
    def _write(self, tmp_path, mode):
        p = tmp_path / "tokens"
        p.write_text(f"alice read {H1}\n")
        os.chmod(p, mode)
        return str(p)

    def test_missing_file_is_empty(self, tmp_path):
        assert load_tokens(str(tmp_path / "nope")) == {}

    def test_ok_modes(self, tmp_path):
        for mode in (0o640, 0o600, 0o644):
            assert H1 in load_tokens(self._write(tmp_path, mode))

    def test_read_only_credential_copy_loads(self, tmp_path):
        # systemd LoadCredential= hands the service a 0400 copy.
        assert H1 in load_tokens(self._write(tmp_path, 0o400))

    @pytest.mark.parametrize("mode", [0o664, 0o646, 0o660, 0o666])
    def test_writable_rejected(self, tmp_path, mode):
        with pytest.raises(ValueError, match="writable"):
            load_tokens(self._write(tmp_path, mode))

    def test_malformed_content_raises(self, tmp_path):
        p = tmp_path / "tokens"
        p.write_text("garbage\n")
        os.chmod(p, 0o640)
        with pytest.raises(ValueError, match="line 1"):
            load_tokens(str(p))


# ---- require_scope / client_from_scope --------------------------------------
class TestScopeHelpers:
    def test_require_scope_ok(self):
        require_scope(Client("a", frozenset({"read"})), "read")

    def test_require_scope_missing(self):
        with pytest.raises(ToolError, match="lacks the 'watchlist' scope"):
            require_scope(Client("a", frozenset({"read"})), "watchlist")

    def test_require_scope_none_client(self):
        with pytest.raises(ToolError):
            require_scope(None, "read")

    def test_client_from_scope(self):
        c = Client("a", frozenset({"read"}))
        assert client_from_scope({"state": {"mcp_client": c}}) is c
        assert client_from_scope({}) is None
        assert client_from_scope({"state": {}}) is None


# ---- middleware --------------------------------------------------------------
TOKENS = {
    hash_token("tok-one"): Client("alice", frozenset({"read"})),
    hash_token("tok-two"): Client("bob", frozenset({"read", "watchlist"})),
}


def make_app(tokens=TOKENS, seen=None, lifespan_flag=None):
    seen = seen if seen is not None else []

    async def inner(scope, receive, send):
        if scope["type"] == "lifespan":
            msg = await receive()
            assert msg["type"] == "lifespan.startup"
            if lifespan_flag is not None:
                lifespan_flag.append("startup")
            await send({"type": "lifespan.startup.complete"})
            msg = await receive()
            if lifespan_flag is not None:
                lifespan_flag.append("shutdown")
            await send({"type": "lifespan.shutdown.complete"})
            return
        seen.append(client_from_scope(scope))
        await JSONResponse({"ok": True})(scope, receive, send)

    return BearerAuthMiddleware(inner, tokens), seen


class TestMiddleware:
    def _assert_401(self, r):
        assert r.status_code == 401
        assert r.json() == {"error": "unauthorized"}
        assert r.headers["www-authenticate"] == "Bearer"
        assert r.headers["content-type"].startswith("application/json")

    def test_no_header(self):
        app, seen = make_app()
        with TestClient(app) as c:
            self._assert_401(c.get("/x"))
        assert seen == []

    @pytest.mark.parametrize("hdr", [
        "Basic dG9rLW9uZQ==", "tok-one", "Bearer", "Bearer ", "Bearer  tok-one",
        "Bearer wrong", "Bearertok-one",
    ])
    def test_bad_headers(self, hdr):
        app, seen = make_app()
        with TestClient(app) as c:
            self._assert_401(c.get("/x", headers={"Authorization": hdr}))
        assert seen == []

    def test_empty_token_dict_fails_closed(self):
        app, seen = make_app(tokens={})
        with TestClient(app) as c:
            self._assert_401(c.get("/x", headers={"Authorization": "Bearer tok-one"}))
            self._assert_401(c.get("/x"))
            self._assert_401(c.get("/x", headers={"Authorization": "Bearer "}))
        assert seen == []

    def test_right_token_sets_client(self):
        app, seen = make_app()
        with TestClient(app) as c:
            r = c.get("/x", headers={"Authorization": "Bearer tok-one"})
        assert r.status_code == 200
        assert seen == [Client("alice", frozenset({"read"}))]

    def test_scheme_case_insensitive(self):
        app, seen = make_app()
        with TestClient(app) as c:
            assert c.get("/x", headers={"Authorization": "bearer tok-one"}).status_code == 200
            assert c.get("/x", headers={"Authorization": "BEARER tok-one"}).status_code == 200
        assert len(seen) == 2

    def test_two_tokens_two_clients(self):
        app, seen = make_app()
        with TestClient(app) as c:
            c.get("/x", headers={"Authorization": "Bearer tok-two"})
            c.get("/x", headers={"Authorization": "Bearer tok-one"})
        assert [x.name for x in seen] == ["bob", "alice"]
        assert "watchlist" in seen[0].scopes and "watchlist" not in seen[1].scopes

    def test_compares_against_every_key(self, monkeypatch):
        calls = []
        real = auth.hmac.compare_digest
        monkeypatch.setattr(auth.hmac, "compare_digest",
                            lambda a, b: calls.append(1) or real(a, b))
        app, _ = make_app()
        with TestClient(app) as c:
            c.get("/x", headers={"Authorization": "Bearer tok-one"})  # matches first key
        assert len(calls) == len(TOKENS)

    def test_lifespan_passes_through(self):
        flag = []
        app, _ = make_app(lifespan_flag=flag)
        with TestClient(app):
            assert flag == ["startup"]
        assert flag == ["startup", "shutdown"]

    def test_lifespan_with_real_starlette_app(self):
        started = []

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def lifespan(_app):
            started.append(True)
            yield

        inner = Starlette(lifespan=lifespan)
        with TestClient(BearerAuthMiddleware(inner, TOKENS)):
            assert started == [True]

    def test_failed_auth_logged_once_per_minute_without_secrets(self, monkeypatch, caplog):
        clock = [1000.0]
        monkeypatch.setattr(auth, "_now", lambda: clock[0])
        app, _ = make_app()
        secret = "supersecret-token-value"
        with caplog.at_level(logging.WARNING, logger=auth.log.name):
            with TestClient(app) as c:
                for _ in range(3):
                    c.get("/x", headers={"Authorization": f"Bearer {secret}"})
                warns = [r for r in caplog.records if r.levelno == logging.WARNING]
                assert len(warns) == 1
                clock[0] += 61
                c.get("/x", headers={"Authorization": f"Bearer {secret}"})
                warns = [r for r in caplog.records if r.levelno == logging.WARNING]
                assert len(warns) == 2
        text = caplog.text
        assert secret not in text and hash_token(secret) not in text
        assert "Bearer" not in text
        assert "testclient" in text      # peer is logged

    def test_successful_auth_does_not_log_warning(self, caplog):
        app, _ = make_app()
        with caplog.at_level(logging.WARNING, logger=auth.log.name):
            with TestClient(app) as c:
                c.get("/x", headers={"Authorization": "Bearer tok-one"})
        assert caplog.records == []


# ---- package import rule -----------------------------------------------------
_FORBIDDEN = re.compile(
    r"^\s*(?:from\s+readsbstats\.(?:database|api)\b"
    r"|import\s+readsbstats\.(?:database|api)\b"
    r"|from\s+\.\.(?:database|api)\b"
    r"|from\s+readsbstats\s+import\s+[^\n]*\b(?:database|api)\b)", re.M)


def test_mcp_server_never_imports_database_or_api():
    files = list(PKG.rglob("*.py"))
    assert files
    for f in files:
        assert not _FORBIDDEN.search(f.read_text()), f"{f.name} imports database/api"


def test_import_scan_pattern_detects_violations():
    for bad in ("from readsbstats import database", "from readsbstats.api._deps import x",
                "from ..database import x", "from readsbstats import config, api",
                "import readsbstats.database"):
        assert _FORBIDDEN.search(bad), bad
    for good in ("from readsbstats import config, http_safe", "from .. import config",
                 "from . import auth"):
        assert not _FORBIDDEN.search(good), good


# ---- config --------------------------------------------------------------------
MCP_ENV = ["RSBS_MCP_HOST", "RSBS_MCP_PORT", "RSBS_MCP_ALLOWED_HOSTS", "RSBS_MCP_TOKENS_FILE",
           "RSBS_MCP_API_URL", "RSBS_MCP_READ_PER_MIN", "RSBS_MCP_WRITE_PER_HOUR",
           "RSBS_MCP_PHOTO_PER_HOUR", "RSBS_MCP_CONCURRENCY", "RSBS_MCP_WATCHLIST_MAX"]


@pytest.fixture
def reload_config(monkeypatch):
    import readsbstats.config as cfg
    for k in MCP_ENV:
        monkeypatch.delenv(k, raising=False)

    def _reload(**env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return importlib.reload(cfg)

    yield _reload
    monkeypatch.undo()          # restore env BEFORE the clean reload
    importlib.reload(cfg)


class TestMcpConfig:
    def test_defaults(self, reload_config):
        c = reload_config()
        assert c.MCP_HOST == "127.0.0.1"
        assert c.MCP_PORT == 8765
        assert c.MCP_ALLOWED_HOSTS == []
        assert c.MCP_TOKENS_FILE == "/etc/readsbstats/mcp-tokens"
        assert c.MCP_API_URL == "http://127.0.0.1:8080"
        assert c.MCP_READ_PER_MIN == 30
        assert c.MCP_WRITE_PER_HOUR == 10
        assert c.MCP_PHOTO_PER_HOUR == 20
        assert c.MCP_CONCURRENCY == 2
        assert c.MCP_WATCHLIST_MAX == 100

    def test_overrides(self, reload_config):
        c = reload_config(RSBS_MCP_HOST="10.0.0.4", RSBS_MCP_PORT="9000",
                          RSBS_MCP_TOKENS_FILE="/tmp/t", RSBS_MCP_API_URL="http://127.0.0.1:9",
                          RSBS_MCP_READ_PER_MIN="60", RSBS_MCP_WRITE_PER_HOUR="5",
                          RSBS_MCP_PHOTO_PER_HOUR="7", RSBS_MCP_CONCURRENCY="4",
                          RSBS_MCP_WATCHLIST_MAX="250")
        assert (c.MCP_HOST, c.MCP_PORT, c.MCP_TOKENS_FILE) == ("10.0.0.4", 9000, "/tmp/t")
        assert c.MCP_API_URL == "http://127.0.0.1:9"
        assert (c.MCP_READ_PER_MIN, c.MCP_WRITE_PER_HOUR, c.MCP_PHOTO_PER_HOUR) == (60, 5, 7)
        assert (c.MCP_CONCURRENCY, c.MCP_WATCHLIST_MAX) == (4, 250)

    def test_allowed_hosts_split(self, reload_config):
        c = reload_config(RSBS_MCP_ALLOWED_HOSTS=" 10.66.0.4:8765 , ,pi.lan:8765,")
        assert c.MCP_ALLOWED_HOSTS == ["10.66.0.4:8765", "pi.lan:8765"]

    def test_allowed_hosts_blank(self, reload_config):
        assert reload_config(RSBS_MCP_ALLOWED_HOSTS=" , ").MCP_ALLOWED_HOSTS == []

    @pytest.mark.parametrize("attr,env,default", [
        ("MCP_PORT", "RSBS_MCP_PORT", 8765),
        ("MCP_READ_PER_MIN", "RSBS_MCP_READ_PER_MIN", 30),
        ("MCP_WRITE_PER_HOUR", "RSBS_MCP_WRITE_PER_HOUR", 10),
        ("MCP_PHOTO_PER_HOUR", "RSBS_MCP_PHOTO_PER_HOUR", 20),
        ("MCP_CONCURRENCY", "RSBS_MCP_CONCURRENCY", 2),
        ("MCP_WATCHLIST_MAX", "RSBS_MCP_WATCHLIST_MAX", 100),
    ])
    @pytest.mark.parametrize("bad", ["abc", "0", "-3", ""])
    def test_invalid_or_below_minimum_falls_back(self, reload_config, attr, env, default, bad):
        c = reload_config(**{env: bad})
        assert getattr(c, attr) == default
