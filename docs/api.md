# API Reference

The web server exposes a JSON API at `http://YOUR_PI_IP/stats/api/`.

## Flights

| Method | Path | Description |
|---|---|---|
| GET | `/api/flights` | Flight list. Filters: `from`/`to` (Unix epoch, preferred — browser-local midnight), `date`, `date_from`/`date_to` (YYYY-MM-DD receiver local time, backward compat), `icao`, `callsign`, `reg`, `type`, `source`, `flags`, `squawk`. Sortable, paginated. |
| GET | `/api/flights/export.csv` | CSV export of flight list (same filters as above, no pagination) |
| GET | `/api/flights/{id}` | Flight detail (metadata + other flights by the same aircraft). The raw position timeline is **not** embedded by default — `positions` is an empty list. Pass `?include_positions=true` to embed the full list (the SPA instead uses the two endpoints below). |
| GET | `/api/flights/{id}/positions` | Paginated raw positions for the inspection table. `limit` (default 1000, max 2000), `offset`. Returns `{total, limit, offset, positions}`. |
| GET | `/api/flights/{id}/positions/chart` | LTTB-downsampled positions for chart/map rendering. `target` (default 500, max 2000). Returns `{total, target, positions}`. |
| GET | `/api/flights/{id}/photo` | Aircraft photo via 6-step ladder: specific-ICAO cache → type cache → DB join → Planespotters/airport-data/hexdb fetch → type probe → Wikipedia. `is_type_photo: bool` in response. |

## Aircraft

| Method | Path | Description |
|---|---|---|
| GET | `/api/aircraft/{icao}/flights` | All flights by ICAO hex |
| GET | `/api/aircraft/{icao}/photo` | Aircraft photo — same 6-step ladder; `is_type_photo: true` when serving a type-level fallback |
| GET | `/api/aircraft/flagged` | Flagged aircraft (military / interesting / anonymous). `flags=military\|interesting\|anonymous` to filter one kind. Paginated. |

## Statistics

| Method | Path | Description |
|---|---|---|
| GET | `/api/stats` | Aggregate stats: summaries, hourly/daily breakdowns, top routes/airports/types/airlines, furthest aircraft. Cached 120 s. |
| GET | `/api/stats/records` | All-time personal records: furthest, fastest, highest, longest |
| GET | `/api/stats/polar` | Max detection range per azimuth bucket (default 10°, 36 buckets) |
| GET | `/api/dates` | Per-day flight counts. Cached 600 s. |

## Map

| Method | Path | Description |
|---|---|---|
| GET | `/api/live` | Currently tracked aircraft (used by nav badge) |
| GET | `/api/map/snapshot` | Aircraft snapshot at a given timestamp (`at`, `trail` params) — powers live map and rewind |
| GET | `/api/map/heatmap` | Position density grid for heatmap overlay. `window`: `24h`/`7d`/`30d`/`all`. GZip-compressed; per-window cache (5 min–6 h). |
| GET | `/api/map/coverage` | Receiver coverage polygon. `window`: `24h`/`7d`/`30d`/`all`. 36-point polygon, one vertex per 10° bearing bucket. |
| GET | `/api/airspace` | Airspace GeoJSON. Cached 1 h. |

## Watchlist

| Method | Path | Description |
|---|---|---|
| GET | `/api/watchlist` | List all watchlist entries (with `airborne` flag) |
| POST | `/api/watchlist` | Add entry: `{match_type, value, label?}`. Requires `X-Requested-With` header. |
| DELETE | `/api/watchlist/{id}` | Remove entry. Requires `X-Requested-With` header. |

## Enrichment

| Method | Path | Description |
|---|---|---|
| GET | `/api/airlines/{prefix}/flights` | All flights by airline ICAO prefix (e.g. `LOT`) |
| GET | `/api/types/{type}/flights` | All flights by aircraft type (e.g. `B738`) |

## System

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | Liveness probe (DB ping) |
| GET | `/api/metrics` | Receiver metrics time-series. `range`: `1h`/`6h`/`24h`/`48h`/`7d`/`30d`/`90d` or custom. Auto-downsamples. |
| GET | `/api/metrics/health` | 9 rule-based and baseline-aware health checks over `receiver_stats`. Cached 60 s. |
| GET | `/api/settings` | Read-only runtime settings dict (secrets masked). Includes `map_history_hours` (rewind slider cap), `time_format`, `page_size`, and all `RSBS_*` tunables. |
| GET | `/api/feeders` | Feeder service status + log/Mode-S details |

## SPA routes

All paths below are handled by the React SPA at the catch-all; FastAPI API routes registered above take precedence.

| External URL | Page |
|---|---|
| `/stats/` | Statistics |
| `/stats/history` | Flight history with filters |
| `/stats/flight/{id}` | Flight detail |
| `/stats/aircraft/{icao}` | Per-aircraft history + Watch toggle |
| `/stats/gallery` | Flagged aircraft gallery |
| `/stats/watchlist` | Watchlist CRUD |
| `/stats/feeders` | Feeder status |
| `/stats/metrics` | Metrics charts + health banner |
| `/stats/settings` | Runtime settings (read-only) |
| `/stats/map` | Live map + rewind + heatmap + coverage |
| `/stats/live` | 302 → `/stats/map` |
| `/stats/v2/*` | 301 → `/stats/*` (back-compat) |

## Database schema

| Table | Purpose |
|---|---|
| `flights` | One row per flight: ICAO, callsign, reg, type, timestamps, aggregates (max alt, max speed, max distance, ADS-B/MLAT position counts, origin/dest ICAO) |
| `positions` | Raw position samples: lat, lon, alt, speed, track, climb rate, RSSI, source type |
| `active_flights` | Currently open flights — persists collector state across restarts |
| `aircraft_db` | Aircraft metadata from tar1090-db (~620k rows) |
| `airlines` | Airline names from OpenFlights |
| `airports` | Airport metadata from adsbdb.com |
| `callsign_routes` | Route cache: callsign → origin/dest airport |
| `photos` | Cached aircraft photo URLs (TTL 30 days) |
| `type_photos` | Cached representative photo per aircraft type code |
| `watchlist` | User-defined watchlist entries |
| `adsbx_overrides` | airplanes.live-confirmed flags |
| `receiver_stats` | Receiver metrics time-series (43 columns; opt-in) |
