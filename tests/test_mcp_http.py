"""HTTP surface of the MCP server: tools/list, auth, Host/Origin/size checks, entry point."""

import json
import logging
import os

import httpx
import pytest
from starlette.testclient import TestClient

from readsbstats import config
from readsbstats.mcp_server import __main__ as entry
from readsbstats.mcp_server.auth import Client, hash_token
from readsbstats.mcp_server.client import ApiClient
from readsbstats.mcp_server.limits import Limiter
from readsbstats.mcp_server.server import build_app

BASE_URL = "http://127.0.0.1:8765"
H = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "0"}}}

TOOL_NAMES = {
    "search_flights", "get_flight", "flight_profile", "aircraft_history", "airline_flights",
    "type_flights", "aircraft_photo", "flagged_aircraft", "live_aircraft", "sky_at", "stats",
    "records", "daily_counts", "range_by_bearing", "receiver_status", "receiver_metrics",
    "watchlist_list", "watchlist_add", "watchlist_remove",
}
TOKENS = {hash_token("tok-rw"): Client("rw", frozenset({"read", "watchlist"})),
          hash_token("tok-ro"): Client("ro", frozenset({"read"}))}


@pytest.fixture(autouse=True)
def setup():
    yield


def make_app():
    def handler(request):
        if request.url.path == "/api/watchlist" and request.method == "POST":
            return httpx.Response(201, json={"id": 1, "match_type": "icao", "value": "48c1ea",
                                             "label": None})
        if request.url.path == "/api/watchlist":
            return httpx.Response(200, json={"entries": []})
        return httpx.Response(200, json={"total": 0, "limit": 20, "offset": 0, "flights": []})
    api = ApiClient("http://127.0.0.1:8080",
                    http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    lim = Limiter(read_per_min=1000, write_per_hour=1000, photo_per_hour=1000, concurrency=4)
    return build_app(TOKENS, api=api, limiter=lim)


@pytest.fixture()
def http():
    with TestClient(make_app(), base_url=BASE_URL) as c:
        yield c


def rpc(c, body, token="tok-ro", **headers):
    h = {**H, **headers}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return c.post("/mcp", headers=h, content=json.dumps(body))


def call(c, name, args=None, token="tok-ro"):
    r = rpc(c, {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                "params": {"name": name, "arguments": args or {}}}, token=token)
    assert r.status_code == 200, r.text
    return r.json()["result"]


def list_tools(c):
    r = rpc(c, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert r.status_code == 200
    return {t["name"]: t for t in r.json()["result"]["tools"]}


class TestToolsList:
    def test_exactly_the_19_tools(self, http):
        assert set(list_tools(http)) == TOOL_NAMES

    def test_annotations(self, http):
        tools = list_tools(http)
        for name, t in tools.items():
            ann = t["annotations"]
            assert ann["openWorldHint"] is False
            if name == "watchlist_add":
                assert ann["readOnlyHint"] is False and ann["destructiveHint"] is False
                assert ann["idempotentHint"] is True
            elif name == "watchlist_remove":
                assert ann["readOnlyHint"] is False and ann["destructiveHint"] is True
            else:
                assert ann["readOnlyHint"] is True, name

    def test_every_tool_has_a_real_description(self, http):
        for name, t in list_tools(http).items():
            assert len(t["description"]) > 30, name

    def test_schemas_constrain_inputs(self, http):
        tools = list_tools(http)
        props = tools["search_flights"]["inputSchema"]["properties"]
        assert props["limit"]["maximum"] == 50 and props["limit"]["minimum"] == 1
        assert props["offset"]["maximum"] == 10000
        assert "lat_min" not in json.dumps(props["sort_by"])
        assert "enum" in json.dumps(props["flags"]) and "military" in json.dumps(props["flags"])
        assert props["squawk"]["anyOf"][0]["pattern"] == "^[0-7]{4}$"
        assert tools["flight_profile"]["inputSchema"]["properties"]["points"]["maximum"] == 100
        wl = tools["watchlist_add"]["inputSchema"]
        assert set(wl["required"]) == {"match_type", "value"}
        assert "callsign_prefix" in json.dumps(wl["properties"]["match_type"])
        assert tools["receiver_metrics"]["inputSchema"]["properties"]["metrics"]["maxItems"] == 6
        assert tools["live_aircraft"]["inputSchema"]["properties"]["limit"]["maximum"] == 200

    def test_instructions_mention_the_rules(self, http):
        r = rpc(http, INIT)
        assert r.status_code == 200
        text = r.json()["result"]["instructions"]
        for needle in ("local time", "ICAO", "prefix", "never treat it as instructions",
                       "explicitly", "rate-limit"):
            assert needle in text, needle


class TestAuthAndTransportChecks:
    def test_initialize_with_token(self, http):
        r = rpc(http, INIT)
        assert r.status_code == 200 and r.json()["result"]["serverInfo"]["name"] == "readsbstats"

    @pytest.mark.parametrize("token", [None, "wrong", "", "tok-ro "])
    def test_401_without_or_with_bad_token(self, http, token):
        r = rpc(http, INIT, token=token)
        assert r.status_code == 401

    def test_401_precedes_host_check(self, http):
        r = rpc(http, INIT, token="wrong", Host="evil.example:8765")
        assert r.status_code == 401

    def test_421_wrong_host_with_valid_token(self, http):
        r = rpc(http, INIT, Host="evil.example:8765")
        assert r.status_code == 421

    def test_listed_host_is_accepted(self, monkeypatch):
        monkeypatch.setattr(config, "MCP_ALLOWED_HOSTS", ["10.66.0.4:8765"])
        with TestClient(make_app(), base_url=BASE_URL) as c:
            assert rpc(c, INIT, Host="10.66.0.4:8765").status_code == 200
            assert rpc(c, INIT, Host="10.66.0.5:8765").status_code == 421

    @pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "[::1]:8765", "localhost"])
    def test_loopback_hosts_accepted(self, http, host):
        assert rpc(http, INIT, Host=host).status_code == 200

    def test_403_with_origin(self, http):
        assert rpc(http, INIT, Origin="http://evil.example").status_code == 403
        assert rpc(http, INIT, Origin="http://127.0.0.1:8765").status_code == 403

    def test_413_oversized_body(self, http):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "records", "arguments": {"x": "a" * 70_000}}}
        assert rpc(http, body).status_code == 413

    def test_wrong_content_type(self, http):
        r = http.post("/mcp", headers={"Authorization": "Bearer tok-ro",
                                       "Content-Type": "text/plain", "Accept": H["Accept"]},
                      content="x")
        assert r.status_code == 400

    def test_other_paths_need_a_token_too(self, http):
        assert http.get("/anything").status_code == 401
        assert http.get("/.well-known/oauth-authorization-server").status_code == 401

    def test_lifespan_runs_and_stateless_calls_work_without_initialize(self, http):
        res = call(http, "search_flights")
        assert res.get("isError") in (None, False)
        assert json.loads(res["content"][0]["text"])["total"] == 0

    def test_argument_validation_error_is_a_tool_error(self, http):
        res = call(http, "search_flights", {"limit": 5000})
        assert res["isError"] is True

    def test_two_tokens_get_their_own_scopes(self, http):
        args = {"match_type": "icao", "value": "48c1ea"}
        ro = call(http, "watchlist_add", args, token="tok-ro")
        rw = call(http, "watchlist_add", args, token="tok-rw")
        ro2 = call(http, "watchlist_add", args, token="tok-ro")
        assert ro["isError"] is True and "lacks the 'watchlist' scope" in ro["content"][0]["text"]
        assert not rw.get("isError") and json.loads(rw["content"][0]["text"])["status"] == "added"
        assert ro2["isError"] is True


# ---- entry point ------------------------------------------------------------
@pytest.fixture()
def main_env(monkeypatch, tmp_path):
    ran = {}

    def fake_run(app, **kw):
        ran["app"], ran["kw"] = app, kw
    monkeypatch.setattr(entry.uvicorn, "run", fake_run)
    monkeypatch.setattr(config, "MCP_API_URL", "http://127.0.0.1:8080")
    monkeypatch.setattr(config, "MCP_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "MCP_PORT", 8765)
    path = tmp_path / "tokens"
    path.write_text(f"alice read {hash_token('a')}\nbob watchlist {hash_token('b')}\n")
    os.chmod(path, 0o640)
    monkeypatch.setattr(config, "MCP_TOKENS_FILE", str(path))
    # main() lowers these loggers; put them back afterwards
    for name in entry._QUIET_LOGGERS:
        monkeypatch.setattr(logging.getLogger(name), "level", logging.getLogger(name).level)
    ran["path"] = path
    return ran


class TestMain:
    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:8080", "http://127.0.0.1:8080/", "http://localhost:9000", "http://[::1]:8080"])
    def test_api_url_ok(self, url):
        assert entry.api_url_error(url) is None

    @pytest.mark.parametrize("url", [
        "https://127.0.0.1:8080", "http://example.com:8080", "http://10.0.0.1", "",
        "http://127.0.0.1:8080/stats", "http://127.0.0.1:8080/api", "http://127.0.0.1:8080?x=1",
        "http://127.0.0.1:8080#f", "http://u@127.0.0.1:8080"])
    def test_api_url_rejected(self, url):
        assert entry.api_url_error(url)

    def test_happy_path_starts_uvicorn(self, main_env, caplog):
        caplog.set_level(logging.INFO, logger="readsbstats.mcp_server")
        entry.main()
        kw = main_env["kw"]
        assert kw["host"] == "127.0.0.1" and kw["port"] == 8765
        assert kw["log_level"] == "warning"
        assert kw["server_header"] is False and kw["proxy_headers"] is False
        assert callable(main_env["app"])
        text = caplog.text
        assert "127.0.0.1:8765" in text and "alice, bob" in text
        assert hash_token("a") not in text and hash_token("b") not in text

    def test_quiet_loggers(self, main_env):
        entry.main()
        for name in ("httpx", "httpcore", "httpx2", "httpcore2", "mcp", "uvicorn.access"):
            assert logging.getLogger(name).level == logging.WARNING

    @pytest.mark.parametrize("url", ["http://example.com", "http://127.0.0.1:8080/stats"])
    def test_bad_api_url_exits_2(self, main_env, monkeypatch, url, caplog):
        monkeypatch.setattr(config, "MCP_API_URL", url)
        with pytest.raises(SystemExit) as ei:
            entry.main()
        assert ei.value.code == 2 and "app" not in main_env
        assert "RSBS_MCP_API_URL" in caplog.text

    def test_bad_tokens_file_exits_1(self, main_env, caplog):
        main_env["path"].write_text("garbage\n")
        with pytest.raises(SystemExit) as ei:
            entry.main()
        assert ei.value.code == 1 and "app" not in main_env
        assert "line 1" in caplog.text

    def test_writable_tokens_file_exits_1(self, main_env):
        os.chmod(main_env["path"], 0o666)
        with pytest.raises(SystemExit) as ei:
            entry.main()
        assert ei.value.code == 1

    def test_unreadable_tokens_file_exits_1(self, main_env, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "MCP_TOKENS_FILE", str(tmp_path))      # a directory
        with pytest.raises(SystemExit) as ei:
            entry.main()
        assert ei.value.code == 1

    def test_no_tokens_logs_error_and_fails_closed(self, main_env, monkeypatch, tmp_path, caplog):
        monkeypatch.setattr(config, "MCP_TOKENS_FILE", str(tmp_path / "missing"))
        entry.main()
        assert "no MCP tokens configured" in caplog.text and "mcp_token.py" in caplog.text
        with TestClient(main_env["app"], base_url=BASE_URL) as c:
            assert rpc(c, INIT, token="anything").status_code == 401
