# Changelog

## Unreleased

## 1.7.0 — 2026-05-11

### Added

- **Wikipedia type-photo fallback** — the photo lookup ladder gains a sixth
  step that queries Wikipedia for a representative photo when the existing
  chain (Planespotters → airport-data.com → hexdb.io) misses for both the
  specific aircraft and a probe ICAO of the same type. Resolution is a
  two-hop call: Wikipedia's `opensearch` endpoint maps `aircraft_db.type_desc`
  (e.g. `"BOEING 737-800"`) to a canonical article title, then
  `/api/rest_v1/page/summary/{title}` returns `thumbnail` + `originalimage` +
  article URL. Result is stored in `type_photos` with
  `photographer="Wikipedia"` and `link_url` pointing to the article
  (CC-BY-SA attribution). Disambiguation pages, missing thumbnails,
  400 / 404 / 410, and malformed responses all return a clean miss. Closes
  the gap for vintage, military, GA, and rotorcraft types that the commercial
  photo APIs under-cover (e.g. `MIG29`, `C152`, `EUFI`, `H60`, `AN26`,
  `BE20`).
  - **Defence-in-depth URL allowlist** — returned photo URLs are constrained
    to `upload.wikimedia.org` (HTTPS) and the article link to
    `en.wikipedia.org`. A wiki edit pointing the infobox image at an
    attacker-controlled host gets dropped before it lands in the cache.
  - **Telegram alerts benefit automatically** — `notifier._get_photo_result`
    already routes through the shared `resolve_photo` ladder, and Wikipedia
    URLs on `upload.wikimedia.org` pass the existing SSRF guard and the
    10 MB download cap.
  - **New env var** `RSBS_WIKIPEDIA_PHOTO` (default `1`) — set to `0` to
    skip step 6 entirely. Lives next to `PHOTO_CACHE_DAYS` and
    `TELEGRAM_PHOTOS` in `config.py`. Toggling it does **not** invalidate
    already-written `type_photos` rows; use
    `DELETE FROM type_photos WHERE photographer='Wikipedia'` (or
    `DELETE FROM type_photos WHERE thumbnail_url IS NULL` for negative rows)
    to force re-evaluation.

### Changed

- **`web._fetch_type_photo` now delegates to `photo_sources.resolve_photo`** —
  removed ~90 lines of duplicated ladder logic between the web and notifier
  paths. Both code paths share a single source of truth for the
  cache → JOIN → probe → Wikipedia sequence. The async wrapper keeps the
  per-type `asyncio.Lock` and a cache-hit fast path so the hot read avoids
  the executor hop entirely. `resolve_photo` now supports a "type-only" mode
  when called with `icao_hex=""` — steps 1 and 4 (the icao-keyed cache and
  fetch) are skipped so the type-only caller doesn't pollute the `photos`
  table with an empty-key row.

### Fixed

- **Photo credit attribution on the frontend** — the `loadPhoto()` credit
  line in `static/js/table-utils.js` previously hardcoded
  `"© {photographer} via Planespotters.net"` for every hit, which was
  already wrong for airport-data and hexdb hits and would have rendered
  `"© Wikipedia via Planespotters.net"` for the new fallback. Replaced
  with a new `photoSourceSuffix(link)` helper that derives the source
  label from the link URL's hostname (Planespotters.net /
  airport-data.com / hexdb.io / Wikipedia); empty suffix when the link is
  missing or on an unrecognised host.

### Operations

- New log lines at `DEBUG` from the `photo_sources` logger on every
  Wikipedia step-6 outcome (hit / miss / failure) — same convention as
  the rest of the photo chain. For ongoing visibility, query the cache
  directly:

  ```sh
  sqlite3 /mnt/ext/readsbstats/history.db \
    "SELECT type_code, link_url,
            datetime(fetched_at,'unixepoch','localtime') AS fetched
     FROM type_photos WHERE photographer='Wikipedia'
     ORDER BY fetched_at DESC;"
  ```

### Tests

- +23 Python tests and 8 JS tests. `TestFetchWikipediaType` (14 cases)
  covers defensive parsing, host allowlist, percent-encoding, HTTP
  400 / 404 / 410 / 429 / 500 handling, User-Agent header, and missing /
  non-list / non-string field handling. `TestUrlHostMatches` (4) covers
  the host-allowlist helper. `TestResolvePhoto::test_wikipedia_*` (7)
  covers integration with the ladder, including the new type-only mode.
  `TestFetchTypePhoto` and `TestGetPhotoResult` each gain a Wikipedia
  end-to-end test. Existing test classes get an autouse fixture that
  disables `_WIKIPEDIA_ENABLED` so probe-miss tests don't accidentally
  hit the network. Total suite: 1108 Python + 62 JS, all passing.

## 1.6.0 — 2026-05-11

### Security

- **Centralised SSRF guard** — new `src/readsbstats/http_safe.py` module
  enforces HTTPS-only, public-IP-only (rejects RFC1918 / loopback / link-local /
  metadata / reserved / multicast addresses via `ipaddress` checks), no
  auto-redirect, and a per-call response-size cap on every outbound HTTP
  request. Two entry points: `safe_urlopen()` for `urllib` callers and
  `safe_httpx_get()` for `httpx` callers. Adopted by `photo_sources`
  (256 KB / 10 MB caps), `route_enricher` (64 KB; callsign now percent-encoded),
  `adsbx_enricher` (4 MB), and `db_updater` (50 MB).
  `db_updater.AIRCRAFT_CSV_URL` switched to the direct `raw.githubusercontent.com`
  URL so the redirect-blocking policy doesn't break the import.
- **HTML-escape Telegram captions** — `registration`, `callsign`, watchlist
  `label`, `type_desc`, `country`, `squawk`, and the daily-summary DB-JOIN
  columns are all run through `notifier._h()` before HTML interpolation.
  Prior to this, a `&` / `<` / `>` in any of those fields caused Telegram's
  `parse_mode=HTML` to return 400 and the alert was silently dropped.
- **Structure-aware caption clamp** — `_clamp_caption` replaces
  `_truncate_caption`. Over-limit captions first drop the trailing
  `<i>Photo …</i>` note, then the trailing `<a href="…">…</a>` link line,
  then plain-truncate the body with `…`. Prevents the previous cut-in-the-middle
  of an `href=` attribute that would also produce a 400 from Telegram.

### Operations

- **Collector failure alert** — `notify-telegram@.service` fires via `OnFailure=`
  when the collector permanently fails (after exhausting `StartLimitBurst=5`
  restarts). Sends a Telegram message with the last 30 lines of `systemctl status`
  output. Reads `RSBS_TELEGRAM_TOKEN` / `RSBS_TELEGRAM_CHAT_ID` from the existing
  env file — no extra configuration required.

### Features

- **Shared photo-source module** — `photo_sources.py` centralises the
  Planespotters → airport-data.com → hexdb.io fallback chain. Both the web
  service (`web.py` via `run_in_executor`) and the notifier use the same chain.
  `SOURCES` is the single extension point: append a new callable to add a source.
- **Shared photo lookup ladder** — `photo_sources.resolve_photo()` factors the
  5-step cache → JOIN → fetch → probe ladder used by
  `notifier._get_photo_result`. The web side keeps its own `_fetch_photo` /
  `_fetch_type_photo` split for the asyncio path.

### Bug fixes

- **Telegram photo delivery** — Planespotters blocks hotlink requests from
  Telegram's bot servers. Photos are now downloaded locally (up to 10 MB)
  and uploaded to the `sendPhoto` API as `multipart/form-data`, so the image
  always arrives in the chat. Content-Type detection maps JPEG / PNG / WebP to
  the correct filename. Drops the dead URL-payload fallback (it almost always
  failed for the same reason). Multipart boundary is randomized per upload via
  `secrets.token_hex(16)`.

- **Photo fallback in Telegram alerts** — when no specific aircraft photo exists,
  the notifier now tries airport-data.com and hexdb.io before giving up (previously
  only Planespotters was checked).

### Performance

- **Notification dispatch queue** — `_poll()` no longer spawns a daemon thread
  per call. A single long-lived consumer thread (`tg-dispatch`, started in
  `collector.main()`) reads alerts off `collector._notification_queue` and
  dispatches them serially. The consumer holds one sqlite connection for its
  lifetime (via `notifier._thread_local`) instead of reopening per alert.
  Eliminates thread pileup under bursty alerts; ~5–10 ms saved per alert from
  connection reuse.

## 1.5.2 — 2026-05-09

### Tests

- Deflake `test_index_build_under_concurrent_writes` — on the small CI
  runner the writer thread could be scheduled out for the entire microsecond
  index build and never get its first INSERT in, failing the
  `count > 0` assertion. Added a `threading.Event` barrier so the test
  blocks on the first successful write before the index build begins.
  No production-code change.

## 1.5.1 — 2026-05-09

Production-readiness sweep (seventh audit pass — see
`internal_docs/improvements.md` items #86–101).

### Performance & reliability

- **Per-thread sqlite connections in web** — `web.py::db()` now lazily opens a
  connection per uvicorn worker thread via `threading.local()`. Python's
  per-connection sqlite mutex previously serialised every request through one
  global lock, throwing away WAL's reader concurrency.
- **30 read-only `async def` handlers → `def`** — FastAPI now dispatches them
  to its threadpool, freeing the asyncio event loop. Endpoints that genuinely
  `await` (photo fetchers, heatmap/coverage, feeder checks) stay async.
- **Watchdog heartbeat is now its own thread** — `_watchdog_loop()` ticks every
  20 s independent of the poll loop. A write inside `_poll()` can block on the
  SQLite write lock for tens of seconds while a background `CREATE INDEX` is
  running; the previous inline `WATCHDOG=1` would have missed `WatchdogSec=60`
  and had systemd kill the collector.
- **Single-source background migrations** — `run_background_migrations()` now
  runs only in the collector. Web no longer spawns a duplicate thread that
  would race on the same `CREATE INDEX` and `backfill_bearing` UPDATEs.
- **`/api/dates` cached** (TTL 600 s) — was doing a full GROUP BY scan of
  `flights` on every request even though the result only ticks daily.
- **Partial index `idx_positions_ts_coords`** — `ON positions(ts) WHERE lat IS
  NOT NULL AND lon IS NOT NULL`. Speeds up cold-cache heatmap/coverage when
  many MLAT-only rows have NULL coords.
- **Background helpers guard `conn.close()`** — `_build_positions_indexes` and
  `backfill_bearing` no longer mask a real `connect()` failure with
  `UnboundLocalError` from the `finally` block.

### Security

- **Telegram bot token redacted in error logs** — `notifier._describe_exc()`
  formats `urllib.error.HTTPError` / `URLError` without echoing the request URL
  (which contains `/bot<TOKEN>/`). Defence in depth: current stdlib `__str__`
  doesn't leak the URL, but third-party libs and future stdlib changes might.
- **`safeHttpUrl()` tightened to `^https://`** — the third-party photo
  providers (Planespotters, airport-data, hexdb) all serve over HTTPS already;
  rejecting `http://` closes the MITM window for users on hostile networks.
  Does not affect the readsbstats app's own URLs (those are relative and never
  go through this function), so HTTP-only LAN deployments are unaffected.
- **Auto DB snapshot before purge `--apply`** — `database.snapshot_db()` does
  an atomic `VACUUM INTO <db>.backup-<ts>.db` before any of the three
  `purge_*.py` scripts mutate. `--i-have-a-backup` opts out.

### Tests

- 972 Python tests (was 948) + 54 JS tests. New: per-thread connection
  behaviour, watchdog loop lifecycle, Telegram token-redaction caplog
  assertions, partial-index DDL verification, snapshot helper, concurrent
  writer + index builder, `backfill_bearing` against out-of-range coords,
  notifier "Country: Unknown" fallback.

### Cleanup

- Removed duplicate `end = last_update` assignment in `import_rrd.py`
- uPlot chart instances destroyed on `beforeunload` in `metrics.js`
- `safeHttpUrl()` JS test suite updated to lock the new https-only contract

## 1.1.1 — 2026-04-26

### Security

- Block `javascript:` / `data:` URIs in third-party photo links via a
  `safeHttpUrl()` allowlist
- Require `X-Requested-With` header on watchlist `POST` / `DELETE` (CSRF
  defence — browsers cannot set custom headers cross-origin without a CORS
  preflight that this app rejects)
- Cap watchlist `value` (64 chars) and `label` (255 chars) lengths at the
  Pydantic-model layer; the same caps are enforced in the Telegram `/watch`
  bot command path
- Show only the database filename, not the full path, on `/settings`
- `safeHttpUrl()` now returns the trimmed URL for consistency

## 1.1.0 — 2026-04-24

- Receiver health dashboard with metrics time-series (43 columns) and 9
  health checks (heartbeat, aircraft visibility, message rate, signal drop,
  CPU saturation, gain hints, range degradation)

## 1.0.0 — 2026-04-17

Initial public release.

### Features

- Collector daemon polling readsb `aircraft.json` every 5 seconds
- Automatic flight grouping (30-minute silence gap)
- ADS-B vs MLAT source tracking per position and per flight
- SQLite database with WAL mode (no external DB server)
- Aircraft enrichment from tar1090-db (~620k aircraft)
- Airline names from OpenFlights
- Aircraft photos from Planespotters.net (cached 30 days)
- Route enrichment via adsbdb.com (origin/destination airports)
- ICAO address range country lookup
- Ghost position filter (real-time + historical cleanup)
- Telegram notifications (military, interesting, emergency squawks, daily summary, watchlist)
- Aircraft watchlist (ICAO hex, registration, callsign prefix)

### Web UI

- Statistics dashboard with date-range picker and trend deltas
- Flight history with search, filter, sort, and CSV export
- Per-flight detail with Leaflet map, altitude/speed profile, RSSI chart
- Aircraft detail page with full history and country of origin
- Live flight board with auto-refresh
- Airspace overlay (CTR/TMA/R/D/P zones via GeoJSON)
- Polar range plot
- All-time personal records
- Unit switching (aeronautical / metric / imperial)
- Watchlist and settings pages
