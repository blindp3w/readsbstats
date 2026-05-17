"""
readsbstats — Telegram notification helper.

Sends alerts for:
  - Military / interesting aircraft (first sighting only)
  - Emergency squawks (7500 / 7600 / 7700)
  - Daily summary at a configurable local time

Also runs an interactive command listener (long polling) in a daemon thread:
  /summary  — on-demand daily summary
  /status   — currently tracked aircraft + today's flight count
  /help     — list commands

All functions are no-ops when RSBS_TELEGRAM_TOKEN / RSBS_TELEGRAM_CHAT_ID
are not set, so the feature is fully opt-in.
"""
from __future__ import annotations

import datetime
import html as _html
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, database, http_safe, icao_ranges, photo_sources

log = logging.getLogger("notifier")

EMERGENCY_SQUAWKS = {"7500", "7600", "7700"}
_SQUAWK_LABELS    = {"7500": "Hijack", "7600": "Radio failure", "7700": "Emergency"}

# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------

_tg_enabled:   bool | None = None   # cached result
_tg_validated: bool        = False   # whether we've run validation


def telegram_enabled() -> bool:
    """Return True if Telegram is properly configured. Validates once, caches."""
    global _tg_enabled, _tg_validated
    if _tg_validated:
        return _tg_enabled  # type: ignore[return-value]

    token   = config.TELEGRAM_TOKEN.strip()
    chat_id = config.TELEGRAM_CHAT_ID.strip()

    _tg_validated = True

    # Both empty — intentionally disabled, no warning
    if not token and not chat_id:
        _tg_enabled = False
        return False

    # One set, one missing
    if not token:
        log.warning("Telegram disabled: RSBS_TELEGRAM_TOKEN is empty "
                     "(RSBS_TELEGRAM_CHAT_ID is set)")
        _tg_enabled = False
        return False
    if not chat_id:
        log.warning("Telegram disabled: RSBS_TELEGRAM_CHAT_ID is empty "
                     "(RSBS_TELEGRAM_TOKEN is set)")
        _tg_enabled = False
        return False

    # Chat ID must be numeric (negative for groups/supergroups)
    try:
        int(chat_id)
    except ValueError:
        log.warning("Telegram disabled: RSBS_TELEGRAM_CHAT_ID=%r is not a "
                     "valid numeric chat ID", chat_id)
        _tg_enabled = False
        return False

    # Validate units (non-fatal — falls back to metric)
    units = config.TELEGRAM_UNITS.strip().lower()
    if units not in ("metric", "imperial", "aeronautical"):
        log.warning("Invalid RSBS_TELEGRAM_UNITS=%r — expected metric, "
                     "imperial, or aeronautical; falling back to metric",
                     config.TELEGRAM_UNITS)

    _tg_enabled = True
    return True


# ---------------------------------------------------------------------------
# Unit formatting
# ---------------------------------------------------------------------------

def _fmt_dist(nm: float | None) -> str:
    if nm is None:
        return "?"
    if config.TELEGRAM_UNITS == "imperial":
        return f"{nm * 1.15078:.0f} mi"
    if config.TELEGRAM_UNITS == "aeronautical":
        return f"{nm:.0f} nm"
    return f"{nm * 1.852:.0f} km"          # metric (default)


def _fmt_alt(ft: int | None) -> str:
    if ft is None:
        return "?"
    if config.TELEGRAM_UNITS in ("imperial", "aeronautical"):
        return f"{ft:,} ft"
    return f"{round(ft * 0.3048):,} m"    # metric (default)


def _fmt_spd(kts: float | None) -> str:
    if kts is None:
        return "?"
    if config.TELEGRAM_UNITS == "imperial":
        return f"{kts * 1.15078:.0f} mph"
    if config.TELEGRAM_UNITS == "aeronautical":
        return f"{kts:.0f} kts"
    return f"{kts * 1.852:.0f} km/h"       # metric (default)


# ---------------------------------------------------------------------------
# Telegram transport
# ---------------------------------------------------------------------------

def _describe_exc(exc: BaseException) -> str:
    """Format a urllib exception for logging without ever revealing the
    request URL (which would expose the bot token in the path).  Current
    stdlib `str()` formatting on HTTPError/URLError does not leak the URL,
    but third-party libs and future stdlib changes might — so we extract
    only fields known to be URL-free."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        return f"URLError: {exc.reason}"
    return type(exc).__name__


def _send(text: str) -> bool:
    """POST a message to the configured Telegram bot. Returns True on success.

    Routes through :func:`http_safe.safe_urlopen` so the policy is consistent
    with every other outbound call (HTTPS-only, no-redirect, size cap).
    See improvements.md #124.
    """
    if not telegram_enabled():
        return False
    try:
        payload = json.dumps({
            "chat_id":                  config.TELEGRAM_CHAT_ID,
            "text":                     text,
            "parse_mode":               "HTML",
            "disable_web_page_preview": True,
        }).encode()
        http_safe.safe_urlopen(
            f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage",
            timeout=10,
            max_bytes=65_536,  # sendMessage response is {"ok":true,"result":{...}} — small
            extra_headers={"Content-Type": "application/json"},
            data=payload,
        )
        return True
    except Exception as exc:
        log.warning("Telegram send failed: %s", _describe_exc(exc))
        return False


# ---------------------------------------------------------------------------
# Photo helpers
# ---------------------------------------------------------------------------

# Thread-local sqlite connection.  The collector's dispatch consumer thread
# sets ``conn`` at startup and clears it on shutdown so that every notification
# in that thread reuses the same connection (avoiding open/close churn).
# Tests and other callers leave it unset and we fall back to opening a fresh
# connection against ``config.DB_PATH`` per call.
_thread_local = threading.local()


def _get_photo_result(
    icao_hex: str,
    type_code: str | None,
    *,
    conn: sqlite3.Connection | None = None,
) -> tuple[str | None, bool]:
    """Return ``(thumbnail_url | None, is_type_photo)`` via the shared ladder
    in :func:`photo_sources.resolve_photo`.

    Uses, in order of preference: an explicitly passed ``conn``, the
    thread-local connection set by the dispatch consumer, or a fresh
    short-lived connection against ``config.DB_PATH``.
    """
    if conn is None:
        conn = getattr(_thread_local, "conn", None)
    if conn is not None:
        try:
            result, is_type = photo_sources.resolve_photo(
                conn, icao_hex, type_code,
                cache_seconds=config.PHOTO_CACHE_DAYS * 86400,
            )
            url = result["thumbnail_url"] if result else None
            return url, is_type
        except Exception as exc:
            log.debug("photo lookup failed for %s: %s", icao_hex, exc)
            return None, False

    if not config.DB_PATH:
        return None, False
    try:
        # Use database.connect() so the fresh connection picks up the
        # project's WAL/synchronous/mmap/busy_timeout pragmas (audit-12 #153).
        # Bare sqlite3.connect() inherited Python defaults — slower writes
        # with synchronous=FULL and no busy_timeout for collector contention.
        fresh = database.connect(config.DB_PATH)
        try:
            result, is_type = photo_sources.resolve_photo(
                fresh, icao_hex, type_code,
                cache_seconds=config.PHOTO_CACHE_DAYS * 86400,
            )
            url = result["thumbnail_url"] if result else None
            return url, is_type
        finally:
            fresh.close()
    except Exception as exc:
        log.debug("photo lookup failed for %s: %s", icao_hex, exc)
        return None, False


_MAX_PHOTO_BYTES = 10 * 1024 * 1024  # Telegram's sendPhoto limit
_PHOTO_CAPTION_MAX = 1024            # Telegram's caption limit on sendPhoto

_MIME_TO_FILENAME: dict[str, str] = {
    "image/jpeg": "photo.jpg",
    "image/png":  "photo.png",
    "image/webp": "photo.webp",
}


def _download_photo(url: str) -> tuple[bytes, str] | None:
    """Fetch image bytes and mime type from *url* via
    :func:`photo_sources._safe_open` (HTTPS-only, redirect-blocked, IP-gated).

    Returns ``(bytes, mime_type)`` or ``None`` on any failure (network, policy
    violation, oversize).
    """
    try:
        data, headers = photo_sources._safe_open(
            url, timeout=8, max_bytes=_MAX_PHOTO_BYTES,
        )
        mime = (headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
        return data, mime
    except Exception as exc:
        log.debug("photo download failed for %s: %s", url, exc)
        return None


def _multipart_photo(
    chat_id: str,
    image_bytes: bytes,
    caption: str,
    content_type: str = "image/jpeg",
    boundary: str | None = None,
) -> tuple[bytes, str]:
    """Build a multipart/form-data body for sendPhoto file upload.

    Returns ``(body_bytes, boundary_str)``.  A fresh random boundary is used
    per call so that adversarial caption / chat_id content cannot prematurely
    terminate the body."""
    if boundary is None:
        boundary = "----RSBS" + secrets.token_hex(16)
    b        = boundary.encode()
    filename = _MIME_TO_FILENAME.get(content_type, "photo.jpg").encode()
    ct_bytes = content_type.encode()
    parts: list[bytes] = []

    def field(name: str, value: str) -> bytes:
        return (
            b"--" + b + b"\r\n"
            + b'Content-Disposition: form-data; name="' + name.encode() + b'"\r\n\r\n'
            + value.encode() + b"\r\n"
        )

    parts.append(field("chat_id", chat_id))
    parts.append(field("caption", caption))
    parts.append(field("parse_mode", "HTML"))
    parts.append(
        b"--" + b + b"\r\n"
        + b'Content-Disposition: form-data; name="photo"; filename="' + filename + b'"\r\n'
        + b"Content-Type: " + ct_bytes + b"\r\n\r\n"
        + image_bytes + b"\r\n"
    )
    parts.append(b"--" + b + b"--\r\n")
    return b"".join(parts), boundary


def _send_photo(photo_url: str, caption: str) -> bool:
    """POST a photo message to Telegram.

    Images are downloaded first and uploaded as multipart bytes —
    Planespotters' hotlink protection blocks direct Telegram fetches, so the
    URL-payload path has been removed.  On any failure (non-https URL, download
    error, upload error) we fall back to a plain text message.
    """
    if not telegram_enabled():
        return False
    if not photo_url.startswith("https://"):
        return _send(caption)
    download = _download_photo(photo_url)
    if not download:
        return _send(caption)
    image_bytes, content_type = download
    body, boundary = _multipart_photo(
        config.TELEGRAM_CHAT_ID, image_bytes, caption, content_type,
    )
    try:
        http_safe.safe_urlopen(
            f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendPhoto",
            timeout=15,
            max_bytes=65_536,  # sendPhoto response is {"ok":true,"result":{...}} — small
            extra_headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            data=body,
        )
        return True
    except Exception as exc:
        log.warning("Telegram sendPhoto failed: %s — falling back to text",
                    _describe_exc(exc))
        return _send(caption)


# ---------------------------------------------------------------------------
# Alert messages
# ---------------------------------------------------------------------------

def _h(s: str | None) -> str:
    """HTML-escape a value for safe interpolation into Telegram ``parse_mode=HTML``
    messages.  Telegram returns 400 on unescaped ``<``, ``>``, ``&`` even
    inside text nodes — and the whole message is then dropped because the
    text fallback also uses HTML mode.  Apply this to every dynamic field
    that could carry user-supplied or third-party-sourced characters
    (registrations, callsigns, watchlist labels, type descriptions)."""
    return _html.escape(s or "")


def _fmt_aircraft_line(
    icao: str,
    registration: str | None,
    callsign: str | None,
    type_desc: str | None = None,
    aircraft_type: str | None = None,
) -> tuple[str, str, str]:
    """Return ``(reg, callsign_suffix, aircraft_type)`` already HTML-escaped
    so callers can interpolate the tuple straight into HTML captions."""
    reg = _h(registration or icao.upper())
    cs  = f" ({_h(callsign)})" if callsign else ""
    ac  = _h(type_desc or aircraft_type or "Unknown type")
    return reg, cs, ac


# Trailing-link-line pattern.  Captions are built so the last line is one or
# two ``<a href="…">…</a>`` anchors; the photo note (if any) is appended below.
# A plain truncation can land inside the link's ``href`` attribute and corrupt
# the HTML, so over-limit captions strip whole lines instead of cutting tags.
_PHOTO_NOTE_RE = re.compile(r"\n<i>Photo: [^<]*</i>\s*$")
_TRAILING_LINK_LINE_RE = re.compile(r"\n[^\n]*<a href=\"[^\"]+\">[^\n]*$")


def _clamp_caption(caption: str, limit: int = _PHOTO_CAPTION_MAX) -> str:
    """Trim *caption* to Telegram's photo-caption ``limit`` (1024) without
    cutting through HTML tags.

    Strategy (only invoked when over-limit):
      1. Drop the optional trailing ``<i>Photo: …</i>`` note line.
      2. Drop the trailing ``<a href="…">…</a>`` link line(s).
      3. Plain-truncate the body with ``…`` as a last resort.

    Steps 1 and 2 use anchored regexes so they only match well-formed
    structures our own builders produce.
    """
    if len(caption) <= limit:
        return caption
    stripped = _PHOTO_NOTE_RE.sub("", caption)
    if len(stripped) <= limit:
        return stripped
    stripped = _TRAILING_LINK_LINE_RE.sub("", stripped)
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 1] + "…"


# Back-compat alias for any external callers / tests that still reference the
# old name.  New code should use ``_clamp_caption``.
_truncate_caption = _clamp_caption


def _dispatch_with_photo(
    caption: str,
    icao: str,
    aircraft_type: str | None,
    type_desc: str | None,
) -> None:
    """Send *caption* via sendPhoto if a photo is available, else sendMessage."""
    if config.TELEGRAM_PHOTOS:
        photo_url, is_type = _get_photo_result(icao, aircraft_type)
        if is_type and photo_url:
            caption += (
                f"\n<i>Photo: {_h(type_desc or aircraft_type)} "
                f"— not this specific aircraft</i>"
            )
        if photo_url:
            _send_photo(photo_url, _clamp_caption(caption))
            return
    _send(caption)


def notify_military(
    icao:          str,
    registration:  str | None,
    callsign:      str | None,
    type_desc:     str | None,
    aircraft_type: str | None,
    distance_nm:   float | None,
) -> None:
    reg, cs, ac = _fmt_aircraft_line(icao, registration, callsign, type_desc, aircraft_type)
    country = _h(icao_ranges.icao_to_country(icao))
    url = f"{config.TELEGRAM_BASE_URL}/aircraft/{icao}"
    caption = (
        f"✈️ <b>Military aircraft — first sighting</b>\n"
        f"<b>{reg}</b>{cs} — {ac}\n"
        f"Country: {country}\n"
        f"Distance: {_fmt_dist(distance_nm)}\n"
        f'<a href="{url}">View profile</a>'
    )
    _dispatch_with_photo(caption, icao, aircraft_type, type_desc)


def notify_interesting(
    icao:          str,
    registration:  str | None,
    callsign:      str | None,
    type_desc:     str | None,
    aircraft_type: str | None,
    distance_nm:   float | None,
) -> None:
    reg, cs, ac = _fmt_aircraft_line(icao, registration, callsign, type_desc, aircraft_type)
    country = _h(icao_ranges.icao_to_country(icao))
    url = f"{config.TELEGRAM_BASE_URL}/aircraft/{icao}"
    caption = (
        f"⭐ <b>Interesting aircraft — first sighting</b>\n"
        f"<b>{reg}</b>{cs} — {ac}\n"
        f"Country: {country}\n"
        f"Distance: {_fmt_dist(distance_nm)}\n"
        f'<a href="{url}">View profile</a>'
    )
    _dispatch_with_photo(caption, icao, aircraft_type, type_desc)


def notify_anonymous(
    icao:          str,
    registration:  str | None,
    callsign:      str | None,
    type_desc:     str | None,
    aircraft_type: str | None,
    distance_nm:   float | None,
) -> None:
    """First-sighting alert for an aircraft whose Mode-S address falls outside
    every ICAO state-allocated block (FLAG_ANONYMOUS / "non-ICAO hex").  These
    are often military/OPSEC contacts or TIS-B rebroadcasts — interesting on a
    civilian receiver.  Country lookup intentionally won't resolve (the hex is
    non-state by definition), so we drop that line and label it explicitly."""
    reg, cs, ac = _fmt_aircraft_line(icao, registration, callsign, type_desc, aircraft_type)
    url = f"{config.TELEGRAM_BASE_URL}/aircraft/{icao}"
    caption = (
        f"❓ <b>Anonymous aircraft — first sighting</b>\n"
        f"<b>{reg}</b>{cs} — {ac}\n"
        f"Non-ICAO Mode-S address (no state allocation)\n"
        f"Distance: {_fmt_dist(distance_nm)}\n"
        f'<a href="{url}">View profile</a>'
    )
    _dispatch_with_photo(caption, icao, aircraft_type, type_desc)


def notify_watchlist(
    icao:          str,
    registration:  str | None,
    callsign:      str | None,
    type_desc:     str | None,
    aircraft_type: str | None,
    distance_nm:   float | None,
    label:         str | None,
    flight_id:     int,
) -> None:
    reg, cs, ac  = _fmt_aircraft_line(icao, registration, callsign, type_desc, aircraft_type)
    label_line   = f"Label: {_h(label)}\n" if label else ""
    aircraft_url = f"{config.TELEGRAM_BASE_URL}/aircraft/{icao}"
    flight_url   = f"{config.TELEGRAM_BASE_URL}/flight/{flight_id}"
    caption = (
        f"👁 <b>Watchlist — {reg}</b>\n"
        f"<b>{reg}</b>{cs} — {ac}\n"
        f"{label_line}"
        f"Distance: {_fmt_dist(distance_nm)}\n"
        f'<a href="{flight_url}">View flight</a> · <a href="{aircraft_url}">View aircraft</a>'
    )
    _dispatch_with_photo(caption, icao, aircraft_type, type_desc)


def notify_squawk(
    icao:         str,
    registration: str | None,
    callsign:     str | None,
    squawk:       str,
    distance_nm:  float | None,
) -> None:
    # _SQUAWK_LABELS values are static strings ("Hijack", "Radio failure",
    # "Emergency") so they don't need escaping, but the squawk code itself
    # comes from readsb output; escape both for defence-in-depth.
    label = _h(_SQUAWK_LABELS.get(squawk, squawk))
    reg, cs, _ = _fmt_aircraft_line(icao, registration, callsign)
    url = f"{config.TELEGRAM_BASE_URL}/aircraft/{icao}"
    _send(
        f"🚨 <b>Squawk {_h(squawk)} — {label}</b>\n"
        f"<b>{reg}</b>{cs}\n"
        f"Distance: {_fmt_dist(distance_nm)}\n"
        f'<a href="{url}">View profile</a>'
    )


# ---------------------------------------------------------------------------
# Daily summary
# ---------------------------------------------------------------------------

def send_daily_summary(conn: sqlite3.Connection) -> None:
    today     = datetime.date.today()
    day_start = int(datetime.datetime.combine(today, datetime.time.min).timestamp())
    day_end   = int(datetime.datetime.combine(today, datetime.time.max).timestamp())

    # OR-merge stored flags (tar1090-db + airplanes.live overrides) with the
    # computed anonymous bit so the same precedence rule as the badges / web
    # UI / first-sighting alerts (military > interesting > anonymous) applies
    # to the daily summary too.
    anon_sql = icao_ranges.anonymous_flag_sql("f.icao_hex", config.FLAG_ANONYMOUS)
    merged_flags = f"(COALESCE(adb.flags,0) | COALESCE(axo.flags,0) | {anon_sql})"

    agg = conn.execute(
        f"""
        SELECT COUNT(*)                                                              AS flights,
               COUNT(DISTINCT f.icao_hex)                                            AS aircraft,
               SUM(CASE WHEN ({merged_flags}) & 1 != 0
                        THEN 1 ELSE 0 END)                                          AS military,
               SUM(CASE WHEN ({merged_flags}) & 2 != 0
                         AND ({merged_flags}) & 1 = 0
                        THEN 1 ELSE 0 END)                                          AS interesting,
               SUM(CASE WHEN ({merged_flags}) & {config.FLAG_ANONYMOUS} != 0
                         AND ({merged_flags}) & 3 = 0
                        THEN 1 ELSE 0 END)                                          AS anonymous,
               SUM(CASE WHEN f.squawk IN ('7500','7600','7700') THEN 1 ELSE 0 END)  AS squawks
        FROM flights f
        LEFT JOIN aircraft_db     adb ON adb.icao_hex = f.icao_hex
        LEFT JOIN adsbx_overrides axo ON axo.icao_hex = f.icao_hex
        WHERE f.first_seen >= ? AND f.first_seen <= ?
        """,
        (day_start, day_end),
    ).fetchone()

    furthest = conn.execute(
        """
        SELECT COALESCE(f.registration, adb.registration, axo.registration, f.icao_hex) AS reg,
               COALESCE(adb.type_desc, axo.type_desc, f.aircraft_type, '')               AS type_desc,
               f.max_distance_nm
        FROM flights f
        LEFT JOIN aircraft_db     adb ON adb.icao_hex = f.icao_hex
        LEFT JOIN adsbx_overrides axo ON axo.icao_hex = f.icao_hex
        WHERE f.first_seen >= ? AND f.first_seen <= ?
          AND f.max_distance_nm IS NOT NULL
        ORDER BY f.max_distance_nm DESC
        LIMIT 1
        """,
        (day_start, day_end),
    ).fetchone()

    busiest = conn.execute(
        """
        SELECT strftime('%H', first_seen, 'unixepoch', 'localtime') AS hour,
               COUNT(*) AS cnt
        FROM flights
        WHERE first_seen >= ? AND first_seen <= ?
        GROUP BY hour
        ORDER BY cnt DESC
        LIMIT 1
        """,
        (day_start, day_end),
    ).fetchone()

    day_str = f"{today.strftime('%A')}, {today.strftime('%b')} {today.day}"
    lines   = [f"📊 <b>Daily summary — {day_str}</b>\n"]
    lines.append(
        f"Flights: <b>{agg['flights']}</b>   Aircraft: <b>{agg['aircraft']}</b>"
    )

    badges = []
    if agg["military"]:    badges.append(f"Military: {agg['military']}")
    if agg["interesting"]: badges.append(f"Interesting: {agg['interesting']}")
    if agg["anonymous"]:   badges.append(f"Anonymous: {agg['anonymous']}")
    if agg["squawks"]:     badges.append(f"⚠️ Emergency squawks: {agg['squawks']}")
    if badges:
        lines.append("  ".join(badges))

    _REG_SQL = "COALESCE(f.registration, adb.registration, axo.registration, f.icao_hex)"
    _TYPE_SQL = "COALESCE(adb.type_desc, axo.type_desc, f.aircraft_type, '')"
    _DAY_JOIN = ("FROM flights f "
                 "LEFT JOIN aircraft_db adb ON adb.icao_hex = f.icao_hex "
                 "LEFT JOIN adsbx_overrides axo ON axo.icao_hex = f.icao_hex "
                 "WHERE f.first_seen >= ? AND f.first_seen <= ?")

    fastest = conn.execute(
        f"SELECT {_REG_SQL} AS reg, {_TYPE_SQL} AS type_desc, f.max_gs "
        f"{_DAY_JOIN} AND f.max_gs IS NOT NULL ORDER BY f.max_gs DESC LIMIT 1",
        (day_start, day_end),
    ).fetchone()

    highest = conn.execute(
        f"SELECT {_REG_SQL} AS reg, {_TYPE_SQL} AS type_desc, f.max_alt_baro "
        f"{_DAY_JOIN} AND f.max_alt_baro IS NOT NULL ORDER BY f.max_alt_baro DESC LIMIT 1",
        (day_start, day_end),
    ).fetchone()

    longest = conn.execute(
        f"SELECT {_REG_SQL} AS reg, {_TYPE_SQL} AS type_desc, "
        f"(f.last_seen - f.first_seen) AS duration_s "
        f"{_DAY_JOIN} AND f.last_seen > f.first_seen ORDER BY duration_s DESC LIMIT 1",
        (day_start, day_end),
    ).fetchone()

    if furthest:
        td = f" ({_h(furthest['type_desc'])})" if furthest["type_desc"] else ""
        lines.append(
            f"Furthest: <b>{_h(furthest['reg'])}</b>{td} — {_fmt_dist(furthest['max_distance_nm'])}"
        )

    if fastest:
        td = f" ({_h(fastest['type_desc'])})" if fastest["type_desc"] else ""
        lines.append(
            f"Fastest: <b>{_h(fastest['reg'])}</b>{td} — {_fmt_spd(fastest['max_gs'])}"
        )

    if highest:
        td = f" ({_h(highest['type_desc'])})" if highest["type_desc"] else ""
        lines.append(
            f"Highest: <b>{_h(highest['reg'])}</b>{td} — {_fmt_alt(highest['max_alt_baro'])}"
        )

    if longest:
        td = f" ({_h(longest['type_desc'])})" if longest["type_desc"] else ""
        ds = longest["duration_s"]
        h, m = divmod(ds // 60, 60)
        lines.append(
            f"Longest: <b>{_h(longest['reg'])}</b>{td} — {h}h {m:02d}m"
        )

    if busiest:
        h = int(busiest["hour"])
        lines.append(
            f"Busiest hour: {h:02d}:00–{(h + 1) % 24:02d}:00 ({busiest['cnt']} flights)"
        )

    _send("\n".join(lines))


# ---------------------------------------------------------------------------
# Interactive command listener (long polling)
# ---------------------------------------------------------------------------

def _get_updates(offset: int, timeout: int = 30) -> list[dict]:
    params = urllib.parse.urlencode({
        "offset":          offset,
        "timeout":         timeout,
        "allowed_updates": '["message"]',
    })
    body, _ = http_safe.safe_urlopen(
        f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/getUpdates?{params}",
        timeout=timeout + 10,
        # 100 updates × ~10 KB each is the realistic worst case for the
        # long-poll response; cap at 4 MB for headroom.
        max_bytes=4 * 1024 * 1024,
    )
    return json.loads(body).get("result", [])


def _send_status(conn: sqlite3.Connection) -> None:
    today     = datetime.date.today()
    day_start = int(datetime.datetime.combine(today, datetime.time.min).timestamp())

    today_count = conn.execute(
        "SELECT COUNT(*) FROM flights WHERE first_seen >= ?", (day_start,)
    ).fetchone()[0]

    active = conn.execute(
        """
        SELECT COALESCE(f.registration, adb.registration, axo.registration, f.icao_hex) AS reg,
               f.callsign,
               COALESCE(adb.type_desc, axo.type_desc, f.aircraft_type, '')               AS type_desc
        FROM active_flights af
        JOIN flights f ON f.id = af.flight_id
        LEFT JOIN aircraft_db     adb ON adb.icao_hex = f.icao_hex
        LEFT JOIN adsbx_overrides axo ON axo.icao_hex = f.icao_hex
        ORDER BY af.last_seen DESC
        """
    ).fetchall()

    lines = ["📡 <b>Status</b>\n", f"Flights today: <b>{today_count}</b>"]

    if active:
        lines.append(f"\nTracking <b>{len(active)}</b> aircraft now:")
        for a in active[:10]:
            cs = f" ({_h(a['callsign'])})" if a["callsign"] else ""
            td = f" — {_h(a['type_desc'])}" if a["type_desc"] else ""
            lines.append(f"  • {_h(a['reg'])}{cs}{td}")
        if len(active) > 10:
            lines.append(f"  … and {len(active) - 10} more")
    else:
        lines.append("No aircraft currently tracked.")

    _send("\n".join(lines))


def _send_help() -> None:
    _send(
        "🤖 <b>Available commands</b>\n\n"
        "/summary — today's flight summary\n"
        "/status — currently tracked aircraft\n"
        "/watchlist — show watchlist entries\n"
        "/watch &lt;icao|reg&gt; — add to watchlist\n"
        "/unwatch &lt;icao|reg&gt; — remove from watchlist\n"
        "/help — this message"
    )


_WATCHLIST_TYPE_LABELS = {"icao": "ICAO", "registration": "Reg", "callsign_prefix": "Prefix"}


def _send_watchlist_list(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT match_type, value, label FROM watchlist ORDER BY created_at"
    ).fetchall()
    if not rows:
        _send("👁 <b>Watchlist</b>\n\nEmpty — use /watch &lt;icao|reg&gt; to add entries.")
        return
    lines = ["👁 <b>Watchlist</b>\n"]
    for row in rows:
        tl = _h(_WATCHLIST_TYPE_LABELS.get(row["match_type"], row["match_type"]))
        lb = f" — {_h(row['label'])}" if row["label"] else ""
        lines.append(f"  [{tl}] {_h(row['value'].upper())}{lb}")
    _send("\n".join(lines))


def _watch_add(conn: sqlite3.Connection, value_raw: str) -> None:
    import re
    from . import database
    value = value_raw.strip().lower()
    if not value:
        _send("Usage: /watch &lt;icao_hex|registration&gt;")
        return
    if len(value) > database.WATCHLIST_VALUE_MAX:
        _send(f"Value too long (max {database.WATCHLIST_VALUE_MAX} characters)")
        return
    match_type = "icao" if re.fullmatch(r"[0-9a-f]{6}", value) else "registration"
    existing = conn.execute(
        "SELECT id FROM watchlist WHERE match_type = ? AND value = ?",
        (match_type, value),
    ).fetchone()
    if existing:
        _send(f"Already watching: {value.upper()}")
        return
    conn.execute(
        "INSERT INTO watchlist (match_type, value, label, created_at) VALUES (?,?,NULL,strftime('%s','now'))",
        (match_type, value),
    )
    conn.commit()
    tl = _WATCHLIST_TYPE_LABELS[match_type]
    _send(f"👁 Added to watchlist: <b>{value.upper()}</b> [{tl}]")


def _watch_remove(conn: sqlite3.Connection, value_raw: str) -> None:
    value = value_raw.strip().lower()
    if not value:
        _send("Usage: /unwatch &lt;icao_hex|registration|callsign_prefix&gt;")
        return
    # Infer match_type from value shape, mirroring _watch_add. We try the
    # inferred type first to preserve Audit 11 #116 — when both an icao and
    # registration row exist with the same 6-hex literal, /unwatch <hex>
    # removes only the icao row. If that lookup matches nothing, fall back
    # to the alternate type so a 6-hex-shaped registration (e.g. ABC123)
    # is still removable via the bot (audit-12 #154).
    primary = "icao" if re.fullmatch(r"[0-9a-f]{6}", value) else "registration"
    cur = conn.execute(
        "DELETE FROM watchlist WHERE match_type = ? AND value = ?",
        (primary, value),
    )
    conn.commit()
    if cur.rowcount == 0:
        fallback = "registration" if primary == "icao" else "icao"
        cur = conn.execute(
            "DELETE FROM watchlist WHERE match_type = ? AND value = ?",
            (fallback, value),
        )
        conn.commit()
    if cur.rowcount:
        _send(f"Removed from watchlist: <b>{value.upper()}</b>")
    else:
        _send(f"Not in watchlist: {value.upper()}")


def _handle_update(update: dict, conn: sqlite3.Connection) -> None:
    msg     = update.get("message", {})
    chat_id = str(msg.get("chat", {}).get("id", ""))
    text    = (msg.get("text") or "").strip()

    # Only respond to the configured chat ID
    if chat_id != str(config.TELEGRAM_CHAT_ID):
        return

    # Strip bot username suffix and normalise (/Summary → /summary)
    cmd = text.split("@")[0].split()[0].lower() if text else ""

    if cmd == "/summary":
        send_daily_summary(conn)
    elif cmd == "/status":
        _send_status(conn)
    elif cmd == "/watchlist":
        _send_watchlist_list(conn)
    elif cmd == "/watch":
        arg = " ".join(text.split()[1:]) if len(text.split()) > 1 else ""
        _watch_add(conn, arg)
    elif cmd == "/unwatch":
        arg = " ".join(text.split()[1:]) if len(text.split()) > 1 else ""
        _watch_remove(conn, arg)
    elif cmd in ("/help", "/start"):
        _send_help()


def _listener_loop(db_path: str) -> None:
    from . import database  # local import to avoid circular dependency at module load
    conn   = database.connect(db_path)
    offset = 0
    log.info("Telegram command listener ready")
    while True:
        try:
            updates = _get_updates(offset, timeout=30)
            for upd in updates:
                offset = upd["update_id"] + 1
                try:
                    _handle_update(upd, conn)
                except Exception:
                    log.exception("Error handling Telegram update")
        except Exception as exc:
            log.warning("Telegram getUpdates failed: %s", _describe_exc(exc))
            time.sleep(5)


def start_command_listener(db_path: str) -> None:
    """Start the interactive command listener in a background daemon thread."""
    if not telegram_enabled():
        return
    t = threading.Thread(
        target=_listener_loop, args=(db_path,), daemon=True, name="tg-listener"
    )
    t.start()
