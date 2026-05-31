"""Calendar of flight days for the date-picker UI."""

from __future__ import annotations

from fastapi import APIRouter

from .. import cache
from . import _deps


router = APIRouter()


@router.get("/api/dates")
def api_dates() -> dict:
    cached = cache._get_cache("dates")
    if cached is not None:
        return cached
    conn = _deps.db()
    rows = conn.execute(
        """
        SELECT date(first_seen, 'unixepoch', 'localtime') AS date,
               COUNT(*) AS flight_count
        FROM flights
        GROUP BY date
        ORDER BY date DESC
        LIMIT 365
        """
    ).fetchall()
    result = {"dates": [dict(r) for r in rows]}
    cache._set_cache("dates", result)
    return result
