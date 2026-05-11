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

from . import config, http_safe

log = logging.getLogger("photo_sources")

PHOTO_UA = http_safe._USER_AGENT

# Per-source response cap — JSON payloads from Planespotters / airport-data /
# hexdb are a few KB at most.  An adversarial upstream returning a multi-GB
# response would otherwise OOM the collector.
_JSON_MAX_BYTES = 256 * 1024

# Wikipedia fallback — env-gated via ``config.WIKIPEDIA_PHOTO`` (env var
# ``RSBS_WIKIPEDIA_PHOTO``, default on).  When False, step 6 of resolve_photo
# is skipped so a type with no specific-aircraft photo lands in the negative
# cache without ever hitting Wikipedia.  Used as a kill-switch when Wikipedia
# mis-attributes an article for a particular ICAO type designator.
#
# Mirrored to a module-level constant so tests can monkeypatch it cheaply
# (``monkeypatch.setattr(photo_sources, "_WIKIPEDIA_ENABLED", False)``) without
# touching the parsed config singleton.
_WIKIPEDIA_ENABLED = config.WIKIPEDIA_PHOTO
# Wikipedia's API policy expects a descriptive User-Agent that identifies the
# tool and provides a way to contact the operator.
_WIKIPEDIA_UA_EXTRA = {
    "User-Agent": (
        "readsbstats (+https://github.com/blindp3w/readsbstats) "
        "Wikipedia type-photo lookup"
    )
}
# Defence-in-depth: refuse to cache or serve a "Wikipedia" photo whose URL
# doesn't live on the expected Wikimedia hosts.  In normal operation the REST
# summary returns thumbnails on upload.wikimedia.org and the article page on
# en.wikipedia.org — anything else would be either a wiki vandalism edit or an
# upstream API bug, and not something we want to render under the "Wikipedia"
# photographer label.
_WIKIPEDIA_IMAGE_HOSTS = ("upload.wikimedia.org",)
_WIKIPEDIA_PAGE_HOSTS = ("en.wikipedia.org",)


def _url_host_matches(url: str | None, hosts: tuple[str, ...]) -> bool:
    if not url:
        return False
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.hostname in hosts


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
# Type-keyed fallback (Wikipedia) — consulted by resolve_photo() after every
# specific-aircraft source misses for both the original ICAO and a probe ICAO
# of the same type.  Not part of SOURCES because SOURCES is icao-keyed.
# ---------------------------------------------------------------------------

def _fetch_wikipedia_type(type_desc: str) -> PhotoResult | None:
    """Resolve an aircraft type description to a representative Wikipedia photo.

    Two hops:
      1) opensearch — resolve ``type_desc`` (e.g. ``"BOEING 737-800"``) to a
         canonical article title (handles fuzzy match → "Boeing 737 Next
         Generation").
      2) REST summary — return ``thumbnail`` + ``originalimage`` + article URL.

    Returns ``None`` on miss (no opensearch hit, no thumbnail, disambiguation
    page, malformed response, 400/404/410).  Other HTTP errors propagate to the
    caller so the broad ``except Exception`` in :func:`resolve_photo` logs
    them at DEBUG and writes the standard negative ``type_photos`` row.

    Photo and article URLs are constrained to known Wikimedia hosts
    (``upload.wikimedia.org`` for images, ``en.wikipedia.org`` for the article
    link).  Anything else is rejected — defence-in-depth against a wiki-edit
    pointing the infobox image at an unrelated host.
    """
    desc = (type_desc or "").strip()
    if not desc:
        return None
    open_url = (
        "https://en.wikipedia.org/w/api.php?action=opensearch"
        f"&format=json&limit=1&namespace=0&search={urllib.parse.quote(desc, safe='')}"
    )
    body, _ = _safe_open(
        open_url, timeout=6, max_bytes=_JSON_MAX_BYTES,
        extra_headers=_WIKIPEDIA_UA_EXTRA,
    )
    arr = json.loads(body)
    # Opensearch is documented as [query, titles, descriptions, urls] but defend
    # against shape drift / error envelopes.
    if not (isinstance(arr, list) and len(arr) > 1 and isinstance(arr[1], list)):
        return None
    titles = arr[1]
    if not titles or not isinstance(titles[0], str) or not titles[0]:
        return None
    title = titles[0]
    summary_url = (
        "https://en.wikipedia.org/api/rest_v1/page/summary/"
        + urllib.parse.quote(title.replace(" ", "_"), safe="")
    )
    try:
        body, _ = _safe_open(
            summary_url, timeout=6, max_bytes=_JSON_MAX_BYTES,
            extra_headers=_WIKIPEDIA_UA_EXTRA,
        )
    except urllib.error.HTTPError as e:
        # 400 (malformed title), 404 (no such article), 410 (gone) are all
        # permanent misses.  429 / 5xx propagate so the caller can decide.
        if e.code in (400, 404, 410):
            return None
        raise
    data = json.loads(body)
    if not isinstance(data, dict):
        return None
    if data.get("type") == "disambiguation":
        return None
    thumb = (data.get("thumbnail") or {}).get("source")
    if not isinstance(thumb, str) or not _url_host_matches(thumb, _WIKIPEDIA_IMAGE_HOSTS):
        return None
    orig_raw = (data.get("originalimage") or {}).get("source")
    orig = orig_raw if (isinstance(orig_raw, str)
                        and _url_host_matches(orig_raw, _WIKIPEDIA_IMAGE_HOSTS)) else thumb
    page_raw = (((data.get("content_urls") or {}).get("desktop") or {}).get("page"))
    page = page_raw if (isinstance(page_raw, str)
                        and _url_host_matches(page_raw, _WIKIPEDIA_PAGE_HOSTS)) else None
    return PhotoResult(
        thumbnail_url=thumb,
        large_url=orig,
        link_url=page,
        photographer="Wikipedia",
    )


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
    """Six-step photo lookup.  Returns ``(result | None, is_type_photo)``.

    Order:
      1. ``photos`` cache hit for the specific ICAO (incl. negative cache).
      2. ``type_photos`` cache hit (incl. negative cache).
      3. ``photos JOIN aircraft_db`` — reuse any cached photo of the same type.
      4. ``fetcher(icao_hex)`` — full source chain for the specific ICAO.
      5. ``fetcher(probe_icao)`` — full source chain for one ICAO of the same
         type pulled from ``aircraft_db``.
      6. ``_fetch_wikipedia_type(probe_desc)`` — Wikipedia opensearch + REST
         summary keyed on ``aircraft_db.type_desc``.  Gated by
         ``RSBS_WIKIPEDIA_PHOTO`` (default on).

    Type-only mode: pass ``icao_hex=""`` to skip steps 1 and 4 — used by
    ``web._fetch_type_photo`` so it doesn't pollute ``photos`` with an
    empty-key row.

    The result dict has keys ``thumbnail_url``, ``large_url``, ``link_url``,
    ``photographer``.  ``is_type_photo`` is True iff the photo represents the
    aircraft type rather than the specific airframe.
    """
    # Resolve fetcher at call-time so monkeypatched ``fetch_photo`` is honoured.
    if fetcher is None:
        fetcher = fetch_photo
    now = int(time.time())
    cutoff = now - cache_seconds

    # 1. specific aircraft cache (type-only callers pass icao_hex="" and skip here)
    if icao_hex:
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

    # 4. fetch for specific ICAO (skip in type-only mode, icao_hex="")
    if icao_hex:
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
            "SELECT icao_hex, type_desc FROM aircraft_db WHERE type_code = ? LIMIT 1",
            (type_code,),
        ).fetchone()
        probe_desc = ""
        if probe_row:
            probe_icao = probe_row["icao_hex"]
            probe_desc = (probe_row["type_desc"] or "").strip()
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

        # 6. Wikipedia fallback — keyed on aircraft_db.type_desc.  Logs are at
        # DEBUG to match the rest of the photo chain; for ongoing visibility,
        # query ``SELECT … FROM type_photos WHERE photographer='Wikipedia'``.
        if _WIKIPEDIA_ENABLED and probe_desc:
            try:
                wiki = _fetch_wikipedia_type(probe_desc)
            except Exception as exc:
                log.debug("wikipedia type-photo lookup failed for %s (%r): %s",
                          type_code, probe_desc, exc)
                wiki = None
            if wiki:
                log.debug("wikipedia type-photo hit for %s (%r) -> %s",
                          type_code, probe_desc, wiki.link_url or wiki.thumbnail_url)
                result = {
                    "thumbnail_url": wiki.thumbnail_url,
                    "large_url":     wiki.large_url,
                    "link_url":      wiki.link_url,
                    "photographer":  wiki.photographer,
                }
                _write_type(conn, type_code, result, now)
                return result, True
            log.debug("wikipedia type-photo miss for %s (%r)", type_code, probe_desc)

        _write_type(conn, type_code, None, now)

    return None, False
