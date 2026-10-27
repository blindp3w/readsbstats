# Configuration

All settings live in `src/readsbstats/config.py` and can be overridden via environment variables without editing any code. The canonical place is one settings file, `/etc/readsbstats/readsbstats.env`, read by `readsbstats-collector` and `readsbstats-web` (optional, `EnvironmentFile=-`) and by `notify-telegram@` (which requires it). `scripts/install.sh` creates it from `systemd/readsbstats.env.example` (mode 0600, root:root — it holds the Telegram bot token) with every option commented out. One `NAME=value` per line (no `Environment=` prefix), comments on their own lines:

```ini
RSBS_RETENTION_DAYS=180
RSBS_FLIGHT_GAP=1200
```

Restart the services after editing: `sudo systemctl restart readsbstats-collector readsbstats-web`.

VDL2 settings stay in `/etc/readsbstats/vdl2.env` (see [VDL2 / ACARS](#vdl2--acars-opt-in)); the web unit loads it after `readsbstats.env`, so it wins for VDL2 variables.

A `systemctl edit <unit>` drop-in with `Environment="NAME=value"` lines is an alternative, but a value from the env file overrides the same variable set in a drop-in's `Environment=`.

## Environment variables

All 91 `RSBS_*` vars, grouped by section, all read in `config.py`. Numeric
values that fall below the documented minimum log a stderr warning and fall
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
| `RSBS_MLAT_OUTLIER_MIN` | `10` | Minimum MLAT GS readings required to apply the outlier filter (min `3`; a value below the minimum falls back to `10`) |

### Database

| Variable | Default | Description |
|---|---|---|
| `RSBS_DB_PATH` | `/mnt/ext/readsbstats/history.db` | SQLite database path. Empty string falls back to the default — an empty path silently creates an in-memory DB. |
| `RSBS_DB_SYNCHRONOUS` | `NORMAL` | SQLite `synchronous` level: `NORMAL` or `FULL`. In WAL mode `NORMAL` is corruption-safe — a power cut can lose at most the last few commits, never the database. `FULL` fsyncs every commit (per-commit durability) at a measurable cost on USB HDDs. Invalid values log an error and fall back to `NORMAL`. See [Database crash safety](#database-crash-safety). |
| `RSBS_RETENTION_DAYS` | `0` | Days to keep raw `positions` rows (`0` = keep forever). **Recommended value for a Pi-class install: `180`.** See [Retention — what survives the horizon](#retention--what-survives-the-horizon) below. |
| `RSBS_PURGE_INTERVAL` | `3600` | Seconds between retention-purge passes (min `1`; only fires when `RETENTION_DAYS > 0`). The purge deletes expired rows in 50 000-row batches with short sleeps between batches so the collector and web readers are not locked out. |
| `RSBS_GRID_FINE_RETENTION_DAYS` | `14` | Days of fine-scale (0.01°) heatmap rollup rows to keep in `grid_daily` (min `8` — the 7d window needs 7 full days + today). Only the fine grid is pruned; the coarse (0.1°) grid and `coverage_daily` rows are permanent, so the 30d/all-time heatmap and coverage views keep working even if raw positions are ever purged. |

### Retention — what survives the horizon

When `RSBS_RETENTION_DAYS` is set, the collector's hourly purge deletes raw
`positions` rows older than the cutoff, then drops the flights that no longer
have enough positions to be worth keeping. Here is what that means in practice:

**Survives the horizon — kept forever:**

- The `grid_daily` coarse (0.1°) heatmap rollup and the `coverage_daily`
  rollup — both permanent. The all-time heatmap and coverage overlays
  therefore keep their full history even after positions are purged.

**Bounded by the horizon — everything derived from `flights` and `positions`:**

- A flight whose positions have all aged out is **deleted** along with them:
  the purge removes flights with fewer than `RSBS_MIN_POSITIONS` positions
  left, `last_seen` before the cutoff and no active session. In practice
  that is every flight that ended before the cutoff.
- The History, Statistics and Aircraft pages and the leaderboards (top
  aircraft, airlines, routes, airports, daily/DOW activity counts) are
  computed from `flights`, so they cover only the retention window — they
  are not all-time once retention is on.
- Per-flight raw track replay (altitude/speed chart, flight-detail map
  trace) exists only for flights inside the window.

**Boundary flights:** a flight that spans the cutoff keeps its surviving
positions and has its per-flight aggregates (position counts, max altitude,
max ground speed, max detection range, bearing, bounding box) recomputed from
them.

**First enablement against a backlog is safe.** The batched purge
(50 000 rows per transaction, 200 ms sleep between batches) lets the
collector and web readers interleave normally. There is no need to stop
services before enabling retention. The purge also waits for the one-time
rollup backfill to finish before deleting anything (automatic; at most a
few minutes after the first deploy), so historical heatmap/coverage days
are always rolled up before their raw rows can be purged.

**DB file size:** the file does not shrink immediately after the first purge
— freed pages are recycled via SQLite's freelist for future inserts. At
180-day retention and current typical Pi traffic, the database plateaus at
roughly 1.8 GB rather than growing without bound. Measured on a busy
(Warsaw-area) receiver at exactly that plateau: `history.db` 1.82 GB for
20.5 M position rows and 228 k flights, ≈ 89 bytes per position row including
indexes (schema v6); before the plateau it grew ≈ 300 MB/month. Traffic-dependent,
so a quieter receiver plateaus lower. A future
`VACUUM` (or the offline migration in `scripts/migrate_v6.py`) reclaims
the freed space if smaller file size is important.

Until the data is older than `RSBS_RETENTION_DAYS`, the purge is a no-op; the
first deletions happen once the oldest positions pass the cutoff.

To enable retention on a running Pi install:

```bash
echo 'RSBS_RETENTION_DAYS=180' | sudo tee -a /etc/readsbstats/readsbstats.env
sudo systemctl restart readsbstats-collector
```

(The example file already contains a commented `# RSBS_RETENTION_DAYS=0` line; either uncomment and edit it, or append as above — the last assignment wins.)

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
| `RSBS_PHOTO_HOST_ENFORCE` | `0` | Hard-drop provider photo URLs whose host is off the per-source CDN allowlist (Planespotters / airport-data / hexdb) **at fetch time**. Default (`0`) logs off-allowlist hosts at WARNING but caches the URL, so legitimate-but-unenumerated CDN hosts aren't silently lost. Independent of this, off-allowlist URLs are **always** suppressed at the API response boundary so the SPA never renders them; the cache row stays for operator diagnostic review. |
| `RSBS_AIRSPACE_GEOJSON` | _(empty)_ | Path to a custom airspace GeoJSON file; empty = use bundled `static/airspace/poland.geojson`. Files larger than 10 MB are refused. |
| `RSBS_ROUTE_CACHE_DAYS` | `30` | How long to cache adsbdb.com route lookups |
| `RSBS_ROUTE_INTERVAL` | `60` | Seconds between route enricher batch runs (min `1`) |
| `RSBS_ROUTE_BATCH` | `20` | Callsigns processed per enricher batch (min `1`) |
| `RSBS_ROUTE_RATE_LIMIT` | `1.0` | Minimum seconds between adsbdb.com API requests (min `0.0`) |

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

### Cache prewarmer

| Variable | Default | Description |
|---|---|---|
| `RSBS_PREWARM_MAP_CACHE` | `1` | Background prewarmer for the all-time stats payload and heatmap/coverage caches (`0` to disable). ≥7d map windows are answered from daily rollup tables and are cheap; 24h stays a small raw scan. |

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
| `RSBS_MAP_TRAIL_WINDOW_SECONDS` | `3600` | How far back the per-aircraft trail on the live map reaches (live mode only — replaying a past `at` is not bounded by it). Bounds the SQL window used to rank position rows so a long flight with thousands of positions doesn't force SQLite to scan the whole partition for a 50-point trail. (min `60`) |

### Web server

| Variable | Default | Description |
|---|---|---|
| `RSBS_ROOT_PATH` | `/stats` | URL prefix for the nginx reverse proxy (trailing slash stripped) |

### Database updaters

| Variable | Default | Description |
|---|---|---|
| `RSBS_AIRCRAFT_DB_MIN_RATIO` | `0.8` | Minimum fraction of the previous `aircraft_db` row count that a freshly-imported tar1090-db CSV must contain before the swap is allowed. Refuses a swap that loses >20% of rows (truncation guard). First-ever imports bypass the check. |
| `RSBS_AIRLINES_DB_MIN_RATIO` | `0.8` | Same min-ratio guard for OpenFlights airlines import. First-ever imports bypass the check. |
| `RSBS_ADSBX_OVERRIDES_TTL_DAYS` | `365` | Maximum age before `adsbx_overrides` rows are eligible for the weekly db_updater purge. The UPSERT clause preserves confirmed metadata across transient upstream gaps; this purge clears genuinely stale rows so a re-registered tail number doesn't keep surfacing the old value. `0` disables the purge. |

### UI / pagination

| Variable | Default | Description |
|---|---|---|
| `RSBS_PAGE_SIZE` | `100` | Default flight list page size (min `1`) |
| `RSBS_MAX_PAGE_SIZE` | `500` | Maximum allowed page size (min `1`); `RSBS_PAGE_SIZE` is clamped down to this |
| `RSBS_MAX_EXPORT` | `50000` | Hard cap on rows returned by `/api/flights/export.csv` (min `1`). The endpoint streams rows, so memory is no longer the limiting factor. |
| `RSBS_TIME_FORMAT` | `24h` | Clock format for UI timestamps. Allowed: `24h`, `12h`. Invalid values fall back to `24h`. Seeded into the browser on first boot; users can override locally via `localStorage.rsbs_clock_format`. |
| `RSBS_CARTO_API_KEY` | _(empty)_ | CARTO basemap API key (<https://carto.com/basemaps/apikey/>). CARTO serves "API KEY REQUIRED" placeholder tiles to keyless requests, so set this for a usable map. Served to the browser via `/api/map/basemap` (it rides on every tile request — browser-visible by design); `/api/settings` shows only `configured`/`not set`. Must be 8–256 chars of `A-Z a-z 0-9 _ -`; anything else is rejected with a stderr error (the value is not echoed) and the map falls back to keyless tiles. Read by `readsbstats-web`. |

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

### MCP server (opt-in)

Off by default. These variables live in `/etc/readsbstats/mcp.env` (template: `systemd/readsbstats-mcp.env.example`) and are read by `readsbstats-mcp` only, not by the collector or web; the unit does not read `readsbstats.env`. Setup, client tokens and the tool list are in [MCP server](mcp.md).

| Variable | Default | Description |
|---|---|---|
| `RSBS_MCP_HOST` | `127.0.0.1` | Listen address. Keep it on loopback unless clients connect from another host; then firewall the port. |
| `RSBS_MCP_PORT` | `8765` | Listen port (min `1`). |
| `RSBS_MCP_ALLOWED_HOSTS` | _(empty)_ | Extra accepted `Host` header values (`host:port`), comma-separated, for non-loopback binds. Loopback names are always accepted. |
| `RSBS_MCP_TOKENS_FILE` | `/etc/readsbstats/mcp-tokens` | Per-client token file: `<name> <scopes> <sha256>` lines, hashes only. Must not be group- or world-writable. Set by the unit to the credential copy (`%d/mcp-tokens`, loaded from `/etc/readsbstats/mcp-tokens`); do not set it in `mcp.env`. |
| `RSBS_MCP_API_URL` | `http://127.0.0.1:8080` | Local web API the server calls. Must be `http://` on a loopback host, with no path. |
| `RSBS_MCP_READ_PER_MIN` | `30` | Read tool calls per client per minute (min `1`). |
| `RSBS_MCP_WRITE_PER_HOUR` | `10` | Watchlist write calls per client per hour (min `1`). |
| `RSBS_MCP_PHOTO_PER_HOUR` | `20` | Aircraft photo lookups per client per hour (min `1`). |
| `RSBS_MCP_CONCURRENCY` | `2` | Concurrent API calls across all clients (min `1`). |
| `RSBS_MCP_WATCHLIST_MAX` | `100` | Watchlist size cap for additions made through MCP (min `1`). |

## Logging

Both services log to journald at `INFO` level:

```bash
journalctl -u readsbstats-collector -f   # collector: startup, flight events, route/ADSBx enrichers, errors
journalctl -u readsbstats-web -f         # web: startup, API errors
```

The web server suppresses HTTP access logs (`--log-level warning`) to reduce I/O on the Pi.

## Route enricher logging

The route enricher runs inside the collector, so these lines appear in `journalctl -u readsbstats-collector`:

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
RSBS_AIRSPACE_GEOJSON=/opt/readsbstats/my_airspace.geojson
```

in `/etc/readsbstats/readsbstats.env`, then restart the services.

## VDL2 / ACARS (opt-in)

Disabled by default. Ingests VDL Mode 2 / ACARS messages from an external
decoder into a **separate** SQLite database, leaving `history.db` untouched.
See [Operations → VDL2 / ACARS ingest](operations.md#vdl2--acars-ingest) for the
decoder runbook and the two-switch enablement model.

| Variable | Default | Description |
|---|---|---|
| `RSBS_VDL2_ENABLED` | `false` | Master switch. Set on **both** `readsbstats-web` (tab + `/api/vdl2`) and `readsbstats-vdl2` (ingest). |
| `RSBS_VDL2_DB_PATH` | `/mnt/ext/readsbstats/vdl2.db` | Separate message store (FTS5 + WAL). Never the core DB. |
| `RSBS_VDL2_RETENTION_DAYS` | `90` | Prune messages older than this. `0` = keep forever. |
| `RSBS_VDL2_UDP_HOST` | `127.0.0.1` | Bind address for the JSON-over-UDP listener. |
| `RSBS_VDL2_UDP_PORT` | `5556` | UDP port the decoder feeds (min `1`). (5555 left free for SpyServer.) |
| `RSBS_VDL2_DECODER` | `dumpvdl2` | JSON dialect to expect: `dumpvdl2` (**recommended and supported**: also ATN CPDLC and CM logons, H1 sublabel/MFI, ADS-C positions, per-frame signal level/SNR) or `vdlm2dec` (limited legacy option: ACARS only — no signal level/SNR, no `sublabel`/`mfi` handling, no CPDLC, no CM logon routes, no ADS-C positions). Since 2.29.0 `dumpvdl2` is the code default; a `vdlm2dec` feed needs `RSBS_VDL2_DECODER=vdlm2dec`. Any other value logs an error and falls back to `dumpvdl2`. Decoder setup: [Operations → VDL2 / ACARS ingest](operations.md#vdl2--acars-ingest). |
| `RSBS_VDL2_PURGE_INTERVAL` | `3600` | Seconds between retention prunes (batched; min `60`). The ingest also prunes once at startup. |
| `RSBS_VDL2_BODY_MAX` | `4096` | Max stored message-body length (chars; floor 256). |
| `RSBS_VDL2_RAW_MAX` | `8192` | Max stored verbatim `raw` decoder JSON (chars; floor 256). |

**Single shared flag.** The VDL2 flag + tunables live in one file,
`/etc/readsbstats/vdl2.env`, read by **both** `readsbstats-web` and
`readsbstats-vdl2` (via `EnvironmentFile=-`). Set `RSBS_VDL2_ENABLED=true` there
(not in per-service drop-ins). `install.sh` seeds it from
`systemd/readsbstats-vdl2.env.example` if absent. Runtime *availability*
(is `vdl2.db` actually reachable) is reported separately at `/api/health`
(`vdl2.available`), distinct from this config flag. To turn the feature off, see
[Operations → Turning VDL2 off](operations.md#turning-vdl2-off).

## Database crash safety

The collector and web server both open the SQLite DB with `journal_mode = WAL` and `synchronous = NORMAL` (the default since v2.16.0; `FULL` was the default from v2.1.19 to v2.15.4). In WAL mode `NORMAL` is corruption-safe: a power cut can lose at most the last few commits, never the database itself. The per-commit fsync that `FULL` adds was measured at ~67 ms per flush on the production USB HDD — too expensive for the durability of a few final seconds of position data. Set `RSBS_DB_SYNCHRONOUS=FULL` to restore per-commit durability (see [ADR-0007](decisions/0007-sqlite-integrity-checks.md)).

On startup, the collector writes a sentinel file at `<DB-dir>/.dirty_shutdown` and removes it only on graceful shutdown. If the sentinel is present at the next startup, the collector runs `PRAGMA quick_check(10)` and logs results to journald. Detected corruption (or a failing check) is logged CRITICAL, a Telegram alert is sent, and the collector exits with code 2 without polling (fail-closed — it never writes to a possibly-corrupt DB). The sentinel is retained, so the check repeats on the next start until you recover; see [Database integrity & startup recovery](operations.md#database-integrity--startup-recovery).

Two systemd timers run periodic integrity checks (configured via `systemd/readsbstats-dbcheck*.{service,timer}`):

- Weekly `PRAGMA quick_check` — Sunday 03:30 local
- Monthly `PRAGMA integrity_check` — 1st Sunday 04:00 local

Both fire `OnFailure=notify-telegram@%n.service` on corruption. For manual checks: `python /opt/readsbstats/scripts/check_db.py --mode {quick,full}`.
