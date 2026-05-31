"""Aircraft photo fetch ladder + per-type async locks.

Used by both ``api/flights.py`` (``/api/flights/{id}/photo``) and
``api/aircraft.py`` (``/api/aircraft/{icao}/photo``). The handlers delegate
the 6-step photo ladder (specific cache → type cache → DB join → specific
fetch → probe → Wikipedia type lookup) to ``photo_sources``; this module
owns the request-side orchestration:

- ``_fetch_photo`` — async wrapper around the specific-ICAO path; persists
  positive and negative results into ``photos``.
- ``_fetch_type_photo`` — async wrapper around the type-level path; serialised
  per type code via an LRU-bounded ``asyncio.Lock`` dict so concurrent
  gallery requests don't trigger duplicate upstream calls.
- ``_annotate_photo`` — attach ``is_type_photo`` / ``type_code`` / ``type_desc``
  fields so the SPA can distinguish specific vs type-level photos.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict as _OrderedDict

from .. import config, database, photo_sources
from . import _deps


# ---------------------------------------------------------------------------
# Per-type asyncio locks — prevent concurrent duplicate fetches for the same type.
# Audit-12 #150 — LRU-capped so the dict can't grow without bound across the
# worker's lifetime. ICAO type designators are ~3k distinct in practice; 1024
# is comfortable headroom for hot types while still capping memory.
# ---------------------------------------------------------------------------
_TYPE_LOCKS_MAX = 1024
_type_fetch_locks: "_OrderedDict[str, asyncio.Lock]" = _OrderedDict()


def _type_lock(type_code: str) -> asyncio.Lock:
    existing = _type_fetch_locks.get(type_code)
    if existing is not None:
        _type_fetch_locks.move_to_end(type_code)
        return existing
    lock = asyncio.Lock()
    _type_fetch_locks[type_code] = lock
    # Audit-13 A13-004: skip eviction of locks that are currently held.
    # Previously, `popitem(last=False)` could remove a held lock; the
    # next caller for the same type_code would then get a fresh lock
    # object and race the in-progress fetch.
    while len(_type_fetch_locks) > _TYPE_LOCKS_MAX:
        oldest_key = next(iter(_type_fetch_locks))
        oldest_lock = _type_fetch_locks[oldest_key]
        if oldest_lock.locked():
            # Rotate to end so we don't pick it next iteration.
            _type_fetch_locks.move_to_end(oldest_key)
            # Safety net: if every lock is held (impossible in practice
            # with ICAO type designators <~3k), break to avoid infinite
            # rotation.
            if all(lk.locked() for lk in _type_fetch_locks.values()):
                break
            continue
        _type_fetch_locks.pop(oldest_key)
    return lock


async def _fetch_photo(icao_hex: str) -> dict | None:
    """Return the cached or freshly-fetched specific-ICAO photo dict (or None).

    Delegates to :func:`photo_sources.fetch_photo` (full source chain), and
    persists the result — including a negative cache row when all sources
    fail — into the ``photos`` table.  Does NOT cascade to a type-level photo;
    callers do that via :func:`_fetch_type_photo`.
    """
    conn = _deps.db()
    cache_seconds = config.PHOTO_CACHE_DAYS * 86400

    cached = conn.execute(
        "SELECT * FROM photos WHERE icao_hex = ? AND fetched_at > ?",
        (icao_hex, int(time.time()) - cache_seconds),
    ).fetchone()
    if cached:
        return dict(cached) if cached["thumbnail_url"] else None

    pr = await asyncio.get_running_loop().run_in_executor(
        None, photo_sources.fetch_photo, icao_hex,
    )
    now = int(time.time())
    if pr:
        result = {
            "icao_hex":      icao_hex,
            "thumbnail_url": pr.thumbnail_url,
            "large_url":     pr.large_url,
            "link_url":      pr.link_url,
            "photographer":  pr.photographer,
            "fetched_at":    now,
        }
        conn.execute(
            "INSERT OR REPLACE INTO photos VALUES (?,?,?,?,?,?)",
            (icao_hex, pr.thumbnail_url, pr.large_url, pr.link_url, pr.photographer, now),
        )
    else:
        result = None
        # Audit-13 A13-014: don't blow away a previously-resolved positive
        # row on a transient fetch failure. If a stale-but-positive row is
        # within the grace window (cache TTL + 7 days), leave it untouched
        # so the cached URL keeps serving requests; the next successful
        # fetch will refresh it normally. Outside the window, the negative
        # row signals "confirmed unknown" to subsequent lookups.
        grace_seconds = 7 * 86400
        existing = conn.execute(
            "SELECT thumbnail_url, fetched_at FROM photos WHERE icao_hex = ?",
            (icao_hex,),
        ).fetchone()
        if existing and existing["thumbnail_url"] and existing["fetched_at"] > now - cache_seconds - grace_seconds:
            pass  # keep stale positive row
        else:
            conn.execute(
                "INSERT OR REPLACE INTO photos "
                "(icao_hex, thumbnail_url, large_url, link_url, photographer, fetched_at) "
                "VALUES (?,NULL,NULL,NULL,NULL,?)",
                (icao_hex, now),
            )
    conn.commit()
    return result


async def _fetch_type_photo(type_code: str | None) -> dict | None:
    """Return a cached or freshly-resolved type-level photo dict (or None).

    Delegates the full ladder (type-cache → photos JOIN aircraft_db → probe one
    ICAO → Wikipedia type lookup) to :func:`photo_sources.resolve_photo` via the
    threadpool.  A per-type asyncio.Lock serialises concurrent gallery requests.
    """
    if not type_code:
        return None

    conn = _deps.db()
    cutoff = int(time.time()) - config.PHOTO_CACHE_DAYS * 86400

    # Fast path — cache hit avoids the executor hop entirely.
    cached = conn.execute(
        "SELECT * FROM type_photos WHERE type_code = ? AND fetched_at > ?",
        (type_code, cutoff),
    ).fetchone()
    if cached is not None:
        return dict(cached) if cached["thumbnail_url"] else None

    async with _type_lock(type_code):
        cached = conn.execute(
            "SELECT * FROM type_photos WHERE type_code = ? AND fetched_at > ?",
            (type_code, cutoff),
        ).fetchone()
        if cached is not None:
            return dict(cached) if cached["thumbnail_url"] else None

        def _resolve() -> dict | None:
            # icao_hex="" is the documented type-only mode: resolve_photo skips
            # the specific-aircraft cache check (step 1) and the specific fetch
            # (step 4) so we don't pollute the ``photos`` table with an
            # empty-key row.  BE-13 (Audit 2026-05-31): open a dedicated
            # connection for this executor worker rather than sharing the
            # request thread's connection — resolve_photo can hold the
            # connection across a slow HTTP probe, and serialising every
            # gallery request on one connection's sqlite mutex defeats the
            # threadpool.  When a test injects ``_deps._db`` (in-memory,
            # unreopenable) we must reuse it; only a real per-thread
            # connection is closed.
            worker_conn = _deps._db if _deps._db is not None else database.connect()
            try:
                result, _is_type = photo_sources.resolve_photo(
                    worker_conn, "", type_code,
                    cache_seconds=config.PHOTO_CACHE_DAYS * 86400,
                )
                return result
            finally:
                if worker_conn is not _deps._db:
                    worker_conn.close()

        return await asyncio.get_running_loop().run_in_executor(None, _resolve)


def _annotate_photo(result: dict | None, *,
                    is_type: bool = False,
                    type_code: str | None = None,
                    type_desc: str | None = None) -> dict | None:
    """Attach is_type_photo / type_code / type_desc fields to a photo result dict."""
    if result is None:
        return None
    return {
        **result,
        "is_type_photo": is_type,
        "type_code":     type_code if is_type else None,
        "type_desc":     type_desc if is_type else None,
    }
