# Configuration

All settings live in `src/readsbstats/config.py` and can be overridden via environment variables without editing any files. Create a systemd drop-in:

```bash
systemctl edit readsbstats-collector
```

```ini
[Service]
Environment="RSBS_RETENTION_DAYS=180"
Environment="RSBS_FLIGHT_GAP=1200"
```

## Environment variables

All 77 `RSBS_*` vars in `config.py`, grouped by section. Numeric values
that fall below the documented minimum log a stderr warning and fall
back to the listed default (see `_min_or_default_int`/`_float`).
Boolean vars accept `1`/`0`, `true`/`false`, `yes`/`no`, `on`/`off`
(case-insensitive; empty string = off).

### Data source

| Variable | Default | Description |
|---|---|---|
| `RSBS_AIRCRAFT_JSON` | `/run/readsb/aircraft.json` | Path to readsb's JSON output |

### Collector behaviour

| Variable | Default | Description |
|---|---|---|
| `RSBS_POLL_INTERVAL` | `5` | Seconds between polls (min `1`) |
| `RSBS_FLIGHT_GAP` | `1800` | Silence gap (seconds) that separates two flights (min `1`) |
| `RSBS_MIN_POSITIONS` | `2` | Discard flights with fewer positions (ghost tracks; min `1`) |
| `RSBS_MAX_SEEN_POS` | `60` | Skip positions older than this many seconds (min `1`) |
| `RSBS_MAX_SPEED_KTS` | `2000` | Ghost-position filter: reject positions implying ground speed above this (kts; min `1`) |
| `RSBS_MAX_GS_CIVIL` | `750` | Null the `gs` field for civil aircraft positions reporting ground speed above this (kts; min `1`) |
| `RSBS_MAX_GS_MILITARY` | `1800` | Null the `gs` field for military or unknown aircraft positions reporting ground speed above this (kts; min `1`) |
| `RSBS_MAX_GS_DEVIATION` | `100` | Null the `gs` field when reported ground speed deviates from position-derived speed by more than this (kts; min `1`) |
| `RSBS_MAX_GS_ACCEL` | `8.0` | Null the `gs` field for MLAT positions when acceleration exceeds this (kts/s; min `0.1`) |
| `RSBS_MLAT_OUTLIER_FACTOR` | `5.0` | Null MLAT GS when it exceeds this × the flight's p75 GS (min `2.0`) |
| `RSBS_MLAT_OUTLIER_MIN` | `10` | Minimum MLAT GS readings required to apply the outlier filter (min `3`) |

### Database

| Variable | Default | Description |
|---|---|---|
| `RSBS_DB_PATH` | `/mnt/ext/readsbstats/history.db` | SQLite database path. Empty string falls back to the default — an empty path silently creates an in-memory DB. |
| `RSBS_RETENTION_DAYS` | `0` | Days to keep raw positions (`0` = keep forever) |
| `RSBS_PURGE_INTERVAL` | `3600` | Seconds between retention-purge passes (min `1`; only fires when `RETENTION_DAYS > 0`) |

### Receiver location

| Variable | Default | Description |
|---|---|---|
| `RSBS_LAT` | `52.24199` | Receiver latitude — **set to your location** |
| `RSBS_LON` | `21.02872` | Receiver longitude — **set to your location** |
| `RSBS_MAX_RANGE` | `450` | Sanity-check max range in nautical miles (min `1`) |

### Enrichment / photos / routes

| Variable | Default | Description |
|---|---|---|
| `RSBS_PHOTO_CACHE_DAYS` | `30` | How long to cache aircraft photo URLs |
| `RSBS_WIKIPEDIA_PHOTO` | `1` | Wikipedia fallback for type photos (`0` to disable) |
| `RSBS_PHOTO_HOST_ENFORCE` | `0` | Hard-drop provider photo URLs whose host is off the per-source CDN allowlist (Planespotters / airport-data / hexdb) **at fetch time**. Default (`0`) logs off-allowlist hosts at WARNING but caches the URL, so legitimate-but-unenumerated CDN hosts aren't silently lost. Independent of this, off-allowlist URLs are **always** suppressed at the API response boundary (audit 2026-05-31 PY-6) so the SPA never renders them; the cache row stays for operator diagnostic review. |
| `RSBS_AIRSPACE_GEOJSON` | _(empty)_ | Path to a custom airspace GeoJSON file; empty = use bundled `static/airspace/poland.geojson`. Files larger than 10 MB are refused (audit-13 A13-041). |
| `RSBS_ROUTE_CACHE_DAYS` | `30` | How long to cache adsbdb.com route lookups |
| `RSBS_ROUTE_INTERVAL` | `60` | Seconds between route enricher batch runs (min `1`) |
| `RSBS_ROUTE_BATCH` | `20` | Callsigns processed per enricher batch (min `1`) |
| `RSBS_ROUTE_RATE_LIMIT` | `1.0` | Minimum seconds between adsbdb.com API requests |

### External ADS-B enrichment (airplanes.live)

| Variable | Default | Description |
|---|---|---|
| `RSBS_ADSBX_ENABLED` | `1` | Enable the background area-poll enricher (`0` to disable) |
| `RSBS_ADSBX_INTERVAL` | `60` | Seconds between area polls (min `1`) |
| `RSBS_ADSBX_RANGE` | `250` | Radius in nautical miles around the receiver (min `1`) |
| `RSBS_ADSBX_URL` | `https://api.airplanes.live/v2` | airplanes.live API base URL |

### Receiver metrics (opt-in)

| Variable | Default | Description |
|---|---|---|
| `RSBS_METRICS_ENABLED` | `0` | Enable receiver metrics collection from `/run/readsb/stats.json` |
| `RSBS_METRICS_INTERVAL` | `60` | Seconds between metrics polls (min `10`). NOTE: readsb's `last1min` stats window is fixed at 60 s upstream — changing the poll cadence does NOT change the CPU/message-rate denominators. |
| `RSBS_STATS_JSON` | `/run/readsb/stats.json` | Path to readsb's stats.json |

### DuckDB analytical accelerator (web process)

| Variable | Default | Description |
|---|---|---|
| `RSBS_USE_DUCKDB` | `0` | Enable DuckDB for heatmap/coverage endpoints (SQLite remains the only write path) |
| `RSBS_DUCKDB_MEMORY_MB` | `256` | DuckDB working-set cap (MB; min `64`) |
| `RSBS_DUCKDB_THREADS` | `2` | DuckDB worker threads (min `1`) |
| `RSBS_DUCKDB_HOME_DIR` | `/mnt/ext/readsbstats/duckdb-home` | DuckDB home directory (extension cache; required because `readsbstats` is a system user with no `/home`) |
| `RSBS_DUCKDB_TEMP_DIR` | `/mnt/ext/readsbstats/duckdb-tmp` | DuckDB spill directory |
| `RSBS_PREWARM_MAP_CACHE` | `1` | Background prewarmer for heatmap/coverage caches (`0` to disable; self-disables when DuckDB is unavailable) |

### Receiver health dashboard

Thresholds for the nine receiver-health checks. All effective values are also rendered on the `/settings` page. Each int/float clamps to a stated minimum and falls back to the documented default below it.

| Variable | Default | Description |
|---|---|---|
| `RSBS_HEALTH_HEARTBEAT_WARN_S` | `120` | Warn if the last `receiver_stats` row is older than this (seconds; min `30`) |
| `RSBS_HEALTH_HEARTBEAT_CRIT_S` | `300` | Critical if older than this (min `30`) |
| `RSBS_HEALTH_AIRCRAFT_GAP_S` | `600` | Critical if 0 aircraft seen for this long (min `60`) |
| `RSBS_HEALTH_NOISE_WARN_DB` | `-28` | Warn if noise floor (dBFS) is above this (higher = worse) |
| `RSBS_HEALTH_NOISE_CRIT_DB` | `-25` | Critical noise-floor threshold (dBFS) |
| `RSBS_HEALTH_CPU_WARN_PCT` | `80` | Warn if demod CPU > this % of one core (min `1.0`) |
| `RSBS_HEALTH_CPU_CRIT_PCT` | `90` | Critical demod CPU % (min `1.0`) |
| `RSBS_HEALTH_BASELINE_WEEKS` | `4` | How many same-hour-of-week samples back to average for baseline-aware checks (min `1`) |
| `RSBS_HEALTH_BASELINE_MIN_SAMPLES` | `3` | Minimum baseline samples required before a comparison fires (min `1`) |
| `RSBS_HEALTH_MSG_DROP_PCT` | `50` | Warn if recent message rate is below this % of the baseline (min `1.0`) |
| `RSBS_HEALTH_AIRCRAFT_DROP_PCT` | `25` | Warn if visible-aircraft count is below this % of baseline (min `1.0`) |
| `RSBS_HEALTH_SIGNAL_DROP_DB` | `3` | Warn if signal level drops by more than this many dB below baseline (min `0.1`) |
| `RSBS_HEALTH_GAIN_STRONG_PCT` | `5` | Warn if strong-signals share exceeds this % of messages (min `0.1`) |
| `RSBS_HEALTH_RANGE_SHORT_DAYS` | `7` | Window for the "recent max range" comparison (days; min `1`) |
| `RSBS_HEALTH_RANGE_LONG_DAYS` | `30` | Window for the "long-term max range" comparison (days; min `1`) |
| `RSBS_HEALTH_RANGE_RATIO` | `0.85` | Info-level alert if short-window range falls below long-window range × this (min `0.1`) |

### Map / historical replay

| Variable | Default | Description |
|---|---|---|
| `RSBS_MAP_HISTORY_HOURS` | `24` | How many hours back the rewind slider can reach (min `1`) |
| `RSBS_MAP_TRAIL_WINDOW_SECONDS` | `3600` | How far back the per-aircraft trail on the live/replay map reaches. Bounds the SQL window used to rank position rows so a long flight with thousands of positions doesn't force SQLite to scan the whole partition for a 50-point trail. (min `60`) |

### Web server

| Variable | Default | Description |
|---|---|---|
| `RSBS_WEB_HOST` | `0.0.0.0` | Uvicorn bind address. The systemd unit overrides this to `127.0.0.1`; nginx fronts the port. |
| `RSBS_WEB_PORT` | `8080` | Internal uvicorn port |
| `RSBS_ROOT_PATH` | `/stats` | URL prefix for the nginx reverse proxy (trailing slash stripped) |
| `RSBS_API_TOKEN` | _(empty)_ | Optional bearer-token gate on mutating endpoints (POST/DELETE `/api/watchlist`, audit 2026-05-31 SH-1). Empty = no auth (default trusted-LAN posture). When set, every mutating call must carry `Authorization: Bearer <token>`. Read endpoints are not gated. See [README — Security model](../README.md#security-model). |

### Database updaters

| Variable | Default | Description |
|---|---|---|
| `RSBS_AIRCRAFT_DB_MIN_RATIO` | `0.8` | Minimum fraction of the previous `aircraft_db` row count that a freshly-imported tar1090-db CSV must contain before the swap is allowed. Refuses a swap that loses >20% of rows (truncation guard). First-ever imports bypass the check. |
| `RSBS_AIRLINES_DB_MIN_RATIO` | `0.8` | Same min-ratio guard for OpenFlights airlines import (audit 2026-05-31 PY-7). First-ever imports bypass the check. |
| `RSBS_ADSBX_OVERRIDES_TTL_DAYS` | `365` | Maximum age before `adsbx_overrides` rows are eligible for the weekly db_updater purge. The UPSERT clause preserves confirmed metadata across transient upstream gaps; this purge clears genuinely stale rows so a re-registered tail number doesn't keep surfacing the old value. `0` disables the purge. |

### UI / pagination

| Variable | Default | Description |
|---|---|---|
| `RSBS_PAGE_SIZE` | `100` | Default flight list page size (min `1`) |
| `RSBS_MAX_PAGE_SIZE` | `500` | Maximum allowed page size (min `1`); `RSBS_PAGE_SIZE` is clamped down to this |
| `RSBS_MAX_EXPORT` | `50000` | Hard cap on rows returned by `/api/flights/export.csv` (min `1`). The endpoint streams rows, so memory is no longer the limiting factor. |
| `RSBS_TIME_FORMAT` | `24h` | Clock format for UI timestamps. Allowed: `24h`, `12h`. Invalid values fall back to `24h`. Seeded into the browser on first boot; users can override locally via `localStorage.rsbs_clock_format`. |

### Telegram notifications

| Variable | Default | Description |
|---|---|---|
| `RSBS_TELEGRAM_TOKEN` | _(empty)_ | Telegram bot token — notifications disabled if unset |
| `RSBS_TELEGRAM_CHAT_ID` | _(empty)_ | Telegram chat/user ID |
| `RSBS_SUMMARY_TIME` | `21:00` | Local time (HH:MM) for daily summary; `""` or `"off"` to disable |
| `RSBS_TELEGRAM_UNITS` | `metric` | Units in notification messages: `metric`, `imperial`, or `aeronautical` |
| `RSBS_TELEGRAM_PHOTOS` | `1` | Send aircraft photo with alerts (`0` to disable) |
| `RSBS_TELEGRAM_ANONYMOUS_ALERT` | `1` | Fire alerts for anonymous (non-ICAO hex) aircraft (`0` to mute) |
| `RSBS_TELEGRAM_BASE_URL` | `http://homepi.local/stats` | Base URL for profile links in Telegram messages — **set to your Pi's URL** (trailing slash stripped) |

### Feeders monitoring

| Variable | Default | Description |
|---|---|---|
| `RSBS_FEEDERS` | _(empty → uses 9-feeder default list)_ | JSON array overriding the built-in feeder list shown on `/feeders`. Each entry needs `name` and `unit`; optional keys: `port`, `status_type` (`readsb`/`fr24`/`piaware`/`mlat`), `status_path`, `status_url`. Malformed JSON or missing keys → warn to stderr, fall back to defaults. |
| `RSBS_FEEDER_STATUS_ROOT` | `/run` | Filesystem root that `status_path` entries in `RSBS_FEEDERS` must resolve under — paths escaping the root are rejected (defence-in-depth against path traversal). Override only for testing; production should leave this at the default. |

## Logging

Both services log to journald at `INFO` level:

```bash
journalctl -u readsbstats-collector -f   # collector: startup, flight events, errors
journalctl -u readsbstats-web -f         # web: startup, route enricher, errors
```

The web server suppresses HTTP access logs (`--log-level warning`) to reduce I/O on the Pi.

## Route enricher logging

```
2026-04-16T00:07:00 INFO Route enricher background thread started
2026-04-16T00:08:28 INFO Route enrichment: processed 20 callsign(s)
2026-04-16T00:09:30 WARNING Route enricher: 3/20 callsign(s) skipped due to transient API errors — will retry next batch
```

A `WARNING` appears whenever adsbdb.com is unreachable; it resolves automatically once connectivity is restored. On first start with a populated database the enricher works through all historical callsigns before settling into incremental mode.

## Airspace overlay

The bundled `static/airspace/poland.geojson` contains Warsaw-area zones. **For navigation use only** — replace with your own airspace data if your receiver is not in Poland.

To use custom airspace data, download a GeoJSON from [openaip.net](https://www.openaip.net/) and point to it:

```ini
Environment="RSBS_AIRSPACE_GEOJSON=/opt/readsbstats/my_airspace.geojson"
```

## DuckDB accelerator

`RSBS_USE_DUCKDB=1` enables columnar scans for `/api/map/heatmap` and `/api/map/coverage`. The DuckDB extension binary is pre-fetched by `scripts/update.sh` to avoid a ~5 s download on first hit. SQLite remains the only write path — DuckDB is read-only. Set `RSBS_PREWARM_MAP_CACHE=1` (on by default when DuckDB is available) to keep all 8 cache entries hot at half-TTL.

## Database crash safety

The collector and web server both open the SQLite DB with `journal_mode = WAL` and `synchronous = FULL` (changed from `NORMAL` in v2.1.19 after a power outage). FULL adds one fsync per write commit — negligible at the 5-second poll cadence, and necessary because USB HDDs commonly lie about `SYNCHRONIZE CACHE`.

On startup, the collector writes a sentinel file at `<DB-dir>/.dirty_shutdown` and removes it only on graceful shutdown. If the sentinel is present at the next startup, the collector runs `PRAGMA quick_check(10)` and logs results to journald. Detected corruption is logged CRITICAL but the service continues (degraded) — observability over availability for the unattended Pi.

Two systemd timers run periodic integrity checks (configured via `systemd/readsbstats-dbcheck*.{service,timer}`):

- Weekly `PRAGMA quick_check` — Sunday 03:30 local
- Monthly `PRAGMA integrity_check` — 1st Sunday 04:00 local

Both fire `OnFailure=notify-telegram@%n.service` on corruption. For manual checks: `python /opt/readsbstats/scripts/check_db.py --mode {quick,full}`.
