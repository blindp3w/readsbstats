"""Shared aircraft photo lookup chain.

Tries sources in SOURCES order and returns the first hit.  Adding a new
source is a one-line change: implement a callable(icao_hex: str) -> PhotoResult | None
and append it to SOURCES.

Network calls go through :func:`http_safe.safe_urlopen` (re-exported here as
``_safe_open`` for backwards compat with tests).  See ``http_safe.py`` for
the full SSRF policy.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import urllib.error
import urllib.parse
from dataclasses import dataclass
from typing import Callable

from . import http_safe

log = logging.getLogger("photo_sources")

PHOTO_UA = http_safe._USER_AGENT

# Per-source response cap — JSON payloads from Planespotters / airport-data /
# hexdb are a few KB at most.  An adversarial upstream returning a multi-GB
# response would otherwise OOM the collector.
_JSON_MAX_BYTES = 256 * 1024


@dataclass
class PhotoResult:
    thumbnail_url: str
    large_url: str | None = None
    link_url: str | None = None
    photographer: str | None = None


# ---------------------------------------------------------------------------
# Re-exports of the SSRF-safe HTTP primitives.  Tests monkeypatch these names
# directly (e.g. ``photo_sources._safe_open``), so they remain stable here
# even though the implementation lives in ``http_safe``.
# ---------------------------------------------------------------------------
_NoRedirectHandler = http_safe._NoRedirectHandler
_no_redirect_opener = http_safe._no_redirect_opener
_validate_url = http_safe.validate_url
_ip_is_public = http_safe._ip_is_public
_safe_open = http_safe.safe_urlopen


# ---------------------------------------------------------------------------
# Individual sources
# ---------------------------------------------------------------------------

def _q(icao_hex: str) -> str:
    """Percent-encode an ICAO for safe interpolation into URL paths/queries."""
    return urllib.parse.quote(icao_hex, safe="")


def _fetch_planespotters(icao_hex: str) -> PhotoResult | None:
    url = f"https://api.planespotters.net/pub/photos/hex/{_q(icao_hex)}"
    body, _ = _safe_open(url, timeout=6, max_bytes=_JSON_MAX_BYTES)
    data = json.loads(body)
    photos = data.get("photos", [])
    if not photos:
        return None
    p = photos[0]
    thumb = (p.get("thumbnail") or {}).get("src")
    if not thumb:
        return None
    return PhotoResult(
        thumbnail_url=thumb,
        large_url=(p.get("thumbnail_large") or {}).get("src"),
        link_url=p.get("link"),
        photographer=p.get("photographer"),
    )


def _fetch_airport_data(icao_hex: str) -> PhotoResult | None:
    url = (
        f"https://airport-data.com/api/ac_thumb.json?m={_q(icao_hex)}&n=1"
    )
    body, _ = _safe_open(url, timeout=6, max_bytes=_JSON_MAX_BYTES)
    data = json.loads(body)
    if data.get("status") != 200 or not data.get("data"):
        return None
    item = data["data"][0]
    img = item.get("image")
    if not img:
        return None
    return PhotoResult(
        thumbnail_url=img,
        large_url=img,
        link_url=item.get("link"),
        photographer=item.get("photographer"),
    )


def _fetch_hexdb(icao_hex: str) -> PhotoResult | None:
    """hexdb.io returns HTTP 404 when no photo exists — swallow that so the
    chain's exception handler doesn't log it as an unexpected failure."""
    url = f"https://hexdb.io/hex-image?hex={_q(icao_hex)}"
    try:
        body, _ = _safe_open(url, timeout=6, max_bytes=_JSON_MAX_BYTES)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    text = body.decode(errors="replace").strip()
    if not text or text == "n/a":
        return None
    return PhotoResult(thumbnail_url=text, large_url=text)


# ---------------------------------------------------------------------------
# Chain
# ---------------------------------------------------------------------------

# Ordered list of sources.  Append a new callable here to extend the chain.
SOURCES: list = [_fetch_planespotters, _fetch_airport_data, _fetch_hexdb]


def fetch_photo(icao_hex: str) -> PhotoResult | None:
    """Try each source in SOURCES order; return the first hit or None."""
    for source in SOURCES:
        try:
            result = source(icao_hex)
            if result:
                return result
        except Exception as exc:
            log.debug("%s failed for %s: %s", source.__name__, icao_hex, exc)
    return None


# ---------------------------------------------------------------------------
# Shared lookup ladder (cache → JOIN → fetch → probe)
# ---------------------------------------------------------------------------

# Default cache horizon, mirrored from config.PHOTO_CACHE_DAYS by callers.
_DEFAULT_CACHE_SECONDS = 30 * 86400


def _row_to_result(row: sqlite3.Row | dict) -> dict:
    return {
        "thumbnail_url": row["thumbnail_url"],
        "large_url":     row["large_url"]      if "large_url"     in row.keys() else None,
        "link_url":      row["link_url"]       if "link_url"      in row.keys() else None,
        "photographer":  row["photographer"]   if "photographer"  in row.keys() else None,
    }


def _write_specific(conn: sqlite3.Connection, icao_hex: str,
                    result: dict | None, now: int) -> None:
    if result:
        conn.execute(
            "INSERT OR REPLACE INTO photos "
            "(icao_hex, thumbnail_url, large_url, link_url, photographer, fetched_at) "
            "VALUES (?,?,?,?,?,?)",
            (icao_hex, result["thumbnail_url"], result.get("large_url"),
             result.get("link_url"), result.get("photographer"), now),
        )
    else:
        conn.execute(
            "INSERT OR REPLACE INTO photos "
            "(icao_hex, thumbnail_url, large_url, link_url, photographer, fetched_at) "
            "VALUES (?,NULL,NULL,NULL,NULL,?)",
            (icao_hex, now),
        )
    conn.commit()


def _write_type(conn: sqlite3.Connection, type_code: str,
                result: dict | None, now: int) -> None:
    if result:
        conn.execute(
            "INSERT OR REPLACE INTO type_photos "
            "(type_code, thumbnail_url, large_url, link_url, photographer, fetched_at) "
            "VALUES (?,?,?,?,?,?)",
            (type_code, result["thumbnail_url"], result.get("large_url"),
             result.get("link_url"), result.get("photographer"), now),
        )
    else:
        conn.execute(
            "INSERT OR REPLACE INTO type_photos "
            "(type_code, thumbnail_url, large_url, link_url, photographer, fetched_at) "
            "VALUES (?,NULL,NULL,NULL,NULL,?)",
            (type_code, now),
        )
    conn.commit()


def resolve_photo(
    conn: sqlite3.Connection,
    icao_hex: str,
    type_code: str | None,
    *,
    fetcher: Callable[[str], PhotoResult | None] | None = None,
    cache_seconds: int = _DEFAULT_CACHE_SECONDS,
) -> tuple[dict | None, bool]:
    """Five-step photo lookup.  Returns ``(result | None, is_type_photo)``.

    Order:
      1. ``photos`` cache hit for the specific ICAO (incl. negative cache).
      2. ``type_photos`` cache hit (incl. negative cache).
      3. ``photos JOIN aircraft_db`` — reuse any cached photo of the same type.
      4. ``fetcher(icao_hex)`` — full source chain for the specific ICAO.
      5. ``fetcher(probe_icao)`` — full source chain for one ICAO of the same
         type pulled from ``aircraft_db``.

    The result dict has keys ``thumbnail_url``, ``large_url``, ``link_url``,
    ``photographer``.  ``is_type_photo`` is True iff the photo represents the
    aircraft type rather than the specific airframe.
    """
    # Resolve fetcher at call-time so monkeypatched ``fetch_photo`` is honoured.
    if fetcher is None:
        fetcher = fetch_photo
    now = int(time.time())
    cutoff = now - cache_seconds

    # 1. specific aircraft cache
    row = conn.execute(
        "SELECT thumbnail_url, large_url, link_url, photographer, fetched_at "
        "FROM photos WHERE icao_hex = ? AND fetched_at > ?",
        (icao_hex, cutoff),
    ).fetchone()
    if row is not None:
        return (_row_to_result(row) if row["thumbnail_url"] else None, False)

    # 2. type cache
    if type_code:
        row = conn.execute(
            "SELECT thumbnail_url, large_url, link_url, photographer, fetched_at "
            "FROM type_photos WHERE type_code = ? AND fetched_at > ?",
            (type_code, cutoff),
        ).fetchone()
        if row is not None:
            return (_row_to_result(row) if row["thumbnail_url"] else None, True)

    # 3. photos JOIN aircraft_db — zero HTTP
    if type_code:
        row = conn.execute(
            """
            SELECT p.thumbnail_url, p.large_url, p.link_url, p.photographer
            FROM photos p
            JOIN aircraft_db adb ON adb.icao_hex = p.icao_hex
            WHERE adb.type_code = ? AND p.thumbnail_url IS NOT NULL
            ORDER BY p.fetched_at DESC LIMIT 1
            """,
            (type_code,),
        ).fetchone()
        if row:
            result = _row_to_result(row)
            _write_type(conn, type_code, result, now)
            return result, True

    # 4. fetch for specific ICAO
    photo = fetcher(icao_hex)
    if photo:
        result = {
            "thumbnail_url": photo.thumbnail_url,
            "large_url":     photo.large_url,
            "link_url":      photo.link_url,
            "photographer":  photo.photographer,
        }
        _write_specific(conn, icao_hex, result, now)
        return result, False
    _write_specific(conn, icao_hex, None, now)

    # 5. probe one ICAO of the same type
    if type_code:
        probe_row = conn.execute(
            "SELECT icao_hex FROM aircraft_db WHERE type_code = ? LIMIT 1",
            (type_code,),
        ).fetchone()
        if probe_row:
            probe_icao = probe_row["icao_hex"]
            probe = fetcher(probe_icao)
            if probe:
                result = {
                    "thumbnail_url": probe.thumbnail_url,
                    "large_url":     probe.large_url,
                    "link_url":      probe.link_url,
                    "photographer":  probe.photographer,
                }
                _write_specific(conn, probe_icao, result, now)
                _write_type(conn, type_code, result, now)
                return result, True
        _write_type(conn, type_code, None, now)

    return None, False
