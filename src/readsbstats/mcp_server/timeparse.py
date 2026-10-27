"""Period parsing in host-local time, with snapped windows (ADR-0022, Decision 4).

Snapping makes repeated questions hit the stats response cache: two calls in
the same slot get identical (from, to). Relative windows end at *now* floored
to 10 minutes (1 hour when the span exceeds 7 days).
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RANGE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$")
_REL_RE = re.compile(r"^(\d+)([hd])$")
_SNAP_SHORT_S = 600
_SNAP_LONG_S = 3600
_SHORT_SPAN_S = 7 * 86400
_DST_SLACK_S = 3600       # a window of whole local days can be 1 h longer than N*24 h


def _midnight(d: date) -> int:
    return int(datetime(d.year, d.month, d.day).timestamp())      # naive = host-local


def day_bounds(day: date) -> tuple[int, int]:
    """[local midnight, next local midnight) as epoch seconds (DST-correct)."""
    return _midnight(day), _midnight(day + timedelta(days=1))


def _parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise ValueError(f"invalid date {s!r}; use YYYY-MM-DD") from None


def parse_instant(s: str, *, now: float | None = None) -> int:
    """``"now"``, ``YYYY-MM-DD`` (local midnight) or an ISO datetime (naive = local)."""
    text = s.strip()
    if text.lower() == "now":
        return int(time.time() if now is None else now)
    if _DATE_RE.match(text):
        return _midnight(_parse_date(text))
    # Python 3.10's fromisoformat rejects a trailing "Z" (3.11+ accepts it).
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError:
        raise ValueError(
            f"invalid time {s!r}; use 'now', YYYY-MM-DD or an ISO datetime "
            "like 2026-06-10T13:30:00") from None


def resolve_period(period: str | None, *, now: float | None = None, default: str = "24h",
                   max_days: int | None = None, allow_all: bool = False,
                   ) -> tuple[int | None, int | None]:
    """Turn a period string into snapped epoch bounds ``(from, to)``.

    Accepts ``today``, ``yesterday``, ``YYYY-MM-DD``, ``YYYY-MM-DD..YYYY-MM-DD``
    (inclusive), ``<N>h`` (1-720), ``<N>d`` (1-3650) and, if *allow_all*, ``all``.
    """
    ts = time.time() if now is None else now
    text = (default if period is None else period).strip().lower()
    hint = "use 24h, 7d, today, yesterday, YYYY-MM-DD or YYYY-MM-DD..YYYY-MM-DD"

    if text == "all":
        if not allow_all:
            raise ValueError("'all' is not allowed for this call; " + hint)
        return None, None

    if text in ("today", "yesterday"):
        d = date.fromtimestamp(ts)
        if text == "yesterday":
            d -= timedelta(days=1)
        frm, to = day_bounds(d)
    elif _DATE_RE.match(text):
        frm, to = day_bounds(_parse_date(text))
    elif (m := _RANGE_RE.match(text)):
        d1, d2 = _parse_date(m.group(1)), _parse_date(m.group(2))
        if d2 < d1:
            raise ValueError(f"invalid period {period!r}: end date is before start date")
        frm, to = day_bounds(d1)[0], day_bounds(d2)[1]
    elif (m := _REL_RE.match(text)):
        n, unit = int(m.group(1)), m.group(2)
        if unit == "h" and not 1 <= n <= 720:
            raise ValueError(f"invalid period {period!r}: hours must be 1-720; " + hint)
        if unit == "d" and not 1 <= n <= 3650:
            raise ValueError(f"invalid period {period!r}: days must be 1-3650; " + hint)
        span = n * (3600 if unit == "h" else 86400)
        snap = _SNAP_LONG_S if span > _SHORT_SPAN_S else _SNAP_SHORT_S
        to = int(ts // snap * snap)
        frm = to - span
    else:
        raise ValueError(f"invalid period {period!r}; " + hint)

    if frm > ts:
        raise ValueError(f"period {period!r} lies in the future")
    if max_days is not None and (to - frm) > max_days * 86400 + _DST_SLACK_S:
        raise ValueError(f"period longer than {max_days} days; use a shorter window")
    return frm, to


def to_local_iso(ts: int | float | None) -> str | None:
    """Local ISO-8601 with UTC offset, seconds precision; None passes through."""
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts)).astimezone().isoformat(timespec="seconds")
