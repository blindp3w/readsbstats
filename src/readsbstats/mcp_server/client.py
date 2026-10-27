"""Narrow client for the local readsbstats web API (ADR-0022, Decision 2).

The MCP server reaches data only through a fixed set of named routes. Every
path parameter is validated before it is formatted into the path, query values
go through ``params=``, and the final URL is checked against an allowlist
built from the route template before ``http_safe.loopback_request`` sends it.

This module makes no network call of its own: it only builds requests and
hands them to ``http_safe.loopback_request``. The httpx module (for ``URL``,
``AsyncClient`` and the exception types) is taken from ``http_safe`` so the
SSRF funnel stays the only place that imports it.
"""

import json
import re
from typing import Any, NamedTuple

from mcp.server.mcpserver.exceptions import ToolError

from .. import cleaners, http_safe

httpx = http_safe.httpx

_KIB = 1024
_DEFAULT_TIMEOUT = 10.0
_DEFAULT_MAX_BYTES = 1024 * _KIB
_BIG_MAX_BYTES = 2048 * _KIB
_DETAIL_MAX = 300

_PATH_PATTERNS = {
    "flight_id": "[1-9][0-9]{0,11}",
    "entry_id": "[1-9][0-9]{0,11}",
    "icao_hex": "[0-9a-f]{6}",
    "prefix": "[A-Z0-9]{2,3}",
    "aircraft_type": "[A-Z0-9]{2,4}",
}
_PARAM_RE = re.compile(r"\{(\w+)\}")
_PREFIX_RE = re.compile(r"[A-Z0-9]{2,3}")
_TYPE_RE = re.compile(r"[A-Z0-9]{2,4}")
_HEX6_RE = re.compile(r"[0-9a-f]{6}")


class Route(NamedTuple):
    method: str
    template: str
    timeout: float
    max_bytes: int


def _r(template, method="GET", timeout=_DEFAULT_TIMEOUT, max_bytes=_DEFAULT_MAX_BYTES):
    return Route(method, template, timeout, max_bytes)


ROUTES: dict[str, Route] = {
    "flights": _r("/api/flights", max_bytes=_BIG_MAX_BYTES),
    "flight": _r("/api/flights/{flight_id}"),
    "flight_chart": _r("/api/flights/{flight_id}/positions/chart"),
    "aircraft_flights": _r("/api/aircraft/{icao_hex}/flights", max_bytes=_BIG_MAX_BYTES),
    "aircraft_photo": _r("/api/aircraft/{icao_hex}/photo", timeout=25.0, max_bytes=64 * _KIB),
    "airline_flights": _r("/api/airlines/{prefix}/flights", max_bytes=_BIG_MAX_BYTES),
    "type_flights": _r("/api/types/{aircraft_type}/flights", max_bytes=_BIG_MAX_BYTES),
    "flagged": _r("/api/aircraft/flagged", max_bytes=_BIG_MAX_BYTES),
    "live": _r("/api/live"),
    "snapshot": _r("/api/map/snapshot"),
    "stats": _r("/api/stats", timeout=30.0),
    "records": _r("/api/stats/records"),
    "dates": _r("/api/dates"),
    "polar": _r("/api/stats/polar"),
    "health": _r("/api/health"),
    "metrics_health": _r("/api/metrics/health"),
    "feeders": _r("/api/feeders"),
    "metrics": _r("/api/metrics", max_bytes=_BIG_MAX_BYTES),
    "watchlist_list": _r("/api/watchlist"),
    "watchlist_add": _r("/api/watchlist", method="POST"),
    "watchlist_delete": _r("/api/watchlist/{entry_id}", method="DELETE"),
}


class ApiConflict(ToolError):
    """The API answered 409 (e.g. watchlist entry already exists)."""


class ApiTimeout(ToolError):
    """The API did not answer within the route's timeout."""


# ---- path parameter validation ---------------------------------------------
def _int_id(name: str, value: Any) -> str:
    if type(value) is not int or not 0 < value < 10 ** 12:
        raise ToolError(f"{name} must be a positive integer below 10^12")
    return str(value)


def _icao(name: str, value: Any) -> str:
    if isinstance(value, str):
        text = value.strip().lower()
        if text.startswith("~"):
            text = text[1:]
        icao = cleaners.valid_icao_hex(text)
        # valid_icao_hex strips again: the explicit regex rejects "~ abc123 " forms.
        if icao is not None and _HEX6_RE.fullmatch(text):
            return icao
    raise ToolError(f"{name} must be 6 hex digits (for example 48c1ea)")


def _code(name: str, value: Any, rx: re.Pattern, hint: str) -> str:
    if isinstance(value, str):
        text = value.strip().upper()
        if rx.fullmatch(text):
            return text
    raise ToolError(f"{name} must be {hint}")


_VALIDATORS = {
    "flight_id": _int_id,
    "entry_id": _int_id,
    "icao_hex": _icao,
    "prefix": lambda n, v: _code(n, v, _PREFIX_RE, "2-3 letters or digits (for example LOT)"),
    "aircraft_type": lambda n, v: _code(n, v, _TYPE_RE, "2-4 letters or digits (for example B738)"),
}


def _format_path(route: Route, path: dict | None) -> str:
    names = _PARAM_RE.findall(route.template)
    given = dict(path or {})
    if set(given) != set(names):
        raise ToolError(f"refused: path parameters {sorted(given)} do not match "
                        f"the route ({sorted(names)})")
    out = route.template
    for name in names:
        out = out.replace("{" + name + "}", _VALIDATORS[name](name, given[name]))
    return out


def _template_regex(template: str) -> re.Pattern:
    parts, pos = [], 0
    for m in _PARAM_RE.finditer(template):
        parts.append(re.escape(template[pos:m.start()]))
        parts.append(_PATH_PATTERNS[m.group(1)])
        pos = m.end()
    parts.append(re.escape(template[pos:]))
    return re.compile("".join(parts))


def _clean_query(query: dict | None) -> dict:
    out = {}
    for key, value in (query or {}).items():
        if not isinstance(key, str) or not key:
            raise ToolError("refused: query keys must be strings")
        if value is None:
            continue
        if not isinstance(value, (str, int, float, bool)):
            raise ToolError(f"refused: query value for {key!r} must be a scalar")
        out[key] = value
    return out


def _check_request(route: Route, url, base) -> None:
    """Final guard on the URL that is about to be sent."""
    if url.scheme != base.scheme or url.host != base.host or url.port != base.port:
        raise ToolError("refused: request leaves the configured API origin")
    if url.userinfo or url.fragment:
        raise ToolError("refused: unexpected URL parts")
    if route.method not in ("GET", "POST", "DELETE"):
        raise ToolError(f"refused: method {route.method} is not allowed")
    raw_path = url.raw_path.split(b"?", 1)[0].decode("ascii", "replace")
    if raw_path != url.path or not _template_regex(route.template).fullmatch(raw_path):
        raise ToolError("refused: path does not match the route allowlist")


# ---- response mapping -------------------------------------------------------
def _is_json(headers) -> bool:
    return headers.get("content-type", "").split(";")[0].strip().lower() == "application/json"


def _detail_text(body: bytes, headers, status: int) -> str:
    generic = f"readsbstats API rejected the request (HTTP {status})"
    if not _is_json(headers):
        return generic
    try:
        data = json.loads(body)
    except ValueError:
        return generic
    if not isinstance(data, dict):
        return generic
    detail = data.get("detail", data.get("error"))
    if isinstance(detail, list):
        msgs = []
        for item in detail:
            if isinstance(item, dict) and isinstance(item.get("msg"), str):
                loc = item.get("loc")
                field = loc[-1] if isinstance(loc, list) and loc and isinstance(loc[-1], str) else None
                msgs.append(f"{field}: {item['msg']}" if field else item["msg"])
        detail = "; ".join(msgs)
    text = cleaners.clean_short_text(detail, _DETAIL_MAX) if isinstance(detail, str) else None
    return text or generic


class ApiClient:
    def __init__(self, base_url: str, *, http=None):
        if not http_safe.is_loopback_http_url(base_url):
            raise ValueError("API URL must be an http:// URL on a loopback host")
        url = httpx.URL(base_url)
        if url.path not in ("", "/") or url.query or url.fragment:
            raise ValueError("API URL must not contain a path, query or fragment")
        self._base = url.copy_with(raw_path=b"/")
        self._owns = http is None
        # trust_env=False: never route loopback calls through an env proxy.
        self._http = http or httpx.AsyncClient(
            trust_env=False, limits=httpx.Limits(max_connections=4))

    async def aclose(self) -> None:
        if self._owns:
            await self._http.aclose()

    async def call(self, route: str, *, path: dict | None = None,
                   query: dict | None = None, json: dict | None = None) -> Any:
        spec = ROUTES.get(route)
        if spec is None:
            raise ToolError(f"refused: unknown route {route!r}")
        if json is not None and spec.method != "POST":
            raise ToolError("refused: a body is only allowed on POST routes")
        url_path = _format_path(spec, path)
        params = _clean_query(query)
        url = self._base.copy_with(path=url_path, params=params or None)
        _check_request(spec, url, self._base)
        try:
            status, headers, body = await http_safe.loopback_request(
                self._http, spec.method, str(url),
                max_bytes=spec.max_bytes, timeout=spec.timeout,
                headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
                json=json)
        except http_safe.UnsafeURLError as exc:
            raise ToolError(f"refused: {str(exc)[:200]}") from None
        except httpx.TimeoutException:
            raise ApiTimeout("readsbstats API is slow right now; retry in about a minute") from None
        except httpx.HTTPError:
            raise ToolError("readsbstats API is unreachable") from None
        return self._map(status, headers, body)

    @staticmethod
    def _map(status: int, headers, body: bytes) -> Any:
        if status == 204:
            return None
        if 200 <= status < 300:
            if not _is_json(headers):
                raise ToolError("readsbstats API returned an unexpected response")
            try:
                return json.loads(body)
            except ValueError:
                raise ToolError("readsbstats API returned an unexpected response") from None
        if status >= 500:
            raise ToolError(f"readsbstats API error (HTTP {status}); retry later")
        if 400 <= status < 500:
            text = _detail_text(body, headers, status)
            raise (ApiConflict if status == 409 else ToolError)(text)
        raise ToolError("readsbstats API returned an unexpected response")
