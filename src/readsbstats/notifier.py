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
import json
import logging
import sqlite3
import threading
import time
import urllib.parse
import urllib.request

from . import config

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
        log.warning("Telegram send failed: %s", exc)
        return False


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


def notify_military(
    icao:          str,
    registration:  str | None,
    callsign:      str | None,
    type_desc:     str | None,
    aircraft_type: str | None,
    distance_nm:   float | None,
) -> None:
    reg, cs, ac = _fmt_aircraft_line(icao, registration, callsign, type_desc, aircraft_type)
    url = f"{config.BASE_URL}/aircraft/{icao}"
    _send(
        f"✈️ <b>Military aircraft — first sighting</b>\n"
        f"<b>{reg}</b>{cs} — {ac}\n"
        f"Distance: {_fmt_dist(distance_nm)}\n"
        f'<a href="{url}">View profile</a>'
    )


def notify_interesting(
    icao:          str,
    registration:  str | None,
    callsign:      str | None,
    type_desc:     str | None,
    aircraft_type: str | None,
    distance_nm:   float | None,
) -> None:
    reg, cs, ac = _fmt_aircraft_line(icao, registration, callsign, type_desc, aircraft_type)
    url = f"{config.BASE_URL}/aircraft/{icao}"
    _send(
        f"⭐ <b>Interesting aircraft — first sighting</b>\n"
        f"<b>{reg}</b>{cs} — {ac}\n"
        f"Distance: {_fmt_dist(distance_nm)}\n"
        f'<a href="{url}">View profile</a>'
    )


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
    _send(
        f"👁 <b>Watchlist — {reg}</b>\n"
        f"<b>{reg}</b>{cs} — {ac}\n"
        f"{label_line}"
        f"Distance: {_fmt_dist(distance_nm)}\n"
        f'<a href="{flight_url}">View flight</a> · <a href="{aircraft_url}">View aircraft</a>'
    )


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
    value = value_raw.strip().lower()
    if not value:
        _send("Usage: /watch &lt;icao_hex|registration&gt;")
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
            log.warning("Telegram getUpdates failed: %s", exc)
            time.sleep(5)


def start_command_listener(db_path: str) -> None:
    """Start the interactive command listener in a background daemon thread."""
    if not telegram_enabled():
        return
    t = threading.Thread(
        target=_listener_loop, args=(db_path,), daemon=True, name="tg-listener"
    )
    t.start()
