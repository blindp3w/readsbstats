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


# ---------------------------------------------------------------------------
# BE-17 — per-source CDN host allowlists.  Specific-aircraft providers
# occasionally return image/link URLs on unexpected hosts (upstream bug,
# cache poisoning, a compromised mirror).  Validate returned URLs against the
# source's own host set before they are persisted to ``photos``/``type_photos``
# or rendered.  Hosts are matched by *suffix* so subdomain drift
# (``t.plnspttrs.net``, ``image.airport-data.com``) keeps working without
# enumerating every CDN edge.
# ---------------------------------------------------------------------------
_PLANESPOTTERS_HOSTS = ("plnspttrs.net", "planespotters.net")
_AIRPORTDATA_HOSTS = ("airport-data.com",)
# hexdb.io returns a bare image URL pointing at whichever upstream CDN it
# resolved (commonly airport-data / planespotters), so its allowlist is the
# union of the known photo CDNs plus its own host.
_HEXDB_HOSTS = _PLANESPOTTERS_HOSTS + _AIRPORTDATA_HOSTS + ("hexdb.io",)

# Fetch-time enforcement of the per-source allowlists. Default OFF (log-only)
# so a legitimate-but-unenumerated CDN host isn't silently dropped at fetch
# time; the API-boundary suppression in api/_photos is_photo_url_allowed
# always filters off-allowlist URLs out of API responses regardless. Mirrored
# to a module global so tests can monkeypatch it cheaply
# (`monkeypatch.setattr(photo_sources, "_HOST_ENFORCE", True)`).
_HOST_ENFORCE = config.PHOTO_HOST_ENFORCE


def _host_suffix_matches(url: str | None, suffixes: tuple[str, ...]) -> bool:
    """True if *url* is empty (nothing to render) or its https host ends with
    one of *suffixes*.  A non-https or unparseable URL never matches."""
    if not url:
        return True
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        return False
    return any(host == s or host.endswith("." + s) for s in suffixes)


# PY-6 (Audit 2026-05-31): union of every per-source CDN allowlist used
# by the photo ladder. Used as the authoritative API-boundary pre-render
# check so cached off-allowlist URLs (written before BE-17 enforcement
# was tightened) are filtered out before the JSON envelope leaves Python,
# even when _HOST_ENFORCE is False (log-only mode). Wikipedia thumbnails
# come from upload.wikimedia.org; article-link enforcement happens in
# _fetch_wikipedia_type itself.
_ALL_PHOTO_HOSTS: tuple[str, ...] = (
    _PLANESPOTTERS_HOSTS
    + _AIRPORTDATA_HOSTS
    + ("hexdb.io", "image.airport-data.com", "upload.wikimedia.org",
       "en.wikipedia.org")
)


def is_photo_url_allowed(url: str | None) -> bool:
    """True if *url* is empty (nothing to render) OR its https host is on
    the union of per-source allowlists. Non-https or unparseable URLs
    return False. Used at the API boundary to suppress off-allowlist
    URLs from API responses regardless of ``_HOST_ENFORCE``."""
    return _host_suffix_matches(url, _ALL_PHOTO_HOSTS)


def _check_hosts(result: PhotoResult | None, source: str,
                 suffixes: tuple[str, ...]) -> PhotoResult | None:
    """Validate a source's returned URLs against its CDN host allowlist.

    Log-only by default (``_HOST_ENFORCE`` False): off-allowlist hosts are
    logged at WARNING and the URL is kept, so we observe real-world hosts for
    one release before enforcing.  Under enforcement a bad *thumbnail* host
    drops the whole result (it is unusable), while a bad *large/link* host
    nulls just that field so the thumbnail still renders.
    """
    if result is None:
        return None
    for field in ("thumbnail_url", "large_url", "link_url"):
        url = getattr(result, field)
        if _host_suffix_matches(url, suffixes):
            continue
        host = (urllib.parse.urlparse(url).hostname or "?")
        log.warning("photo host off-allowlist: source=%s field=%s host=%s%s",
                    source, field, host, "" if _HOST_ENFORCE else " (log-only)")
        if not _HOST_ENFORCE:
            continue
        if field == "thumbnail_url":
            return None
        setattr(result, field, None)
    return result


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
_validate_url = http_safe.validate_url
_ip_is_public = http_safe._ip_is_public
_safe_open = http_safe.safe_urlopen
# Note: pre-Phase-9 we also re-exported ``_no_redirect_opener``, but the
# Phase 9 redesign of safe_urlopen builds a fresh opener per call (see
# http_safe._build_pinned_opener) so a module-level singleton no longer
# exists. Tests that need to intercept the fetch should monkey-patch
# ``http_safe._build_pinned_opener`` to return a mock.


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
    return _check_hosts(PhotoResult(
        thumbnail_url=thumb,
        large_url=(p.get("thumbnail_large") or {}).get("src"),
        link_url=p.get("link"),
        photographer=p.get("photographer"),
    ), "planespotters", _PLANESPOTTERS_HOSTS)


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
    # The API returns the thumbnail URL on the `/thumbnails/` subpath
    # (~150 px wide, ~2 KB). The full-resolution image is NOT at the
    # same host with `/thumbnails/` stripped — that path 404s. The
    # source HTML for the photo page references the full-res image at
    # the dedicated CDN host: `image.airport-data.com/aircraft/<file>`
    # (verified ~40× larger payload). Derive that URL from the
    # thumbnail's basename so the photo lightbox isn't a blurry upscale.
    large = img
    if "/images/aircraft/thumbnails/" in img:
        basename = img.rsplit("/", 1)[-1]
        if basename:
            large = f"https://image.airport-data.com/aircraft/{basename}"
    return _check_hosts(PhotoResult(
        thumbnail_url=img,
        large_url=large,
        link_url=item.get("link"),
        photographer=item.get("photographer"),
    ), "airport-data", _AIRPORTDATA_HOSTS)


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
    return _check_hosts(PhotoResult(thumbnail_url=text, large_url=text),
                        "hexdb", _HEXDB_HOSTS)


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


def _fetch_photo_with_status(icao_hex: str) -> tuple[PhotoResult | None, str]:
    """Try each source in SOURCES order; return ``(result, status)``.

    ``status`` is one of:

    * ``"hit"``   — a source returned a usable result.
    * ``"miss"``  — every source completed cleanly and returned ``None``.
    * ``"error"`` — no source returned a result AND at least one raised.

    Audit 2026-05-25: the ``"miss"`` vs ``"error"`` distinction lets
    :func:`resolve_photo` skip writing a negative cache row when the chain
    failed transiently (DNS hiccup, rate-limit, source schema change). A
    confirmed unknown stays in the cache; a transient outage leaves a
    previously-resolved positive row in place so subsequent requests keep
    serving it instead of an empty result for ``PHOTO_CACHE_DAYS``.
    """
    had_error = False
    for source in SOURCES:
        try:
            result = source(icao_hex)
            if result:
                return result, "hit"
        except Exception as exc:
            log.debug("%s failed for %s: %s", source.__name__, icao_hex, exc)
            had_error = True
    return None, ("error" if had_error else "miss")


def fetch_photo(icao_hex: str) -> PhotoResult | None:
    """Try each source in SOURCES order; return the first hit or None.

    Thin wrapper over :func:`_fetch_photo_with_status` that drops the status
    string for callers that only need the result.
    """
    result, _status = _fetch_photo_with_status(icao_hex)
    return result


def fetch_photo_with_status(icao_hex: str) -> tuple[PhotoResult | None, str]:
    """Public alias of :func:`_fetch_photo_with_status` — returns
    ``(result, status)`` where ``status`` is ``"hit"``, ``"miss"``, or
    ``"error"``.

    PY-5 (Audit 2026-05-31): API-side photo handlers should call this
    instead of :func:`fetch_photo` so that a transient outage (every
    source raised) doesn't get cached as a 30-day confirmed miss.
    """
    return _fetch_photo_with_status(icao_hex)


# Captured at module load so :func:`resolve_photo` and api/_photos can
# tell whether tests have monkey-patched ``fetch_photo`` away from this
# wrapper. When patched, the patched function is authoritative and the
# status-aware grace is bypassed (tests inject deterministic results
# and don't want callers to second-guess them). In production this
# identity check is True so callers use the status-aware helper.
_DEFAULT_FETCH_PHOTO = fetch_photo


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
    ``api._photos._fetch_type_photo`` so it doesn't pollute ``photos`` with
    an empty-key row.

    The result dict has keys ``thumbnail_url``, ``large_url``, ``link_url``,
    ``photographer``.  ``is_type_photo`` is True iff the photo represents the
    aircraft type rather than the specific airframe.
    """
    # Resolve fetcher at call-time so monkeypatched ``fetch_photo`` is honoured.
    # When no fetcher is injected AND ``fetch_photo`` has not been
    # monkey-patched (production path), use the status-aware helper so
    # transient source outages don't poison the cache (audit 2026-05-25).
    # Tests that inject a ``fetcher`` or patch ``fetch_photo`` directly keep
    # the legacy "None == confirmed miss" contract since they control the
    # return value deterministically.
    if fetcher is None:
        fetcher = fetch_photo
    use_status_helper = fetcher is _DEFAULT_FETCH_PHOTO
    now = int(time.time())
    cutoff = now - cache_seconds

    def _call_fetcher(target: str) -> tuple[PhotoResult | None, str]:
        if use_status_helper:
            return _fetch_photo_with_status(target)
        photo = fetcher(target)
        return photo, ("hit" if photo else "miss")

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

    # Track whether any source chain hit a transient error during this call.
    # An error anywhere (specific fetch or probe fetch) suppresses the
    # type-level negative write at step 6 too, so a flaky source doesn't
    # poison the type cache for `PHOTO_CACHE_DAYS`.
    chain_errored = False

    # 4. fetch for specific ICAO (skip in type-only mode, icao_hex="")
    if icao_hex:
        photo, status = _call_fetcher(icao_hex)
        if photo:
            result = {
                "thumbnail_url": photo.thumbnail_url,
                "large_url":     photo.large_url,
                "link_url":      photo.link_url,
                "photographer":  photo.photographer,
            }
            _write_specific(conn, icao_hex, result, now)
            return result, False
        if status == "error":
            chain_errored = True
            # Stale-grace: return any previously-resolved positive row rather
            # than evicting it. Mirrors the per-spot grace in
            # `web._fetch_photo` so the shared resolver behaves the same way.
            stale = conn.execute(
                "SELECT thumbnail_url, large_url, link_url, photographer "
                "FROM photos WHERE icao_hex = ? AND thumbnail_url IS NOT NULL",
                (icao_hex,),
            ).fetchone()
            if stale:
                return _row_to_result(stale), False
            # No stale row to serve and no confirmed miss — leave the cache
            # untouched so the next batch retries.
        else:
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
            probe, probe_status = _call_fetcher(probe_icao)
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
            if probe_status == "error":
                chain_errored = True

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

        if chain_errored:
            # A transient source error during the specific or probe fetch
            # means we can't tell "type has no photo anywhere" from "we
            # couldn't reach a source this minute". Serve any stale positive
            # type row, or just return None — but don't poison the cache.
            stale = conn.execute(
                "SELECT thumbnail_url, large_url, link_url, photographer "
                "FROM type_photos WHERE type_code = ? AND thumbnail_url IS NOT NULL",
                (type_code,),
            ).fetchone()
            if stale:
                return _row_to_result(stale), True
        else:
            _write_type(conn, type_code, None, now)

    return None, False
