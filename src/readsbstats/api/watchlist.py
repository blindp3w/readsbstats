"""Watchlist CRUD endpoints (per-user-defined ICAO/registration/callsign-prefix)."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .. import database, schemas
from . import _deps


router = APIRouter()


_VALID_MATCH_TYPES = {"icao", "registration", "callsign_prefix"}


class _WatchlistEntry(BaseModel):
    match_type: str = Field(max_length=32)
    value: str = Field(max_length=database.WATCHLIST_VALUE_MAX)
    label: str | None = Field(default=None, max_length=database.WATCHLIST_LABEL_MAX)


@router.get("/api/watchlist", response_model=schemas.WatchlistListResponse,
            response_model_exclude_unset=True)
def api_watchlist_list() -> dict:
    rows = _deps.db().execute(
        """
        SELECT w.id, w.match_type, w.value, w.label, w.created_at,
               CASE WHEN w.match_type = 'icao' AND af.icao_hex IS NOT NULL
                    THEN 1 ELSE 0 END AS airborne
        FROM watchlist w
        LEFT JOIN active_flights af ON w.match_type = 'icao' AND af.icao_hex = w.value
        ORDER BY w.created_at
        """
    ).fetchall()
    return {"entries": [dict(r) for r in rows]}


@router.post("/api/watchlist", status_code=201,
             dependencies=[Depends(_deps._csrf_check),
                           Depends(_deps._auth_check)])
def api_watchlist_add(body: _WatchlistEntry) -> dict:
    if body.match_type not in _VALID_MATCH_TYPES:
        raise HTTPException(422, "match_type must be icao, registration, or callsign_prefix")
    value = body.value.strip().lower()
    if not value:
        raise HTTPException(422, "value is required")
    label = body.label.strip() if body.label else None
    try:
        with _deps.db():
            cur = _deps.db().execute(
                "INSERT INTO watchlist (match_type, value, label, created_at) "
                "VALUES (?, ?, ?, strftime('%s','now'))",
                (body.match_type, value, label),
            )
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Entry already exists")
    return {"id": cur.lastrowid, "match_type": body.match_type, "value": value, "label": label}


@router.delete("/api/watchlist/{entry_id}", status_code=204,
               dependencies=[Depends(_deps._csrf_check),
                             Depends(_deps._auth_check)])
def api_watchlist_delete(entry_id: int) -> Response:
    row = _deps.db().execute("SELECT id FROM watchlist WHERE id = ?", (entry_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Not found")
    with _deps.db():
        _deps.db().execute("DELETE FROM watchlist WHERE id = ?", (entry_id,))
    return Response(status_code=204)
