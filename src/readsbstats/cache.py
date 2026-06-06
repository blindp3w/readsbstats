"""Response cache + map-cache prewarmer.

Shared bounded-TTL in-memory store used by every web endpoint that wants
to coalesce repeated work (stats, polar, heatmap, coverage, feeders, …).
Also owns the background prewarmer thread that keeps the heatmap/coverage
caches hot.

Audit 2026-05-25: keys may be caller-controlled (filtered /api/stats keys
are ``stats:{from}:{to}``) so the cache caps total entries and evicts the
oldest on overflow.

BE-12 (Audit 2026-05-31): the cache is read/written from the request
thread pool AND the prewarmer thread. OrderedDict mutation isn't atomic
across those, so all `_cache` access goes through `_CACHE_LOCK`.

The map-compute functions (`_compute_heatmap_sync`,
`_compute_coverage_sync`) live in `api/map.py`; the prewarmer imports
them lazily inside `_prewarm_one` to break what would otherwise be a
``cache → api.map → cache`` cycle at import time.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import OrderedDict as _OrderedDict

from . import config

log = logging.getLogger("web")


# ---------------------------------------------------------------------------
# Response cache — bounded TTL store, keyed by endpoint name
# ---------------------------------------------------------------------------
_cache: "_OrderedDict[str, tuple[float, object]]" = _OrderedDict()
_CACHE_LOCK = threading.RLock()
# Serializes computation of the expensive all-time /api/stats payload so a
# cold-cache thundering herd ("All time" clicked by several users at once) and
# the hourly prewarmer don't all run the ~15-query scan in parallel. Filtered
# stats keys are cheap (index-scoped) and quantized by the SPA, so they stay
# lock-free.
_stats_compute_lock = threading.Lock()
_CACHE_MAX_ENTRIES = 256
_CACHE_TTLS: dict[str, int] = {
    "stats":        7200,  # 2 h — all-time payload is prewarmed & very stable. Filtered
                           # stats:{from}:{to} keys inherit this via the prefix fallback,
                           # but the SPA quantizes the window to 5-min buckets so their
                           # effective freshness stays ~5 min (new bucket → new key).
    "stats:all":    7200,  # cadence key the prewarm loop reads via f"{kind}:{window}";
                           # nothing is stored here — the payload lives under bare "stats".
    "polar":        300,   # seconds — max range rarely shifts
    "records":      300,   # seconds — all-time bests, very stable
    "health":        60,   # seconds — matches metrics_collector poll cycle
    "airspace":    3600,   # 1 h — airspace GeoJSON rarely changes (== _AIRSPACE_TTL)
    "dates":        600,   # seconds — calendar of flight days; only ticks daily
    "heatmap:24h":   300,   # 5 min — recent data changes frequently
    "heatmap:7d":   1800,   # 30 min
    "heatmap:30d":  7200,   # 2 h — large query, cache aggressively
    "heatmap:all":  21600,  # 6 h — full-history scan, very stable
    "coverage:24h":  300,
    "coverage:7d":  1800,
    "coverage:30d": 7200,
    "coverage:all": 21600,
    "feeders":       10,   # BE-18 — feeder checks fan out subprocesses; short TTL
    "flagged":       60,   # Audit 17 — flagged gallery; heavy uncached scan before
}
_DEFAULT_TTL  = 30    # seconds
_AIRSPACE_TTL = 3600  # seconds — airspace data rarely changes


def _ttl_for(key: str) -> int:
    # Filtered stats keys arrive as ``stats:{from}:{to}``; the base prefix
    # ``stats`` already has an entry in _CACHE_TTLS, so look that up first
    # before falling back to the default.
    if key in _CACHE_TTLS:
        return _CACHE_TTLS[key]
    base = key.split(":", 1)[0]
    return _CACHE_TTLS.get(base, _DEFAULT_TTL)


def _get_cache(key: str) -> object | None:
    with _CACHE_LOCK:
        entry = _cache.get(key)
        if entry is None:
            return None
        if time.time() - entry[0] < _ttl_for(key):
            return entry[1]
        # Lazy eviction so an expired key doesn't keep occupying the cap.
        del _cache[key]
        return None


def _set_cache(key: str, value: object) -> None:
    now = time.time()
    with _CACHE_LOCK:
        # Refreshing an existing key keeps insertion order useful only when
        # we move it to the end; otherwise the same key would be evicted
        # before never-touched-since keys.
        if key in _cache:
            _cache.move_to_end(key)
        _cache[key] = (now, value)
        if len(_cache) > _CACHE_MAX_ENTRIES:
            # Drop any expired entries first; if still over cap, evict in
            # insertion order (oldest first).
            for k in list(_cache.keys()):
                ts, _ = _cache[k]
                if now - ts >= _ttl_for(k):
                    del _cache[k]
                if len(_cache) <= _CACHE_MAX_ENTRIES:
                    break
            # Audit 17: evict caller-controlled keys (stats:{from}:{to},
            # flagged:*) before the bounded set of named keys — the prewarmed
            # map entries (heatmap:all, coverage:all, …) are the costliest to
            # recompute, so a flood of cheap filtered keys must not push them
            # out. Named keys have an exact entry in _CACHE_TTLS; everything
            # else is fair game first, oldest-first within each group.
            while len(_cache) > _CACHE_MAX_ENTRIES:
                victim = next((k for k in _cache if k not in _CACHE_TTLS), None)
                if victim is None:
                    # Unreachable today (_CACHE_MAX_ENTRIES=256 ≫ the ~30 named
                    # keys), but kept as a guard in case the cap is ever shrunk
                    # below the named-key count: evict the oldest protected key.
                    victim = next(iter(_cache))  # all protected → evict oldest
                del _cache[victim]


# ---------------------------------------------------------------------------
# Per-window async locks for heatmap + coverage handlers
# ---------------------------------------------------------------------------
# These assume a single long-lived event loop (the prod Uvicorn loop): a Lock is
# created once per window and never rebound. Unlike _feeders_lock below (reset to
# None so it rebinds per loop for multi-loop tests), these are not test-reset —
# tests exercising them run on one loop.
_heatmap_locks: dict[str, asyncio.Lock] = {}


def _heatmap_lock(window: str) -> asyncio.Lock:
    if window not in _heatmap_locks:
        _heatmap_locks[window] = asyncio.Lock()
    return _heatmap_locks[window]


_coverage_locks: dict[str, asyncio.Lock] = {}


def _coverage_lock(window: str) -> asyncio.Lock:
    if window not in _coverage_locks:
        _coverage_locks[window] = asyncio.Lock()
    return _coverage_locks[window]


# ---------------------------------------------------------------------------
# Per-event-loop async lock for feeder batch coalescing (BE-18)
# ---------------------------------------------------------------------------
# Created lazily on first use so it binds to the running event loop
# (mirrors _heatmap_lock / _coverage_lock); tests reset it to None to rebind.
_feeders_lock: "asyncio.Lock | None" = None


def _feeder_lock() -> asyncio.Lock:
    global _feeders_lock
    if _feeders_lock is None:
        _feeders_lock = asyncio.Lock()
    return _feeders_lock


# ---------------------------------------------------------------------------
# Background prewarmer — keep heatmap+coverage caches hot so users never pay
# the cold-scan latency. Each entry is refreshed at half its TTL so the
# cache is renewed well before users could see an expiry.
# ---------------------------------------------------------------------------

_PREWARM_TARGETS: list[tuple[str, str]] = [
    ("heatmap", "24h"), ("heatmap", "7d"),  ("heatmap", "30d"),  ("heatmap", "all"),
    ("coverage", "24h"), ("coverage", "7d"), ("coverage", "30d"), ("coverage", "all"),
    ("stats", "all"),   # all-time /api/stats payload (stored under the bare "stats" key)
]

# Stagger gap between the first refresh of consecutive targets. With 9
# targets and a 15s gap, the initial burst is spread across ~120s instead
# of all of them contending immediately at process startup (audit-12 #185).
_PREWARM_INITIAL_STAGGER_S = 15

# TTL-priority order for the initial schedule: shortest-TTL windows are
# the ones a user is most likely to hit first, so they run first. The
# longest-TTL ("all") windows are slowest to compute and rarely hit cold —
# they can wait.
_PREWARM_TTL_PRIORITY = {"24h": 0, "7d": 1, "30d": 2, "all": 3}


def _initial_prewarm_schedule(
    targets: list[tuple[str, str]],
    *,
    now: float,
) -> dict[tuple[str, str], float]:
    """Return ``{(kind, window): epoch_seconds}`` mapping each target to its
    desired *first* refresh time. Earliest first is ``now``; subsequent
    targets are spaced by ``_PREWARM_INITIAL_STAGGER_S``.

    Ordering: the all-time stats payload goes first — it's the only stats
    target, and a cold "All time" click otherwise pays the full ~15-query scan
    for ~120 s after a restart on DuckDB hosts (where it would sort last among
    9 targets). Map targets follow, by TTL ascending (shortest-window =
    most-user-hit = first), heatmap before coverage at each TTL.

    Pulled out so we can unit-test the ordering without spinning up the
    thread or sleeping for real time.
    """
    ordered = sorted(
        targets,
        key=lambda kw: (
            0 if kw[0] == "stats" else 1,
            _PREWARM_TTL_PRIORITY.get(kw[1], 99),
            0 if kw[0] == "heatmap" else 1,
        ),
    )
    return {kw: now + i * _PREWARM_INITIAL_STAGGER_S for i, kw in enumerate(ordered)}


_prewarmer_stop = threading.Event()
_prewarmer_thread: threading.Thread | None = None


def _prewarm_one(kind: str, window: str) -> None:
    """Run the heavy compute for one (kind, window) and populate the cache.
    Cheap to call from any thread — the compute helpers open per-thread DB
    connections via ``api._deps.db()`` and the cache dict is set-only (no
    race risk beyond a last-writer-wins value swap).

    The compute functions live in ``api/map.py`` and ``api/stats.py``; we
    import them lazily to break the ``cache → api.* → cache`` cycle that would
    otherwise form at module load time.
    """
    if kind == "stats":
        # All-time /api/stats payload. Stored under the BARE "stats" key (not
        # "stats:all") to match the unfiltered handler's cache_key. Hold
        # _stats_compute_lock so a concurrent on-demand "All time" request
        # doesn't run the same scan in parallel — it waits, then reuses ours.
        from .api import stats as _api_stats  # noqa: PLC0415 — deferred for anti-cycle
        with _stats_compute_lock:
            _set_cache("stats", _api_stats._compute_stats_sync(None, None))
        return
    from .api import map as _api_map  # noqa: PLC0415 — deferred for anti-cycle
    if kind == "heatmap":
        result = _api_map._compute_heatmap_sync(window)
    else:
        result = _api_map._compute_coverage_sync(window)
    _set_cache(f"{kind}:{window}", result)


def _prewarm_loop(targets: list[tuple[str, str]] | None = None) -> None:
    """Refresh one target per pass with a cool-off between heavy queries.

    The cool-off prevents back-to-back full-table scans from saturating
    the web service for 60+ s on startup — the collector and incoming user
    requests both need a slice of CPU. Steady-state refreshes are sparse
    (half-TTL: 150 s for 24h, 10800 s for `all`) so the thread spends most
    of its life sleeping.
    """
    if targets is None:
        targets = _PREWARM_TARGETS
    # Staggered initial schedule — see _initial_prewarm_schedule().
    next_at: dict[tuple[str, str], float] = _initial_prewarm_schedule(
        targets, now=time.time(),
    )
    if _prewarmer_stop.wait(5):
        return

    while not _prewarmer_stop.is_set():
        target = min(targets, key=lambda kw: next_at[kw])
        kind, window = target
        wait_for = next_at[target] - time.time()
        if wait_for > 0:
            if _prewarmer_stop.wait(min(wait_for, 60)):
                return
            continue

        try:
            _prewarm_one(kind, window)
            ttl = _CACHE_TTLS.get(f"{kind}:{window}", _DEFAULT_TTL)
            next_at[target] = time.time() + max(ttl // 2, 60)
            log.debug("prewarm: refreshed %s:%s (next in %ds)",
                      kind, window, max(ttl // 2, 60))
        except Exception:  # noqa: BLE001 — must not kill the thread
            log.warning("prewarm: %s:%s failed; retry in 5 min",
                        kind, window, exc_info=True)
            next_at[target] = time.time() + 300

        if _prewarmer_stop.wait(10):
            return


def _start_prewarmer(include_map: bool = True) -> None:
    """Start the cache prewarmer daemon.

    ``include_map=False`` warms only the all-time stats payload (pure SQLite)
    and skips heatmap/coverage — used when DuckDB is unavailable so a bare Pi
    doesn't run scheduled full-``positions`` scans through the SQLite fallback.
    """
    global _prewarmer_thread
    if _prewarmer_thread is not None and _prewarmer_thread.is_alive():
        return
    targets = (
        _PREWARM_TARGETS if include_map
        else [t for t in _PREWARM_TARGETS if t[0] == "stats"]
    )
    _prewarmer_stop.clear()
    _prewarmer_thread = threading.Thread(
        target=_prewarm_loop, args=(targets,), name="cache-prewarm", daemon=True
    )
    _prewarmer_thread.start()


def _stop_prewarmer() -> None:
    global _prewarmer_thread
    # Audit 17: join the running thread BEFORE nil-ing the handle. The loop
    # only checks _prewarmer_stop between passes / inside its wait()s, so an
    # in-flight _prewarm_one keeps running after .set(). If we nil the handle
    # without joining, a subsequent _start_prewarmer() clears the stop event
    # and spawns a second thread while the orphaned first one is still doing
    # full-`positions` scans. Joining drains it deterministically.
    t = _prewarmer_thread
    _prewarmer_stop.set()
    if t is not None and t.is_alive():
        t.join(timeout=15)
    _prewarmer_thread = None
