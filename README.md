# readsbstats

Extended flight history and track logging for [readsb](https://github.com/wiedehopf/readsb) ADS-B receivers, with a web UI for browsing historical flights and statistics.

Designed to run alongside readsb, tar1090, and feed clients on a **Raspberry Pi 4** without overwhelming it.

## Features

- Collects aircraft positions from readsb's `aircraft.json` every 5 seconds
- Automatically groups positions into flights (30-minute silence gap = new flight)
- Tracks **ADS-B vs MLAT** source per position and per flight
- Stores everything in a lightweight SQLite database (no extra database server)
- **Aircraft enrichment**: registration, type code, and full type description sourced from [tar1090-db](https://github.com/wiedehopf/tar1090-db) (~620k aircraft)
- **Airline names**: full airline names sourced from [OpenFlights](https://openflights.org/data.php) and matched to callsign ICAO prefix
- **Aircraft photos**: thumbnails with fallback chain — [Planespotters.net](https://www.planespotters.net/) → [airport-data.com](https://www.airport-data.com/) → [hexdb.io](https://hexdb.io/); cached 30 days
- **Max distance**: tracks furthest detected aircraft (great-circle distance in nautical miles)
- **Unit switching**: toggle between Aeronautical, Metric, and Imperial — persisted in browser
- **Military & interesting aircraft**: flags from tar1090-db surfaced as badges in the flight list and detail page, with counts on the statistics page; military and interesting are mutually exclusive in all counts and filters; dedicated **Gallery** page with card grid view showing photo, registration, type, country of origin, and flight count for every flagged aircraft detected
- **Country of origin**: ICAO address range lookup maps every aircraft to its registration country, shown on the aircraft detail page and in the Top Countries statistics tile
- **Route enrichment**: origin and destination airport per flight via [adsbdb.com](https://www.adsbdb.com/) free API (no auth); stored and cached in SQLite; shown on flight detail, history list, live board, and stats
- **All-time personal records**: furthest detected, fastest, highest altitude, and longest tracked flight — each linked to the source flight; always all-time regardless of date range filter; units-aware
- **Airspace overlay**: CTR / TMA / Restricted / Danger / Prohibited zones rendered as semi-transparent Leaflet overlays on the flight track map; bundled Polish airspace (`static/airspace/poland.geojson`); toggle via Leaflet layer control; zone-type legend below the map; override data with `RSBS_AIRSPACE_GEOJSON`
- **Telegram notifications**: optional bot notifications for first sighting of military/interesting aircraft, emergency squawks (7500/7600/7700), watchlist hits, and configurable daily summary; interactive commands (`/summary`, `/status`, `/watchlist`, `/watch`, `/unwatch`, `/help`) via long polling; notification units (metric/imperial/aeronautical) are independently configurable
- **Aircraft watchlist**: track specific aircraft by ICAO hex, registration, or callsign prefix; Telegram alert fires once per flight when a watched aircraft is first detected; managed via the `/watchlist` web UI or bot commands (`/watch`, `/unwatch`); "Watch" button on aircraft detail page
- Web UI accessible via your existing nginx at `/stats/`
  - Statistics page with date-range picker (all time, last 7/30 days, this/last month, custom): hourly activity, activity heatmap (day × hour), new aircraft, daily unique aircraft, data source breakdown, altitude distribution, emergency squawk counts with links to filtered flight list, top airlines, top aircraft types, top routes, top airports, top countries, most frequent aircraft, polar range plot, all-time personal records; all sections collapsible with state persisted in localStorage
  - Flight list with search/filter by callsign, ICAO hex, registration, type, date, source, military/interesting flag, squawk code; merged route column (origin→destination)
  - Per-flight "Flight Track" tile: Leaflet map with color-coded track (solid blue = ADS-B, dashed orange = MLAT), semi-transparent airspace overlay (CTR/TMA/R/D/P) with toggle and zone legend
  - Per-flight altitude + speed profile chart with RSSI signal strength chart
  - Per-flight aircraft photo thumbnail
  - Aircraft detail page (`/aircraft/{icao}`): full flight history for a single tail number, with aggregate stats, country of origin, and photo
  - Live flight board (`/live`): currently tracked aircraft with auto-refresh, including route column and Leaflet mini-map showing aircraft positions
  - Flight list CSV export (respects active filters and sort)
  - Polar range plot on the statistics page: max detection distance per compass direction, units-aware
  - Flagged aircraft gallery (`/gallery`): card grid of all detected military and interesting aircraft, with photo, registration, type, country of origin, flight count; filterable by flag type (all/military/interesting), sortable by last seen, first seen, or flight count
  - Watchlist page (`/watchlist`): add/remove watched aircraft (ICAO hex, registration, callsign prefix), with "In range" badge for aircraft currently being tracked
  - Settings page (`/settings`): read-only display of all effective runtime configuration values, grouped by category, showing the env var name for each setting; sensitive values (Telegram token, chat ID) are masked
  - Receiver metrics dashboard (`/metrics`): 11 uPlot time-series charts (signal/noise, aircraft counts, messages, range, positions, CPU, feed traffic, tracks, decoder, CPR) with 1h/6h/24h/48h/7d/30d/90d range presets and custom range picker; cursor legend uses 24-hour clock; opt-in via `RSBS_METRICS_ENABLED=1`

## Requirements

- Raspberry Pi 4 (or any Linux machine) running Ubuntu 22.04 / 24.04
- [readsb by wiedehopf](https://github.com/wiedehopf/readsb) writing JSON to `/run/readsb/`
- Python 3.10+ (Ubuntu 24.04 ships 3.12)
- nginx already installed (for the `/stats/` reverse proxy)

## Installation

```bash
# 1. Sync source to the Pi (from your Mac/PC)
rsync -avz --delete \
  --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='.venv' --exclude='*.egg-info' \
  --exclude='docs' --exclude='db' --exclude='.DS_Store' \
  --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
  /path/to/readsbstats/ pi@your-pi:/tmp/readsbstats/

# 2. SSH into the Pi and run the installer as root
ssh pi@your-pi
sudo bash /tmp/readsbstats/scripts/install.sh
```

The installer:
- Creates a `readsbstats` system user
- Sets up `/opt/readsbstats/` with a Python virtualenv
- Creates `/mnt/ext/readsbstats/history.db` (SQLite, on external storage)
- Grants the service user read access to `/run/readsb/`
- Installs and starts systemd services (collector, web server, weekly DB updater timer)
- Downloads aircraft and airline databases (~30 seconds on first run)

After installation, set your receiver coordinates (used for distance calculations and the polar range plot):

```bash
systemctl edit readsbstats-collector readsbstats-web
```

```ini
[Service]
Environment="RSBS_LAT=YOUR_LATITUDE"
Environment="RSBS_LON=YOUR_LONGITUDE"
```

Then restart services and open **`http://YOUR_PI_IP/stats/`** in your browser.

### nginx setup

The installer copies `nginx-readsbstats.conf` to `/opt/readsbstats/`. Add one line to your nginx `server {}` block in `/etc/nginx/sites-enabled/default`:

```nginx
server {
    # ... existing config ...
    include /opt/readsbstats/nginx-readsbstats.conf;
}
```

Then reload nginx:

```bash
nginx -t && systemctl reload nginx
```

The conf file contains:

```nginx
location /stats/ {
    proxy_pass         http://127.0.0.1:8080/;
    proxy_http_version 1.1;
    proxy_set_header   Host              $host;
    proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto $scheme;
    proxy_read_timeout 30s;
}
```

## Local development

You can develop and test on a Mac (or any machine) without deploying to the Pi each time.

### Setup

```bash
# Create virtualenv and install dependencies
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .
```

Copy `history.db` from the Pi to `./db/` once to have real data locally:

```bash
rsync homepi.local:/mnt/ext/readsbstats/history.db ./db/
```

For development and testing (includes pytest, coverage):

```bash
.venv/bin/pip install -r requirements-dev.txt
```

### Run the web server

```bash
bash scripts/dev.sh
```

Opens at **`http://127.0.0.1:8080/`** against the local database. `--reload` is enabled, so saving any `.py` or template file restarts the server automatically.

### Test the collector

To exercise `collector.py` without a live readsb feed, run the aircraft simulator in one terminal and the collector in another:

```bash
# Terminal 1 — generate a rotating aircraft.json
.venv/bin/python -m readsbstats.sim   # writes to /tmp/rsbs_sim.json by default

# Terminal 2 — run the collector against it
RSBS_AIRCRAFT_JSON=/tmp/rsbs_sim.json \
RSBS_DB_PATH=./db/history.db \
.venv/bin/python -m readsbstats.collector
```

The simulator writes 8 aircraft orbiting Warsaw every 5 seconds in readsb's aircraft.json format.

### Running tests

```bash
.venv/bin/pytest                                              # 777 Python tests
node --test tests/js/test_*.mjs                               # 35 JS tests (Node 22+)
for f in static/js/*.js; do node --check "$f"; done           # JS syntax check
```

To see coverage:

```bash
.venv/bin/pytest --cov=readsbstats --cov-report=term-missing
```

| Test file | What it covers |
|---|---|
| `tests/test_web.py` | FastAPI routes (JSON API + HTML pages), filter helpers, cache (incl. filtered-range cache), `_fmt_ts`, personal records, host-local TZ date filter |
| `tests/test_collector.py` | Haversine, source classification, flight open/close, ghost-position filter, GS hard-limit filter, GS cross-validation, MLAT GS acceleration filter, lat/lon bounds rejection |
| `tests/test_notifier.py` | Telegram notifications, daily summary, command listener |
| `tests/test_purge_ghosts.py` | Ghost-position purge script: velocity pass, backward-pass fallback, apply |
| `tests/test_purge_bad_gs.py` | Bad ground-speed purge: hard-limit, cross-validation, boundary conditions, CLI |
| `tests/test_purge_mlat_gs_spikes.py` | MLAT GS spike purge: acceleration detection, orphan max_gs, apply |
| `tests/test_route_enricher.py` | Route enrichment: adsbdb.com parsing, cache logic, transient error handling |
| `tests/test_icao_ranges.py` | ICAO address → country lookup |
| `tests/test_sim.py` | Aircraft simulator |
| `tests/test_db_updater.py` | Aircraft/airline database download and backfill |
| `tests/test_adsbx_enricher.py` | ADSBexchange flag enrichment |
| `tests/test_concurrency.py` | WAL-mode concurrent read/write verification |
| `tests/test_config.py` | Config parsing, validation, clamping, string normalisation |
| `tests/test_database.py` | Schema migrations, index creation, backfills |
| `tests/test_metrics_collector.py` | `/run/readsb/stats.json` parsing, downsampling tiers, allowlist |
| `tests/test_import_rrd.py` | RRD fetch parsing, DERIVE conversion, multi-tier merge |
| `tests/js/test_units.mjs` | JS formatters: `fmtAlt`/`fmtSpd`/`fmtDist`/`fmtClimb`, labels, `getUnits`/`setUnits` |
| `tests/js/test_table_utils.mjs` | JS `flagBadge` bitmask interpretation (military precedence, all flag combinations) |

Python tests use an in-memory SQLite database — no Pi required. JS tests use Node 22's built-in `node --test` runner with `vm`-sandboxed loading; no npm/package.json/node_modules.

### Deploy to the Pi

```bash
rsync -avz --delete \
  --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='.venv' --exclude='*.egg-info' \
  --exclude='docs' --exclude='db' --exclude='.DS_Store' \
  --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
  ~/projects/readsbstats/ homepi.local:/tmp/readsbstats/
ssh homepi.local sudo bash /tmp/readsbstats/scripts/update.sh
```

## Updating code

After syncing new source files to the Pi, run as root on the Pi:

```bash
# Sync code and restart services (most common)
bash /opt/readsbstats/scripts/update.sh

# Sync code only, also re-download aircraft/airline database (no service restart)
bash /opt/readsbstats/scripts/update.sh --db-only

# Sync code, restart services, and re-download database
bash /opt/readsbstats/scripts/update.sh --full
```

All three modes always sync the code first. Only `--db-only` and `--full` trigger a database download (which takes ~30 seconds and downloads ~10 MB). During the download the collector is stopped automatically to avoid SQLite write conflicts, then restarted when the import finishes.

## Aircraft & airline database

Registration, aircraft type, and airline name data is maintained separately from the main flight database and updated weekly by a systemd timer:

```bash
# Manual update
bash /opt/readsbstats/scripts/update.sh --db-only

# Check timer status
systemctl status readsbstats-updater.timer

# Check last run log
journalctl -u readsbstats-updater -n 30
```

The updater also backfills existing flights that have missing registration or type data. tar1090-db stores flags as a binary string (`'10'` = military, `'0001'` = LADD, etc.) — `db_updater.py` parses this correctly so military, interesting, PIA, and LADD flags are all stored accurately.

## Configuration

All settings live in `src/readsbstats/config.py` and can be overridden via environment variables without editing any files. Create a systemd drop-in:

```bash
systemctl edit readsbstats-collector
```

```ini
[Service]
Environment="RSBS_RETENTION_DAYS=180"
Environment="RSBS_FLIGHT_GAP=1200"
```

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
| `RSBS_MAX_SPEED_KTS` | `2000` | Ghost-position filter: reject positions implying ground speed above this threshold (kts); protects `max_distance_nm` from ADS-B ICAO-collision outliers |
| `RSBS_MAX_GS_CIVIL` | `750` | Null the `gs` field for civil aircraft positions reporting ground speed above this (kts) |
| `RSBS_MAX_GS_MILITARY` | `1800` | Null the `gs` field for military or unknown aircraft positions reporting ground speed above this (kts) |
| `RSBS_MAX_GS_DEVIATION` | `100` | Null the `gs` field when reported ground speed deviates from position-derived speed by more than this (kts); applied when dt ≥ 30 s to the previous accepted position |
| `RSBS_MAX_GS_ACCEL` | `8.0` | Null the `gs` field for MLAT positions when acceleration exceeds this (kts/s); catches single-sample multilateration spikes |
| `RSBS_ROUTE_CACHE_DAYS` | `30` | How long to cache adsbdb.com route lookups (confirmed-unknown sentinel TTL) |
| `RSBS_ROUTE_INTERVAL` | `60` | Seconds between route enricher batch runs |
| `RSBS_ROUTE_BATCH` | `20` | Callsigns processed per enricher batch |
| `RSBS_ROUTE_RATE_LIMIT` | `1.0` | Minimum seconds between adsbdb.com API requests |
| `RSBS_AIRSPACE_GEOJSON` | _(empty)_ | Path to a custom airspace GeoJSON file; empty = use bundled `static/airspace/poland.geojson` |
| `RSBS_PHOTO_CACHE_DAYS` | `30` | How long to cache aircraft photo URLs (all sources) |
| `RSBS_ROOT_PATH` | `/stats` | URL prefix for nginx reverse proxy |
| `RSBS_WEB_PORT` | `8080` | Internal uvicorn port |
| `RSBS_PAGE_SIZE` | `100` | Default flight list page size |
| `RSBS_MAX_PAGE_SIZE` | `500` | Maximum allowed page size |
| `RSBS_TELEGRAM_TOKEN` | _(empty)_ | Telegram bot token from @BotFather — notifications disabled if unset |
| `RSBS_TELEGRAM_CHAT_ID` | _(empty)_ | Telegram chat/user ID to send messages to |
| `RSBS_SUMMARY_TIME` | `21:00` | Local time (HH:MM) to send the daily summary; `""` or `"off"` to disable |
| `RSBS_TELEGRAM_UNITS` | `metric` | Units in notification messages: `metric`, `imperial`, or `aeronautical` |
| `RSBS_BASE_URL` | `http://homepi.local/stats` | Base URL used for profile links in Telegram messages |

### Logging

Both services log to journald at `INFO` level with ISO timestamps:

```bash
journalctl -u readsbstats-collector -f   # collector: startup, flight events, purge, errors
journalctl -u readsbstats-web -f         # web: startup, route enricher activity, errors
```

The web server uses uvicorn's `--log-level warning`, so HTTP access logs are suppressed to reduce I/O on the Pi. Application-level events (startup, route enrichment, errors) are still emitted at `INFO`.

### Route enricher logging

The route enricher logs at `INFO` level to the web service journal (same timestamp format as all other services):

```
2026-04-16T00:07:00 INFO Route enricher background thread started
2026-04-16T00:08:28 INFO Route enrichment: processed 20 callsign(s)
2026-04-16T00:09:30 WARNING Route enricher: 3/20 callsign(s) skipped due to transient API errors — will retry next batch
```

A `WARNING` appears whenever adsbdb.com is unreachable; it resolves automatically once connectivity is restored (callsigns are retried, never blacklisted on transient errors).

**Initial catch-up:** On first start with a populated database, the enricher works through all historical callsigns in batches of `RSBS_ROUTE_BATCH` (default 20) every `RSBS_ROUTE_INTERVAL` seconds (default 60 s). This produces a sustained burst of adsbdb.com API calls that subsides once every historical callsign has been fetched. After that, only newly-seen callsigns trigger lookups.

Backfilling historical flights with current route data is correct for scheduled airline flights, where a callsign (flight number) maps to the same origin/destination indefinitely. In the rare case a flight number is reassigned to a different route, historical flights will show the new route.

### Telegram notifications

1. Create a bot via [@BotFather](https://t.me/BotFather) and copy the token.
2. Get your chat ID: message the bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `result[0].message.chat.id`.
3. Set the environment variables on the Pi (e.g. via `systemctl edit readsbstats-collector`):

```ini
[Service]
Environment="RSBS_TELEGRAM_TOKEN=123456:ABCdef..."
Environment="RSBS_TELEGRAM_CHAT_ID=987654321"
Environment="RSBS_SUMMARY_TIME=21:00"
Environment="RSBS_TELEGRAM_UNITS=metric"
Environment="RSBS_BASE_URL=http://homepi.local/stats"
```

Notifications are disabled when the token or chat ID is not set, so this is fully opt-in. If only one of the two is set (or the chat ID is not numeric), the collector logs a warning at startup explaining what's wrong. When Telegram is disabled, all notification-related logic is skipped entirely — no watchlist queries, no flag checks, no daily summary timer, no background listener thread.

**What gets sent:**
- **Military aircraft** — once per ICAO hex, the first time it's ever detected (not repeated on subsequent visits)
- **Interesting aircraft** — same (government, VIP, air ambulance, special mission per tar1090-db flags); mutually exclusive with military — an aircraft counts as one or the other, never both
- **Emergency squawk** — once per flight when squawk 7500, 7600, or 7700 is detected
- **Daily summary** — sent at `RSBS_SUMMARY_TIME` local time: total flights, unique aircraft, military/interesting counts, emergency squawks, furthest/fastest/highest/longest aircraft, busiest hour

**Interactive commands** (text the bot directly):
- `/summary` — on-demand daily summary
- `/status` — aircraft currently in range + today's flight count
- `/help` — list available commands

The bot only responds to the configured `RSBS_TELEGRAM_CHAT_ID` — all other senders are ignored.

### Ghost position filtering

ADS-B receivers occasionally decode "ghost" positions — phantom ICAO address collisions where two different aircraft share the same hex, or multipath/spoofing artefacts that place an aircraft thousands of nautical miles away for a single sample. Left unchecked these inflate `max_distance_nm` and distort the polar range plot.

**Real-time filter (collector):** Any position whose implied ground speed from the previous accepted position exceeds `RSBS_MAX_SPEED_KTS` (default 2000 kts — above any operational aircraft) is silently dropped before it reaches the database. The reference point is only updated on accepted positions, so a single ghost doesn't cascade to reject the next real position.

**Recommended readsb settings** (`/etc/default/readsb` or your service args):

```
--json-reliable 2       # raise reliability threshold above default of 1
--position-persistence 4
```

**One-time historical cleanup:** If ghost positions were recorded before the filter was active, `purge_ghosts.py` removes them and recalculates `max_distance_nm` for affected flights:

```bash
# Dry-run first — shows what would be removed
/opt/readsbstats/venv/bin/python /opt/readsbstats/scripts/purge_ghosts.py

# Apply
/opt/readsbstats/venv/bin/python /opt/readsbstats/scripts/purge_ghosts.py --apply
```

Options: `--db PATH` (default: `RSBS_DB_PATH`), `--max-speed N` (default: `RSBS_MAX_SPEED_KTS`).

### Ground speed filtering

Transponders occasionally report implausible ground speeds — either a single-sample GPS velocity error or a systematic bias where the velocity field is consistently inflated relative to what the actual position track implies. Left unchecked these inflate `max_gs` and corrupt the "Fastest Recorded" statistic.

**Three real-time filters are applied in the collector before each position is written:**

1. **Hard-limit check** — `gs` is nulled if it exceeds `RSBS_MAX_GS_CIVIL` (750 kts) for civil aircraft, or `RSBS_MAX_GS_MILITARY` (1800 kts) for military or unknown aircraft (not in `aircraft_db`). Civil limit is set well above the documented Virgin Atlantic 787 GS record of ~696 kts.

2. **Cross-validation check** — `gs` is nulled if it deviates from the position-derived implied speed by more than `RSBS_MAX_GS_DEVIATION` (100 kts). Only applied when the time delta to the previous accepted position is ≥ 30 seconds, to avoid false positives from position noise at short intervals. This catches cases like MLAT-sourced flights where the velocity field comes from a different message than the position fix.

3. **MLAT acceleration check** — for MLAT positions only, `gs` is nulled if the rate of change from the previous position exceeds `RSBS_MAX_GS_ACCEL` (8.0 kts/s). This catches single-sample multilateration spikes where the reported GS jumps to an implausible value (e.g. 90→700→95 kts) and immediately returns to normal. ADS-B positions are not filtered — their apparent 8–20 kts/s readings during climb and descent are legitimate. The reference GS is only updated on accepted values, so a single spike cannot cascade.

In all cases the position itself is retained — only the `gs` field is set to NULL.

**One-time historical cleanup:** Two scripts clean up historical data and recalculate `max_gs` for affected flights:

```bash
# purge_bad_gs.py — hard-limit and cross-validation checks
/opt/readsbstats/venv/bin/python /opt/readsbstats/scripts/purge_bad_gs.py          # dry-run
/opt/readsbstats/venv/bin/python /opt/readsbstats/scripts/purge_bad_gs.py --apply  # commit

# purge_mlat_gs_spikes.py — MLAT acceleration spikes + orphan max_gs fix
/opt/readsbstats/venv/bin/python /opt/readsbstats/scripts/purge_mlat_gs_spikes.py          # dry-run
/opt/readsbstats/venv/bin/python /opt/readsbstats/scripts/purge_mlat_gs_spikes.py --apply  # commit
```

Options for `purge_bad_gs.py`: `--db PATH`, `--civil-limit N`, `--military-limit N`, `--deviation N`.
Options for `purge_mlat_gs_spikes.py`: `--db PATH`, `--accel-limit N`.
All default to the matching `RSBS_*` config values.

### Airspace overlay

The bundled `static/airspace/poland.geojson` contains approximate Warsaw-area zones (WARSZAWA CTR, WARSZAWA TMA 1/2, BABICE CTR, EP R-40B, EP D-129) as simplified polygons. **These are illustrative only — not for navigation use.** If your receiver is not in Poland, this overlay won't be useful — replace it with your own airspace data.

To use your own airspace data, download a GeoJSON from [openaip.net](https://www.openaip.net/) (free account required) and point to it:

```ini
[Service]
Environment="RSBS_AIRSPACE_GEOJSON=/opt/readsbstats/poland_airspace.geojson"
```

The endpoint accepts any GeoJSON `FeatureCollection` where each feature has:
- `properties.name` — display name
- `properties.type` — `CTR`, `TMA`, `D`, `R`, or `P` (controls fill colour)
- `properties.icaoClass` — ICAO class letter (shown in popup)
- `properties.upperLimit` / `properties.lowerLimit` — `{value, unit}` objects (shown in popup)

### Polar range plot tuning

The plot auto-scales ring spacing to the actual max detection range in your database (25/50/100 nm steps depending on range). If the shape looks too coarse or too spiky on real data, adjust the angular bucket size in `web.py` — look for `BUCKET_DEG = 10` in `api_stats_polar`:

| `BUCKET_DEG` | Buckets | Use when |
|---|---|---|
| `5` | 72 | Dense data, want fine directional detail |
| `10` | 36 | Default, good for most setups |
| `15` | 24 | Sparse data or very uneven coverage |

Only `web.py` needs editing — the frontend reads bearings dynamically.

## Resource usage

Both services are configured to be polite to the Pi:

| Service | CPU quota | Memory limit |
|---|---|---|
| collector | 15% | 192 MB |
| web server | 20% | 384 MB |

Database growth is roughly **50–150 MB/month** depending on local air traffic, at 5-second polling and 90-day retention.

## Useful commands

```bash
# Service status
systemctl status readsbstats-collector readsbstats-web

# Live collector log
journalctl -u readsbstats-collector -f

# Web server log
journalctl -u readsbstats-web -f

# Restart after config change
systemctl restart readsbstats-collector readsbstats-web

# Stop and disable everything
systemctl disable --now readsbstats-collector readsbstats-web readsbstats-updater.timer
```

## Project structure

```
readsbstats/
├── pyproject.toml              # Package config, pytest/coverage settings
├── requirements.txt
├── requirements-dev.txt        # Dev/test dependencies (pytest, coverage)
├── nginx-readsbstats.conf      # nginx location block (include in your server{} block)
├── src/readsbstats/            # Python package
│   ├── config.py               # All tunables (env var overrides), validated
│   ├── database.py             # SQLite schema, WAL setup, connection, migrations
│   ├── geo.py                  # Shared geometry (haversine, bearing)
│   ├── collector.py            # Polling daemon, flight detection, writes
│   ├── enrichment.py           # In-process cache for aircraft_db and airlines lookups
│   ├── icao_ranges.py          # ICAO 24-bit address → country lookup table
│   ├── notifier.py             # Telegram notification helper (mil/interesting/squawk/daily)
│   ├── route_enricher.py       # Background thread: callsign → route via adsbdb.com
│   ├── db_updater.py           # Downloads tar1090-db CSV and OpenFlights airlines.dat
│   ├── web.py                  # FastAPI app, API endpoints, page routes
│   └── sim.py                  # Local dev: generates a fake aircraft.json
├── scripts/
│   ├── install.sh              # First-time installer (run as root on Pi)
│   ├── update.sh               # Code sync + restart + optional DB update
│   ├── dev.sh                  # Local dev: runs web server against ./db/history.db
│   ├── purge_ghosts.py         # One-shot cleanup: removes ghost positions
│   ├── purge_bad_gs.py         # One-shot cleanup: nulls implausible gs values
│   └── purge_mlat_gs_spikes.py # One-shot cleanup: nulls MLAT gs acceleration spikes
├── tests/                      # pytest suite (16 files, 777 tests) + JS tests (tests/js/, 35 tests)
├── templates/
│   ├── base.html               # Shared layout, nav bar with unit selector
│   ├── index.html              # Flight list
│   ├── flight.html             # Flight detail
│   ├── live.html               # Live flight board
│   ├── aircraft.html           # Per-aircraft history
│   ├── stats.html              # Statistics
│   ├── gallery.html            # Flagged aircraft card gallery
│   ├── watchlist.html          # Aircraft watchlist management
│   └── settings.html           # Runtime settings (read-only display)
├── static/
│   ├── css/app.css
│   └── js/
│       ├── units.js            # Unit conversion helpers (aero/metric/imperial)
│       ├── flights.js          # Flight list page
│       ├── flight_detail.js    # Per-flight map, photo, and detail page
│       ├── live.js             # Live flight board (auto-refresh)
│       ├── stats.js            # Statistics charts
│       ├── aircraft.js         # Per-aircraft history + Watch button
│       ├── gallery.js          # Flagged aircraft card gallery
│       └── watchlist.js        # Watchlist management page
├── systemd/
│   ├── readsbstats-collector.service
│   ├── readsbstats-web.service
│   ├── readsbstats-updater.service
│   └── readsbstats-updater.timer
└── docs/
    ├── improvements.md             # Tracked codebase improvements (64 items, all resolved)
    ├── ux-review.md                # UI/UX review (12 items, all resolved)
    ├── Realistic ADS-B and MLAT Reception Ranges for Home Receivers.md
    ├── readsb_README.md            # Upstream readsb reference
    ├── readsb_README-json.md       # readsb JSON output specification
    ├── piaware_install_ubuntu_24.04_arm64.md
    └── internal/                   # Dev notes, investigations, raw data
```

## Database schema

| Table | Purpose |
|---|---|
| `flights` | One row per detected flight: ICAO, callsign, reg, type, timestamps, aggregates (max alt, max speed, max distance, ADS-B/MLAT position counts, bounding box, origin/dest ICAO) |
| `positions` | Raw position samples: lat, lon, alt, speed, track, climb rate, RSSI, source type |
| `active_flights` | Currently open flights — persists collector state across restarts |
| `aircraft_db` | Aircraft metadata from tar1090-db: registration, type code, type description, flags (military, interesting, PIA, LADD) |
| `airlines` | Airline names from OpenFlights: ICAO code, full name, IATA code, country |
| `airports` | Airport metadata from adsbdb.com: ICAO/IATA codes, name, country, lat/lon |
| `callsign_routes` | Route cache: callsign → origin/dest airport; NULL sentinel for confirmed-unknown callsigns |
| `photos` | Cached aircraft photo URLs from Planespotters.net / airport-data.com / hexdb.io (keyed by ICAO hex, TTL 30 days) |
| `watchlist` | User-defined aircraft watchlist: ICAO hex, registration, or callsign prefix entries with optional labels |

SQLite is opened in **WAL mode** so the web server can read while the collector is writing.

## API endpoints

The web server exposes a JSON API alongside the HTML pages:

| Method | Path | Description |
|---|---|---|
| GET | `/api/flights` | Flight list (filterable: date, icao, callsign, reg, type, source) |
| GET | `/api/flights/export.csv` | CSV export of flight list (same filters as above, no pagination) |
| GET | `/api/flights/{id}` | Full flight detail + all positions |
| GET | `/api/flights/{id}/photo` | Aircraft photo (Planespotters → airport-data.com → hexdb.io; cached) |
| GET | `/api/aircraft/{icao}/flights` | All flights by a given ICAO hex |
| GET | `/api/aircraft/{icao}/photo` | Aircraft photo by ICAO hex (same fallback chain; cached) |
| GET | `/api/aircraft/flagged` | Unique military + interesting aircraft (filterable, paginated) |
| GET | `/api/stats` | Aggregate statistics: summaries, charts, top routes/airports, furthest aircraft |
| GET | `/api/stats/records` | All-time personal records: furthest, fastest, highest, longest |
| GET | `/api/airspace` | Airspace GeoJSON (configured path or bundled poland.geojson; cached 1 h) |
| GET | `/api/stats/polar` | Max detection range per azimuth bucket (default 10°, 36 buckets) |
| GET | `/api/live` | Currently tracked aircraft |
| GET | `/api/dates` | Per-day flight counts |
| GET | `/api/airlines/{prefix}/flights` | All flights by airline prefix (e.g. `LOT`) |
| GET | `/api/types/{type}/flights` | All flights by aircraft type (e.g. `B738`) |
| GET | `/api/health` | Service health check |
| GET | `/api/watchlist` | List all watchlist entries (with airborne flag) |
| POST | `/api/watchlist` | Add a watchlist entry (`match_type`, `value`, optional `label`) |
| DELETE | `/api/watchlist/{id}` | Remove a watchlist entry |

## License

MIT
