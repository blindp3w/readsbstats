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

| Variable | Default | Description |
|---|---|---|
| `RSBS_AIRCRAFT_JSON` | `/run/readsb/aircraft.json` | Path to readsb's JSON output |
| `RSBS_POLL_INTERVAL` | `5` | Seconds between polls |
| `RSBS_FLIGHT_GAP` | `1800` | Silence gap (seconds) that separates two flights |
| `RSBS_MIN_POSITIONS` | `2` | Discard flights with fewer positions (ghost tracks) |
| `RSBS_MAX_SEEN_POS` | `60` | Skip positions older than this many seconds |
| `RSBS_DB_PATH` | `/mnt/ext/readsbstats/history.db` | SQLite database path |
| `RSBS_RETENTION_DAYS` | `0` | Days to keep raw positions (0 = keep forever) |
| `RSBS_LAT` | `52.24199` | Receiver latitude — **set to your location** |
| `RSBS_LON` | `21.02872` | Receiver longitude — **set to your location** |
| `RSBS_MAX_RANGE` | `450` | Sanity-check max range in nautical miles |
| `RSBS_MAX_SPEED_KTS` | `2000` | Ghost-position filter: reject positions implying ground speed above this threshold (kts) |
| `RSBS_MAX_GS_CIVIL` | `750` | Null the `gs` field for civil aircraft positions reporting ground speed above this (kts) |
| `RSBS_MAX_GS_MILITARY` | `1800` | Null the `gs` field for military or unknown aircraft positions reporting ground speed above this (kts) |
| `RSBS_MAX_GS_DEVIATION` | `100` | Null the `gs` field when reported ground speed deviates from position-derived speed by more than this (kts) |
| `RSBS_MAX_GS_ACCEL` | `8.0` | Null the `gs` field for MLAT positions when acceleration exceeds this (kts/s) |
| `RSBS_ROUTE_CACHE_DAYS` | `30` | How long to cache adsbdb.com route lookups |
| `RSBS_ROUTE_INTERVAL` | `60` | Seconds between route enricher batch runs |
| `RSBS_ROUTE_BATCH` | `20` | Callsigns processed per enricher batch |
| `RSBS_ROUTE_RATE_LIMIT` | `1.0` | Minimum seconds between adsbdb.com API requests |
| `RSBS_AIRSPACE_GEOJSON` | _(empty)_ | Path to a custom airspace GeoJSON file; empty = use bundled `static/airspace/poland.geojson` |
| `RSBS_PHOTO_CACHE_DAYS` | `30` | How long to cache aircraft photo URLs |
| `RSBS_WIKIPEDIA_PHOTO` | `1` | Wikipedia fallback for type photos (`0` to disable) |
| `RSBS_ROOT_PATH` | `/stats` | URL prefix for nginx reverse proxy |
| `RSBS_WEB_PORT` | `8080` | Internal uvicorn port |
| `RSBS_PAGE_SIZE` | `100` | Default flight list page size |
| `RSBS_MAX_PAGE_SIZE` | `500` | Maximum allowed page size |
| `RSBS_TELEGRAM_TOKEN` | _(empty)_ | Telegram bot token — notifications disabled if unset |
| `RSBS_TELEGRAM_CHAT_ID` | _(empty)_ | Telegram chat/user ID |
| `RSBS_SUMMARY_TIME` | `21:00` | Local time (HH:MM) for daily summary; `""` or `"off"` to disable |
| `RSBS_TELEGRAM_UNITS` | `metric` | Units in notification messages: `metric`, `imperial`, or `aeronautical` |
| `RSBS_TELEGRAM_PHOTOS` | `1` | Send aircraft photo with alerts (`0` to disable) |
| `RSBS_TELEGRAM_ANONYMOUS_ALERT` | `1` | Fire alerts for anonymous (non-ICAO hex) aircraft (`0` to mute) |
| `RSBS_TELEGRAM_BASE_URL` | `http://homepi.local/stats` | Base URL for profile links in Telegram messages |
| `RSBS_MAP_HISTORY_HOURS` | `24` | How many hours back the rewind slider can reach (1–168) |
| `RSBS_HEALTH_*` | _(see /settings)_ | Health-dashboard thresholds — all effective values listed on the `/settings` page |
| `RSBS_USE_DUCKDB` | `0` | Enable DuckDB analytical accelerator for heatmap/coverage endpoints |
| `RSBS_DUCKDB_MEMORY_MB` | `256` | DuckDB working-set cap (MB) |
| `RSBS_DUCKDB_THREADS` | `2` | DuckDB worker threads |
| `RSBS_DUCKDB_HOME_DIR` | `/mnt/ext/readsbstats/duckdb-home` | DuckDB home directory (extension cache) |
| `RSBS_DUCKDB_TEMP_DIR` | `/mnt/ext/readsbstats/duckdb-tmp` | DuckDB spill directory |
| `RSBS_PREWARM_MAP_CACHE` | `1` | Background prewarmer for heatmap/coverage caches (`0` to disable) |
| `RSBS_METRICS_ENABLED` | `0` | Enable receiver metrics collection from `/run/readsb/stats.json` |

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
