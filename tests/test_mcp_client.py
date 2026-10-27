"""Tests for readsbstats.mcp_server.client (ADR-0022, Decision 2)."""

import json
from urllib.parse import parse_qsl

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from readsbstats.mcp_server import client as client_mod
from readsbstats.mcp_server.client import ApiClient, ApiConflict, ApiTimeout, ROUTES, Route
from tests._helpers import run_async

BASE = "http://127.0.0.1:8080"


@pytest.fixture(autouse=True)
def setup():
    yield


def make(handler, base=BASE):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ApiClient(base, http=http)


def ok(payload=None, status=200, ctype="application/json", headers=None):
    h = {"content-type": ctype, **(headers or {})}
    if payload is None:
        return httpx.Response(status, headers=h)
    return httpx.Response(status, content=json.dumps(payload).encode(), headers=h)


def call(api, route, **kw):
    return run_async(api.call(route, **kw))


class Recorder:
    def __init__(self, response=None):
        self.requests = []
        self.response = response or ok({"ok": True})

    def __call__(self, request):
        self.requests.append(request)
        return self.response


# ---- routes table -----------------------------------------------------------
EXPECTED_ROUTES = {
    "flights": ("GET", "/api/flights", {}),
    "flight": ("GET", "/api/flights/1234", {"flight_id": 1234}),
    "flight_chart": ("GET", "/api/flights/1234/positions/chart", {"flight_id": 1234}),
    "aircraft_flights": ("GET", "/api/aircraft/abc123/flights", {"icao_hex": "ABC123"}),
    "aircraft_photo": ("GET", "/api/aircraft/abc123/photo", {"icao_hex": " ~abc123 "}),
    "airline_flights": ("GET", "/api/airlines/LOT/flights", {"prefix": "lot"}),
    "type_flights": ("GET", "/api/types/B738/flights", {"aircraft_type": "b738"}),
    "flagged": ("GET", "/api/aircraft/flagged", {}),
    "live": ("GET", "/api/live", {}),
    "snapshot": ("GET", "/api/map/snapshot", {}),
    "stats": ("GET", "/api/stats", {}),
    "records": ("GET", "/api/stats/records", {}),
    "dates": ("GET", "/api/dates", {}),
    "polar": ("GET", "/api/stats/polar", {}),
    "health": ("GET", "/api/health", {}),
    "metrics_health": ("GET", "/api/metrics/health", {}),
    "feeders": ("GET", "/api/feeders", {}),
    "metrics": ("GET", "/api/metrics", {}),
    "watchlist_list": ("GET", "/api/watchlist", {}),
    "watchlist_add": ("POST", "/api/watchlist", {}),
    "watchlist_delete": ("DELETE", "/api/watchlist/77", {"entry_id": 77}),
}


class TestRoutes:
    def test_route_table_is_exactly_the_expected_set(self):
        assert set(ROUTES) == set(EXPECTED_ROUTES)

    def test_only_watchlist_writes(self):
        writes = {n for n, r in ROUTES.items() if r.method != "GET"}
        assert writes == {"watchlist_add", "watchlist_delete"}
        assert ROUTES["watchlist_add"].method == "POST"
        assert ROUTES["watchlist_delete"].method == "DELETE"

    def test_no_stats_prefix(self):
        assert all(r.template.startswith("/api/") for r in ROUTES.values())

    def test_limits(self):
        assert ROUTES["stats"].timeout == 30
        assert ROUTES["aircraft_photo"].timeout == 25
        assert ROUTES["aircraft_photo"].max_bytes == 64 * 1024
        assert ROUTES["flights"].max_bytes == 2 * 1024 * 1024
        assert ROUTES["metrics"].max_bytes == 2 * 1024 * 1024
        assert ROUTES["live"].timeout == 10
        assert ROUTES["live"].max_bytes == 1024 * 1024
        assert all(isinstance(r, Route) for r in ROUTES.values())

    @pytest.mark.parametrize("name", sorted(EXPECTED_ROUTES))
    def test_each_route_builds_expected_method_and_path(self, name):
        method, path, pathargs = EXPECTED_ROUTES[name]
        rec = Recorder()
        api = make(rec)
        kw = {"path": pathargs} if pathargs else {}
        if method == "POST":
            kw["json"] = {"match_type": "icao", "value": "abc123"}
        assert call(api, name, **kw) == {"ok": True}
        (req,) = rec.requests
        assert req.method == method
        assert req.url.path == path
        assert req.url.host == "127.0.0.1" and req.url.port == 8080


class TestRequestShape:
    def test_headers_sent(self):
        rec = Recorder()
        call(make(rec), "live")
        h = rec.requests[0].headers
        assert h["x-requested-with"] == "XMLHttpRequest"
        assert h["accept"] == "application/json"

    def test_headers_sent_on_post_with_json_body(self):
        rec = Recorder(ok({"id": 1}, 201))
        body = {"match_type": "icao", "value": "abc123", "label": "x"}
        assert call(make(rec), "watchlist_add", json=body) == {"id": 1}
        req = rec.requests[0]
        assert req.headers["x-requested-with"] == "XMLHttpRequest"
        assert json.loads(req.content) == body

    def test_query_goes_through_params(self):
        rec = Recorder()
        call(make(rec), "flights", query={
            "from": 100, "to": 200, "callsign": "LOT", "flags": None,
            "limit": 20, "has": True, "x": 1.5, "sp": "a b&c=d#e"})
        q = dict(parse_qsl(rec.requests[0].url.query.decode()))
        assert q == {"from": "100", "to": "200", "callsign": "LOT", "limit": "20",
                     "has": "true", "x": "1.5", "sp": "a b&c=d#e"}
        assert rec.requests[0].url.path == "/api/flights"

    def test_none_values_are_dropped(self):
        rec = Recorder()
        call(make(rec), "flights", query={"callsign": None})
        assert rec.requests[0].url.query == b""

    def test_query_value_cannot_inject_path_or_host(self):
        rec = Recorder()
        call(make(rec), "flights", query={"callsign": "../../x?y=1#z"})
        req = rec.requests[0]
        assert req.url.path == "/api/flights"
        assert req.url.host == "127.0.0.1"
        assert req.url.fragment == ""

    @pytest.mark.parametrize("bad", [{"a": [1]}, {"a": {"b": 1}}, {"a": (1,)}, {"a": b"x"},
                                     {"a": object()}, {1: "x"}])
    def test_nested_or_odd_query_values_rejected(self, bad):
        rec = Recorder()
        with pytest.raises(ToolError):
            call(make(rec), "flights", query=bad)
        assert rec.requests == []

    def test_unknown_route(self):
        with pytest.raises(ToolError):
            call(make(Recorder()), "settings")

    def test_missing_path_param(self):
        with pytest.raises(ToolError):
            call(make(Recorder()), "flight")

    def test_unexpected_path_param(self):
        with pytest.raises(ToolError):
            call(make(Recorder()), "live", path={"flight_id": 1})

    def test_json_only_on_post(self):
        rec = Recorder()
        with pytest.raises(ToolError):
            call(make(rec), "live", json={"a": 1})
        assert rec.requests == []


# ---- path validators --------------------------------------------------------
class TestPathValidators:
    @pytest.mark.parametrize("bad", ["1", 1.0, True, False, 0, -1, 10 ** 12, None, [1], 2 ** 70])
    def test_flight_id_rejected(self, bad):
        rec = Recorder()
        with pytest.raises(ToolError):
            call(make(rec), "flight", path={"flight_id": bad})
        assert rec.requests == []

    @pytest.mark.parametrize("good", [1, 999_999_999_999])
    def test_flight_id_accepted(self, good):
        rec = Recorder()
        call(make(rec), "flight", path={"flight_id": good})
        assert rec.requests[0].url.path == f"/api/flights/{good}"

    @pytest.mark.parametrize("bad", [True, "5", 0, 10 ** 12])
    def test_entry_id_rejected(self, bad):
        with pytest.raises(ToolError):
            call(make(Recorder(), ), "watchlist_delete", path={"entry_id": bad})

    @pytest.mark.parametrize("bad", ["abc12", "zzzzzz", "../settings", "%2e%2e", "abc123/x",
                                     "abc1234", "", "  ", 123456, None, "abc12\n", "~~abc123",
                                     "abc123?x=1", "abc123#", "ａｂｃ１２３"])
    def test_icao_rejected(self, bad):
        rec = Recorder()
        with pytest.raises(ToolError):
            call(make(rec), "aircraft_flights", path={"icao_hex": bad})
        assert rec.requests == []

    @pytest.mark.parametrize("given", ["ABC123", "abc123", " AbC123 ", "~abc123", "~ABC123"])
    def test_icao_normalised(self, given):
        rec = Recorder()
        call(make(rec), "aircraft_flights", path={"icao_hex": given})
        assert rec.requests[0].url.path == "/api/aircraft/abc123/flights"

    @pytest.mark.parametrize("bad", ["", "L", "LOTX", "L/T", "LO?", "L#T", "L%T", "..", "١٢٣",
                                     " 1_0 ", "L T", "L\nT", 12, None, "é1", "LO/../x"])
    def test_prefix_rejected(self, bad):
        rec = Recorder()
        with pytest.raises(ToolError):
            call(make(rec), "airline_flights", path={"prefix": bad})
        assert rec.requests == []

    @pytest.mark.parametrize("given,want", [("lot", "LOT"), ("4o", "4O"), (" ba ", "BA")])
    def test_prefix_normalised(self, given, want):
        rec = Recorder()
        call(make(rec), "airline_flights", path={"prefix": given})
        assert rec.requests[0].url.path == f"/api/airlines/{want}/flights"

    @pytest.mark.parametrize("bad", ["", "B", "B7388", "B/38", "B73?", "B#38", "B%38", "..",
                                     "١٢٣", " 1_0 ", "B 38", "B7\n38", 738, None, "../x"])
    def test_type_rejected(self, bad):
        rec = Recorder()
        with pytest.raises(ToolError):
            call(make(rec), "type_flights", path={"aircraft_type": bad})
        assert rec.requests == []

    @pytest.mark.parametrize("given,want", [("b738", "B738"), ("a20n", "A20N"), ("c1", "C1")])
    def test_type_normalised(self, given, want):
        rec = Recorder()
        call(make(rec), "type_flights", path={"aircraft_type": given})
        assert rec.requests[0].url.path == f"/api/types/{want}/flights"

    def test_error_message_is_helpful(self):
        with pytest.raises(ToolError, match="icao_hex"):
            call(make(Recorder()), "aircraft_flights", path={"icao_hex": "xyz"})


# ---- final request check ----------------------------------------------------
class TestFinalCheck:
    def _check(self, route, url, base=BASE):
        client_mod._check_request(ROUTES[route], httpx.URL(url), httpx.URL(base))

    def test_ok(self):
        self._check("flight", "http://127.0.0.1:8080/api/flights/12?x=1")

    @pytest.mark.parametrize("route,url", [
        ("flight", "http://127.0.0.1:8080/api/flights/12/../settings"),
        ("flight", "http://127.0.0.1:8080/api/flights/012"),
        ("flight", "http://127.0.0.1:8080/api/flights/%2e%2e"),
        ("flight", "http://127.0.0.1:8080/stats/api/flights/12"),
        ("live", "http://127.0.0.1:8080/api/live/"),
        ("live", "http://127.0.0.1:8081/api/live"),
        ("live", "http://localhost:8080/api/live"),
        ("live", "https://127.0.0.1:8080/api/live"),
        ("live", "http://user@127.0.0.1:8080/api/live"),
        ("live", "http://127.0.0.1:8080/api/live#frag"),
        ("aircraft_flights", "http://127.0.0.1:8080/api/aircraft/ABC123/flights"),
        ("airline_flights", "http://127.0.0.1:8080/api/airlines/lot/flights"),
    ])
    def test_refused(self, route, url):
        with pytest.raises(ToolError, match="refused"):
            self._check(route, url)

    def test_method_mismatch_refused(self):
        bad = Route("PUT", "/api/live", 10, 1000)
        with pytest.raises(ToolError, match="refused"):
            client_mod._check_request(bad, httpx.URL(BASE + "/api/live"), httpx.URL(BASE))

    def test_call_refuses_if_route_table_is_tampered(self, monkeypatch):
        monkeypatch.setitem(ROUTES, "live", Route("GET", "/api/live/../x", 10, 1000))
        rec = Recorder()
        with pytest.raises(ToolError, match="refused"):
            call(make(rec), "live")
        assert rec.requests == []


# ---- construction -----------------------------------------------------------
class TestConstruction:
    @pytest.mark.parametrize("base", [
        "https://127.0.0.1:8080", "http://example.com", "http://10.0.0.1:8080",
        "http://127.0.0.2:8080", "http://127.0.0.1:8080/stats", "http://user@127.0.0.1:8080",
        "ftp://127.0.0.1", "", "127.0.0.1:8080", "http://127.0.0.1:8080?x=1",
    ])
    def test_non_loopback_or_bad_base_rejected(self, base):
        with pytest.raises(ValueError):
            ApiClient(base)

    @pytest.mark.parametrize("base", ["http://127.0.0.1:8080", "http://127.0.0.1:8080/",
                                      "http://localhost:9000", "http://[::1]:8080"])
    def test_loopback_accepted(self, base):
        ApiClient(base)

    def test_trailing_slash_base_builds_clean_paths(self):
        rec = Recorder()
        call(make(rec, base="http://127.0.0.1:8080/"), "live")
        assert rec.requests[0].url.path == "/api/live"

    def test_default_client_does_not_trust_env_proxies(self):
        api = ApiClient(BASE)
        assert api._http._trust_env is False
        run_async(api.aclose())

    def test_aclose_closes_owned_client_only(self):
        http = httpx.AsyncClient(transport=httpx.MockTransport(Recorder()))
        api = ApiClient(BASE, http=http)
        run_async(api.aclose())
        assert not http.is_closed
        run_async(http.aclose())
        owned = ApiClient(BASE)
        run_async(owned.aclose())
        assert owned._http.is_closed


# ---- response mapping -------------------------------------------------------
class TestResponses:
    def test_204_is_none(self):
        rec = Recorder(httpx.Response(204))
        assert call(make(rec), "watchlist_delete", path={"entry_id": 1}) is None

    def test_json_null_is_none(self):
        rec = Recorder(httpx.Response(200, content=b"null",
                                      headers={"content-type": "application/json"}))
        assert call(make(rec), "aircraft_photo", path={"icao_hex": "abc123"}) is None

    def test_content_type_with_charset_ok(self):
        rec = Recorder(ok({"a": 1}, ctype="application/json; charset=utf-8"))
        assert call(make(rec), "live") == {"a": 1}

    @pytest.mark.parametrize("ctype", ["text/html", "text/html; charset=utf-8", "", "text/plain"])
    def test_non_json_is_unexpected(self, ctype):
        rec = Recorder(httpx.Response(200, content=b"<html>spa shell</html>",
                                      headers={"content-type": ctype} if ctype else {}))
        with pytest.raises(ToolError, match="unexpected response"):
            call(make(rec), "live")

    def test_invalid_json_body_is_unexpected(self):
        rec = Recorder(httpx.Response(200, content=b"{nope",
                                      headers={"content-type": "application/json"}))
        with pytest.raises(ToolError, match="unexpected response"):
            call(make(rec), "live")

    def test_4xx_detail_string(self):
        rec = Recorder(ok({"detail": "Flight not found"}, 404))
        with pytest.raises(ToolError, match="Flight not found") as ei:
            call(make(rec), "flight", path={"flight_id": 5})
        assert not isinstance(ei.value, ApiConflict)

    def test_4xx_validation_list(self):
        detail = [{"loc": ["query", "limit"], "msg": "Input should be less than or equal to 500",
                   "type": "x", "input": "9999"},
                  {"loc": ["query", "offset"], "msg": "Input should be a valid integer"}]
        rec = Recorder(ok({"detail": detail}, 422))
        with pytest.raises(ToolError) as ei:
            call(make(rec), "flights")
        text = str(ei.value)
        assert "less than or equal to 500" in text and "valid integer" in text
        assert "9999" not in text            # echoed input is not forwarded

    def test_4xx_error_key(self):
        rec = Recorder(ok({"error": "unknown metrics: foo"}, 400))
        with pytest.raises(ToolError, match="unknown metrics: foo"):
            call(make(rec), "metrics")

    def test_4xx_text_is_truncated_to_300(self):
        rec = Recorder(ok({"detail": "x" * 1000}, 400))
        with pytest.raises(ToolError) as ei:
            call(make(rec), "flights")
        assert len(str(ei.value)) <= 300

    def test_4xx_non_json_body_has_generic_message(self):
        rec = Recorder(httpx.Response(400, content=b"<html>boom</html>",
                                      headers={"content-type": "text/html"}))
        with pytest.raises(ToolError, match="HTTP 400") as ei:
            call(make(rec), "flights")
        assert "boom" not in str(ei.value)

    def test_4xx_odd_json_shape(self):
        rec = Recorder(ok(["weird"], 400))
        with pytest.raises(ToolError, match="HTTP 400"):
            call(make(rec), "flights")

    def test_409_is_api_conflict(self):
        rec = Recorder(ok({"detail": "Entry already exists"}, 409))
        with pytest.raises(ApiConflict, match="already exists"):
            call(make(rec), "watchlist_add", json={"match_type": "icao", "value": "abc123"})

    def test_api_conflict_is_a_tool_error(self):
        assert issubclass(ApiConflict, ToolError)

    @pytest.mark.parametrize("status", [500, 502, 503])
    def test_5xx(self, status):
        rec = Recorder(ok({"detail": "secret internals /opt/x.py"}, status))
        with pytest.raises(ToolError, match=rf"HTTP {status}.*retry later") as ei:
            call(make(rec), "live")
        assert "secret" not in str(ei.value)

    def test_timeout(self):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)
        with pytest.raises(ApiTimeout, match="slow right now"):
            call(make(handler), "stats")

    def test_connect_error(self):
        def handler(request):
            raise httpx.ConnectError("refused", request=request)
        with pytest.raises(ToolError, match="unreachable"):
            call(make(handler), "live")

    def test_other_http_error(self):
        def handler(request):
            raise httpx.RemoteProtocolError("bad", request=request)
        with pytest.raises(ToolError, match="unreachable"):
            call(make(handler), "live")

    def test_redirect_is_refused(self):
        rec = Recorder(httpx.Response(307, headers={"location": "http://evil.example/"}))
        with pytest.raises(ToolError, match="refused"):
            call(make(rec), "live")
        assert len(rec.requests) == 1

    def test_oversized_body_is_refused(self):
        big = httpx.Response(200, content=b"[" + b"1," * 40_000 + b"1]",
                             headers={"content-type": "application/json"})
        with pytest.raises(ToolError, match="refused"):
            call(make(Recorder(big)), "aircraft_photo", path={"icao_hex": "abc123"})

    def test_route_timeout_and_cap_are_passed(self, monkeypatch):
        seen = {}

        async def fake(client, method, url, *, max_bytes, timeout, headers=None, json=None):
            seen.update(method=method, url=url, max_bytes=max_bytes, timeout=timeout)
            return 200, httpx.Headers({"content-type": "application/json"}), b"{}"

        monkeypatch.setattr(client_mod.http_safe, "loopback_request", fake)
        call(make(Recorder()), "stats", query={"from": 1})
        assert seen["timeout"] == 30 and seen["max_bytes"] == ROUTES["stats"].max_bytes
        assert seen["url"] == "http://127.0.0.1:8080/api/stats?from=1"
