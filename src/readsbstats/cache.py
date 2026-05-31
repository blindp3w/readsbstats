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
_CACHE_MAX_ENTRIES = 256
_CACHE_TTLS: dict[str, int] = {
    "stats":        120,   # seconds — aggregate data, no need to recompute often
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
            while len(_cache) > _CACHE_MAX_ENTRIES:
                _cache.popitem(last=False)


# ---------------------------------------------------------------------------
# Per-window async locks for heatmap + coverage handlers
# ---------------------------------------------------------------------------
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
]

# Stagger gap between the first refresh of consecutive targets. With 8
# targets and a 15s gap, the initial burst is spread across ~105s instead
# of all 8 contending immediately at process startup (audit-12 #185).
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
    targets are spaced by ``_PREWARM_INITIAL_STAGGER_S``. Targets are
    ordered by TTL ascending (shortest-window = most-user-hit = first),
    with kind as a tiebreaker so heatmap runs before coverage at each TTL.

    Pulled out so we can unit-test the ordering without spinning up the
    thread or sleeping for real time.
    """
    ordered = sorted(
        targets,
        key=lambda kw: (_PREWARM_TTL_PRIORITY.get(kw[1], 99), 0 if kw[0] == "heatmap" else 1),
    )
    return {kw: now + i * _PREWARM_INITIAL_STAGGER_S for i, kw in enumerate(ordered)}


_prewarmer_stop = threading.Event()
_prewarmer_thread: threading.Thread | None = None


def _prewarm_one(kind: str, window: str) -> None:
    """Run the heavy compute for one (kind, window) and populate the cache.
    Cheap to call from any thread — the compute helpers open per-thread DB
    connections via ``api._deps.db()`` and the cache dict is set-only (no
    race risk beyond a last-writer-wins value swap).

    The compute functions live in ``api/map.py``; we import them lazily
    here to break the ``cache → api.map → cache`` cycle that would
    otherwise form at module load time.
    """
    from .api import map as _api_map  # noqa: PLC0415 — deferred for anti-cycle
    if kind == "heatmap":
        result = _api_map._compute_heatmap_sync(window)
    else:
        result = _api_map._compute_coverage_sync(window)
    _set_cache(f"{kind}:{window}", result)


def _prewarm_loop() -> None:
    """Refresh one target per pass with a cool-off between heavy queries.

    The cool-off prevents 8 back-to-back full-table scans from saturating
    the web service for 60+ s on startup — the collector and incoming user
    requests both need a slice of CPU. Steady-state refreshes are sparse
    (half-TTL: 150 s for 24h, 10800 s for `all`) so the thread spends most
    of its life sleeping.
    """
    # Staggered initial schedule — see _initial_prewarm_schedule().
    next_at: dict[tuple[str, str], float] = _initial_prewarm_schedule(
        _PREWARM_TARGETS, now=time.time(),
    )
    if _prewarmer_stop.wait(5):
        return

    while not _prewarmer_stop.is_set():
        target = min(_PREWARM_TARGETS, key=lambda kw: next_at[kw])
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


def _start_prewarmer() -> None:
    global _prewarmer_thread
    if _prewarmer_thread is not None and _prewarmer_thread.is_alive():
        return
    _prewarmer_stop.clear()
    _prewarmer_thread = threading.Thread(
        target=_prewarm_loop, name="map-prewarm", daemon=True
    )
    _prewarmer_thread.start()


def _stop_prewarmer() -> None:
    global _prewarmer_thread
    _prewarmer_stop.set()
    _prewarmer_thread = None
