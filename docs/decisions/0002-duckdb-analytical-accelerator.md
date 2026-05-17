# DuckDB for analytical endpoints — plan & context

*Captured: 2026-05-16. Trigger: `/api/map/heatmap?window=30d` returning 504
Gateway Timeout (nginx 60 s `proxy_read_timeout`) from the v2 SPA after the
Heatmap toggle landed. Decision: defer until v2 cutover is done, then come
back to this doc.*

---

## STATUS — Phase 1 implemented 2026-05-17

Shipped to prod on the Pi 4. Heatmap and coverage now route their full-table
scans through DuckDB's `sqlite_scanner` (read-only ATTACH to the live
SQLite file) when `RSBS_USE_DUCKDB=1`. SQLite fallback is automatic on any
DuckDB error so endpoints can't regress.

**Reference plan for what actually shipped:** `~/.claude/plans/read-internal-docs-internal-duckdb-analy-hazy-comet.md`
(the implementation plan, refined from this design doc). All steps in the
"Order of operations when we resume" section below were followed.

**Key files added/changed:**
- `src/readsbstats/analytics.py` — new module, ~200 LOC
- `src/readsbstats/web.py` — engine dispatch in `_compute_heatmap_sync` /
  `_compute_coverage_sync`; per-window `asyncio.Lock` single-flight;
  background prewarmer thread (`_prewarm_loop`); eager init in lifespan
- `src/readsbstats/config.py` — 6 new env knobs
- `tests/test_analytics.py` — 8 parity + behaviour tests
- `tests/test_web.py::TestMapPrewarmer` — 1 prewarmer functional test
- `scripts/update.sh` — pre-flight `INSTALL sqlite_scanner` after pip install
- `requirements.txt` / `pyproject.toml` — `duckdb==1.5.2` pinned

**Measured on prod (~3.3 M positions in DB, 2026-05-17):**
- Heatmap `?window=30d` cold: ~5 s (was 60 s+ → nginx 504)
- Heatmap `?window=all` cold: similar; with prewarmer running, users see 41 ms cache hits
- Coverage `?window=all` cold: ~6–10 s
- DuckDB init (extension load + ATTACH): ~1–2 s, paid at lifespan startup (eager)

**Phase 2 candidates** (analysis only — none implemented in Phase 1):
- `/api/metrics` 30 d / 90 d downsampling (no cache, ~2.5 M `receiver_stats` rows at 90 d) — **next**
- 3 health baseline checks in `health.py` (`_check_message_rate` / `_signal_drop` / `_aircraft_drop`) — modest win, mechanical port
- `/api/stats`, `/api/stats/polar`, `/api/aircraft/flagged`, daily summary, purge scripts — **explicitly not worth porting** (flights table is ~35 k rows, already cached, marginal gain)

**Engine quirks documented inline in `analytics.py` and CLAUDE.md:**
1. `CAST(double AS INTEGER)` rounds in DuckDB / truncates in SQLite — coverage SQL uses `FLOOR()::INTEGER`
2. `round(x, n)` is banker's in DuckDB / half-up in SQLite — ≤0.01 % of cell-boundary points may bucket differently (cosmetic)

**Two extra fixes discovered on the Pi during deploy:**
- The `readsbstats` system user has no `/home` directory → DuckDB errors on `INSTALL` because the default home doesn't exist. Solution: `RSBS_DUCKDB_HOME_DIR` env knob (default `/mnt/ext/readsbstats/duckdb-home`); the `SET home_directory='...'` runs before `INSTALL` in both `update.sh` pre-flight and `analytics._init_connection`.
- `update.sh`'s `chown -R root:readsbstats; chmod -R u=rwX,g=rX,o= /opt/readsbstats` makes the install dir read-only for the service user → can't put writable state inside `/opt/readsbstats`. Resolved by keeping the home dir on `/mnt/ext` (already writable for `readsbstats`).

---

## Problem statement

A handful of FastAPI endpoints run **aggregate scans over the full
`positions` table**:

| Endpoint | Aggregation | Window scaling |
|---|---|---|
| `/api/map/heatmap` | `GROUP BY round(lat, p), round(lon, p)` | 24h / 7d / **30d** / all |
| `/api/map/coverage` | per-bearing-bucket max distance via SQLite trig | 24h / 7d / 30d / all |
| `/api/stats` (positions cohort fields, daily series, heatmap rows) | several pass-once aggregates | range filter or all-time |
| `/api/stats/polar` | per-azimuth-bucket max distance | all-time |

On a busy receiver the `positions` table grows by ~3–5 M rows/week. A 30-day
window is ~15–25 M rows. SQLite is **single-threaded per query** and uses a
sort-or-hash temp structure for `GROUP BY` over computed columns
(`round(lat, …)`) that no index can serve. CPU and memory both spike.

### What we observed on the Pi 4 (2026-05-16)

- `/api/map/heatmap?window=24h`: completes fast, ~1–3 s, server-side cache
  hides subsequent hits (5-min TTL).
- `/api/map/heatmap?window=7d`: completes ~10–20 s, within nginx's 60 s
  timeout. Cached 30 min.
- `/api/map/heatmap?window=30d`: **exceeds 60 s, nginx returns 504**.
- (Untested but probable) `window=all`: even worse.

Earlier same-day fix raised `MemoryMax` 384M → 1024M to absorb the SQLite
sort buffer. That fixed OOM but didn't fix latency.

---

## Why DuckDB fits this workload

DuckDB is an embedded OLAP database with:

- **Columnar in-memory representation** during query execution — only the
  columns the query touches (`lat`, `lon`, `ts`) are materialised.
- **Vectorised execution** — operates on batches of ~1024 values at a time,
  amortising per-row Python/C dispatch cost.
- **Multi-threaded query execution** — uses all 4 Pi 4 cores; SQLite uses 1.
- **`sqlite_scanner` extension** — reads SQLite files directly via the
  same VFS layer, **no migration, no data duplication, no separate write
  path**. Same on-disk SQLite stays the source of truth; DuckDB is a
  query-time accelerator only.

The aggregate `GROUP BY round(lat, p), round(lon, p) COUNT(*)` is exactly
DuckDB's sweet spot:
- One sequential scan, no index lookups.
- Cheap arithmetic per row (round + hash + accumulate).
- Final result is small (~10–100 K cells) and ships back as a normal
  Python list of tuples.

Realistic speedup on the Pi 4 for the 30 d heatmap query: **5–15×** vs
current SQLite. 30 d should drop from 60 s+ to ~5–10 s.

---

## What we are NOT proposing

- **Not** a full migration. SQLite remains the write path (collector,
  notifier, watchlist, web mutations).
- **Not** a separate analytical DB to keep in sync. DuckDB reads SQLite
  directly each query — no data duplication, no replication lag.
- **Not** changing any existing tests. The new path is feature-flagged so
  the SQLite path is the fallback if DuckDB is missing/broken.

---

## Architecture sketch

```
web.py  ─── existing SQLite for: live polls, mutations, single-row
         │   lookups, `flights` table scans (small, ~35K rows)
         │
         └── NEW analytics module (e.g. analytics.py):
             import duckdb
             _CONN = duckdb.connect()      # in-memory, lazy-loaded once
             _CONN.execute("INSTALL sqlite_scanner; LOAD sqlite_scanner;")
             _CONN.execute("SET memory_limit='256MB'")
             _CONN.execute("SET threads=4")            # use all Pi cores

             def heatmap(window: str) -> dict:
                 cutoff = ...
                 rows = _CONN.execute("""
                   SELECT round(lat, ?) AS rlat,
                          round(lon, ?) AS rlon,
                          COUNT(*) AS w
                   FROM sqlite_scan(?, 'positions')
                   WHERE lat IS NOT NULL AND lon IS NOT NULL
                     AND ts > ?
                   GROUP BY rlat, rlon
                 """, [precision, precision, DB_PATH, cutoff]).fetchall()
                 ...

api_map_heatmap() → analytics.heatmap(window) if config.USE_DUCKDB
                  → _compute_heatmap_sync(window)  # SQLite fallback
```

### Lifecycle

- DuckDB connection is **process-wide singleton**, opened lazily on first
  use. Same `_CONN` handles all analytical queries (heatmap, coverage,
  polar, stats).
- `memory_limit` configurable via env var `RSBS_DUCKDB_MEMORY_MB` (default
  256). Hard cap on DuckDB's working set; queries exceeding it spill to
  disk in `RSBS_DUCKDB_TEMP_DIR` (default `/tmp`).
- `threads` set to 4 (Pi 4 has 4 cores). Don't oversubscribe — the
  collector and other services need CPU too.

### Read consistency

DuckDB's `sqlite_scanner` opens the file as a shared reader. SQLite's WAL
mode means writers don't block readers and vice versa. There's a window
where DuckDB might see a slightly stale snapshot (data committed during
the scan won't be visible) — for heatmap / coverage / stats that's
**completely irrelevant**. None of those are real-time.

### Failure mode / kill switch

`config.USE_DUCKDB` env var (default off initially, then default on after
soak). If DuckDB isn't installed or the import fails, fall back to
SQLite — log a warning, never crash.

---

## Effort & risks

### Effort

- Install (Pi 4 has `aarch64` wheels on PyPI since DuckDB 0.9): `pip install duckdb`.
  ~30 MB on disk including the loaded `sqlite_scanner` extension binary.
- New `src/readsbstats/analytics.py` module: ~150 LOC, 4 endpoints to port
  (heatmap, coverage, stats aggregates, polar).
- Feature flag wiring in `web.py`: ~10 LOC.
- Tests: a parametrised test class that runs each ported function against
  both engines and asserts identical results. ~80 LOC.
- README + CLAUDE.md updates.

**Estimate: 1 working day**, including soak test.

### Risks & mitigations

| Risk | Mitigation |
|---|---|
| DuckDB wheel doesn't build on the Pi for some reason | Pi 4 `aarch64` wheels are first-class on PyPI; pre-flight `pip install duckdb` on the Pi before touching code. |
| Memory blow-up on `window=all` | `memory_limit='256MB'` is a hard cap. DuckDB spills to disk if needed. Combined with our 1024 MB systemd limit there's headroom. |
| Read-locking conflicts with the collector's writes | DuckDB uses shared reads via the SQLite VFS. SQLite WAL allows concurrent readers + a single writer. No conflict in practice. |
| Adding a 30 MB dep for one feature | The same engine accelerates ALL aggregates (stats endpoint, polar, plus future analytics). The dep cost is per-deployment, not per-endpoint. |
| Slightly stale reads | Heatmap / coverage / polar / stats are not real-time-critical. The live page (`/api/live`, `/api/map/snapshot`) keeps using SQLite directly. |
| Future schema drift between SQLite and DuckDB queries | Centralise all DuckDB SQL in `analytics.py`; if a column changes, one file to update. Add an integration test that runs each query end-to-end. |

---

## Decision: deferred

Today we're focused on the v2 SPA UI work and cutover. The current
mitigation (24 h default heatmap window + `MemoryMax=1024M`) keeps the
common case working. 30 d / all-time heatmaps return 504 — annoying but
not blocking for the post-cutover daily-use case.

**Pick this up after:** v2 SPA cutover is done (Phase 5 in the UI rebuild
plan). At that point the codebase is simpler (no Jinja2), there's no
parallel UI work to coordinate, and any backend changes can land cleanly.

**Order of operations when we resume:**

1. `pip install duckdb` on the Pi, confirm wheel installs cleanly.
2. Write `analytics.py` with the 4 ported endpoints + feature flag.
3. Add a side-by-side comparison test for each function.
4. Deploy with `RSBS_USE_DUCKDB=0` (off). Verify nothing regresses.
5. Flip the flag on. Watch `journalctl` + manually hit each endpoint at
   each window size. Confirm latency drops.
6. Bench the 30 d / all heatmap before & after; document in this file.
7. Once stable for ~1 week, flip the default to on, document in
   CHANGELOG.

---

## References for resumption

- DuckDB Python: https://duckdb.org/docs/api/python/overview
- `sqlite_scanner` extension: https://duckdb.org/docs/extensions/sqlite
- Pi 4 aarch64 wheels: https://pypi.org/project/duckdb/#files
- Current heatmap impl: `src/readsbstats/web.py:1713-1766` (function
  `_compute_heatmap_sync` + endpoint `api_map_heatmap`)
- Current coverage impl: `src/readsbstats/web.py` (search for
  `api_map_coverage` / `_compute_coverage_sync`)
- Current stats aggregate: `src/readsbstats/web.py` (search for
  `api_stats` — the long function around line 1100+)
- Current polar impl: `src/readsbstats/web.py` (search for
  `api_stats_polar`)
- Existing memory-cap context: `internal_docs/internal/` +
  `CLAUDE.md` "Heatmap endpoint is memory-heavy on first hit" gotcha.
- Steady-state Pi 4 memory: ~1.7 GB / 8 GB.

---

## Cheap interim mitigations (if 30 d 504 becomes painful before DuckDB lands)

Each of these is half-a-day at most. Pick whichever is least intrusive
when needed:

1. **Covering index** — `CREATE INDEX idx_positions_heat ON positions(ts, lat, lon) WHERE lat IS NOT NULL AND lon IS NOT NULL`.
   - Migration goes in `database.run_background_migrations()` so it
     doesn't block service startup.
   - Storage cost: ~400 MB on `/mnt/ext/readsbstats`.
   - Speedup: 3–8× for the GROUP BY (index-only scan, no row fetches).
   - Likely drops 30 d from 60 s+ to 10–20 s.

2. **Background prewarmer** — extend the existing background worker
   pattern (cf. `route_enricher`) to hit `/api/map/heatmap` for each
   window every 10 min, populating `_cache`. Users always hit warm
   cache, latency = 0.
   - Wastes ~5 s CPU every 10 min on a busy receiver.
   - Doesn't fix cold-start (e.g. right after a service restart) — but
     the first user click triggers the slow path and subsequent ones
     are fast.

3. **Cap the window at `7d` in the v2 SPA** until DuckDB lands. Remove the
   `30d` and `all` options from the window selector.

If we end up doing (1) AND (2) before DuckDB, that's also fine — they're
complementary and don't conflict with the future DuckDB migration. The
DuckDB path renders both fixes redundant but they wouldn't be wasted: the
covering index also benefits any future query that filters by `ts` AND
projects `lat`/`lon`.
