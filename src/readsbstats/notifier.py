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
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, icao_ranges

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
    """POST a message to the configured Telegram bot. Returns True on success."""
    if not telegram_enabled():
        return False
    try:
        payload = json.dumps({
            "chat_id":                  config.TELEGRAM_CHAT_ID,
            "text":                     text,
            "parse_mode":               "HTML",
            "disable_web_page_preview": True,
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except Exception as exc:
        log.warning("Telegram send failed: %s", _describe_exc(exc))
        return False


# ---------------------------------------------------------------------------
# Photo helpers
# ---------------------------------------------------------------------------

_PHOTO_UA = {"User-Agent": "readsbstats/1.0"}


def _get_photo_result(
    icao_hex: str,
    type_code: str | None,
    type_desc: str | None,
) -> tuple[str | None, bool]:
    """Return (thumbnail_url | None, is_type_photo).

    Lookup order (all cache-first, no unnecessary HTTP):
    1. photos cache for the specific aircraft.
    2. type_photos cache for the type code.
    3. photos JOIN aircraft_db — reuse any already-cached photo of that type.
    4. Planespotters fetch for the specific ICAO.
    5. Planespotters fetch for one probe ICAO of the same type.
    """
    if not config.DB_PATH:
        return None, False

    cutoff = int(time.time()) - config.PHOTO_CACHE_DAYS * 86400

    try:
        conn = sqlite3.connect(config.DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            # 1. Specific aircraft cache (includes negative cache)
            row = conn.execute(
                "SELECT thumbnail_url, fetched_at FROM photos WHERE icao_hex = ?",
                (icao_hex,),
            ).fetchone()
            if row and row["fetched_at"] > cutoff:
                return row["thumbnail_url"], False

            # 2. Type cache (includes negative cache)
            if type_code:
                row = conn.execute(
                    "SELECT thumbnail_url, fetched_at FROM type_photos WHERE type_code = ?",
                    (type_code,),
                ).fetchone()
                if row and row["fetched_at"] > cutoff:
                    return row["thumbnail_url"], True

            # 3. photos JOIN aircraft_db — zero HTTP
            if type_code:
                row = conn.execute(
                    """
                    SELECT p.thumbnail_url
                    FROM photos p
                    JOIN aircraft_db adb ON adb.icao_hex = p.icao_hex
                    WHERE adb.type_code = ? AND p.thumbnail_url IS NOT NULL
                    ORDER BY p.fetched_at DESC LIMIT 1
                    """,
                    (type_code,),
                ).fetchone()
                if row:
                    url = row["thumbnail_url"]
                    _cache_type_photo(conn, type_code, url)
                    return url, True

            # 4. Fetch Planespotters for the specific ICAO
            url = _planespotters_fetch(icao_hex)
            now = int(time.time())
            conn.execute(
                "INSERT OR REPLACE INTO photos "
                "(icao_hex, thumbnail_url, large_url, link_url, photographer, fetched_at) "
                "VALUES (?,?,NULL,NULL,NULL,?)",
                (icao_hex, url, now),
            )
            conn.commit()
            if url:
                return url, False

            # 5. Probe one ICAO of the same type
            if type_code:
                probe_row = conn.execute(
                    "SELECT icao_hex FROM aircraft_db WHERE type_code = ? LIMIT 1",
                    (type_code,),
                ).fetchone()
                if probe_row:
                    probe_url = _planespotters_fetch(probe_row["icao_hex"])
                    _cache_type_photo(conn, type_code, probe_url)
                    if probe_url:
                        return probe_url, True

            # Store negative for type too
            if type_code:
                _cache_type_photo(conn, type_code, None)
            return None, False

        finally:
            conn.close()
    except Exception as exc:
        log.debug("photo lookup failed for %s: %s", icao_hex, exc)
        return None, False


def _planespotters_fetch(icao_hex: str) -> str | None:
    """Try Planespotters.net for a single ICAO; return thumbnail_url or None."""
    try:
        req = urllib.request.Request(
            f"https://api.planespotters.net/pub/photos/hex/{icao_hex}",
            headers=_PHOTO_UA,
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read())
            if data.get("photos"):
                return data["photos"][0].get("thumbnail", {}).get("src") or None
    except Exception as exc:
        log.debug("Planespotters fetch failed for %s: %s", icao_hex, exc)
    return None


def _cache_type_photo(conn: sqlite3.Connection, type_code: str, url: str | None) -> None:
    now = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO type_photos "
        "(type_code, thumbnail_url, large_url, link_url, photographer, fetched_at) "
        "VALUES (?,?,NULL,NULL,NULL,?)",
        (type_code, url, now),
    )
    conn.commit()


def _send_photo(photo_url: str, caption: str) -> bool:
    """POST a photo message to Telegram. Falls back to text on any failure."""
    if not telegram_enabled():
        return False
    if not photo_url.startswith(("http://", "https://")):
        return _send(caption)
    try:
        payload = json.dumps({
            "chat_id":    config.TELEGRAM_CHAT_ID,
            "photo":      photo_url,
            "caption":    caption,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendPhoto",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except Exception as exc:
        log.warning("Telegram sendPhoto failed: %s — falling back to text",
                    _describe_exc(exc))
        return _send(caption)


# ---------------------------------------------------------------------------
# Alert messages
# ---------------------------------------------------------------------------

def _fmt_aircraft_line(
    icao: str,
    registration: str | None,
    callsign: str | None,
    type_desc: str | None = None,
    aircraft_type: str | None = None,
) -> tuple[str, str, str]:
    """Return (reg, callsign_suffix, aircraft_type) formatted strings."""
    reg = registration or icao.upper()
    cs  = f" ({callsign})" if callsign else ""
    ac  = type_desc or aircraft_type or "Unknown type"
    return reg, cs, ac


def _dispatch_with_photo(
    caption: str,
    icao: str,
    aircraft_type: str | None,
    type_desc: str | None,
) -> None:
    """Send *caption* via sendPhoto if a photo is available, else sendMessage."""
    if config.TELEGRAM_PHOTOS:
        photo_url, is_type = _get_photo_result(icao, aircraft_type, type_desc)
        if is_type and photo_url:
            label = _html.escape(type_desc or aircraft_type or "")
            caption += f"\n<i>Photo: {label} — not this specific aircraft</i>"
        if photo_url:
            _send_photo(photo_url, caption)
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
    country = icao_ranges.icao_to_country(icao)
    url = f"{config.BASE_URL}/aircraft/{icao}"
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
    country = icao_ranges.icao_to_country(icao)
    url = f"{config.BASE_URL}/aircraft/{icao}"
    caption = (
        f"⭐ <b>Interesting aircraft — first sighting</b>\n"
        f"<b>{reg}</b>{cs} — {ac}\n"
        f"Country: {country}\n"
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
    label_line   = f"Label: {label}\n" if label else ""
    aircraft_url = f"{config.BASE_URL}/aircraft/{icao}"
    flight_url   = f"{config.BASE_URL}/flight/{flight_id}"
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
    label = _SQUAWK_LABELS.get(squawk, squawk)
    reg, cs, _ = _fmt_aircraft_line(icao, registration, callsign)
    url = f"{config.BASE_URL}/aircraft/{icao}"
    _send(
        f"🚨 <b>Squawk {squawk} — {label}</b>\n"
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

    agg = conn.execute(
        """
        SELECT COUNT(*)                                                              AS flights,
               COUNT(DISTINCT f.icao_hex)                                           AS aircraft,
               SUM(CASE WHEN (COALESCE(adb.flags,0) | COALESCE(axo.flags,0)) & 1 != 0
                        THEN 1 ELSE 0 END)                                         AS military,
               SUM(CASE WHEN (COALESCE(adb.flags,0) | COALESCE(axo.flags,0)) & 2 != 0
                         AND (COALESCE(adb.flags,0) | COALESCE(axo.flags,0)) & 1  = 0
                        THEN 1 ELSE 0 END)                                         AS interesting,
               SUM(CASE WHEN f.squawk IN ('7500','7600','7700') THEN 1 ELSE 0 END) AS squawks
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
        td = f" ({furthest['type_desc']})" if furthest["type_desc"] else ""
        lines.append(
            f"Furthest: <b>{furthest['reg']}</b>{td} — {_fmt_dist(furthest['max_distance_nm'])}"
        )

    if fastest:
        td = f" ({fastest['type_desc']})" if fastest["type_desc"] else ""
        lines.append(
            f"Fastest: <b>{fastest['reg']}</b>{td} — {_fmt_spd(fastest['max_gs'])}"
        )

    if highest:
        td = f" ({highest['type_desc']})" if highest["type_desc"] else ""
        lines.append(
            f"Highest: <b>{highest['reg']}</b>{td} — {_fmt_alt(highest['max_alt_baro'])}"
        )

    if longest:
        td = f" ({longest['type_desc']})" if longest["type_desc"] else ""
        ds = longest["duration_s"]
        h, m = divmod(ds // 60, 60)
        lines.append(
            f"Longest: <b>{longest['reg']}</b>{td} — {h}h {m:02d}m"
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
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/getUpdates?{params}"
    )
    with urllib.request.urlopen(req, timeout=timeout + 10) as resp:
        return json.loads(resp.read()).get("result", [])


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
            cs = f" ({a['callsign']})" if a["callsign"] else ""
            td = f" — {a['type_desc']}" if a["type_desc"] else ""
            lines.append(f"  • {a['reg']}{cs}{td}")
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
        tl = _WATCHLIST_TYPE_LABELS.get(row["match_type"], row["match_type"])
        lb = f" — {row['label']}" if row["label"] else ""
        lines.append(f"  [{tl}] {row['value'].upper()}{lb}")
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
    cur = conn.execute("DELETE FROM watchlist WHERE value = ?", (value,))
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
