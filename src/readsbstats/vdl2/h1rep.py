"""Parse H1 position reports (ABS/EB0/SB0 multi-fix, CF ``FDE``/``MM``, ``(POS-``).

Three H1 families carry aircraft positions that no earlier parser read. Each was validated
against ADS-B tracks of the same airframe (nearest ADS-B fix within +/-60 s of the fix
time; snapshot 2026-11-23, see ADR-0019 for the method). Query-time and read-only, never
stored (same pattern as m1bpos.py / rte.py / positions.py).

1. **ABS / EB0 / SB0** (``sublabel DF``) — **multi-fix**: the header line is followed by one
   fix per line, in one of two dialects (:func:`_parse_abs`):

   - *CSV* (LOT 737 MAX)::

         N5210.5,E02055.2,231035,01973,-02.8,286,026,IC,00000,0,

     N/S ``ddmm.m``, E/W ``dddmm.m``, ``DDHHMM`` (UTC day + time of the fix), altitude ft,
     temperature C, wind direction deg, wind speed kt. Median/p90 vs ADS-B with the
     embedded time: **1.26 / 3.61 nm** (19,332 fixes).
   - *fixed-width*::

          52183  208871359002189+08-241014XI8GMY...

     latitude x1000 in cols [0:6], longitude x1000 in [6:13], ``HHMM`` in [13:17], altitude
     ft in [17:23]. Only lat/lon/time/alt are read (the temperature/wind layout is
     ambiguous). **2.14 / 4.46 nm** (3,122 fixes). Some EB0 bodies use a different,
     non-position layout; the strict per-column patterns reject those lines.

   The receive time is the WRONG time base for ABS (one message holds many minutes of fixes;
   4.5 / 17 nm p90 vs ADS-B), so every fix gets its own embedded time via :func:`fix_epoch`.

2. **CF ``FDE`` / ``MM``** (``sublabel CF``) — ``FDE133409090111752.17933/20.83380`` =
   ``HHMMSS``, ``MMDD``, 3 digits, decimal lat/lon. Time = embedded ``HHMMSS``:
   **0.20 / 0.40 nm** (914 fixes). Bodies such as ``FDE1227481123ACA`` carry no position.

3. **``(POS-``** (``sublabel DF``) — ``(POS-KLM75C  -5222N02005E/104548 F180``: ``ddmm`` N/S,
   ``dddmm`` E/W, ``/HHMMSS``, flight level (alt = FL x 100). **0.95 / 2.00 nm** (91 fixes).

Dispatch is on ``(sublabel, body prefix)`` — never on body shape alone — so the same text
under another sublabel is never trusted. Every fix is range-checked with
``cleaners.valid_lat/valid_lon`` and a message yields at most :data:`MAX_FIXES` fixes.
"""
from __future__ import annotations

import datetime as dt
import re

from ..cleaners import valid_lat, valid_lon

MAX_FIXES = 64            # per message; a real ABS report holds a handful of fixes
_FUTURE_SLACK_SEC = 600   # an HHMM time may be this far past the receive time (clock skew)
_DAY_FUTURE_SLACK_SEC = 2 * 86400

_UTC = dt.timezone.utc


def fix_epoch(recv_ts: float, hh: int, mm: int, ss: int = 0, day: int | None = None) -> int | None:
    """Resolve an embedded UTC time against the row's receive time ``recv_ts``.

    * ``day is None`` (``HHMM[SS]`` only): the latest such time not later than
      ``recv_ts + 600`` s — i.e. today, or yesterday when today's would be in the future
      (midnight rollover: ``2359`` received at ``00:03`` is yesterday 23:59).
    * ``day`` given (``DDHHMM``): that day in the receive month, or in the previous month
      when it would be more than 2 days in the future (month/year rollover; a day that does
      not exist in the receive month, e.g. the 31st, also falls back to the previous one).

    Returns ``None`` for an invalid hour/minute/second/day or an unusable ``recv_ts``."""
    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
        return None
    try:
        base = dt.datetime.fromtimestamp(recv_ts, _UTC)
        if day is None:
            cand = base.replace(hour=hh, minute=mm, second=ss, microsecond=0)
            if cand.timestamp() > recv_ts + _FUTURE_SLACK_SEC:
                cand -= dt.timedelta(days=1)
            return int(cand.timestamp())
    except (OverflowError, OSError, ValueError, TypeError):
        return None
    if not 1 <= day <= 31:
        return None
    y, m = base.year, base.month
    for _ in range(2):
        try:
            cand = dt.datetime(y, m, day, hh, mm, ss, tzinfo=_UTC)
        except ValueError:
            cand = None
        if cand is not None and cand.timestamp() <= recv_ts + _DAY_FUTURE_SLACK_SEC:
            return int(cand.timestamp())
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return None


def _fix(lat: float, lon: float, ts: int | None, source: str, **extra) -> dict | None:
    lat, lon = valid_lat(round(lat, 5)), valid_lon(round(lon, 5))
    if lat is None or lon is None or ts is None:
        return None
    return {"lat": lat, "lon": lon, "ts": ts, "source": source, **extra}


# --- ABS / EB0 / SB0 ------------------------------------------------------------------
_ABS_CSV = re.compile(
    r"([NS])(\d{2})(\d{2}\.\d),([EW])(\d{3})(\d{2}\.\d),(\d{2})(\d{2})(\d{2}),"
    r"(-?\d{1,5}),\s*([-+]?\d{1,2}\.\d),(\d{3}),(\d{3}),")
# lat x1000 (6 cols), lon x1000 (7 cols), HHMM, altitude ft (6 cols). Strict column
# patterns: other EB0 layouts (``SP-LRH...``, `` 999 999``, ``161126062124...``) don't match.
_ABS_FIXED = re.compile(
    r"([ -]\d{5})([ -]\d{6}|(?: -|  )\d{5})(\d{2})(\d{2})([ +-]\d{5}|\d{6})")
_ABS_PREFIXES = ("ABS", "EB0", "SB0")
_ALT_MIN, _ALT_MAX = -2000, 60000


def _parse_abs(body: str, recv_ts: float) -> list[dict]:
    out: list[dict] = []
    for line in re.split(r"\r?\n", body)[1:]:
        if len(out) >= MAX_FIXES:
            break
        try:
            f = _abs_line(line, recv_ts)
        except (ValueError, OverflowError):
            continue   # malformed radio line: skip it, never fail the message
        if f is not None:
            out.append(f)
    return out


def _abs_line(line: str, recv_ts: float) -> dict | None:
    """One ABS fix line (CSV or fixed-width dialect) → fix dict, or None."""
    m = _ABS_CSV.match(line)
    if m:
        if float(m[3]) >= 60 or float(m[6]) >= 60:
            return None
        lat = int(m[2]) + float(m[3]) / 60
        lon = int(m[5]) + float(m[6]) / 60
        alt = int(m[10])
        if not _ALT_MIN <= alt <= _ALT_MAX:
            return None
        ts = fix_epoch(recv_ts, int(m[8]), int(m[9]), day=int(m[7]))
        return _fix(-lat if m[1] == "S" else lat, -lon if m[4] == "W" else lon, ts, "h1-abs",
                    alt=alt, temp_c=float(m[11]), wind_dir=int(m[12]), wind_kt=int(m[13]))
    m = _ABS_FIXED.match(line)
    if m:
        alt = int(m[5])
        if not _ALT_MIN <= alt <= _ALT_MAX:
            return None
        lat = int(m[1]) / 1000
        lon = int(m[2].replace(" ", "")) / 1000
        return _fix(lat, lon, fix_epoch(recv_ts, int(m[3]), int(m[4])), "h1-abs", alt=alt)
    return None


# --- CF FDE / MM ------------------------------------------------------------------------
_CF = re.compile(
    r"(?:FDE|MM)(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})\d{3}(-?\d{1,2}\.\d{5})/(-?\d{1,3}\.\d{5})")


def _parse_cf(body: str, recv_ts: float) -> list[dict]:
    m = _CF.match(body)
    if m is None:
        return []
    f = _fix(float(m[6]), float(m[7]), fix_epoch(recv_ts, int(m[1]), int(m[2]), int(m[3])), "h1-cf")
    return [f] if f is not None else []


# --- (POS- ------------------------------------------------------------------------------
_POS = re.compile(
    r"\(POS-\S+\s+-(\d{2})(\d{2})([NS])(\d{3})(\d{2})([EW])/(\d{2})(\d{2})(\d{2})\s+F(\d{3})")


def _parse_pos(body: str, recv_ts: float) -> list[dict]:
    m = _POS.match(body)
    if m is None or int(m[2]) >= 60 or int(m[5]) >= 60:
        return []
    lat = int(m[1]) + int(m[2]) / 60
    lon = int(m[4]) + int(m[5]) / 60
    f = _fix(-lat if m[3] == "S" else lat, -lon if m[6] == "W" else lon,
             fix_epoch(recv_ts, int(m[7]), int(m[8]), int(m[9])), "h1-pos", alt=int(m[10]) * 100)
    return [f] if f is not None else []


def parse_positions(sublabel: object, body: object, recv_ts: float) -> list[dict]:
    """Fixes of an H1 row as ``[{"lat","lon","ts","source", "alt"?, "temp_c"?, "wind_dir"?,
    "wind_kt"?}]`` (``ts`` = the fix's own time, resolved against ``recv_ts``), or ``[]``.

    Dispatches on ``(sublabel, body prefix)``: ``DF`` + ``ABS``/``EB0``/``SB0`` (multi-fix),
    ``CF`` + ``FDE``/``MM``, ``DF`` + ``(POS-``. Anything else yields no fixes."""
    if not isinstance(body, str) or not isinstance(sublabel, str):
        return []
    if sublabel == "DF":
        if body.startswith(_ABS_PREFIXES):
            return _parse_abs(body, recv_ts)
        if body.startswith("(POS-"):
            return _parse_pos(body, recv_ts)
    elif sublabel == "CF" and body.startswith(("FDE", "MM")):
        return _parse_cf(body, recv_ts)
    return []
