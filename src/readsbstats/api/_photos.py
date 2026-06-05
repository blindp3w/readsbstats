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
    # Thread-safety invariant (Audit 17): `_type_fetch_locks` is mutated only
    # from this function, which runs exclusively on the event-loop thread (the
    # async photo handlers). There is therefore no concurrent access and no
    # lock guarding the OrderedDict is needed. If a future caller ever touches
    # it from a thread-pool worker, add a threading.Lock around the mutations.
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

    PY-5 (Audit 2026-05-31): in production we resolve via the status-aware
    helper so a *transient* outage (every source raised) doesn't get
    cached as a 30-day confirmed miss. When tests have monkey-patched
    ``photo_sources.fetch_photo`` we honour their legacy
    ``None == confirmed-miss`` contract (mirrors the escape hatch in
    ``resolve_photo``); they inject deterministic results and don't
    want the resolver to second-guess them.

    Does NOT cascade to a type-level photo; callers do that via
    :func:`_fetch_type_photo`.
    """
    conn = _deps.db()
    cache_seconds = config.PHOTO_CACHE_DAYS * 86400

    cached = conn.execute(
        "SELECT * FROM photos WHERE icao_hex = ? AND fetched_at > ?",
        (icao_hex, int(time.time()) - cache_seconds),
    ).fetchone()
    if cached:
        return dict(cached) if cached["thumbnail_url"] else None

    # PY-5: pick the status-aware path only when fetch_photo hasn't been
    # monkey-patched away. Identity check mirrors photo_sources.resolve_photo.
    use_status_helper = photo_sources.fetch_photo is photo_sources._DEFAULT_FETCH_PHOTO
    loop = asyncio.get_running_loop()
    if use_status_helper:
        pr, status = await loop.run_in_executor(
            None, photo_sources.fetch_photo_with_status, icao_hex,
        )
    else:
        pr = await loop.run_in_executor(None, photo_sources.fetch_photo, icao_hex)
        status = "hit" if pr else "miss"

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
            "INSERT OR REPLACE INTO photos "
            "(icao_hex, thumbnail_url, large_url, link_url, photographer, fetched_at) "
            "VALUES (?,?,?,?,?,?)",
            (icao_hex, pr.thumbnail_url, pr.large_url, pr.link_url, pr.photographer, now),
        )
    elif status == "error":
        # PY-5: every source raised. DO NOT poison the cache with a
        # negative row — the next fetch may well succeed. Serve a stale
        # positive if one exists (no TTL check; outage shouldn't drop
        # coverage), else return None without writing.
        result = None
        existing = conn.execute(
            "SELECT thumbnail_url, large_url, link_url, photographer, fetched_at "
            "FROM photos WHERE icao_hex = ?",
            (icao_hex,),
        ).fetchone()
        if existing and existing["thumbnail_url"]:
            result = dict(existing)
            result["icao_hex"] = icao_hex
    else:
        # status == "miss": every source completed cleanly and returned
        # None. Persist a negative-cache row so we don't refetch for
        # PHOTO_CACHE_DAYS, but keep the Audit-13 A13-014 grace: don't
        # overwrite a previously-resolved positive row that's still
        # within cache TTL + 7-day grace.
        result = None
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


def _suppress_off_allowlist(result: dict | None) -> dict | None:
    """PY-6 (Audit 2026-05-31): drop off-allowlist URLs before returning
    to the client, even when ``photo_sources._HOST_ENFORCE`` is False
    (log-only mode).

    The server-side allowlist is the authoritative pre-render check.
    A stale cached row written before BE-17 enforcement was tightened
    could otherwise still flow through the API verbatim. If the
    ``thumbnail_url`` is missing or off-allowlist the whole result is
    dropped (no thumbnail = unusable photo card); ``large_url`` and
    ``link_url`` are nulled individually if off-allowlist.

    Image fields (thumbnail_url, large_url) and link fields (link_url)
    use separate allowlists — en.wikipedia.org is valid as a link host
    but not as an image host. Without this split, a malformed cache
    row whose thumbnail_url pointed at en.wikipedia.org would render
    as a broken image (HTML article fetched as <img src>).

    Applied unconditionally — when enforcement is True, every URL was
    already gated at fetch time so this is a defensive no-op.
    """
    if result is None:
        return None
    thumb = result.get("thumbnail_url")
    # Drop on missing thumbnail too: a dict with no thumbnail is an
    # unusable photo card, regardless of allowlist verdict. (The
    # allowlist helper treats empty/None as "nothing to render" and
    # returns True, so the explicit check is needed here.)
    if not thumb or not photo_sources.is_photo_image_url_allowed(thumb):
        return None
    out = dict(result)
    if not photo_sources.is_photo_image_url_allowed(out.get("large_url")):
        out["large_url"] = None
    if not photo_sources.is_photo_link_url_allowed(out.get("link_url")):
        out["link_url"] = None
    return out


def _annotate_photo(result: dict | None, *,
                    is_type: bool = False,
                    type_code: str | None = None,
                    type_desc: str | None = None) -> dict | None:
    """Attach is_type_photo / type_code / type_desc fields to a photo
    result dict. Applies the PY-6 host-allowlist suppression first so
    every photo emitted to the API goes through the same boundary check.
    """
    result = _suppress_off_allowlist(result)
    if result is None:
        return None
    return {
        **result,
        "is_type_photo": is_type,
        "type_code":     type_code if is_type else None,
        "type_desc":     type_desc if is_type else None,
    }
