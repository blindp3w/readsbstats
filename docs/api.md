# API Reference

The web server exposes a JSON API at `http://YOUR_PI_IP/stats/api/`.

## Flights

| Method | Path | Description |
|---|---|---|
| GET | `/api/flights` | Flight list. Filters: `from`/`to` (Unix epoch, preferred — browser-local midnight), `date`, `date_from`/`date_to` (YYYY-MM-DD receiver local time, backward compat), `icao`, `callsign`, `reg`, `type`, `source`, `flags`, `squawk`. Sortable, paginated. **When `RSBS_VDL2_ENABLED` (and `vdl2.db` is attachable), each row gains `has_acars` (0/1) and `has_acars=true` filters to flights with ACARS in their window; absent/ignored otherwise.** |
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
| POST | `/api/watchlist` | Add entry: `{match_type, value, label?}`. Requires `X-Requested-With: XMLHttpRequest`. Also requires `Authorization: Bearer <token>` when `RSBS_API_TOKEN` is set. |
| DELETE | `/api/watchlist/{id}` | Remove entry. Same auth requirements as POST. |

## VDL2 / ACARS (opt-in)

Registered only when `RSBS_VDL2_ENABLED` is set. Read-only; queries the separate
`vdl2.db`; typed via `schemas.Vdl2*` response models. All endpoints return 404
when the feature is disabled, and **503 `{"detail": "VDL2 database unavailable"}`**
when enabled but `vdl2.db` can't be opened/queried. `since`/`until` are `ge=0`
and a request with `until <= since` is rejected with 400. Runtime availability is
exposed at `/api/health` → `vdl2.available` (the SPA gates surfaces on it).

| Method | Path | Description |
|---|---|---|
| GET | `/api/vdl2/messages` | Newest-first feed. Query: `limit` (≤100), `before_id` (keyset pagination), `label`, `hex` (prefix), `reg` (prefix), `since`/`until` (epoch), `q` (FTS5 full-text, `LIKE` fallback). Returns `{messages, next_before_id}`. |
| GET | `/api/vdl2/messages/{icao_hex}` | All messages from one airframe (6-hex ICAO), newest-first. Accepts `since`/`until` (epoch) to scope to a flight window (used by the flight-detail ACARS panel), plus `limit`/`before_id`/`q`. |
| GET | `/api/vdl2/stats` | `{total, last_hour, aircraft, top_labels[], top_airlines[], hourly[24], flights_overlap_pct}`. `top_airlines` codes are name-resolved via the core `airlines` table (degrades to codes); `hourly` is last-24h message counts, zero-filled. `flights_overlap_pct` = % of last-24h flights also seen on VDL2 (computed on the core conn with `vdl2.db` ATTACHed read-only; `null` when the ATTACH is unavailable). |
| GET | `/api/vdl2/reception` | Receiver-health card (Metrics page). `{msgs_last_min, msgs_last_hour, msgs_24h, aircraft_last_hour, newest_ts, newest_age_sec, per_freq[], rate_sparkline[60]}`. `per_freq` groups by `ROUND(freq,3)` MHz; `rate_sparkline` is messages/min for the last 60 min (zero-filled, newest last). vdlm2dec-only — **no signal level** (that field exists only in dumpvdl2). |
| GET | `/api/vdl2/active` | `{icao_hex[], count}` — airframes that transmitted ACARS in the last `minutes` (default 10, 1–120). Map "transmitting now" badge. |
| GET | `/api/vdl2/positions` | `{points[], count}` of `{lat, lon, icao_hex, ts, label}` from the last `minutes` (default 60, 1–1440). Only structured-position messages carry coordinates, so sparse on an H1-dominated feed. Capped at 2000. |
| GET | `/api/vdl2/oooi/{icao_hex}` | OOOI block-time summary for a flight window (`since`/`until`). `{dep, arr, dsta, has_oooi}` — latest DEP + ARR parsed from ACARS **bodies** (OOOI is not a label), plus a `dsta` destination fallback. EXPERIMENTAL; commonly empty on an H1-dominated feed. |

## Enrichment

| Method | Path | Description |
|---|---|---|
| GET | `/api/airlines/{prefix}/flights` | All flights by airline ICAO prefix (e.g. `LOT`) |
| GET | `/api/types/{type}/flights` | All flights by aircraft type (e.g. `B738`) |

## System

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | Liveness probe (DB ping). Includes a `vdl2` block: `{enabled, available, schema_version, fts, messages, newest_ts, newest_age_sec, attach_available}` (available=false / fields omitted when the feature is off or vdl2.db is unreachable). |
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
| `/stats/vdl2` | VDL2 / ACARS messages (shows a disabled notice unless `RSBS_VDL2_ENABLED`) |
| `/stats/live` | 302 → `/stats/map` |
| `/stats/v2/*` | 301 → `/stats/*` (back-compat) |

## Database schema

Current schema version: **5** (stored in the internal `schema_version` table). The collector owns full schema creation and slow background migrations; the web server applies only fast `_migrate()` additions.

> The opt-in VDL2 feature uses a **separate** database (`RSBS_VDL2_DB_PATH`, default `vdl2.db`) with its own `PRAGMA user_version` schema (table `vdl2_messages` + FTS5 `vdl2_fts`). It is independent of the version-5 core schema below and is never created or migrated unless `RSBS_VDL2_ENABLED` is set.

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
| `receiver_stats` | Receiver metrics time-series (44 columns; opt-in) |
| `schema_version` | Internal: one row per applied schema version (`version`, `applied_at`); latest is 5 |
