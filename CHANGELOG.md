# Changelog

## 2.9.4 — 2026-05-25

### Settings page — env-var copy + default indicator + drift defence

Settings was a read-only env-var dump that hardcoded env-var names in the
frontend (frontend audit finding #5) and gave the operator no signal about
which values were the shipped default vs. their own overrides. M5's
search-first command-palette redesign was descoped as over-engineered for a
low-frequency, single-operator reference page; this release instead lands
three small correctness + ergonomics fixes that match how the page is
actually used (glance during troubleshooting, find the env var, copy its
name, edit the systemd override, restart).

User-visible changes:

- **Click-to-copy on every env-var name.** The env-var cell on each row
  is a `<button>` that copies the name to the clipboard. On desktop a
  small icon reveals on hover; on mobile the env-var is visible by
  default with a 44 × min-height tap target. Includes a `document.execCommand('copy')`
  fallback for plain-HTTP LAN contexts where `navigator.clipboard` is
  undefined — that's the **primary** runtime path on the Pi, not an
  edge case.
- **"(default)" muted suffix** on rows whose value matches the shipped
  default, computed against the raw config attribute on the backend so
  masked fields like `telegram_token` don't always read as customized.
  Suppressed when the displayed value already implies default
  (`"not set"`, `"(bundled poland.geojson)"`, etc.) so the page doesn't
  show e.g. `"not set (default)"`.
- **Env-var names ship from the backend, not hardcoded in the
  frontend.** The drift bug from `internal_docs/frontend_audit_2026-05-22.md`
  finding #5 is now structurally impossible: if a `config.py`
  registration is removed, the env-var name passed to the parser
  disappears with it and the line stops compiling. Frontend reads the
  name out of the new `_metadata` block on `/api/settings`.
- **Settings rows work on every viewport** (xs through xl). Dropped
  the `<Table>` primitive entirely for these rows in favour of a CSS
  grid that switches `grid-template-columns` at the `md:` breakpoint
  with a one-class swap. Env-var column was previously
  `hidden md:table-cell` (unreachable on phones); now it's stacked
  vertically per row on mobile with the copy button bottom-right.

Internal:

- **`config._register(payload_key, env_var, default, config_attr, *, secret)`**
  wrapper records env-var reads at their call site and returns
  `(env_var, default)` for splatting into `_int / _float / _bool /
  os.getenv`. 49 payload-bound call sites wrapped. Internal tunables
  (DuckDB, MLAT outlier filter, etc.) left untouched. Raises on
  duplicate payload-key registration at module load.
- **`web._settings_metadata(config_namespace, payload_keys)`** —
  pure function that builds the `_metadata: { key: {env_var, default,
  customized} }` block from `config._META_REGISTRY`. `customized`
  compares the **raw** config attribute against the registered default
  (not the masked display value); `secret=True` keys (`db_path`,
  `stats_json`, `telegram_token`, `telegram_chat_id`) have their
  default masked to `null` to keep the metadata masking consistent
  with the payload masking.
- **`previous_window` upper bound is now exclusive** (`first_seen <
  ts_lo`) so a flight whose `first_seen` falls on the boundary second
  is not double-counted between current and previous. Delta chips
  happened to net out before, but raw `previous_window.total_flights`
  was off by 1 for boundary-second flights. New regression test
  `test_previous_window_boundary_flight_not_double_counted`.
- **Vitest build-constants stub** — `__APP_VERSION__` and
  `__FRONTEND_BUILD__` are now defined in `vitest.config.ts` so the
  Build info card doesn't throw `ReferenceError` in jsdom (those
  constants are vite-define only at build time).
- **Drift defence tests**: `test_metadata_block_present_for_every_payload_key`
  (set equality), `test_metadata_env_vars_resolve_in_config_source`
  (regex grep), `test_register_present_for_every_payload_key`
  (runtime `_META_REGISTRY` membership check — more robust than the
  source-text grep an earlier draft used).

Test count: 1411 → 1418 backend (+7), 230 → 240 frontend (+10 across
new `clipboard.test.ts` and `settings-row.test.tsx`). Full suite green.

## 2.9.3 — 2026-05-25

### Statistics page — phone density + KPI deltas everywhere

Mobile-only polish for the Statistics page. The KPI cards used to break
2-up only at `sm:` (= 600 px under the project's `html { font-size: 15px }`),
so every iPhone in portrait fell into the 1-col branch and the right half
of every card sat empty. Companion fix: the delta chips below the big
numbers had been wired to a backend field that only existed for the 24h
and 7d windows, so the default 30d view (and 90d / All / Custom) always
rendered "—" em-dashes regardless of how much trend data was actually
available.

User-visible changes:

- **2-up KPI grid on phones.** A new `--breakpoint-xs: 22.5rem` Tailwind
  v4 breakpoint (~337 px at the project's 15 px html font-size) triggers
  the 2-column layout on every iPhone including the SE. The big-number
  text in each card scales `text-2xl` → `sm:text-3xl` so 9-char values
  like "3,102,772" still have room in the narrower half-card. Halves
  the scroll height of the overview block on mobile.
- **KPI delta chips work in every window.** Backend `/api/stats` now
  returns a `previous_window` block (totals for the period of equal
  length immediately preceding the requested range) whenever `from` /
  `to` are supplied. Frontend feeds `previous_window.{total_flights,
  unique_aircraft, total_positions}` into the `prev` prop on the three
  numeric KPI cards, so a 30d view shows a real `+X (↑Y%) vs previous
  period` chip instead of an em-dash. `previous_window` is `null` for
  unfiltered (all-time) requests — there is no equivalent prior all-time
  period, so the "All" pill correctly continues to show em-dashes.
  Flights cards still fall back to the legacy `trends.flights_{24h,7d}_prev`
  field for the unfiltered case so the 24h / 7d windows keep showing
  their delta there too.
- **Personal Records and About-receiver footer also 2-up on phones.**
  Same `sm:` → `xs:` breakpoint shift, fixing the same iPhone-
  empty-right-side pattern in two more spots: the four record cells
  (Furthest / Fastest / Highest / Longest) and the six receiver-metadata
  rows inside the "About this receiver" expandable.

Intentionally **not** changed (1-up on phones is the right call):

- Gallery photo grid — each tile needs enough resolution to be useful.
- History filters form — DatePicker inputs are too wide for a ~150 px
  column.
- Activity-by-hour and Daily-unique-aircraft bar charts — need
  horizontal room to be legible.
- FlagBadgeStrip was already `grid-cols-2` on mobile.

Test count: 1409 → 1411 (+2 backend regression tests for the new
`previous_window` shape). Vitest unchanged at 230/230. No frontend
component tests for the breakpoint flip — verified visually on the
deployed Pi at iPhone-portrait widths.

## 2.9.2 — 2026-05-25

### Backend audit 2026-05-25 — all 9 findings closed

Patch release. No user-visible features; addresses every finding from
`internal_docs/backend_audit_2026-05-25.md`. Production-verified on the
Pi — both route and ADSBx fetches now connect via the new pinned-IP
TLS path and continue to return 200.

User-affecting fixes:

- **Route enricher no longer wipes known origin/dest on partial
  adsbdb responses.** `_apply_to_flights` and `_store_route` now use
  `COALESCE` / `ON CONFLICT(callsign) DO UPDATE` so an origin-only or
  destination-only payload from adsbdb refreshes only the side it
  knows about. Previously a partial response after a 30-day cache
  expiry could silently NULL out the other column on every flight
  sharing the callsign.
- **Photo cache: transient source outages no longer evict known-good
  rows.** `photo_sources.fetch_photo` now reports `hit`/`miss`/`error`;
  `resolve_photo` keeps stale positive `photos` / `type_photos` rows
  when the whole source chain errored (DNS hiccup, rate-limit) instead
  of writing a NULL sentinel for `PHOTO_CACHE_DAYS`. A confirmed
  upstream miss still writes the negative cache row exactly as
  before. Mirrors the per-spot grace already in `web._fetch_photo`.
- **Collector: a ghost-position sample can no longer queue an
  emergency alert.** `_poll` was queuing Telegram alerts and mutating
  `_notified_icao` / `_squawk_notified` *before* the ghost-position
  filter ran. A bad ADS-B jump carrying squawk `7700` produced an
  emergency alert for a position the collector then discarded, and
  locked the flight out of any future legitimate squawk alert. Filters
  now run before the notification block.
- **`/api/dates` groups by receiver-local time.** Previously bucketed
  with `date(first_seen, 'unixepoch')` (UTC), so a Warsaw `00:30`
  flight showed under the previous date — disagreeing with the
  receiver-local date filter. Now matches.
- **Date-filter Query descriptions say "receiver local time".**
  `/api/flights`, `/api/flights/export.csv`, and `docs/api.md`
  previously claimed `date=YYYY-MM-DD (UTC)`. The runtime always used
  host-local midnight (pinned by `test_date_uses_host_local_timezone`);
  only the docs were wrong.

Internal:

- **`http_safe`: removed the process-wide `_RESOLVER_LOCK`.**
  `safe_httpx_get` now connects to the pre-validated IP directly using
  the httpx `extensions={"sni_hostname": ...}` request extension and a
  `Host:` header override — mirroring the urllib `_PinnedHTTPSConnection`
  design. The previous `socket.getaddrinfo` monkey-patch was held under
  a module-level lock spanning the full streaming-body window, so two
  concurrent httpx calls (even to different hosts) serialized
  end-to-end. Production logs after deploy confirm pinned-IP URLs
  (`https://91.99.163.199/v0/callsign/...` to adsbdb,
  `https://172.67.71.61/v2/point/...` to airplanes.live) negotiate TLS
  + cert validation correctly via SNI override and return 200.
- **`web._cache`: bounded `OrderedDict` (cap 256).** Filtered
  `/api/stats?from=…&to=…` keys are caller-controlled and produced
  unbounded distinct cache entries (each TTL-expired but never
  evicted). New cache evicts expired entries opportunistically on
  `_set_cache`, then falls back to insertion-order eviction at the
  cap.
- **`config._parse_feeders`: validate item + field types.** A non-dict
  `RSBS_FEEDERS` array item (e.g. `[null]`) used to crash config
  import with a TypeError outside the handled exception set. `port`
  values are now required to be an int in `1..65535`, name/unit must
  be non-empty strings, and `status_*` fields must be strings.
- **`scripts/update.sh`: DuckDB pre-cache hardened.** The home
  directory is now passed through `RSBS_DUCKDB_HOME_DIR` env var and
  validated + quoted inside Python using the same
  `analytics._is_safe_sql_path` / `_quote_sql_string` helpers as the
  runtime path — instead of being shell-interpolated directly into a
  `python -c` string and a SQL literal.

Test count: 1383 → 1409. Net +26 regression tests added across
route_enricher, http_safe, photo_sources, collector, web cache,
`/api/dates`, and config parsing. Full suite: 1409 passed, 2 skipped.

## 2.9.1 — 2026-05-24

### Flight detail polish + photo lightbox

Three small follow-ups to v2.9.0 from screenshot review.

User-visible changes:

- **Route map zoom controls** (`/stats/flight/:id`). The `RouteMap`
  component now includes a MapLibre `<NavigationControl>` in the
  top-right corner: + / − zoom buttons. Compass hidden — bearing
  rotation isn't relevant for a 2D flight track.
- **Route start + end markers**. Small coloured circles on the
  flight line: green at the first plotted position ("start"), red at
  the last ("end"). Same circular shape as the receiver dot; colour
  alone differentiates role (matches the line's source-colour
  convention — green = ADS-B). Hover/tap shows the role via
  `aria-label` + `title`.
- **Photo lightbox** on both `/stats/flight/:id` AND `/stats/aircraft/:icao`.
  Aircraft photos in both detail pages are now click-to-enlarge: a
  centred Radix Dialog opens with the image at its natural pixel size
  capped by the viewport (`w-fit`, `max-w-[min(960px, calc(100vw -
  2rem))]`, max-h `calc(100vh - 6rem)`) — small thumbnails shrink-wrap
  (no blurry upscale) and full-resolution images preserve their aspect
  ratio. Footer carries `© photographer` and a `view on source →` link
  to the original listing when `link_url` is present. Esc /
  outside-click / close button dismiss. Degrades gracefully — when no
  large URL is available (or the URL fails `safeUrl`'s HTTPS-only
  check), the thumbnail renders without a click action.
- **Higher-quality enlarged photos from airport-data.com**. The
  airport-data source previously returned only a ~150 px thumbnail
  (~2 KB), which the lightbox displayed at low resolution. The photo
  fetcher now derives the full-resolution URL from the dedicated CDN
  host (`image.airport-data.com/aircraft/<basename>`) — ~40× the byte
  size, sharp at viewport scale. Cached `photos` rows with the
  previous URL refresh naturally at the 30-day cache TTL.
- **Route map zoom + start/end markers** circles. After initial visual
  review, the start/end markers were switched from square to circular
  to match the receiver dot's shape — colour alone differentiates role.

Internal:

- **`PhotoLightbox`** new shared component
  (`frontend/src/components/PhotoLightbox.tsx`) wraps the caller's
  thumbnail `<button>` in a Radix Dialog trigger. Single API; both
  Flight and Aircraft pages consume it.
- 6 new frontend tests covering: trigger renders, click opens dialog
  with image + photographer + source link, degrades to plain children
  when URLs are missing, rejects non-HTTPS via `safeUrl`, falls back
  to thumbnail when large is absent, omits source link when missing.
  Total suite: **230 passed**.

## 2.9.0 — 2026-05-24

### Flight detail compact header + chart tone-down + position-log polish

Closes Milestone 3 of `internal_docs/uiux/CLAUDE_DESIGN_BRIEF.md`. Three
sub-items on `/stats/flight/:id`. No backend changes —
`/api/flights/:id` already returns every field this redesign consumes.

User-visible changes:

- **Compact horizontal header** (M3.1). The previous ~300 px-tall header
  (220 px photo on the left + 8 stacked label/value rows on the right)
  is replaced by a ~170 px compact bar: 140×100 photo on the left,
  identity row (`{reg} · {callsign}` + hex chip + squawk + flag + source
  badge) + dim subtitle (`aircraft type · description · operator ·
  route`) + a 4-column metric grid (Max alt / Max speed / Max distance
  / Window). Each metric has a derived sublabel — vert rate at max alt,
  track at max speed, bearing-from-receiver at max distance, duration
  for the window. The map's top edge now sits within the first
  viewport-height on laptop.
- **Altitude chart tone-down + click-to-isolate** (M3.2). The orange
  area under altitude is now a soft top→transparent gradient (≈30%
  alpha at the top) instead of a solid 40% flood. A pill row above the
  chart lets you isolate Altitude or Speed — click a pill to fade the
  other series to 20% opacity; click again to restore. Reuses the
  v2.8.0 isolation pattern.
- **Position log RSSI polish** (M3.3). RSSI cells gained a per-row
  horizontal mini bar: width encodes the value's position within this
  flight's [min, max] range; color is green if above the flight's
  median, amber if at-or-below. The text becomes dim (the bar is the
  primary signal — the previous absolute-threshold color is gone). A
  60×16 px sparkline appears in the column header showing the whole-
  flight RSSI trend. Other columns (time, lat, lon, alt, speed) also
  go dim so RSSI is the visual anchor.
- **Per-position source stripe**. Every row's leftmost cell carries a
  3 px coloured stripe keyed to `source_type` (green=ADS-B, amber=MLAT,
  faint default for other). Same v2.8.0 pattern as the History flight
  rows. Useful for spotting MLAT/ADS-B handoff patterns within a flight.
- **iPhone inline disclosure**. On `<sm` the position log hides
  lat/lon/source columns; tapping a row expands it inline to show the
  hidden fields plus track and source as a dim two-column detail row.
  Native interaction; no modal.

Internal:

- **`IsolationPills`** extracted from v2.8.0 `Metrics.tsx`'s inline
  pill JSX into `frontend/src/components/charts/IsolationPills.tsx` so
  Metrics and Flight share the same primitive. `testIdPrefix` prop
  preserves the existing `metrics-aircraft-pill-{k}` testids exactly;
  Flight uses `flight-profile-pill-{alt|gs}`.
- **`buildFlightProfileOption` gains optional `isolated?: string |
  null` arg** (mirrors the v2.8.0 `buildPanelOption` change). Series
  carry STABLE `name: 'alt'` / `'gs'` so the isolation lookup doesn't
  break when the user toggles units (`altLabel()` / `spdLabel()`
  change between sessions). The built-in ECharts `legend` was removed
  from the option — the HTML pill row replaces it.
- **`SourceBadge size="sm"`** (v2.8.0) used in two new places: the
  Flight identity chip row and the position log Source column at md+.
- **New `lib/geo.ts`** with `haversineNm` + `bearingFromReceiver` —
  parallel to backend `src/readsbstats/geo.py`. Used by the Flight
  header to compute the at-max-distance position's bearing client-side
  (the backend doesn't pre-compute it). Float-equality on `max_gs`
  and `max_distance_nm` is deliberately avoided — the at-max lookups
  scan the positions array via `Math.max` and per-position haversine.
- **`MetricCell`** + **`RssiCell`** new in `frontend/src/components/flight/`
  — lightweight tiles, no Card chrome.
- 24 new frontend tests across `geo.test.ts`, `flight-header.test.tsx`,
  `position-table-rssi.test.tsx`, plus extensions to
  `echarts-option-builders.test.ts`. Total suite: **224 passed**.

## 2.8.0 — 2026-05-24

### Metrics small-multiples + History stripe + Gallery type-photo stamp

Four small, additive items from `internal_docs/uiux/CLAUDE_DESIGN_BRIEF.md`
(Milestones 2.1, 2.2, 8.2, 8.4). No backend changes — `/api/metrics`,
`/api/flights`, and `/api/aircraft/flagged` already return the fields
this UI consumes.

User-visible changes:

- **Signal quality — 4 stacked sub-panels** (M2.1). The Metrics page's
  "Signal quality" panel was a 3-series overlay with no legend; replaced
  with four stacked sub-panels (Peak / Mean / Noise / Strong signals),
  each ~50 px tall, sharing a single x-axis at the bottom. Each sub-panel
  carries its label on the left and the most recent value on the right.
  Hover anywhere → vertical crosshair across all 4 in lockstep. Added
  `strong_signals` as the new 4th series (already collected, never
  previously rendered).
- **Aircraft-count isolation pills** (M2.2). The 4-series Aircraft panel
  gained a pill row above the chart (color-keyed to each line):
  `With pos · No pos · ADS-B · MLAT`. Click a pill to isolate that
  series — others fade to 20 % opacity rather than disappear. Click
  again to restore. Faithful to the brief; uses an HTML pill row + opacity
  mutation (not ECharts' built-in legend, which hides instead of fading).
- **History rows — 3 px source stripe** (M8.4). Every row in the History
  table now carries a coloured left-border stripe keyed to
  `primary_source`: green for ADS-B, amber for MLAT, blue for mixed,
  faint border-default for other. The right-side Source pill shrinks to
  10 px and stays at desktop; on mobile the column is still hidden but
  the stripe is the visible signal. Source taxonomy unchanged.
- **Gallery — type-photo corner stamp** (M8.2). The "type photo"
  caption that used to sit below the metadata (reading as data) is now
  a small `[ type ]` stamp in the top-right corner of the photo itself.
  4 px from edges, 10 px text (the brief specified 9 px; bumped to 10 px
  to stay sharp under retina anti-aliasing), 1 px border, semi-transparent
  surface background. Cards with a specific aircraft photo carry no stamp.

Internal:

- **ECharts multi-grid pattern**: `buildSignalSmallMultiplesOption` in
  `frontend/src/pages/Metrics.tsx` builds a 4-grid option in a single
  chart instance (one canvas, one ResizeObserver), with `axisPointer.link:
  [{ xAxisIndex: 'all' }]` for the synchronized crosshair. Verified via
  context7 that `'all'` is valid syntax and must live at the root option
  level, not per-axis.
- **`buildPanelOption` gains `isolated?: string | null` arg**. When set,
  non-matching series get `lineStyle.opacity: 0.2` + `areaStyle.opacity:
  0.06`. Backward compatible (default `null` ⇒ unchanged).
- **`SourceBadge` gains `size?: 'sm' | 'md'`**. Default `'md'` keeps every
  other call site (`FlightDetail`, `Aircraft`) unchanged. `'sm'` uses
  10 px text + tighter padding for the dense History rows.
- **History stripe lives on the first `<td>`, not on `<tr>`**, because the
  shared `<Table>` primitive uses `border-collapse` which suppresses
  row-level borders. Test guards this directly so a future refactor
  can't silently break the stripe.
- **`Panel.labels?: string[]`** added to the Metrics PANELS schema for
  the two panels that render labels visibly (signal sub-titles,
  aircraft pills).
- 16 new frontend tests: 6 for the History stripe contract, 3 for the
  Gallery type-photo stamp, 7 for the new ECharts builders +
  isolation arg. Total suite: **200 passed**.

## 2.7.0 — 2026-05-24

### Metrics health stripe + paper-cuts

Picks up four small, additive items from
`internal_docs/uiux/CLAUDE_DESIGN_BRIEF.md` (Milestones 2.3, 2.4, 9, 10.3).
No backend changes — `/api/metrics/health` and its `check.message` strings
were already in the shape this UI consumes.

User-visible changes:

- **Receiver health stripe** on `/metrics` (M2.3). The old collapsible
  banner is replaced by a horizontal strip of squares — one per health
  check, colored green / amber / red — with a summary line:
  `N checks · n OK · n warn · n down [▾]`. Each square is hover-
  tooltipped with the check name + current message, and clicking it
  expands the detail panel + focuses the corresponding row.
- **First-failing check summaries inline** (M2.4). When any check is
  warn / critical, up to two single-line summaries appear immediately
  below the stripe (e.g. `⚠ message_rate 1008/min vs 641/min baseline
  (157%)`). The body comes verbatim from `check.message`.
- **`Health unavailable` notice on error**. The previous banner
  silently disappeared when `/api/metrics/health` errored. The new
  stripe shows a small "Receiver health checks unavailable — retry on
  next poll" alert so the failure is visible.
- **Units selector tooltip + per-option subtitles** (M9). Hovering or
  focusing the `Aeronautical ▾` selector in the nav now reveals a
  three-row tooltip — system name on the left, units on the right
  (`Aeronautical · nm · ft · kts`, `Metric · km · m · km/h`,
  `Imperial · mi · ft · mph`). Touch users get the same units as
  inline subtitles inside each dropdown option, since Radix tooltips
  don't fire on tap.
- **Nav clears the iPhone notch** (M10.3). The sticky top bar now
  respects `env(safe-area-inset-top)`; on desktop the offset resolves
  to `0` so layout is unchanged. The `--rsbs-nav-h` CSS variable used
  by the v2.6.0 Stats sticky range bar grows in lockstep, so docked
  page chrome stays glued to the nav's actual bottom edge on notched
  devices.

Internal:

- New `frontend/src/components/metrics/HealthStripe.tsx` component
  (mirrors the `components/stats/` layout convention from v2.6.0).
  `statusColor` / `StatusIcon` helpers moved out of `Metrics.tsx` into
  the new component.
- `frontend/src/components/ui/Select.tsx::SelectItem` gains an optional
  `subtitle?: ReactNode` prop, rendered as an `ml-auto` sibling to
  `<Select.ItemText>`. Backward compatible — no other call site changes.
- Radix Tooltip + Select composition uses controlled state in the nav
  (`tipOpen && !selectOpen`) so the tooltip auto-dismisses when the
  dropdown opens. Verified via context7 that this isn't covered by
  Radix's documentation; controlled coordination was the safest pattern.
- 6 new frontend tests across `metrics-health.test.tsx` and `nav.test.tsx`
  (stripe squares + summary counts + first-failing logic + empty/error
  states; tooltip content assertion). Total frontend suite: **183 passed**.

## 2.6.0 — 2026-05-24

### Statistics page redesign — time-window narrative

Reworks the Statistics page (`/stats/`) around the **selected time window** instead
of treating every metric with equal visual weight. Implements Milestone 6 ("Option
C — time-window narrative") from `internal_docs/uiux/CLAUDE_DESIGN_BRIEF.md` and
lands the M1 paper-cut fixes that were still outstanding. Lighter, more scannable,
and consistent with the dark blue chrome of the rest of the SPA.

User-visible changes:

- **Sticky range bar with context sentence.** `24h | 7d | 30d | 90d | All | Custom`
  segmented control docks under the nav as you scroll, with a one-line caption
  underneath: *"Showing **last 24 hours** · YYYY-MM-DD HH:MM → … · compared with
  previous 24h"*. A small refreshing indicator appears next to it during
  background refetches.
- **4 large KPI cards** replace the previous 12-tile grid: Flights, Unique
  aircraft, Position fixes, Max range. Each card has a delta line (where a
  comparison exists), an inline sparkline (where the window has enough points),
  and aligns uniformly across the row regardless of which sublabels exist.
- **Inline flag/squawk badge strip** replaces the second row of flagged cards.
  Equal-width pills for Military · Interesting · Anonymous · 7700 · 7600 · 7500;
  each pill is a `<Link>` into the pre-filtered History view.
- **In-page section anchors** (xl only): `Overview · Activity · Rankings ·
  Coverage` chips with scroll-spy active state.
- **xl small-multiples** for the rankings panel: all six Top-N charts visible at
  once at laptop width (aircraft types / airlines / countries / visitors / routes
  / airports). Single-card switcher kept for narrower screens.
- **Collapsible "About this receiver" footer** for lifetime totals (Total
  flights, Unique airlines, Total positions, DB size, Oldest flight, Source
  breakdown). Always shows receiver-wide values — does **not** change when the
  range picker moves.
- **Daily-unique chart reads chronologically** in every window (was reversed in
  range=all). Today's bar is always the rightmost.
- **Heatmap discrimination** improved with a 5-stop blue alpha ramp. Hot cells
  are unambiguously distinct from cold cells; legend matches the actual stops.
- **Top-N x-axis labels abbreviate to k/M** (e.g. `12k`, `1.5M`) and avoid
  collisions on the dense small-multiples cells.
- **SWR refresh pattern**: previous data stays visible while the next range
  loads, so changing presets no longer flashes the skeleton.
- **Personal records** and **polar range** kept and lightly polished.

Map fixes:

- **No more duplicate-aircraft markers during Rewind / HIST scrubbing.** Two
  contributing causes addressed: (a) the snapshot query's `placeholderData`
  fallback now only applies in Live mode, so scrubbing doesn't keep the
  previous timestamp's markers on screen; (b) `LiveMap` now de-dups the
  aircraft array by `icao_hex` defensively, so the rare case of two `flight_id`
  rows for one aircraft inside the 600-second snapshot window only renders as
  one marker.

Backend:

- **New `lifetime: {...}` block** in `/api/stats` carrying the receiver-wide
  totals (total_flights, unique_aircraft, total_positions, unique_airlines,
  oldest_flight, db_size_bytes, source_breakdown). Always populated, independent
  of `from`/`to`. Consumed by the "About this receiver" footer.
- **Daily-unique chart SQL** (`web.py:1489`): `ORDER BY day DESC LIMIT 30` →
  `ORDER BY day ASC LIMIT 31` for the unfiltered path. The `+1` keeps today's
  bar in view (the 30-day window spans 31 distinct UTC date strings).

Internal:

- New `frontend/src/components/stats/` directory with seven small primitives:
  `KpiCard`, `KpiSparkline`, `FlagBadgeStrip`, `RangeContextLine`,
  `AboutReceiverFooter`, `SectionAnchors`, `TopChartMultiples`.
- `TopChart.tsx` refactored: option builder + view definitions extracted to a
  shared `charts/topRows.ts` consumed by both the single-card and
  small-multiples variants.
- `RangePicker.tsx` gains optional `sticky` and `right` slot props (default
  off so Metrics / History / Map call sites are unchanged).
- `--rsbs-nav-h` CSS variable published from `index.css` so sticky chrome can
  dock under the nav without hardcoding heights at the component level.
- `HEATMAP_RAMP` constant added to `charts/theme.ts` per ADR-0008 (chart colors
  live in the chart theme, not Tailwind tokens).
- `IntersectionObserver` shim added to `frontend/test/setup.ts` (jsdom doesn't
  provide one).
- 31 new tests across 6 new frontend files (KPI card, sparkline, flag strip,
  range context, stats page layout, abbreviateAxis) plus backend tests for the
  daily-chart ASC ordering, the lifetime block constancy, NULL coercion on
  empty DB, and snapshot dedup.

## 2.5.2 — 2026-05-24

### Audit-13 backlog cleanup — Low-severity sweep

Closes the last items from the audit-13 review queue: the `noImplicitAny`
half of A13-043 plus 8 of the 43 Low-severity findings that were left
as opportunistic future work. Production verified — all 5 security
headers including the new `Content-Security-Policy: default-src 'none'`
land on `/stats/api/*` responses post-deploy.

User-visible changes:

- **Telegram alerts honour mixed-case `RSBS_TELEGRAM_UNITS`** (A13-035).
  Setting `RSBS_TELEGRAM_UNITS=Imperial` (or `IMPERIAL`, or any
  non-lowercase variant) previously fell back to metric silently. Now
  normalised at every comparison site in `notifier.py`.
- **`is_anonymous_icao()` no longer flags ICAO-reserved sentinels**
  (A13-024). `0x000000` (null / no-information) and `0xFFFFFF`
  (all-call / broadcast) are protocol artifacts, not real aircraft —
  treating them as anonymous aircraft polluted the Telegram channel
  and the FLAG_ANONYMOUS retroactive scoring. Both the Python helper
  and the SQL CASE expression now guard them out.

Internal:

- **TypeScript `noImplicitAny: true`** (A13-043 follow-up). The
  long-deferred second half of the strict-mode adoption — turned out
  the codebase was already clean from incremental annotations across
  v2.3–v2.5, so the flip was a one-line config change with zero tsc
  errors.
- **systemd-analyze security verified on Pi** (A13-046 verification).
  All 6 service units (`readsbstats-collector`, `readsbstats-web`,
  `notify-telegram@`, `readsbstats-updater`, `readsbstats-dbcheck`,
  `readsbstats-dbcheck-full`) score **2.9 OK** — well below the
  audit's <5 target. Remaining ✗ rows are intrinsic to the workload
  (Internet sockets, RTC) or trivial future hardening (UMask,
  SystemCallFilter block).
- **Metrics parse guard** (A13-026). `int(last1min.end)` could raise
  `ValueError`/`TypeError` on garbage upstream and abort the whole
  metrics row; now wraps the conversion, logs a warning, and returns
  `(None, None)` so the next poll picks up cleanly.
- **Dispatch unknown-kind observability** (A13-027). `_dispatch_one`
  silently dropped notifications with an unknown `kind`; now logs a
  warning so the loss is visible in journalctl.
- **Dead-column drop on existing DBs** (already in v2.5.1, mentioned
  here for completeness — `watchlist_alerted`).
- **Hardening / hygiene**:
  - `http_safe._USER_AGENT` wrapped in `MappingProxyType` so the
    `photo_sources.PHOTO_UA` re-export can't be `.pop()`'d by a
    downstream caller (A13-053).
  - `requirements-dev.txt` `httpx>=0.27.2` removed (was already pinned
    via `-r requirements.txt`; redundant floor created a path for dev
    to resolve older than prod) (A13-051).
  - nginx `/stats/api/` block now re-states all 5 parent security
    headers including `Content-Security-Policy: default-src 'none'`
    (strictest possible — JSON endpoints load no scripts/images/frames).
    Immunises the `add_header` inheritance trap permanently (A13-052).
- **Dead code removed**: `collector._dispatch_notifications` (no
  callers anywhere, queue-backed consumer is the only production
  path) and `notifier._truncate_caption` back-compat alias
  (A13-091 + A13-092).
- **Post-commit review fix**: the original A13-052 hardening patch
  added only 4 of the 5 parent headers to the `/api/*` location and
  inadvertently triggered the very inheritance trap it was meant to
  prevent — for `Content-Security-Policy`. Caught by the project's
  reviewer agent before deploy; CSP added with a JSON-appropriate
  `default-src 'none'` strict policy.

Test count: 1374 → 1376. Net +3 regression tests added (5 new, -2
stale: one alias-enforcement test, one assertion of pre-A13-024
buggy behaviour).

## 2.5.1 — 2026-05-24

### Time format and schema cleanup

Two `toLocaleTimeString()`/`toLocaleString()` call sites that ignored the
project's 12h/24h preference (Settings → time_format) and silently fell
back to the OS locale — giving 12h on macOS even when the user picked
24h — now route through `useFormat()` / `lib/format.ts::fmtTs`.

User-visible changes:

- **LiveCountBadge tooltip.** "Active aircraft — updated HH:MM:SS" in
  the nav now matches the user's selected clock format instead of the
  OS locale.
- **Watchlist entry timestamps.** Per-row "added on" dates likewise
  honour the user setting.

Internal:

- **Dead `watchlist_alerted` column removed.** An earlier `_migrate()`
  added an `INTEGER DEFAULT 0` column on `flights` that no code ever
  read or wrote — watchlist dedup is handled by `is_new_flight` in
  `collector._poll()`. Removed from `_migrate()`'s `new_cols` dict and
  dropped from existing DBs via `_drop_dead_watchlist_alerted_column()`
  in `run_background_migrations()`. The drop lives in the background
  path because `ALTER TABLE DROP COLUMN` rewrites the entire `flights`
  table — too slow for `_migrate()`'s pre-`READY=1` window. Conditional
  on column presence and wrapped in `try/except`, so it's a clean
  no-op on fresh DBs. Three regression tests cover the matrix.
- **v2.5.0 audit closeout.** The full-codebase audit run after the
  v2.5.0 cut flagged this column as its only finding (Low); no
  Critical/High/Medium issues. All security non-negotiables (SSRF
  guard, CSRF, Telegram escaping, sort whitelist, open-redirect
  sanitisation), reliability rules (watchdog placement, slow ops
  outside `_migrate()`), and SQLite invariants (per-thread connections,
  WAL, correlated-subquery `registration` fix) verified clean.

## 2.5.0 — 2026-05-24

### Live map redesign — bottom command bar + HIST mode

The `/stats/map` page's three floating UI islands — top-left mode card,
top-right snapshot timestamp pill, bottom rewind scrubber — collapse into
a single bottom command bar that overlays the map. Frees the top corners
of the map entirely and gives the controls a single coherent home.

User-visible changes:

- **New HIST mode.** A third entry in the Live / Rewind / HIST segmented
  control. Pick any date + time within `map_history_hours` and jump
  directly to that moment, instead of only scrubbing backward from "now".
  The date picker disables out-of-range days (using `react-day-picker`'s
  `disabled={[{before}, {after}]}` matchers) so users can't accidentally
  request a moment older than the DB keeps. Playback in HIST advances
  `histAt` forward at `speed × tick` and auto-stops on reaching the live
  edge.
- **Bottom command bar.** Two rows: controls on top (mode, range pills,
  layer toggles, snapshot timestamp + aircraft count), playback on
  bottom (seek, scrubber, play/pause, speed). Row 2 collapses in Live
  mode. 95% paper alpha + backdrop blur.
- **Phone-aware condensed bar.** Below the `sm` breakpoint, the bar
  shrinks to mode-toggle + chevron; tapping the chevron expands the
  rest. Auto-expands when the user switches to Rewind/HIST so the
  scrubber is reachable in one tap. Scrubber thumb bumps from 16px to
  24px on small screens for reliable finger-scrubbing.
- **Layers Popover at narrow widths.** Below `lg`, the Heatmap /
  Coverage / List toggles fold into a single icon Popover with an
  active-count badge — keeps Row 1 on a single line at iPad portrait
  widths.
- **MapLibre native controls lifted above the bar.** The zoom +/− stack
  (bottom-right) and attribution (bottom-left) now sit above the bar
  rather than under it. NavigationControl is hidden on phones — pinch
  covers it.

Internal:

- New `frontend/src/components/map/` directory: `MapCommandBar`,
  `MapModeControl`, `MapLayersControl`, `MapHistDatePicker`,
  `MapRewindControls`.
- `components/ui/DatePicker.tsx` and `TimePicker.tsx` grew optional
  `disabledMatcher`, `defaultOpen`, and `popoverSide` props — all
  backward-compatible; existing History / RangePicker call sites are
  unchanged.
- The bar measures its own height via `ResizeObserver`
  (`getBoundingClientRect().height`, **not** `contentRect.height` — the
  latter excludes safe-area padding) and writes `--map-bar-height` on
  the `.map-with-bar` container. The MapLibre control wrappers read
  that variable. Hoisted out of `@layer components` because cascade-layer
  ordering otherwise lets maplibre-gl's unlayered `bottom: 0` rule win
  regardless of specificity.

## 2.4.1 — 2026-05-23

### Wire React Compiler

`babel-plugin-react-compiler` was already installed but not active — `@vitejs/plugin-react@6`
dropped the inline `babel.plugins` escape hatch in favour of `reactCompilerPreset` wired
through `@rolldown/plugin-babel`. Both `vite.config.ts` and `vitest.config.ts` now use
this path so production builds and test runs apply automatic memoisation.

No user-visible behaviour changes. Performance improvement expected on chart-heavy pages
(`/stats`, `/metrics`, `/flight`) where ECharts option objects were previously recomputed
on every render.

## 2.4.0 — 2026-05-23

### SPA map stack: react-leaflet → MapLibre GL

Frontend map library swap across the live `/stats/map` view and the
per-flight route map. The Leaflet stack (`react-leaflet@5` + `leaflet@1.9`
+ `leaflet.heat@0.2`) is fully replaced by `maplibre-gl@5.24` +
`react-map-gl@8.1` (`react-map-gl/maplibre` endpoint). See
`docs/decisions/0009-maplibre-gl-frontend-map.md` for the full rationale.

User-visible changes:

- **Heatmap is finally legible.** Native MapLibre `heatmap` layer with a
  6-stop inferno-derived ramp (perceptually uniform, monotonically
  increasing luminance, colorblind-safe). Replaces the royal-blue
  `leaflet.heat` overlay that previously flooded the basemap at any
  meaningful density.
- **Receiver marker pulses.** A static ring + animated pulse + center
  dot driven by `requestAnimationFrame` and `setPaintProperty` on the
  MapLibre `circle` layer. 1.8s period, 12→36px radius, 0.6→0 stroke
  opacity.
- **Dark basemap.** Tiles served from CartoDB Dark Matter — a native
  dark raster basemap (CC-BY 4.0, no API key). The previous
  `.map-tiles-dark` CSS filter chain (`brightness(0.7) saturate(0.85)
  invert(0.92) hue-rotate(180deg)`) is dropped; labels stay crisp at
  all zoom levels.
- **Smoother pan/zoom**, particularly on iPad Safari when both heatmap
  and aircraft markers are active. MapLibre's WebGL renderer is
  GPU-accelerated where Leaflet's CPU/SVG renderer was the bottleneck.

Internal:

- **`aircraftIcon.ts` → `aircraftIcon.tsx`.** API surface changed from
  `aircraftIcon(track, flags, type): L.DivIcon` to
  `aircraftIconSvg(flags, type): React.ReactElement`. Rotation moved
  from a CSS `transform:rotate(${deg}deg)` string interpolation to the
  typed `Marker.rotation` prop with `rotationAlignment="map"`. The
  string-template surface flagged by audit-12 #176 is eliminated by
  construction (the API can no longer route a string through this
  path).
- **`(L as any).heatLayer` cast gone.** Closes audit-13 A13-089. The
  heatmap is now a declarative `<Source><Layer/></Source>` pair with
  typed paint properties throughout.
- **CSP updated for MapLibre.** `worker-src 'self' blob:` added (tile
  decoder Web Workers bootstrap from blob URLs), `connect-src` and
  `img-src` extended for `*.basemaps.cartocdn.com` (MapLibre fetches
  raster tiles via `fetch()`, not `<img>`). The previously-overlooked
  inline theme bootstrap script in `frontend/index.html` (left
  unenforced after audit-13 dropped `'unsafe-inline'`) is now allowed
  via a SHA-256 hash in `script-src`.

Bundle delta (gzipped, lazy-loaded by `/stats/map` and
`/stats/flight/:id` only):

- `maps` chunk: previously **45 KB gz** (Leaflet stack) → **283 KB gz**
  (MapLibre stack)
- Net +238 KB gz on the two affected pages on first visit. Shell,
  vendor, radix, and charts chunks are untouched.
- `chunkSizeWarningLimit` in `vite.config.ts` raised from 600 → 1500
  (KB raw) — both `maps` and `charts` are intentionally large lazy
  chunks; the warning's signal is gone.

Migration shipped as three commits on `main`: PR #1 ported `RouteMap.tsx`
+ added deps + nginx CSP, PR #2 ported `LiveMap.tsx` + native heatmap +
receiver pulse + dropped `leaflet.heat`, PR #3 dropped the remaining
`leaflet` / `react-leaflet` / `@types/leaflet*` deps, wrote ADR-0009,
and updated `THIRD_PARTY_NOTICES.md`.

Test count unchanged at **143 Vitest** (rotation-coercion tests in
`aircraftIcon.test.ts` replaced 1:1 with fill/viewbox tests on the new
JSX surface).

Deploy notes:

- nginx must reload after pulling the updated `nginx-readsbstats.conf`
  to pick up the new CSP directives.
- No backend changes; all map data comes from existing API endpoints
  unchanged.

---

## 2.3.5 — 2026-05-22

### Refactor (no behaviour change)

- **`config.FEEDER_STATUS_ROOT` is now env-overridable** (`RSBS_FEEDER_STATUS_ROOT`,
  default `/run`). Previously hardcoded as `_FEEDER_STATUS_PATH_ROOT` in
  `web.py`. Lets tests pin the root via `monkeypatch` without depending on a
  writable `/run`. Operators should leave the default; documented under
  `RSBS_FEEDERS` in `docs/configuration.md`.
- **Single source of truth for shared-table DDL.** The six tables that were
  declared twice in `database.py` (top-of-file `DDL` and again inside
  `_migrate()`) — `watchlist`, `adsbx_overrides`, `type_photos`, `airports`,
  `callsign_routes`, `receiver_stats` — are now each defined once as a
  module-level `_DDL_*` constant referenced from both sites.
- **`_settings_payload()` decomposed by domain** into seven small helpers
  (`_settings_receiver`, `_settings_collector`, `_settings_database`,
  `_settings_enrichment`, `_settings_metrics`, `_settings_health`,
  `_settings_ui`, `_settings_telegram`). The flat 50-key payload shape is
  unchanged.

---

## 2.3.4 — 2026-05-22

### Security defence-in-depth

- **`icao_hex` HTML-escaped in every `notify_*` Telegram URL.** All five
  alert helpers in `notifier.py` (`notify_military`, `notify_interesting`,
  `notify_anonymous`, `notify_watchlist`, `notify_squawk`) now wrap
  `{icao}` with `_h(icao)` before interpolating into `<a href="…">`. The
  collector still guarantees 6-char lowercase hex; this is purely defensive.
- **Telegram bot token removed from `curl` argv.** `notify-telegram-failure.sh`
  now writes the URL line (containing the token) to a 0600 tmpfile, feeds
  it to `curl --config`, and removes it on `trap EXIT`. The token no longer
  appears in `/proc/<pid>/cmdline`.
- **`RSBS_AIRSPACE_GEOJSON` path verified to be a regular file.** `api_airspace`
  resolves the configured path with `Path.resolve(strict=True)` and rejects
  anything that isn't a regular file — blocks device files (`/dev/random`),
  symlinks-to-dirs, and missing paths. Path is not pinned to `static/airspace/`
  so operators can keep airspace data on external storage; the existing 10 MB
  size cap stays.

### Performance / correctness

- **Purge scripts no longer issue per-flight SELECTs.** `purge_ghosts`,
  `purge_bad_gs`, and `purge_mlat_gs_spikes` now stream one ordered
  `positions` query through `itertools.groupby` instead of fanning out to a
  `SELECT … WHERE flight_id = ?` per flight. On a 35 k-flight DB that
  eliminates ~35 k round trips per scan; `purge_bad_gs` also bulk-loads the
  `(flight_id → icao_hex)` mapping.
- **`_baseline_avg` is now sargable.** Builds an OR-of-narrow-BETWEEN clause
  from per-week target windows computed in Python, replacing the `strftime`
  filter that forced a full-range scan. DST is handled correctly via
  `datetime.fromtimestamp` + `timedelta(weeks=N)` round-trip; same DOW+hour
  is guaranteed by construction so the `strftime` predicate is no longer
  needed.
- **Heatmap rounding now agrees across DuckDB and SQLite.** Both engines
  GROUP BY an integer bucket (`CAST(FLOOR(lat * 10^p + 0.5) AS INTEGER)`)
  and divide in Python on the way out, removing both the per-engine
  `round()` divergence (SQLite is half-away-from-zero, DuckDB is banker's)
  and a residual per-engine float drift on the divide step. The 24h fine-
  grid heatmap parity test now passes even on exact-half decimal
  coordinates (`lat=52.05, lon=21.05, precision=1`).

### Regression guards (no behaviour change)

- Explicit assertion in `test_purge_ghosts.py` that
  `max_distance_after_purge` takes the no-`IN ()` branch when `ghost_ids=[]`
  (originally fixed by audit-12 #143; now pinned with a SQL-shape test).

---

## 2.3.3 — 2026-05-22

### Bug fixes

- **Map rewind slider now respects `RSBS_MAP_HISTORY_HOURS`** — the rewind
  cap was hardcoded to 24 h in the frontend regardless of the backend config.
  The frontend now reads `map_history_hours` from `/api/settings` (also newly
  added to the settings payload) and uses it as the slider bound.
- **History date filters now use browser-local midnight** — `/api/flights`
  was receiving `date_from`/`date_to` as UTC date strings, causing off-by-
  a-timezone-offset errors for users outside UTC. The History page now sends
  `from`/`to` as Unix timestamps anchored to the user's local midnight.
  The backend `/api/flights` endpoint accepts both forms; the old
  `date_from`/`date_to` string params remain for backward compatibility.
- **Settings page env-var hints corrected** — six labels on the Settings page
  showed env-var names that didn't exist (`RSBS_MAX_RANGE_NM`,
  `RSBS_MIN_POSITIONS_KEEP`, `RSBS_ROUTE_BATCH_SIZE`,
  `RSBS_ADSBX_POLL_INTERVAL`, `RSBS_ADSBX_RANGE_NM`, `RSBS_ADSBX_API_URL`).
  Corrected to match the actual names in `config.py`.

### Developer tooling

- **`npm run lint` now works** — `@eslint/js` was imported by `eslint.config.mjs`
  but absent from `devDependencies`, causing ESLint to exit before linting any
  source. Package added at `^10.0.1`.
- **ECharts chunk size warning silenced** — `chunkSizeWarningLimit` raised from
  250 KB to 600 KB to reflect the intentionally isolated ECharts chunk (~193 KB
  gzip). No change to the bundle split strategy.

---

## 2.3.2 — 2026-05-22

### Reliability

- **DNS failures no longer permanently blacklist the ADSBx enricher** — a
  transient DNS outage in `_fetch_area()` previously wrapped the
  `socket.gaierror` in a plain `ValueError`, which `_fetch_area`'s exception
  handler mis-classified as a permanent policy error. The enricher would then
  back off forever for that process lifetime. Fixed by introducing
  `http_safe.UnsafeURLError` (a `ValueError` subclass) for genuine policy
  violations (non-HTTPS scheme, private destination IP, redirect, body size
  cap). DNS failures remain plain `ValueError` and are correctly routed to
  the transient-error retry path.
- **Background workers are now idempotent** — `route_enricher`,
  `adsbx_enricher`, and `metrics_collector` each held a module-level thread
  handle but lacked an `is_alive()` guard on their `start_*` functions.
  Repeated calls (test lifecycle, future hot-reload) silently spawned
  duplicate threads, leading to redundant API polling and duplicate SQLite
  writes. Each function now returns the existing thread if it is still alive.
- **Startup SQLite connection no longer leaks** — the `_lifespan` startup
  lambda called `db()` to obtain a connection for `_migrate()`, which stored
  it in the `asyncio.to_thread` worker's `threading.local`. That connection
  stayed open for the worker thread's lifetime, contradicting the
  per-request thread-local design. The startup path now opens and closes an
  explicit connection.
- **DuckDB `INSTALL sqlite_scanner` is best-effort** — the extension
  download step is now wrapped in a silent `try/except`; `LOAD` is the real
  gate. This prevents a network timeout at startup from permanently disabling
  the DuckDB analytics engine when the extension is already cached locally.

---

## 2.3.1 — 2026-05-20

### Reliability

- **DuckDB shutdown race eliminated** — `analytics.coverage()` and
  `analytics.heatmap()` no longer log a spurious WARNING + traceback
  when the DuckDB connection is closed mid-query during service shutdown.
  A `_SHUTDOWN` event is set by `close()` before the connection is torn
  down; in-flight queries that race past the initial availability check
  detect it and return `None` silently, letting the caller fall through
  to the SQLite path as intended.

---

## 2.3.0 — 2026-05-20

Coordinated post-audit-13 sweep. 53 items across security, reliability,
performance, and hardening — bundled under one minor bump rather than
sliced into a chain of patch releases. Full per-item index lives in
`internal_docs/security/audit-13-2026-05-20.md` (gitignored, local).

### Security

- **CSRF check tightened** — `_csrf_check` now requires the canonical
  `X-Requested-With: XMLHttpRequest` value (case-insensitive). The
  previous truthy-only check accepted any non-empty string and relied
  entirely on the absence of CORS middleware to stay sound; tightening
  removes a class of accidental-bypass mistakes if CORS is ever added.
- **DNS-rebinding resolver race** in `safe_httpx_get` closed via a
  module-level lock; concurrent httpx requests can no longer leak a
  stale scoped resolver into `socket.getaddrinfo` after teardown.
- **Streaming `safe_httpx_get`** — body is read incrementally with an
  early `max_bytes` cutoff via `client.stream()` + `iter_bytes()`, so
  an oversized upstream response is aborted before it lands in RAM.
- **`_top1()` allowlist** for the stats-records helper — `order_col`
  is now validated against a frozen set; the `MAX_GS_*` numerics in
  the `extra_where` clause are parameterised instead of f-stringed.
  `backfill_bearing` receiver lat/lon similarly bound, not interpolated.
- **systemctl / journalctl unit name guard** — unit names from
  `RSBS_FEEDERS` that start with `-` are rejected; both shell-out call
  sites pass `--` between the args and the unit name.
- **Airspace GeoJSON 10 MB cap** — operator-misconfigured 100 MB files
  no longer land in the per-process cache and starve the Pi.
- **Open-redirect defence on `/live`** — `redirect_live` runs the same
  `urlparse(target)` scheme/netloc check as `_v2_compat`, so a hostile
  reverse-proxy injected `root_path` cannot redirect off-host.
- **nginx CSP** — `'unsafe-inline'` dropped from `script-src` (Vite
  emits no inline scripts); `Cross-Origin-Opener-Policy: same-origin`
  added; HSTS template included (commented until HTTPS).
- **systemd hardening** — `ProtectKernelTunables` / `ProtectKernelModules`
  / `LockPersonality` / `RestrictNamespaces` / `RestrictRealtime` /
  `SystemCallArchitectures=native` / empty `CapabilityBoundingSet`
  applied uniformly across all six service units. `notify-telegram@`
  (which sources the Telegram token via `EnvironmentFile=`) gained the
  full hardening block too — previously had zero directives.
- **GitHub Actions pinned to full SHAs** — `actions/checkout`,
  `setup-python`, `setup-node` no longer track floating tags.
- **`.github/dependabot.yml`** — weekly grouped updates for
  github-actions, pip, and npm (`/frontend`).
- **TypeScript `strictNullChecks: true`** enabled in
  `frontend/tsconfig.app.json`. Build clean — no source edits required.

### Reliability

- **`route_enricher._apply_to_flights` no longer NULL-overwrites
  previously-resolved flights** when `adsbdb.com` later returns 404 for
  a callsign. Silent data loss in `flights.origin_icao` / `dest_icao`
  closed (test landed before the fix, per the TDD rule).
- **`_fetch_photo` 7-day grace window** — a transient upstream failure
  on a previously-positive cached row no longer blows that row away to
  NULL. Within `PHOTO_CACHE_DAYS + 7d` the working URL keeps serving.
- **NTP-backstep tolerance** — `_open_flight` initialises `last_pos_ts`
  to `pos_ts` (not `pos_ts - 1`) and `_poll()` uses strict `<` (not
  `<=`); a one-second clock step no longer drops the next position.
- **`compute_health` per-check isolation** — each receiver-health
  check runs in its own try/except so a single bad query degrades to
  `severity="info"` instead of 500ing the entire `/api/health` endpoint.
- **`_check_range_degradation` zero-divide guard** — combined the two
  range queries into one and added an explicit `long_max <= 0` check.
- **`_get_updates` Telegram response shape validation** — non-list
  `result` (schema drift, TLS mangling) returns `[]` with a single
  log line, instead of iterating characters or raising mid-batch.
- **`_TYPE_LOCKS_MAX` LRU now skips held locks** — a held asyncio.Lock
  is rotated to the end of the OrderedDict instead of evicted, so two
  concurrent fetches for the same ICAO type can't race past dedup.
- **`_v2_compat` redirect** moved outside the `if _SPA_AVAILABLE:`
  gate, so the URL bar rewrites cleanly mid-deploy.
- **`db_updater` enrichment-cache clear is per step** rather than
  end-of-run; closes a stale-cache window during the bulk reload.
- **`update_aircraft_db` chunked at 5000 rows per transaction**, so
  the writer lock releases between batches and concurrent collector
  writes don't hit the busy-timeout ceiling.
- **`_purge()` batched** when `RETENTION_DAYS > 0` — the correlated
  `COUNT(*)` UPDATE commits in 500-flight chunks.
- **`PermanentError` separated from transient retries in
  `adsbx_enricher`** — `ValueError` from policy violations (oversize,
  redirect, scheme) backs off 1 h instead of retrying every 60 s.
- **`_transient_failure_at` evicts expired cooldown entries** in
  `route_enricher` — the dict no longer grows unboundedly after long
  upstream outages.

### Performance

- **`_load_active()` collector startup** — replaced the full-positions
  `ROW_NUMBER() OVER (PARTITION BY ...)` scan with a per-flight
  correlated subquery against `idx_positions_flight_id_desc`
  (`ORDER BY id DESC LIMIT 1`). Sub-second startup on multi-million-row
  databases.
- **CSV export streams** — `/api/flights/export.csv` now uses
  `StreamingResponse` + `fetchmany(1000)` instead of buffering the
  entire CSV in memory.
- **`_upsert_overrides` uses `executemany`** — adsbx batches commit
  in one round trip instead of N.
- **`api_live` single query** — collapsed the prior fetch-IDs +
  IN-clause pattern into one correlated subquery.
- **`httpx.Client` hoisted to loop lifetime** in both `adsbx_enricher`
  and `route_enricher` — TLS session and connection pool persist
  across polls.
- **`_migrate` runs in an executor** during `_lifespan` so the event
  loop stays free while indexes/ALTERs land on cold disk.
- **MLAT outlier clamp default fixed** — `RSBS_MLAT_OUTLIER_FACTOR`
  out-of-range values now fall back to the documented `5.0` (was
  silently `20.0`, four times the documented default).
- **`_check_cpu_saturation` denominator decoupled** from
  `METRICS_INTERVAL` — readsb's `last1min` window is fixed at 60 s
  upstream, so setting `RSBS_METRICS_INTERVAL=30` no longer doubled
  the reported demod %.

### Refactor / cleanup

- **`geo.haversine_sql()` / `geo.bearing_sql()`** — single source of
  truth for the inline SQL geometry expressions. Three duplicated
  copies in `analytics.py` and `web.py` collapsed into helper calls.
- **`_FLAGGED_SORT_COLS`** — `api_aircraft_flagged` now uses a
  module-level allowlist sibling to `_SORT_COLS` instead of an
  inline ad-hoc map.
- **`frontend/src/lib/api.types.ts` deleted** — 46 KB of
  OpenAPI-generated types with zero consumers; hand-typed shapes in
  `lib/types.ts` remain the working source of truth.
- **`useSearchParam` setters now write `{ replace: true }`** — typing
  into a live filter input no longer floods browser history one
  entry per keystroke.

### Tests + docs

- **`docs/configuration.md` rewritten** — all 70 `RSBS_*` env vars
  documented in 13 sections matching `config.py` layout. README's
  "All 43" claim corrected to 70. 1:1 coverage verified.
- **`docs/integrations.md`** — Telegram setup now documents both
  env-var locations (collector/web systemctl-edit override AND
  `/etc/readsbstats/readsbstats.env` for the failure notifier).
- **`docs/operations.md`** chart count 11 → 10 (matches the SPA).
- **`README.md`** web-server CPU quota corrected 20 % → 50 %
  (matches the unit file `CPUQuota=50%`).
- **`CONTRIBUTING.md`** adds the missing `pip install -e .` step.
- **Settings page** now shows "App version:" alongside the existing
  "Frontend build:" line. Version is read at build time from
  `pyproject.toml` so there's one source of truth for the version
  string (no `package.json` drift).
- **`tests/test_import_rrd.py`** gained 11 tests covering the
  `fetch_rrd` / `get_last_update` / `merge_tier` / `main` orchestrator
  surface (previously untested).
- **`tests/test_concurrency.py`** gained
  `TestPurgeVsCollectorConcurrency` — proves the purge script no
  longer hits `database is locked` against a live writer.
- **`frontend/test/smoke.test.tsx`** — Gallery stub shape corrected
  (`items` → `aircraft`); Settings stub gained the missing
  `time_format` key.
- **`.github/workflows/shellcheck.yml`** — runs `shellcheck
  scripts/*.sh` on every PR that touches shell scripts.

### Scripts

- All four purge / import scripts (`purge_ghosts`, `purge_bad_gs`,
  `purge_mlat_gs_spikes`, `import_rrd`) now use `database.connect()`
  instead of `sqlite3.connect()`, inheriting WAL + `busy_timeout=30s`.
  Purges against a live collector no longer fail immediately on
  `database is locked`.
- `purge_mlat_gs_spikes.py --min-gs-count` clamped at 2 so a typo
  can't crash `statistics.quantiles`.
- `database.snapshot_db()` now goes through `connect()` too.

### Test totals

Python **1330 → 1356** (+26). Vitest **142 → 143** (no shape changes;
one stub corrected). Frontend `npm run build` and `npx tsc -b --noEmit`
both clean under `strictNullChecks: true`.

## 2.2.3 — 2026-05-20

### Documentation refresh

- **README**: v2 UI screenshots replace all v1 images; live map and aircraft
  gallery lead the grid. Added "Why readsbstats?" paragraph and release badge.
- **CONTRIBUTING.md**: removed stale v1 references (`templates/`, `static/`,
  Jinja2, Leaflet, uPlot); added frontend dev setup (npm).
- **CODE_OF_CONDUCT.md**: new community standards file.
- **docs/development.md**: test counts updated to current figures.
- **pyproject.toml**: version field brought in sync with git tags.

## 2.2.2 — 2026-05-19

### Flight detail page polish

- **Layout**: Route and Altitude+speed are now full-row blocks stacked
  vertically, replacing the previous `lg:grid-cols-3` (2/3 Route + 1/3
  chart) layout that left the chart cramped on desktop and squeezed the
  legend into the axis tick row.
- **Chart legend** moved from `bottom: 0` to `top: 0` — at the bottom it
  collided with the x-axis tick labels on narrow viewports (e.g.
  `21:39 Alt(m) 21:41 Speed(km/h) …`). Y-axis `name` labels dropped since
  the top legend now carries the series identifiers.
- **Route map height** bumped at `lg:` to fit the new full-width row.

### Nav bar opacity / z-index

- Sticky nav z-index lifted from `z-40` to `z-[1000]` so it sits above
  Leaflet's pane stack (max 800 for `.leaflet-control`).
- Fallback / `supports-[backdrop-filter]` background opacity raised from
  `/85` and `/70` to `/95` and `/85` — fixes the iOS Safari edge case
  where satellite tiles on `/flight` bled through the translucent nav
  during scroll.

### Badge `whitespace-nowrap`

Source / flag badges like `ADS-B` no longer break at the hyphen when
their parent column is narrow (e.g. the position-log Source column on
iPhone portrait).

### Top statistics view picker on mobile

The six tabs (`Aircraft types`, `Airlines`, `Countries`, `Visitors`,
`Routes`, `Airports`) overflowed iPhone portrait into two rows. Mobile
(`< sm`) now shows a Radix Select dropdown; desktop (`≥ sm`) keeps the
familiar tab strip. Both controls share the same `view` state.

## 2.2.1 — 2026-05-19

### Stats activity heatmap: responsive layout for narrow viewports

The DOW × hour heatmap on `/stats` overflowed iPhone portrait viewports
(393 px) because its 24 hour columns needed ~500 px even at the
`minmax(18px, 1fr)` floor, triggering horizontal scroll. Switched to a
two-layout design gated purely by Tailwind:

- **< `sm:` (≤ 639 px)**: hours run as **rows** (24), days as **7
  columns** (Sun … Sat across the top). 7 × ~50 px ≈ 350 px — fits
  portrait comfortably.
- **≥ `sm:` (≥ 640 px)**: original layout — 24 hour columns, 7 day rows.

Both layouts share the same `<Cell>` component, so the Radix tooltip,
keyboard focus, and per-cell `aria-label` from the existing custom-SVG
design carry over unchanged (the a11y posture documented in ADR-0008
is preserved).

## 2.2.0 — 2026-05-19

### Frontend chart library: Recharts → Apache ECharts

All four chart surfaces (`/metrics`, `/stats` bars + top-N, `/flight`
altitude+speed profile) now render via Apache ECharts 6 on a canvas
backend. See `docs/decisions/0008-apache-echarts-frontend-charts.md` for
the full rationale.

- **`/metrics`** — Panels share a connected group so hovering on one
  shows a synchronized vertical guide + axis-pointer label on all of
  them (`echarts.connect`). Each panel has a `dataZoom: 'inside'`
  (wheel/pinch) for sub-range exploration without an API round-trip.
- **Panel layout**: 11 → 10 panels; "Network — feed out" and "Network —
  feed in" merged into a single two-series "Network" panel (even count
  pairs cleanly in the 2-column layout at `xl:` breakpoint).
- **Grid breakpoint**: panels lay out 1-per-row up to `xl:` (1280 px),
  2-per-row beyond — wider charts at typical laptop / tablet widths,
  pair-density on wide monitors.
- **Axis tick formatter**: span-aware. < 36 h shows `HH:MM`; ≥ 36 h
  shows locale-aware `DD/MM`. On-hover axis-pointer label keeps the
  full timestamp via `useFormat().fmtTs` (12 h / 24 h respected per
  `RSBS_TIME_FORMAT`).
- **LTTB sampling** (`series.sampling: 'lttb'`) on every line series —
  kicks in when point count exceeds rendered pixel width.
- **Custom React wrapper** at `frontend/src/components/charts/EChart.tsx`,
  hand-rolled on `echarts/core` with tree-shaken component imports
  (`LineChart`, `BarChart`, `GridComponent`, `TooltipComponent`,
  `DataZoomComponent`, `LegendComponent`, `CanvasRenderer`). No third-
  party React wrapper — `echarts-for-react@3` was evaluated and rejected
  because its transitive dep `size-sensor` is flagged as malware in
  [GHSA-gx6x-v325-85g4](https://github.com/advisories/GHSA-gx6x-v325-85g4).
- **`Heatmap.tsx` + `PolarRange.tsx`** intentionally remain custom SVG /
  CSS. The heatmap's per-cell Radix tooltip + keyboard focus +
  `aria-label` is an a11y win that ECharts canvas would erase.
- **Bundle**: `charts-*.js` chunk 112 KB gz → 188 KB gz (Recharts SVG →
  ECharts canvas + zrender). Other chunks unchanged. The chunk is
  lazy-loaded by stats / metrics / flight only — shell unaffected.

### Licensing

- New `THIRD_PARTY_NOTICES.md` at repo root — verbatim Apache ECharts
  `NOTICE` block + d3-shape BSD-3 sub-license. `README.md`'s License
  section links to it. Apache 2.0 attribution requirements now satisfied
  for the bundled frontend.

### Tests

- **+25 Vitest** (117 → 142). New: `echart-wrapper.test.tsx` (lifecycle:
  init, setOption, group sync, dispose, events), `echarts-option-builders.test.ts`
  (pure unit tests on all 4 builders + span-switch HH:MM ↔ DD/MM),
  `top-chart-click.test.tsx` (visitors-view nav, non-visitors ignore,
  missing-icao_hex tolerance), `echarts-time-format.test.ts`
  (`RSBS_TIME_FORMAT` propagation through axis labels).
- **+1 Playwright assertion**: `metrics-panel-*` count regression guard.
- **Pre-existing Playwright failures fixed** (unrelated to charts but
  surfaced during the v2.2.0 test sweep): `stat-squawk-XXXX` testid
  typo (was `stats-squawk-XXXX`); watchlist add-form tests now use
  valid 6-hex ICAOs (form validation was added after the test was
  written); custom-range popover test applies with form defaults
  instead of `fill()`-ing the themed `DatePicker` DOM.

## 2.1.19 — 2026-05-19

### SQLite crash-safety hardening

After a recent power outage, three reliability gaps were addressed:

- **`synchronous = FULL`** in both `database.DDL` and `database.connect()`.
  Adds one fsync per write commit, ensuring committed transactions survive
  power loss. Negligible throughput impact at the 5-second poll cadence.
- **Dirty-shutdown sentinel** at `<db-dir>/.dirty_shutdown`. The collector
  writes it on startup and removes it on graceful shutdown. If the sentinel
  is present at next startup, the collector runs `PRAGMA quick_check(10)` —
  on success, also `PRAGMA wal_checkpoint(TRUNCATE)` to clean WAL state.
  On detected corruption, logs CRITICAL and continues degraded rather than
  refusing to start.
- **Periodic integrity checks** via two new systemd timers:
  - `readsbstats-dbcheck.timer` — weekly `quick_check` (Sun 03:30 local)
  - `readsbstats-dbcheck-full.timer` — monthly `integrity_check`
    (1st Sun 04:00 local)
  Both trigger `OnFailure=notify-telegram@%n.service` so corruption
  detected overnight surfaces immediately. Schedule chosen from 90 days of
  traffic data (03:00–04:00 = absolute trough, 0.3–0.6 aircraft average).
- New script `scripts/check_db.py` for manual integrity checks. Opens the
  DB read-only (`?mode=ro` URI), safe to run while the collector is writing.
  Exit codes: 0 = OK, 1 = corruption, 2 = open/query error.

See `docs/decisions/0007-sqlite-integrity-checks.md` for the full rationale.

## 2.1.18 — 2026-05-19

### UI polish — personal records, watchlist, frontend build info

- **Personal records tile layout.** Each tile now shows the record label
  and timestamp on the same top row (label left, date right), the metric
  value on the second row, and the aircraft identifier on the third row.
  The identifier shows callsign when available, falling back to ICAO hex
  in monospace; aircraft type description follows it when known
  (e.g. `BAW123 · Boeing 737`). Previously only the raw ICAO hex was
  shown, and the timestamp was inline at the end of the same row.
- **Watchlist Add button height.** The Add button was taller than the
  adjacent input fields (Button `md` = 40 px, Input = 36 px). Added
  `'field'` size to Button (`py-2 text-sm min-h-[36px]`) that matches
  Input padding exactly. The size is available for other form-inline
  buttons going forward.
- **Watchlist ICAO hex validation.** Submitting a value under the
  "ICAO hex" match type now enforces exactly 6 hexadecimal characters
  before the network call, with a clear inline error. Registration and
  callsign prefix remain length-only (formats are too varied
  internationally to constrain).
- **Watchlist value placeholder.** The static hint `e.g. 3c4b17 /
  SP-LRF / LOT` is replaced by a per-match-type placeholder that
  updates as the match type selector changes.
- **Frontend build info in Settings.** A "Build info" card at the bottom
  of the Settings page shows the git short SHA and build date injected
  at compile time (e.g. `e597b42 · 2026-05-19`). Zero runtime overhead;
  useful for confirming which frontend build is deployed without mapping
  a version number to a commit.

## 2.1.17 — 2026-05-18

### Themed DatePicker + TimePicker replace native date/time inputs

The browser's native calendar and time-picker popups (Chrome's WebKit
chrome) ignored our dark theme — bright white widgets on dark pages.
Both replaced with themed components.

- New `components/ui/DatePicker.tsx` — Radix Popover shell wrapping
  `react-day-picker` (10.0.1), styled with our existing color tokens
  (`--color-accent`, `--color-surface-2`, `--color-border-default`).
  Trigger button matches `<Input>` shape so it drops into a `<Field>`
  without layout shifts. Round-trips ISO date strings (`YYYY-MM-DD`).
- New `components/ui/TimePicker.tsx` — Radix Popover with two
  scrollable HH / MM columns, themed identically to DatePicker.
  Round-trips `HH:MM`. Requires both columns be touched in a single
  session before committing — guards against partial-edit close races.
  Minute step defaults to 5; pass `minuteStep={1}` for finer control.
- `History.tsx` — From / To filters now use `<DatePicker>` (was two
  `<input type="date">`).
- `RangePicker.tsx` `CustomRangeForm` — was `<input type="datetime-
  local">` × 2; now `<DatePicker>` + `<TimePicker>` per field. Serves
  the Stats and Metrics "Custom" range pickers.

Bundle delta: +1 KB gz on the main chunk (react-day-picker tree-shakes
cleanly; TimePicker uses only existing primitives).

Tests: `frontend/test/date-picker.test.tsx` (3) and
`frontend/test/time-picker.test.tsx` (4) smoke-test the
open-popover → pick → onChange roundtrip and the two-column
commit-on-both-touched contract.

## 2.1.16 — 2026-05-18

### UI/UX polish — audit v2 follow-up

Targeted refinements found by reviewing the v2 SPA screenshots in
`internal_docs/uiux/audit-v2-2026-05-18.md`:

- **Flag tiles fully clickable.** Military / Interesting / Anonymous
  cards on the Stats page are now single `<Link>` elements (was a
  card with a small inner "See in history →" link). Matches the
  squawk tile pattern next to them. `aria-label` preserves the
  affordance for screen readers.
- **TopChart tab labels normalised.** "Frequent visitors" → "Visitors",
  "Top routes" → "Routes", "Top airports" → "Airports". Cleaner at
  narrow viewports.
- **Polar range subtitle deduplicated.** Was `max 706.2 km · Dist (km)`
  (unit twice). Now just `max 706.2 km`.
- **Gallery hex vs registration distinction.** Cards without a
  registration now show the ICAO hex in monospace with a dim `hex`
  label adjacent, so `SP-LIG` and `0222` no longer look identical.
- **History ROUTE column hidden when empty.** When no flight in the
  current filtered result has origin/dest data, the Route column is
  dropped entirely instead of showing a column full of `—`. Reappears
  as soon as any row has route data.
- **Activity heatmap legend.** A small 4-swatch gradient strip is
  rendered below the DOW × hour grid showing `1` → max `flights/hr`.
- **Gallery type-photo label lightened.** The `is_type_photo` marker
  is now dim caption text instead of a full badge; signal preserved,
  visual weight reduced.
- **Personal records density.** Each card's ICAO + timestamp now
  share a single line under the value rather than stacking on two.

### New: `RSBS_TIME_FORMAT` (24h / 12h)

New env var controlling the clock format for UI timestamps across
the whole app (FlightsTable, Gallery, Aircraft, Flight, Map, Records).
Accepted values: `24h` (default), `12h`. Invalid values fall back to
`24h`. Seeded into the browser on first boot via `/api/settings`;
users can override locally by setting `localStorage.rsbs_clock_format`,
which then wins over the env var on subsequent loads.

## 2.1.15 — 2026-05-18

### Stats page — layout restructure + unified TopChart

**Unified bar chart replaces six stat tables.** The six separate
top-N tiles (top aircraft types, airlines, countries, frequent
visitors, routes, airports) are replaced by a single `TopChart`
component — horizontal bars, up to 15 entries, tab-switcher to
select the dataset. The Frequent visitors tab retains click-through
navigation to the aircraft detail page.

**Page layout reordered for hierarchy.** Sections now read top to
bottom in decreasing time-sensitivity:

1. Summary cards (two rows)
2. Activity by hour + Daily unique aircraft (two-col grid)
3. Activity heatmap — DOW × hour (full width)
4. New aircraft + Polar range (two-col grid)
5. Top statistics bar chart (full width)
6. Personal records

Previously the heatmap was squashed into a three-col grid alongside
the polar chart, and TopChart was sandwiched above the New aircraft
section. Both sections now have room to breathe.

**Summary row 1 restructured (6 cards).** Total flights, Last 24h,
Last 7 days, Unique aircraft, Position fixes, DB size — on a
`grid-cols-2 sm:grid-cols-3 lg:grid-cols-6` grid. Last 24h and
Last 7 days move from row 2 into row 1, immediately after Total
flights so the three time-aggregate numbers sit together.

**Summary row 2 restructured (6 cards).** Military, Interesting,
Anonymous, 7700, 7600, 7500 — same grid. Emergency squawks tiles
migrate from their own card into this row, replacing the old 8-tile
wide row. Labels shortened to fit the narrower columns: "General
emergency" → "Emergency".

**TrendCard tooltip.** The "vs prev period" label is removed from
the inline card text. Hovering either trend card shows a Radix
Tooltip with the full delta, percentage, and "vs previous period"
context. When no previous-period data exists the tooltip says "No
previous period data"; the inline area shows `—`.

### Test count

```
1317 Python + 103 Vitest = 1420 unit tests
```

(unchanged — UI-only restructure)

---

## 2.1.14 — 2026-05-17

### Documentation refresh

**DuckDB ADR rewrite** (`docs/decisions/0002`) — collapses the
278-line "captured at time of decision" planning doc into a 41-line
classic ADR (Status / Context / Decision / Consequences). The
detailed status snapshots, phase plans, and measured numbers move
out of the public ADR; the ADR now reads as the architectural
record only.

**Setup examples polish** — clarifies in `docs/configuration.md`
that `RSBS_TELEGRAM_BASE_URL` is a value users must set to their
own URL, and switches the rsync / SSH examples in
`docs/development.md` and `docs/integrations.md` to a generic
placeholder host so the snippets work as templates regardless of
which network the operator is on.

**Stale-number sweep** — caught up references that drifted during
the v2.1.13 UI consistency pass:

- `docs/development.md` — Python test count `1198` → `1317`;
  Vitest count `43` → `103`.
- `README.md` — file tree pytest count `1198` → `1317`.

### Test count

```
1317 Python + 103 Vitest = 1420 unit tests
```

(unchanged from 2.1.13 — this release is docs-only)

---

## 2.1.13 — 2026-05-17

### UI consistency pass — Radix primitives across the SPA

Four cosmetic-but-substantive frontend refactors that bring the SPA's
icon, menu, tooltip, and dropdown language into one source. All four
land as separate commits before this release tag so the history reads
cleanly.

**1. `@radix-ui/react-icons` replaces every text glyph + inline SVG.**
`☰` `▾` `▴` `▼` `▲` `✓` `←` `→` and three hand-rolled SVGs (Sheet
close, Map play/pause, Gallery sort) now use typed Radix components.
Stats trend cards switch to `TriangleUp`/`Down`/`DotFilled`. Brand
`✈` and inline route-string arrows (`EPWA→EDDF`) intentionally
remain — Radix has no plane glyph and those arrows are text content.

**2. Mobile hamburger nav is now a Radix `DropdownMenu`.** Drops the
hand-rolled `open`/`setOpen` toggle and conditional `<ul>`; gains
focus trap, ESC-to-close, outside-click dismissal, arrow-key
navigation, and proper `role="menu"`/`role="menuitem"` ARIA. Splits
the nav into a desktop horizontal list (`md:flex`) and a mobile
DropdownMenu (`md:hidden`).

**3. Native `title="…"` replaced by Radix Tooltip on six sites.**
LiveCountBadge, Heatmap (168 cells, now keyboard-focusable), Gallery
sort (nested `Tooltip + Popover` via stacked `asChild` Slot
composition), Map stale-snapshot badge, Aircraft "Watching" toggle,
Stats emergency squawks. Adds `<TooltipProvider>` at the app root
with a 300 ms delay / 500 ms skip.

**4. History Source + Flag filters migrate to Radix `Select`.** The
only `NativeSelect`s left in the app are gone; the `NativeSelect`
export is removed from `ui/Input.tsx` (zero consumers). Adds an
`ANY_VALUE='__any__'` sentinel because Radix `SelectItem` rejects
`""` as a value at runtime, with translation at the URL-param
boundary so the existing search params stay unchanged.

### Bugfix — receiver-health per-check icons rendered identically

The /metrics receiver-health panel showed nine rows with the same
dim info icon regardless of severity. Root cause was a field-name
mismatch the type system couldn't catch: the backend dataclass
returns `Check.severity` but the frontend `HealthCheck` interface
declared `status`. Every read fell through to the
`InfoCircledIcon` fallback and the same dim grey colour.

Renamed the interface field with a comment referencing
`health.py` as the source of truth, and added
`test/metrics-health.test.tsx` (2 tests) that render the panel with
a synthetic fixture and assert the `data-status` attribute and icon
colour differ across severities — would have caught the original
drift.

### Test count

```
1317 Python + 103 Vitest = 1420 unit tests
```

(+13 vitest from this release: 4 nav, 3 tooltip, 4 history-filters,
2 metrics-health.)

---

## 2.1.12 — 2026-05-17

### Security — CodeQL #29 (py/url-redirection) defence-in-depth

CodeQL alert
[#29](https://github.com/blindp3w/readsbstats/security/code-scanning/29)
flagged the `/v2/{rest:path}` redirect handler with the same
`py/url-redirection` rule that produced #28 in v2.1.1. The previous fix
added a `_sanitize_v2_rest` custom sanitizer that strips leading `/` /
`\\` / CR / LF and percent-encodes URL-special characters — functionally
correct, but CodeQL's data-flow analyzer cannot statically recognise our
custom helper as a safe sanitizer, so it kept flagging the path from
the `rest` parameter into `RedirectResponse(url=target)`.

This release adds the recognized sanitizer pattern from CodeQL's own
documentation: an `urllib.parse.urlparse(target)` check that the final
redirect target has neither a scheme nor a netloc. If anything slips
past `_sanitize_v2_rest` (defence-in-depth — should never happen given
the existing strips), the handler falls back to redirecting to the SPA
root instead of honouring the off-site target.

```python
parsed_target = urllib.parse.urlparse(target)
if parsed_target.scheme or parsed_target.netloc:
    return RedirectResponse(url=f"{root}/", status_code=301)
return RedirectResponse(url=target, status_code=301)
```

Same shape as the example in
https://codeql.github.com/codeql-query-help/python/py-url-redirection/
— ensures CodeQL recognises the guard.

**Test**: `tests/test_web.py::TestSpaMount::test_v2_compat_urlparse_guard_falls_back_to_root`
— monkey-patches `_sanitize_v2_rest` to a deliberately broken version
that returns `/evil.com`, then verifies the route still produces a
same-origin redirect rather than honouring the off-site target.

**Test totals**: Python 1316 → 1317 (+1). Vitest 90 (unchanged).

## 2.1.11 — 2026-05-17

### Audit 12 Phase 9 — DNS-rebinding fix redesigned (H1 + H2)

The Phase 2 (v2.1.4) DNS-rebinding TOCTOU fix worked but was brittle: it
installed a process-wide ``socket.getaddrinfo`` patch at module load that
checked a thread-local pin. Any test doing the obvious
``monkeypatch.setattr(socket, "getaddrinfo", ...)`` was silently no-op'd,
and the design wouldn't naturally cover ``httpx.AsyncClient`` (which
bypasses ``socket.getaddrinfo`` via ``anyio.getaddrinfo``).

Phase 9 eliminates the global patch entirely. Two distinct mechanisms
now close the TOCTOU per code path:

**urllib path — custom HTTPSConnection (audit-12 H1)**

`safe_urlopen` now builds a one-shot opener per call (via
`_build_pinned_opener`) whose HTTPS handler issues every connection
through a new `_PinnedHTTPSConnection`. The connection:

- Connects to the pre-validated IP directly via `socket.create_connection`
  — no DNS lookup happens between `validate_url` and the connect.
- TLS handshake uses the original hostname for SNI AND triggers Python's
  standard hostname-vs-cert verification.
- urllib's `Host:` header is set automatically from the URL host.

No reliance on `socket.getaddrinfo` at all between resolve+validate and
the actual fetch. The rebinding window is closed at the protocol layer.

**httpx path — scoped resolver redirect (audit-12 H1 partial)**

`safe_httpx_get` wraps the call in `_pinned_socket_resolver`, a
`@contextmanager` that temporarily redirects `socket.getaddrinfo` to
return the pre-validated info tuple for the duration of the single
request, then restores the original in `finally`. No module-load global
patch; the redirection is fully scoped to one fetch.

The redirection is technically still process-wide for the brief window
inside the `with` block — but unlike Phase 2's permanent patch, tests
patching `socket.getaddrinfo` outside this narrow window now behave as
expected. The trade-off is documented at the top of `http_safe.py`.

**Async-httpx guard (audit-12 H2)**

`safe_httpx_get` now raises `RuntimeError` immediately if passed an
`httpx.AsyncClient`. Async httpx bypasses `socket.getaddrinfo` via
`anyio.getaddrinfo`, so our scoped pin doesn't protect it. We don't
currently use async httpx anywhere; the guard is defensive against
future drift. If you ever need async support, implement a custom
`httpcore.NetworkBackend` with the resolution baked in.

**Code-shape changes**

Removed (module-level globals):

- `_dns_pin` (thread-local pin storage)
- `_pinned_getaddrinfo` (the global wrapper)
- `_set_dns_pin`, `_clear_dns_pin` (pin lifecycle helpers)
- `_no_redirect_opener` (the module-level opener — replaced by
  per-call `_build_pinned_opener`)
- `socket.getaddrinfo = _pinned_getaddrinfo` (the module-load patch)

Added:

- `_PinnedHTTPSConnection` — `http.client.HTTPSConnection` subclass
  that connects to a pre-validated IP with proper SNI.
- `_PinnedHTTPSHandler` — `urllib.request.HTTPSHandler` factory using
  the connection.
- `_build_pinned_opener(parsed, target_ip, timeout)` — one-shot
  opener builder.
- `_pinned_socket_resolver(hostname, infos)` — scoped resolver
  context-manager for the httpx path.
- `_resolve_and_validate(url) -> (parsed, infos)` — the shared
  resolve+validate helper used by both code paths.

Kept (back-compat):

- `validate_url(url)` — still public, still validates URLs, but now
  discards the addrinfo (callers that want to fetch should use
  `safe_urlopen` / `safe_httpx_get` which do their own resolution).
- `_real_getaddrinfo` — captured `socket.getaddrinfo` reference, kept
  so tests can monkey-patch resolution without fighting the missing
  global patch.

**Tests**

- New `TestUrllibPinnedConnection` (3 cases) — verifies
  `_PinnedHTTPSConnection` is constructed with the right IP, both
  handlers wired into the opener, the resolve helper returns infos.
- New `TestHttpxScopedResolver` (3 cases) — verifies the resolver
  redirect only applies inside the `with` block, restores on
  exception, falls through for other hosts.
- New `TestHttpxAsyncRejection` (1 case) — `AsyncClient` raises
  `RuntimeError`.
- `TestSafeUrlopen` rewritten — now mocks `_build_pinned_opener`
  factory instead of the deleted `_no_redirect_opener`.
- `test_photo_sources.py`, `test_db_updater.py` — monkey-patches
  updated to the new surface.

Two old tests removed (no longer applicable): the `_no_redirect_opener`
wiring guard (replaced by `test_build_pinned_opener_wires_both_handlers`)
and four `TestDnsPinning` tests for the removed thread-local pin
behaviour (replaced by the new redesigned-path tests).

**Test totals**: Python 1314 → 1316 (+2 net). Vitest 90 (unchanged).
Frontend `npm run build` clean.

**This closes Audit 12.** All High-severity findings now have proper
fixes. The three large refactors (#193 web.py split, #194 _migrate
split, #195 page extractions) and a handful of Low-severity cosmetics
remain as opportunistic future work, but no further phases planned.

## 2.1.10 — 2026-05-17

### Audit 12 Phase 8 — self-review follow-up

Three parallel review agents went through every Audit 12 change. Phase 8
ships the actionable findings from that review. No new functionality;
small bug fixes, defence-in-depth additions, and documentation /
test-correctness fixes.

**Security defence-in-depth**

- **#149 P8** `_v2_compat` sanitizer now `urllib.parse.quote(rest, safe="/")`
  on top of the CR/LF strip — the audit's original recommendation
  included this step but only the strip shipped in v2.1.4. A path with
  literal spaces / quotes / `?` / `#` characters now produces a
  well-formed (percent-encoded) Location header instead of a potentially
  malformed one.
- **#171 P8** `/api/settings` `stats_json` label stopped hardcoding
  `/run/readsb/stats.json` as the "default" sentinel. The comparison
  duplicated the default from `config.py` (drift-prone) and leaked one
  bit ("did the operator customise this path"). Now uniformly reports
  `(configured)` or `(not set)`.

**Reliability**

- **#154 P8** `_watch_remove` now falls back to `callsign_prefix` as the
  third match-type. The usage string promised all three (`icao` /
  `registration` / `callsign_prefix`) but the fallback chain only tried
  the first two. Telegram-bot users could not remove a callsign_prefix
  entry without using the HTTP API.

**Type safety / data quality**

- **#P8** `lib/types.ts::WatchlistEntry.created_at` and `airborne`
  marked optional. The `GET /api/watchlist` endpoint returns all six
  fields; `POST /api/watchlist` returns only four. Previously the type
  declared all six as required which would surface as silent
  `undefined`s if anyone read those fields off a mutation result.

**Test correctness**

- **#P8** `frontend/test/smoke.test.tsx` had stub shapes that didn't
  match the real response interfaces — pages rendered the empty path by
  accident rather than by exercising the actual code path. Stubs for
  `/api/metrics`, `/api/metrics/health`, and `/api/flights` corrected to
  the real `MetricsResp` / `HealthResp` / `FlightsResponse` shapes.

**Dead code / style cleanup**

- **#P6 follow-up** Removed two duplicate `import re` / `from . import
  database` inside `notifier._watch_add` and `notifier._listener_loop`
  that should have been caught in Phase 6's "imports at module top"
  sweep. (`database` is already imported at module top; the "circular
  dependency" comment in `_listener_loop` was historical and no longer
  applies — verified by grep.)
- **#P6 follow-up** Moved the `from _purge_helpers import` line in both
  purge scripts to the top import block. Moved `from collections
  import OrderedDict as _OrderedDict` in `web.py` to the top.
- **#P6 follow-up** `.claude/rules/python.md` referenced the old
  `_clamp_int` / `_clamp_float` names — renamed to
  `_min_or_default_int` / `_min_or_default_float` to match the actual
  function names. Also documents `_bool()` as the canonical boolean
  env-var parser.

**Doc clarity**

- Three `apply_purge()` docstrings now document the batched-commit
  semantics introduced in Phase 3. The old docstrings claimed atomicity
  that no longer holds; the new text explicitly notes that an
  interrupted run can leave the DB partially purged, and that the
  script is idempotent so re-running finishes the work.
- **#197 P8** CHANGELOG entry for v2.1.8 now explicitly notes the
  case-sensitivity behaviour change in `_bool` (two flags previously
  treated `False` as truthy because of case-sensitive comparison).

**Test deltas**

- `tests/test_web.py`: added 1 new test for the `_v2_compat` quote step
  + 1 updated test for the new CR/LF-then-quote ordering + 1 updated
  assertion for the new `stats_json` label.
- `tests/test_notifier.py`: added 1 new test for the
  `callsign_prefix` fallback.

**Test totals**: Python 1312 → 1314 (+2). Vitest 90 (unchanged).

**Other items from the self-review intentionally not addressed in this
phase**:

- **H1 / H2** (DNS-pin scope is too broad and doesn't reliably cover
  async httpx) — would require a per-transport resolver hook, a
  multi-file refactor of `http_safe.py`. Worth its own dedicated PR.
- **M8** (three new tests reach across global state in fragile ways) —
  works today, would surface as flakes only under `pytest-xdist` or
  reordering. Defer to the next time someone actually wants parallel
  test execution.
- **M1** (purge scripts now non-atomic) — addressed via docstring
  updates rather than restoring atomicity; the Phase 3 trade-off
  (lock-starvation avoidance) is the right call for the actual
  workload.

## 2.1.9 — 2026-05-17

### Audit 12 Phase 7 — documentation hygiene

Doc-only. No source-code behavior change. Fixes stale references to
files/paths/identifiers that didn't survive v2.0.0 or the audit work.

- `src/readsbstats/http_safe.py` module docstring referenced
  `static/js/table-utils.js` — replaced with the current
  `frontend/src/lib/safeUrl.ts` path.
- `frontend/src/lib/safeUrl.ts` opening comment said "Ported from
  static/js/table-utils.js:safeHttpUrl" with no acknowledgement that
  the file is deleted — clarified.
- `frontend/src/main.tsx` basename comment described the
  v2.0.0-rc.1 transitional `/stats/v2/` prefix instead of the
  current canonical `/stats/`.
- `frontend/CLAUDE.md` claimed "Vitest + jsdom (43 tests)" — updated
  to the post-audit count.

No tests change.

This is the final phase of Audit 12. Across six shipped phases
(v2.1.3 → v2.1.8) plus this doc cleanup, **~60 of ~75 numbered
audit findings** are closed. Remaining items are three large
deferred refactors (#193 web.py split, #194 _migrate() split,
#195 page extractions) plus a handful of Low-severity cosmetic
items, all tracked in `internal_docs/security/audit-12-2026-05-17-post-v2.md`.

## 2.1.8 — 2026-05-17

### Audit 12 Phase 6 — style + dead-code cleanup

Refactor-only phase. No behavior change. Closes the "smaller" audit
items: dead code deletions, duplicated definitions consolidated,
inconsistent env parsing unified, and a few stale names corrected.

**Dead code removed**

- `frontend/src/pages/Hello.tsx` — Phase 0 PoC, never routed.
- `metrics_collector._g` — helper defined but never called.
- `route_enricher._is_confirmed_unknown` + its 3 tests — only used in
  tests, no production caller.

**Module-top imports**

- `web.py` `import re` was inside `_feeder_details_mlat` — moved to top.
- `db_updater.py` `from . import http_safe` was inside `_fetch` —
  promoted to the module-level import block.
- `scripts/import_rrd.py` `from datetime import ...` was inside a loop
  — moved to module top.

**De-duplication**

- **#197** Centralised boolean env parsing in `config._bool(name, default)`.
  Replaced five inconsistent `os.getenv(...) not in (...)` patterns
  (`WIKIPEDIA_PHOTO`, `ADSBX_ENABLED`, `METRICS_ENABLED`, `USE_DUCKDB`,
  `PREWARM_MAP_CACHE`) that had drifted in their tuple ordering and
  empty-string handling. 13 new tests pin the contract.

  **⚠ Minor behaviour change:** the new helper case-normalises before
  comparing, where two of the old patterns (`ADSBX_ENABLED`,
  `PREWARM_MAP_CACHE`) did not. Operators who had `RSBS_ADSBX_ENABLED=False`
  (capital F) — previously treated as truthy because `"False" != "false"`
  — will now see those flags correctly recognised as falsy. The fix is
  to use `0` (or lowercase `false`/`no`/`off`) — the documented falsy
  values. Audit-12 Phase 8 follow-up documented this explicitly.
- **#198** `_TransientError` was declared identically in 3 modules
  (`route_enricher`, `adsbx_enricher`, `metrics_collector`). Now a
  single `http_safe.TransientError` aliased into each consumer; tests
  still resolve `<module>._TransientError` so no test churn.
- **#199** `_new_max_gs` was duplicated in `purge_bad_gs` and
  `purge_mlat_gs_spikes`. Extracted to new `scripts/_purge_helpers.py`
  with both scripts importing the canonical version.

**Renames for clarity**

- **#196** `_clamp_int` / `_clamp_float` → `_min_or_default_int` /
  `_min_or_default_float`. The helpers only enforce a lower bound;
  "clamp" implied two-sided clamping. Docstrings updated to be
  explicit.
- **#P6.6** `components/ui/Input.tsx::Select` → `NativeSelect`.
  Disambiguates from the Radix `Select` in `@/components/ui/Select`
  (the styled-dropdown primitive).
- **#P6.7** `WatchlistEntry` type unified in new
  `frontend/src/lib/types.ts`. Was declared in two places
  (`Aircraft.tsx`, `Watchlist.tsx`) with divergent shapes.

**Test suite**: Python 1299 → 1312 (+13). Vitest 90 (unchanged).

**Deferred to a later release** (too large for a single phase):
- `web.py` 2535-line file split into `routes/` + `prewarm.py` + etc. (#193)
- `database._migrate()` 170-line monolith split into focused helpers (#194)
- Page extractions (Stats/Map/Flight/Metrics over 300 lines) (#195)

## 2.1.7 — 2026-05-17

### Audit 12 Phase 5 — test coverage hardening

Test-only phase. No production code changes. Closes the most-leverage
coverage gaps the audit flagged so future refactors trip a test before
reaching prod.

**Frontend (Vitest 54 → 90, +36 tests)**

- **#200** `useSearchParamBatch` covered for the first time. 12 tests
  exercise single-param and multi-param updates, default-stripping,
  explicit `null` removal, and the documented contract that the helper
  is the v7 stale-state *fix* (one call with multiple keys), not a
  workaround for two-call usage. CLAUDE.md flagged this as the
  highest-leverage missing test.
- **#210** `lib/flags.ts` covered with 12 tests: FLAG_* bit values pinned
  to backend `config.py`, the `primaryFlagLabel` precedence ladder
  (military > interesting > anonymous > none), and PIA/LADD non-
  surfacing as primary labels.
- **#201** Smoke tests for App shell + ErrorBoundary + 9 pages
  (Settings, Feeders, Watchlist, History, Gallery, Stats, Metrics,
  Aircraft, Flight). Shared QueryClient + MemoryRouter + global-fetch
  stub harness. Each test verifies "renders without throwing on
  minimal/empty data" so a regression in imports, required props, or
  initial-state assumptions surfaces in CI. `Map.tsx` skipped
  (Leaflet's imperative DOM mutation isn't fully shimmed in jsdom;
  Playwright mobile suite covers it). `Hello.tsx` skipped (PoC, not
  routed).

**Backend (Python 1252 → 1299, +47 tests)**

- **#212** `notifier._h()` — direct unit tests for the HTML escape
  primitive (was indirectly covered via `notify_*`).
- **#206** `http_safe` IPv6 reject branches: loopback (`::1`),
  link-local (`fe80::`), unique-local (`fc00::/7`), multicast
  (`ff00::/8`), unspecified (`::`). Plus the previously-uncovered
  IPv4 `0.0.0.0`, RFC1918 (10/8, 172.16/12, 192.168/16), and a
  "mixed addrinfo with one private result rejects the whole URL"
  rebinding-defence test.
- **#204** `country_sql_case` — the SQL twin of `icao_to_country` had
  no direct tests. Added parity vs. Python for diverse hexes, the
  apostrophe-escape contract (`'` → `''`), and a synthetic execute
  check for hypothetical apostrophe-bearing country names.
- **#205** `_RAW` boundary edges parametrised for 6 representative
  blocks: exact start + exact end include, start-1 / end+1 fall out
  to a neighbour, and a "no partial overlap in _RAW" structural
  invariant.
- **#208** `analytics` engine-init error branches: unsafe DB_PATH
  rejects, OSError on `mkdir`, DuckDB exception during INSTALL/LOAD/
  ATTACH. Plus per-query exception → None fallback for both
  `heatmap` and `coverage`, and `close()` resets `_CONN`.
- **#211** `_prewarm_loop` survives one `_prewarm_one` raising —
  loop catches, schedules backoff for that target, continues with
  the next.
- **#203** `purge_mlat_gs_spikes` `TestMain` class — dry-run report,
  --apply modifies data, no-spikes-clean message, snapshot-by-default
  on --apply.

## 2.1.6 — 2026-05-17

### Audit 12 Phase 4 — performance + UX polish

Performance + UX cleanup phase. No new features; reduces a long-running
data backfill from O(n²) to O(n), spreads prewarmer startup CPU across
~100s instead of bunching it at boot, bounds three previously-unbounded
collections, prevents an in-memory hammering loop during upstream
outages, and tightens a handful of UI rough edges.

**Backend performance**

- **#147** `database.backfill_bearing` now uses a `WHERE id > last_id`
  cursor pattern. Each row is examined exactly once; the previous
  LIMIT-subquery pattern re-scanned the table from the top on every
  iteration (O(n²)). On a Pi 4 with 200k+ flights this turns hours of
  backfill into ~30s.
- **#185** Prewarmer no longer starts all 8 cache targets at `next_at=0.0`
  (which caused 8 back-to-back full-table scans across the first ~80s
  of startup). New `_initial_prewarm_schedule()` helper staggers the
  first refresh of each target by 15s, ordered by TTL ascending so
  the shortest-window heatmap/coverage (most-likely-hit by a user) is
  warmed first.

**Bounded memory**

- **#150** `web._type_fetch_locks` is now an LRU-bounded `OrderedDict`
  capped at 1024 entries. ICAO type designators are ~3k distinct so
  the cap is comfortable headroom; the previous unbounded dict would
  grow across worker lifetime without bound.
- **#186** `collector._squawk_notified` now gets `discard(flight_id)` in
  `_close_flight`, so the set is naturally bounded by max-concurrent
  active flights (a few thousand) rather than growing forever.
  `_notified_icao` is intentionally left unbounded — bounded LRU
  semantics would re-alert for the oldest first-sighting ICAOs after
  wraparound, which is the wrong behaviour for that data shape. The
  set is bounded in practice by tens of thousands of distinct flagged
  ICAOs (<50MB resident) over years of operation. A comment now
  documents this.

**Reliability**

- **#155** `route_enricher` now keeps a per-callsign cooldown after a
  transient (network / HTTP) failure. Without it, a multi-hour
  upstream outage hammered the same N callsigns every batch interval.
  Default cooldown is 300s; success clears the cooldown entry so
  recovered state isn't sticky.
- **#154** `notifier._watch_remove` now tries the inferred match_type
  first (preserves Audit 11 #116 — when both icao and registration
  rows exist for the same 6-hex value, icao wins). If that lookup
  matches nothing, falls back to the alternate type so a 6-hex-shaped
  registration (e.g. `ABC123`) is still removable via the bot.

**Frontend UX**

- **#157** `Map.tsx`: deleted dead `tickRef` state. Consolidated three
  duplicate comment blocks about the `<input type="range">` quirk into
  one coherent paragraph.
- **#158** `Map.tsx`: when a rewind snapshot fetch fails, the previous
  moment's data stays visible (intentional — avoids flicker). Now
  shows an inline "stale" badge on the snapshot timestamp pill so the
  user knows the displayed time is not the requested time.
- **#159** `Metrics.tsx`: hoisted `ALL_METRICS` from `useMemo(..., [])`
  to module scope. PANELS is a module constant so the join is
  constant — computing it once at import time is simpler and avoids
  the future-bug shape of a memo silently freezing on first render if
  PANELS ever became dynamic.
- **#160** `Watchlist.tsx`: tightened the optimistic-delete context via
  the 4th `useMutation` generic. `onError`'s `ctx` is now typed
  `DelMutCtx` automatically instead of a hand-typed annotation that
  would drift if `onMutate`'s return shape changed.

**Test suite**: Python 1245 → 1252 (+7). Frontend Vitest unchanged at 54.

## 2.1.5 — 2026-05-17

### Audit 12 Phase 3 — reliability fixes

Phase 3 of the post-v2 audit. Prevents silent outages: stale DB handles,
dropped alerts at shutdown, leaked subprocesses, silently-swallowed
feeder errors, slow/contended SQLite pragmas on a fresh photo connection,
and PK pollution in `adsbx_overrides`. All test-first.

**Metrics collector**

- **#142** `run_metrics_loop` now catches `sqlite3.OperationalError`,
  closes the bad handle, and re-connects via `database.connect()` so a
  moved DB / disk error / WAL hiccup doesn't wedge the collector for
  the rest of the process lifetime.
- **#148** Apply exponential backoff (capped at 300s) on the broad
  `Exception` path too — previously only `_TransientError` triggered
  backoff and a persistent DB error would tight-loop the log at the
  configured 60s interval.

**Purge scripts — batched commits**

- All three `apply_purge()` functions
  (`scripts/purge_ghosts.py`, `scripts/purge_bad_gs.py`,
  `scripts/purge_mlat_gs_spikes.py`) now commit every `_BATCH_SIZE = 100`
  flights instead of wrapping the whole flight loop in one transaction.
  On a database with thousands of flagged flights, the old pattern held
  the SQLite write lock for the full run and starved the collector. A
  single flight's delete + flight-row update still lives in one
  transaction; only the batch boundary commits early.

**Notification queue — drain on shutdown**

- **#145** New `collector.stop_notification_consumer(timeout=5.0)`
  helper drains the queue, posts the `None` sentinel, and joins the
  `tg-dispatch` thread. `main()` calls it during shutdown, after
  finalising active flights, before closing the DB. Previously the
  consumer was a daemon thread the interpreter killed abruptly at
  process exit, dropping any Telegram alerts queued by the last
  `_poll()` before SIGTERM.

**Subprocess leak**

- **#152** `_check_systemd_unit` and `_feeder_details_mlat` now
  `proc.kill()` + `await proc.wait()` on `asyncio.TimeoutError` so a
  hung systemctl/journalctl doesn't pile up zombie children under load.

**Operator visibility**

- **#151** `_fetch_feeder_details` no longer silently returns `[]` on
  exception — logs `WARNING ... exc_info=True` with the feeder name +
  status_type. A misconfigured feeder or corrupted status file would
  otherwise have been invisible.

**Photo lookup connection hygiene**

- **#153** `notifier._get_photo_result` now opens its fresh fallback
  connection via `database.connect()` instead of a bare
  `sqlite3.connect()`. Picks up the project's WAL / synchronous=NORMAL
  / mmap / busy_timeout pragmas — faster writes and 30s busy_timeout
  for collector contention.

**Data quality**

- **#156** `adsbx_enricher._parse_area_response` rejects any `hex` field
  that isn't exactly 6 lowercase hex chars (via new `_is_valid_icao_hex`
  helper). Prevents `~abcdef`-style anonymous-prefix strings and other
  malformed values from polluting the `adsbx_overrides` PK column.

**Test suite**: Python 1232 → 1245 (+13). Vitest unchanged.

## 2.1.4 — 2026-05-17

### Audit 12 Phase 2 — security hardening

Phase 2 of the post-v2 audit. Real-world risk reduction across SSRF,
SQL injection, info disclosure, and XSS surface area. All test-first.

**SSRF guard (http_safe)**

- **#167 + #168** Closed the DNS-rebinding TOCTOU in `safe_urlopen` and
  `safe_httpx_get`. `validate_url()` now resolves DNS via a captured
  `_real_getaddrinfo` and pins the validated infos in thread-local
  storage; a process-wide `socket.getaddrinfo` wrapper returns the
  pinned values for the same host within the same thread, so the
  subsequent fetch resolves to the same IPs we just verified. Other
  threads and other hostnames fall through to the real resolver
  unchanged. Pin is cleared in `finally` after every fetch.

**SQL injection defence-in-depth**

- **#169** Added `_BASELINE_ALLOWED_COLS = {"messages", "signal",
  "ac_with_pos"}` allowlist + `if column not in ...: raise ValueError`
  guards at the entry to `_baseline_avg` and `_recent_avg` in
  `health.py`. Both functions interpolate the column name into SQL via
  f-string; current callers pass literals, but the explicit boundary
  blocks the SQL-injection sink a future caller could otherwise open.

**Info disclosure (`/api/settings`)**

- **#171** Dropped `web_host` and `web_port` from the payload (the
  client is already at that URL; on a reverse-proxied deploy the bind
  host `0.0.0.0` would be misleading anyway). Masked filesystem paths:
  `airspace_geojson` and `stats_json` now return `(set)` / `(default)`
  / `(bundled poland.geojson)` rather than the actual paths. Frontend
  Settings page updated to match. Field names stay so the UI continues
  to show "configured".

**Frontend XSS surface**

- **#174** `react/no-danger` was declared in `eslint.config.mjs` but
  `eslint-plugin-react` was never loaded — the rule was silently
  inactive. Replaced with a Vitest grep test
  (`frontend/test/no-danger.test.ts`) that uses `import.meta.glob` to
  scan every `src/**/*.{ts,tsx}` source file at test time and fails
  the run if any of them contain `dangerouslySetInnerHTML`. Lighter
  than adding the eslint plugin; matches the project's "keep deps
  tight" stance.
- **#176** Hard-coerced `track` in `lib/aircraftIcon.ts`:
  `Math.round(Number.isFinite(Number(track)) ? Number(track) : 0)`
  before interpolating it into the `transform:rotate(${deg}deg)`
  inline style attribute. TypeScript declares `track` as `number |
  null | undefined`, but API drift or a hostile feed could land a
  string here — the coercion ensures CSS injection is impossible
  even under that scenario. Also fixes `isMilitary` to be a true
  boolean rather than a bitwise int.

**Open redirect / response splitting defence-in-depth**

- **#149** Extracted `_sanitize_v2_rest(rest)` helper from
  `_v2_compat` and added CR/LF scrubbing on top of the existing
  leading-`/`/`\\` strip. Starlette rejects raw CR/LF in path
  components today, but the helper is now unit-testable and the
  scrub provides belt-and-braces if a future ASGI server weakens
  that.

**Failure-notification reliability**

- **#166** `scripts/notify-telegram-failure.sh` now guards
  `RSBS_TELEGRAM_TOKEN` and `RSBS_TELEGRAM_CHAT_ID` with `:?` parameter
  expansion at the top of the script. Without it, `set -u` would still
  trip but deeper in the script, producing a confusing journal log
  with no actionable error.

**Test suite**: Python 1218 → 1232 (+14); Vitest 44 → 54 (+10). Both
suites green, frontend builds clean.

## 2.1.3 — 2026-05-17

### Audit 12 Phase 1 — invariant violations + latent footguns

Full post-v2 security/quality audit ran today (`internal_docs/security/audit-12-2026-05-17-post-v2.md`).
This release ships Phase 1 of the proposed sequence: documented-invariant breaks,
one user-visible data-loss bug, and latent footguns. Every fix is test-first; no
functional change for users beyond the `category` data quality improvement.

**Database**

- **#139** Moved the closed-flight `primary_source` backfill out of `_migrate()`
  into a new `_backfill_primary_source()` helper that runs in
  `run_background_migrations()`. The full-table UPDATE was holding the SQLite
  write lock during web startup, violating the documented
  "_migrate is web-hot-path; slow ops go to run_background_migrations" invariant.
- **#140** Added `CREATE TABLE IF NOT EXISTS` for `airports` and `callsign_routes`
  to `_migrate()`. Both tables existed in the DDL (collector path) but were
  missing from `_migrate()` (web path), so a web-only restart against an old
  DB would leave route_enricher writes and airport joins failing until the
  collector restarted.

**Collector**

- **#146** `_poll` now treats an explicit `seen_pos: null` (or missing field) as
  "stale → skip" rather than letting `None > MAX_SEEN_POS_SEC` raise a
  TypeError that the outer `except Exception` swallowed by aborting the whole
  poll cycle. One malformed aircraft entry no longer drops every other aircraft
  for that tick.
- **#144** `_update_flight_agg` now persists `category` via `COALESCE(category, ?)`.
  Previously `category` was set on the first INSERT only; readsb often emits
  it after the first position, leaving the flights row permanently NULL on
  category even though the data was available mid-flight.

**Enrichment cache**

- **#141** Added `_LRUDict.invalidate(key)` and `.clear_locked()` public methods.
  `enrichment.invalidate_adsbx` / `clear_cache` now use them instead of reaching
  into `_LRUDict._lock` / `.pop()` / `.clear()` directly. The previous pattern
  violated the documented "always use get_cached()/put()" contract from
  `src/readsbstats/CLAUDE.md`.

**Purge scripts**

- **#143 + #164** Guarded the empty-list `IN ()` SQL path in
  `purge_ghosts.max_distance_after_purge`, `purge_bad_gs._new_max_gs`, and
  `purge_mlat_gs_spikes._new_max_gs`. SQLite actually accepts `NOT IN ()`
  (audit was wrong about a crash), but standard SQL forbids it — same
  portability concern raised in Audit 11 #118.

**Test suite**: 1200 → 1214 (added 14 regression tests across
`test_database`, `test_purge_*`, `test_enrichment`, `test_collector`).

## 2.1.2 — 2026-05-17

### Docs — restructured documentation tree

Split the monolithic README into purpose-specific guides under `docs/`:

| New file | Contents |
|---|---|
| [`docs/configuration.md`](docs/configuration.md) | All 38 `RSBS_*` environment variables |
| [`docs/api.md`](docs/api.md) | All API endpoints, SPA routes, database schema |
| [`docs/integrations.md`](docs/integrations.md) | Telegram setup, bot commands, ghost/GS filtering |
| [`docs/operations.md`](docs/operations.md) | Updating, useful commands, backups |
| [`docs/development.md`](docs/development.md) | Local dev, tests, build, deploy |
| [`docs/decisions/`](docs/decisions/) | Architecture Decision Records (ADR 0001–0006) |

`README.md` trimmed from 697 to ~270 lines — installation and feature overview only; detailed
reference lives in the guides above.

`docs/piaware_install_ubuntu_24.04_arm64.md` removed (personal setup note, not a readsbstats
guide).

## 2.1.1 — 2026-05-17

### Security — open-redirect in /v2 compat handler (CodeQL py/url-redirection)

CodeQL alert
[#28](https://github.com/blindp3w/readsbstats/security/code-scanning/28)
flagged the `_v2_compat` route in `web.py` (introduced for v2.0.0-rc.1
bookmark compatibility): the captured `rest:path` segment was
interpolated into the Location header without sanitisation. A crafted
request to `/v2//evil.com` produced `Location: //evil.com`, which
browsers treat as a scheme-relative URL and follow off-site —
classical CWE-601 open redirect / phishing vector.

**Exploitable** only when `RSBS_ROOT_PATH=""` (dev mode, the `tests/ui/`
ASGI wrapper, or any deploy without nginx's `/stats` prefix). The
production setup uses `root_path=/stats` so the Location always starts
with `/stats/…` and the scheme-relative smuggle never lands. Fixed
anyway as defence in depth and to silence the static-analysis alert.

**Fix**: `rest.lstrip("/\\")` before constructing the target. Strips
both leading forward and back slashes (some browsers treat `\` as `/`
in URLs per the CodeQL guidance). One-line change in `web.py`. New
test `tests/test_web.py::TestSpaMount::test_v2_open_redirect_blocked`
covers five hostile inputs (`/v2//evil.com`, `/v2///evil.com`,
`/v2/\evil.com`, `/v2/\\evil.com`, `/v2//\evil.com`) and explicitly
sets `app.root_path=""` so the assertion fires against the actual
vulnerability shape, not the prod-config-shielded one.

## 2.1.0 — 2026-05-17

### Fixed — SPA favicon 404 + nginx-direct static serving

`/stats/favicon.svg` was returning 404. The Vite build emits
`frontend/dist/favicon.svg` at the root of `dist/`, but FastAPI's
`/assets` mount only covered `dist/assets/*`, and the SPA catch-all at
`/{spa_path:path}` (web.py:2491) **deliberately** 404s requests ending
in known asset extensions to surface deploy bugs instead of returning
HTML for them. So the file existed on disk and nothing served it.

Two changes:

1. **FastAPI fallback** — added an explicit `GET /favicon.svg` route in
   `web.py` that returns the dist file via `FileResponse` with
   `Cache-Control: public, max-age=86400`. Works on first deploy with
   no nginx changes required.

2. **nginx-direct static serving** — `nginx-readsbstats.conf` now
   serves `/stats/assets/` and `/stats/favicon.svg` from
   `/opt/readsbstats/frontend/dist/` via `alias` instead of proxying to
   FastAPI. One fewer hop per static request, lighter on uvicorn. The
   FastAPI mounts stay registered as a fallback for direct `:8080`
   access (tests, dev), so the nginx alias is a pure perf
   optimisation — not load-bearing.

The nginx-direct path requires `www-data` to be able to read
`/opt/readsbstats/frontend/dist/`. `scripts/update.sh`'s recursive
`chown root:readsbstats; chmod u=rwX,g=rX,o=` locks "other" out, so
`update.sh` now also runs `usermod -aG readsbstats www-data`
(idempotent) and restarts nginx on the first add — group membership
only applies at process start, not on `systemctl reload`. After that
one-time restart, subsequent deploys are silent (the `if !
id -nG | grep -qx readsbstats` guard short-circuits).

### Added — DuckDB analytical accelerator for /api/map/heatmap and /api/map/coverage

`/api/map/heatmap?window=30d` and `?window=all` previously returned 504
from nginx (single-threaded SQLite GROUP BY over millions of `positions`
rows exceeded the 60 s `proxy_read_timeout`). Both endpoints now route
heavy aggregates through DuckDB's `sqlite_scanner` extension attached
read-only to the live SQLite file. Same on-disk DB, no migration, no
write path changes — DuckDB is a query-time accelerator only.
Vectorised multi-core scans drop the worst case from 60 s+ to ~5–15 s
on a Pi 4 with 3.3 M positions.

New module: `src/readsbstats/analytics.py`. Lazy singleton DuckDB
connection, double-checked init under a lock, per-call cursors so
concurrent endpoints don't serialise on the connection's internal
mutex, path validator for `ATTACH` / `SET temp_directory` (DuckDB has
no parameter binding for either, so paths become SQL text), three-layer
failure handling (import / first-connection / per-query), all with
fall-through to the original SQLite query so the endpoints can't
regress if the engine is unavailable.

Web-side additions:

- Per-window `asyncio.Lock` single-flight wrappers on both endpoints so
  two concurrent cold-cache misses don't both spawn a full-table scan.
- Background **prewarmer thread** (`map-prewarm`, daemon) that refreshes
  all 8 (heatmap × coverage × {24h, 7d, 30d, all}) cache entries at
  half-TTL. Users always hit warm cache; refreshes run one at a time
  with a 10 s cool-off between heavy queries so the warmup doesn't
  starve the collector. Gated on `RSBS_PREWARM_MAP_CACHE` (default on
  when DuckDB is on).
- Eager init in the FastAPI lifespan: the ~1–2 s extension-load + ATTACH
  cost is paid during service startup rather than the first user hit.

Configuration knobs (all opt-in, defaults safe):

| Var | Default | Purpose |
|---|---|---|
| `RSBS_USE_DUCKDB` | `0` (off) | Master flag; flip to `1` after deploy soak |
| `RSBS_DUCKDB_MEMORY_MB` | `256` | DuckDB working-set cap |
| `RSBS_DUCKDB_THREADS` | `2` | Worker threads (matches web's `CPUQuota=50%`) |
| `RSBS_DUCKDB_HOME_DIR` | `/mnt/ext/readsbstats/duckdb-home` | Extension cache + DuckDB state (the `readsbstats` system user has no `/home`) |
| `RSBS_DUCKDB_TEMP_DIR` | `/mnt/ext/readsbstats/duckdb-tmp` | Spill directory for queries exceeding the memory cap |
| `RSBS_PREWARM_MAP_CACHE` | `1` | Background prewarmer enable |

`scripts/update.sh` now pre-fetches the `sqlite_scanner` extension
binary at deploy time so the first user hit after a service restart
doesn't pay the ~5 s HTTPS download to `extensions.duckdb.org`. The
binary is cached in `$RSBS_DUCKDB_HOME_DIR/.duckdb/extensions/`.

Dependency: `duckdb==1.5.2` added to `requirements.txt` and
`pyproject.toml` (pinned, required, ~25 MB aarch64 wheel).

Tests: 9 new (`tests/test_analytics.py`: 8 parity + behaviour tests
including `cutoff_ts=None` / boundary-safe coords / fallback when
analytics is unavailable / per-query exception doesn't poison the
engine / env-flag flip without restart / path-validator rejects
injection; `tests/test_web.py::TestMapPrewarmer::test_prewarm_one_populates_cache`).
1197 Python passing, 0 regressions.

Two SQLite ↔ DuckDB math divergences caught during testing and
documented inline in `analytics.py`:

1. `CAST(double AS INTEGER)` rounds (banker's) in DuckDB; truncates in
   SQLite. The coverage SQL uses `FLOOR()::INTEGER` explicitly so
   bucket assignment matches.
2. `round(x, n)` uses banker's rounding in DuckDB; SQLite is half-up.
   Tests use boundary-safe coordinates to avoid the ≤0.01 % of
   cell-boundary points where the two engines disagree.

### Changed — CI workflow swapped vanilla-JS tests for frontend build + Vitest

`.github/workflows/test.yml` previously ran `node --check static/js/*.js`
and `node --test tests/js/test_*.mjs` — both directories were deleted at
v2.0.0 cutover, so every CI run on `main` failed. Replaced with
`corepack enable && corepack prepare npm@11 --activate` +
`npm ci --no-audit --no-fund` + `npm run build` (tsc -b + vite build) +
`npm test` (Vitest, 43 tests), all inside `frontend/`. Gated to the
Python 3.12 matrix slot. corepack used to swap Node 22's broken bundled
npm 10.9.4 (`Cannot find module 'promise-retry'` on `-g install`).

### Added — `frontend/.npmrc` registry pin

Pins `registry=https://registry.npmjs.org/` so the lockfile resolves
against the public npm registry on every machine — local and CI alike.
Without this pin, `npm ci` on CI can hang ~72 s and exit with the
misleading `Exit handler never called!` error if the lockfile resolves
against a registry the CI runner can't reach.

### Fixed — `frontend/package-lock.json` regenerated against the public registry

Lockfile regenerated and CI hook tightened so future regenerations stay
clean. No code or feature change.

A local `.git/hooks/pre-commit` (not tracked) provides a backstop against
inadvertent reintroduction of non-public hostnames.

## 2.0.0 — 2026-05-16

### Removed — Jinja2 UI

The Jinja2 templates, vanilla JS, vendored uPlot/Leaflet assets, the v1
Playwright smoke file, and the `RSBS_ENABLE_V2` env-var kill-switch are
all deleted. The React SPA from v2.0.0-rc.1 is now the only UI, mounted
directly at `/stats/`. `static/airspace/` is the only surviving subdir
of `static/`.

### Changed — SPA mount moved from `/stats/v2/` to `/stats/`

Vite `base: '/stats/'`; React Router `basename: '/stats'`. The
`/stats/v2/*` URL space remains as a **301 redirect** to `/stats/*` so
v2.0.0-rc.1-era bookmarks keep working. `/live` is still a 302 to
`/map` (historical alias). The SPA catch-all is registered at the END
of `web.py` so it never shadows the `/api/*` routes.

### Added — nginx asset-cache + auto-reload in update.sh

`nginx-readsbstats.conf` gained a `/stats/assets/` nested location that
adds `expires 1y` + `Cache-Control: public, immutable` for the hashed
asset URLs. `index.html` continues to serve with `Cache-Control:
no-store` (every deploy rewrites the asset hashes inside it).
`scripts/update.sh` now runs `nginx -t && systemctl reload nginx` after
systemd daemon-reload, so deploys pick up nginx changes automatically.

### Tests

- **1190 Python + 43 Vitest + 81 Playwright = 1314** passing.
- The 69 vanilla-JS tests (`tests/js/`) and 35 v1 Playwright tests are
  deleted alongside the v1 surface they covered.

## 2.0.0-rc.1 — 2026-05-16

### Changed — web service memory cap raised (384M → 1024M)

`systemd/readsbstats-web.service` `MemoryMax` bumped from 384M to 1024M.
Driven by the v2 SPA's discoverable Heatmap toggle: `/api/map/heatmap` runs a
`GROUP BY round(lat, p), round(lon, p)` over the positions table, and the
SQLite in-memory sort/hash can spike to several hundred MB on a busy
receiver. The previous 384M cap got the worker OOM-killed mid-request.
Pi 4 has 8GB and steady-state utilisation around 1.7GB across all services,
so 1GB for the web worker is well within budget. Coverage and snapshot
endpoints unaffected. After the first hit per window the heatmap result is
cached (5 min for 24h, 30 min for 7d, 2h for 30d, 6h for all), so
subsequent toggles cost nothing.

### Added — v2 React SPA (coexists with the Jinja2 UI)

A complete React + Vite + TypeScript single-page app rebuild of the v1
Jinja2 UI ships alongside the original. Mounted at `/stats/v2/` whenever
`RSBS_ENABLE_V2=1` (default) AND `frontend/dist/` is built; otherwise the
mount silently doesn't register and the Jinja2 UI at `/stats/` is unaffected.
The Jinja2 UI is unchanged — no routes deleted, no behaviour modified.

**Stack:** React 19 + Compiler 1.0, Vite 7 (Rolldown), Tailwind CSS v4,
Radix UI primitives (Select, Dialog, Sheet, Popover, ToggleGroup, DropdownMenu,
Tooltip), TanStack Query v5, Zustand, React Router v7, Recharts,
react-leaflet 5 (Leaflet 1.9), Sonner toasts. React Compiler 1.0 enabled via
Babel plugin. Bundle: shell ~80 KB gz, vendor 34 KB, radix 30 KB, charts 112
KB lazy, leaflet 45 KB lazy; per-route chunks ≤ 8 KB.

**Pages shipped** (all at `/stats/v2/*` — same URL shape as v1):

- `/v2/` Statistics — summary cards, 24h/7d trend cards with delta arrows,
  flagged-flight counts, hourly + daily bar charts, DOW × hour heatmap, polar
  range plot, top types/airlines/countries/routes/airports, frequent + new
  aircraft, emergency squawks (clickable → history filter), personal records.
  Range picker with 24h/7d/30d/90d/All presets + Custom popover with
  datetime-local hour-precision pickers.
- `/v2/history` — filters (date range from/to, ICAO, callsign, registration,
  type, source, flag, squawk), sort headers, pagination, CSV export, URL
  state preservation, mobile-collapsing columns.
- `/v2/flight/{id}` — info card with photo + flag + source + squawk + airline,
  Leaflet route map with ADS-B / MLAT segment colouring + dark tile filter,
  Recharts altitude+speed ComposedChart, sampled positions log with RSSI
  colour bands, "Other flights by this aircraft" linked list.
- `/v2/aircraft/{icao}` — info card with photo, flag, country, first/last/
  duration, ✓ Watching / + Watch toggle (POST/DELETE `/api/watchlist`),
  full per-aircraft flights table.
- `/v2/gallery` — 1-row header (filter pills + sort icon Popover, both
  aligned right), card grid (60 per page), lazy photo loading, type-photo
  badge.
- `/v2/watchlist` — Add form, entries table, Radix Dialog delete confirmation,
  Sonner toast feedback, length-cap validation matching `database.WATCHLIST_*_MAX`.
- `/v2/feeders` — status table with manual refresh, "all-unavailable" notice,
  "not configured" empty state.
- `/v2/metrics` — 11 Recharts AreaChart panels (signal, aircraft, messages,
  range, positions, CPU, network out, network in, tracks, decoder, CPR),
  range picker matching `/v2/`, clickable health banner with per-check rows
  using a coloured left border for status (green/yellow/red/grey).
- `/v2/settings` — read-only display of all 39 runtime settings, secrets
  masked server-side via `_settings_payload()`.
- `/v2/map` — full-screen react-leaflet, Live + Rewind toggle, 10 s
  refetchInterval in Live mode, rewind slider with themed thumb,
  per-aircraft Sheet detail panel, **heatmap layer** (`leaflet.heat` on
  `/api/map/heatmap`) + 24h/7d/30d/all window selector, **coverage range
  overlay** (Leaflet `<Polygon>` on `/api/map/coverage`), **playback
  controls** (play/pause + ±10 m / ±1 h jump + 1×/2×/5×/10× speed
  buttons), **aircraft sidebar list** (left-side Sheet with 8-column
  sortable table — coexists with the right-side per-aircraft detail
  Sheet).

**Shared infrastructure:** Nav with brand, 8 links, units selector (Radix
Select), live aircraft count badge polling `/api/live` every 15 s (green dot
when active, click → `/map`), mobile hamburger.

**Backend additions (additive only, no v1 changes):**

- `GET /api/settings` — JSON mirror of `/settings` (single
  `_settings_payload()` source of truth).
- `GET /api/feeders` — JSON mirror of `/feeders`.
- `GET /api/flights` accepts `date_from` / `date_to` (YYYY-MM-DD, end-exclusive
  by adding 86400 to `date_to`) alongside the original single-`date` param.
  `date=` wins if both are set so v1 bookmarks keep working. Same params
  added to `/api/flights/export.csv`. Four pinning tests in
  `TestApiFlightsDateRange`.
- `RSBS_ENABLE_V2` env var (default `1`) — set to `0` for instant rollback;
  `web.py` SPA mount becomes a no-op and `/stats/v2/*` returns 404.

**Build & deploy:** `scripts/update.sh` aborts if `frontend/dist/index.html`
is older than anything under `frontend/src/` or if `package-lock.json` is
newer (unbuilt). Atomic-swap rsync: ship to `dist.new/`, mv server-side.
SPA `index.html` is served with `Cache-Control: no-store` (hashed asset URLs
inside change every deploy); assets get `public, immutable` via the optional
nginx asset-cache block.

**Tests:** 4 new Python tests (date range), 43 Vitest frontend unit tests
(format helpers, units store, CSRF wrapper, safe URL allowlist), 81 v2
Playwright tests across 6 device profiles covering every page (filter +
sort actually fire API calls, CSRF rejected without header, popover apply
writes URL params, live badge mounts, watch toggle round-trips, emergency
squawk links resolve, map rewind reveals slider, heatmap + coverage
toggles fire their API endpoints, sidebar list opens, playback advances
the slider, etc.). All v1 tests unchanged (1198 Python + 69 vanilla JS +
35 Playwright still pass).

**Plumbing notes for future cutover:**

- `tests/ui/_v2_app.py` — ASGI wrapper that strips `/stats/` from incoming
  paths for the v2 Playwright fixture (no nginx in tests).
- `useSearchParamBatch()` — multi-param URL updates must go through this,
  because React Router v7's `setSearchParams` reads stale `prev` when
  called twice in a row from the same handler. Single setters in onChange
  handlers stay fine.
- Telegram URLs stay pointed at `/stats/` (Jinja) throughout coexistence;
  flip at cutover commit 3.

**All 10 v2 pages reached full v1 parity** on 2026-05-16. No remaining
feature gaps before cutover. The four `/map` features (heatmap, coverage,
playback, sidebar) landed in the same session that hit parity.

**Post-cutover follow-ups** (tracked in
`internal_docs/uiux/v2-implementation-status.md` and
`internal_docs/internal/duckdb-analytics-plan.md`):
- DuckDB sqlite_scanner for analytical endpoints —
  `/api/map/heatmap?window=30d` currently times out at nginx (60 s) on
  busy receivers; DuckDB's vectorised multi-core GROUP BY over the same
  SQLite file should drop that to a few seconds. Deferred to post-cutover.
- v2.1 — `/metrics` to ECharts canvas + LTTB.
- v2.2 — stats heatmap + polar to ECharts native.
- v2.3 — `/map` to MapLibre GL v5.
- Mobile filter pane on `/v2/history`; scroll-fade mask on overflowing
  tables.

### Changed (breaking — env var rename, no back-compat shim)

- **`RSBS_BASE_URL` renamed to `RSBS_TELEGRAM_BASE_URL`** — and the
  corresponding `config.BASE_URL` attribute renamed to
  `config.TELEGRAM_BASE_URL`. The variable is only used to build profile /
  flight links in Telegram alerts, so the new name matches the rest of
  the Telegram-scoped namespace (`RSBS_TELEGRAM_TOKEN`,
  `RSBS_TELEGRAM_CHAT_ID`, `RSBS_TELEGRAM_UNITS`, `RSBS_TELEGRAM_PHOTOS`,
  `RSBS_TELEGRAM_ANONYMOUS_ALERT`). Deployments must update their systemd
  environment file at upgrade time — there is no fallback to the old name.
  README env-var table and the example systemd snippet both updated; the
  `/settings` page now displays the new env var name. No version bump
  required (release this with the next non-trivial change).

## 1.8.2 — 2026-05-13

Closes a small daily-summary coverage gap noted while back-filling the
FLAG_ANONYMOUS README documentation in v1.8.1: `send_daily_summary`
emitted Military and Interesting counts but never carried over the
**Anonymous** count, despite the rest of the codebase (badges, web UI,
`/api/stats`, first-sighting Telegram alerts) all treating anonymous
as a peer flag.

### Added

- **Anonymous count in the daily Telegram summary** — the summary's
  aggregate query now OR-merges `aircraft_db.flags` + `adsbx_overrides.flags`
  with the computed anonymous bit (via `icao_ranges.anonymous_flag_sql`),
  applies the same military > interesting > anonymous precedence used
  everywhere else (each flight counts under exactly one kind), and emits
  a new "Anonymous: N" badge alongside "Military: N" / "Interesting: N"
  when N > 0. Zero-suppression matches the existing badges. Four new
  tests in `TestSendDailySummary`: bare anonymous-badge presence, both
  precedence-exclusion directions (military and interesting suppress
  the anon count), and zero-suppression when no anon flights exist.
  Resolves a documentation-vs-behaviour drift surfaced during the v1.8.1
  README anonymous-aircraft pass.

### Changed

- README's daily-summary description (Telegram section) now lists
  `military/interesting/anonymous counts` with an explicit note about
  the shared precedence rule.

### Test counts

- Python: **1188 passing** (was 1184) — +4 from this release.
- JS: 69 passing (unchanged).
- Playwright UI: 35 (unchanged).

## 1.8.1 — 2026-05-13

Follow-up from the eleventh audit pass — bug fixes, a defensive refactor on
the Telegram path, test coverage for the in-process enrichment caches, and
five small cleanups. No new features; deploy is in-place.

### Fixed

- **`/api/metrics` returned HTTP 500 on non-integer `from` / `to`** — the
  handler called `int(request.query_params.get("from", ...))` directly, so
  garbage input bubbled up as `ValueError` and FastAPI mapped it to 500
  instead of 400. Switched to typed `Query(None, alias="from")` parameters
  so the validation layer rejects them at the boundary with 422. Five new
  tests in `tests/test_web.py::TestApiMetricsQueryValidation`.
- **Telegram `/unwatch <hex>` could delete a registration-typed watchlist
  entry with the same literal value** — `_watch_remove` ran `DELETE FROM
  watchlist WHERE value = ?` with no `match_type` filter. Now mirrors the
  `_watch_add` inference (`re.fullmatch(r"[0-9a-f]{6}", value)` → `icao`,
  else `registration`) and adds `AND match_type = ?` to the DELETE. The
  HTTP `DELETE /api/watchlist/{id}` endpoint remains the authoritative
  cross-type removal path. Three new tests pin the behaviour.
- **`scripts/notify-telegram-failure.sh` silently dropped alerts when the
  failing unit's journal contained `<`, `>`, or `&`** — Telegram's
  `parse_mode=HTML` returns 400 on those characters even inside text
  nodes, so a single weird-looking traceback would suppress the very
  alert you most needed. The shell script now pipes `systemctl status`
  output through `sed` to HTML-escape `&`/`<`/`>` (in that order — `&`
  first so it doesn't eat the entities the later substitutions emit)
  before interpolation into the `<pre>...</pre>` block.

### Changed

- **All Telegram outbound calls now route through `http_safe.safe_urlopen`**
  — `notifier._send`, `_send_photo`, and `_get_updates` previously used raw
  `urllib.request.urlopen`, bypassing the central SSRF guard's redirect
  blocker and response-size cap. To make this work for the POSTs,
  `safe_urlopen` gained an optional `data: bytes | None = None` parameter
  that flows into `urllib.request.Request`. All four policies (HTTPS-only,
  public-IP-only, no-redirect, size cap) now apply uniformly to every
  outbound call in the codebase, including `api.telegram.org`. If Telegram
  ever 302s during a region migration, the notifier will surface the
  redirect as a `ValueError` instead of following blindly. Four new tests
  in `test_http_safe.py` cover the POST capability; the existing 13 mock
  sites in `test_notifier.py` were rewritten to patch the new symbol.
- **`templates/base.html` active-nav match now requires a segment boundary**
  — `path.startsWith(href)` would have lit up `/history` on a future
  `/history-archive` path. Tightened to `path === href || (href !== "/"
  && path.startsWith(href + "/"))`. Cosmetic only — none of the current
  nav routes have this collision.
- **`<select>` option value `aero` renamed to `aeronautical`** — matches
  the existing `config.TELEGRAM_UNITS == "aeronautical"` literal in the
  Python notifier. `initUnitSelector()` carries a one-time migration that
  rewrites a stored `"aero"` in `localStorage` to `"aeronautical"` so
  existing users see the correct option highlighted in the dropdown after
  the rename. Frontend behaviour is unchanged in every other respect.
- **`_csrf_check` gained a load-bearing `# CRITICAL` comment** explaining
  that the X-Requested-With header check works *because* this app has no
  CORS middleware that whitelists custom headers. A future engineer
  adding `CORSMiddleware(allow_headers=["*"])` will now hit the warning
  at the diff level.

### Removed

- **Deleted dead code: `static/js/live.js` (116 lines) and
  `templates/live.html` (34 lines)** — the `/live` route has been a
  301-redirect to `/map` since v1.4.0 and never actually rendered the
  template. Pre-flight grep confirmed nothing else referenced them. The
  `/api/live` JSON endpoint (used by the nav live-badge) is independent
  and stays. Tests that probe the `/live` redirect (`test_web.py:750`,
  `test_map.py:228`, `tests/ui/test_mobile_smoke.py:148`) still pass.

### Tests

- **New `tests/test_enrichment.py` (20 tests)** — direct coverage for
  `_LRUDict` (basic put/get, None-as-value vs. miss, eviction at maxsize,
  LRU touch-to-end) and its thread safety (8-thread concurrent put + get
  with 1000 ops each + a concurrent-clear test). Plus negative-cache and
  positive-cache behaviour of `lookup_aircraft`, `lookup_airline`, and
  `lookup_adsbx`, the `invalidate_adsbx` busting path, and `clear_cache`
  resetting all three module-level caches. Closes the longest-standing
  test-coverage hole (the enrichment module was previously covered only
  transitively via collector / web tests).

### Internal cleanups

- Dropped unused `import math` / `import sys` from `scripts/purge_ghosts.py`,
  `scripts/purge_bad_gs.py`, and `scripts/purge_mlat_gs_spikes.py`.
- Tightened the "Background migrations — single owner" paragraph in
  `CLAUDE.md` to spell out which indexes belong in `_migrate()` (small
  `flights` table, e.g. `idx_flights_max_gs` / `idx_flights_max_alt`) vs
  in `run_background_migrations()` (heavy composite/partial indexes on
  the millions-row `positions` table).

### Test counts

- Python: **1184 passing** (was 1152) — +32 from this release.
- JS: 69 passing (unchanged).
- Playwright UI: 35 (unchanged).

## 1.8.0 — 2026-05-13

### Added

- **`FLAG_ANONYMOUS` — surface non-ICAO Mode-S addresses** — a new flag bit
  (16) is computed at query time from `src/readsbstats/icao_ranges.py` and
  OR-merged into every flag projection alongside `aircraft_db.flags` and
  `adsbx_overrides.flags`. An address gets the bit set when it falls outside
  every ICAO state-allocated block — typically military / OPSEC contacts,
  TIS-B / ADS-R rebroadcasts, and MLAT-synthetic identifiers. No DB column,
  no backfill: editing the state-allocation table retroactively reclassifies
  every historical flight on the next query. Motivated by a real-world
  sighting of hex `dd85cb` (a clean westbound transit across central Poland
  whose Mode-S address ADSBExchange flagged as "Non-ICAO hex (dynamic)").
  - **Filters and gallery** — `flags=anonymous` on `/api/flights` and
    `/api/aircraft/flagged` returns anon-only contacts (military / interesting
    take precedence under their own filters, matching the existing
    interesting/military exclusion pattern). The "All" tab on the flagged
    gallery now includes anonymous hits alongside military and interesting.
    New "Anonymous" filter button in `templates/gallery.html`.
  - **UI badge** — `flagBadge()` in `static/js/table-utils.js` gains a third
    branch with a `"?"` short label / `"Anonymous"` long label and a new
    `.badge-anon` CSS class. Precedence stays military > interesting >
    anonymous so the existing badges aren't disturbed.
  - **Stats page** — new "Anonymous" mini-stat alongside "Military" and
    "Interesting" inside the redesigned flagged-flights card.
  - **Telegram alert** — `notifier.notify_anonymous()` fires once per
    first-ever-sighting of a non-ICAO hex (Country line intentionally
    omitted since the address has no state by definition). Gated by
    `RSBS_TELEGRAM_ANONYMOUS_ALERT` (default `1`). `_load_notified()` is
    extended via a `LEFT JOIN` + the anon CASE so a restart doesn't re-fire
    historical anon alerts.
  - **Retention** — `_close_flight()` ghost-purge exemption is extended to
    keep single-position anonymous sightings (same precedent as military /
    interesting). The whole point of the flag is to surface edge-of-range
    contacts, so a one-sample track is exactly what we want to preserve.

### Changed

- **Stats page top-card redesign** — added a third mini-stat (Anonymous)
  next to Military and Interesting. To keep all summary cards at the same
  height, the flagged card now lays its three sub-stats out horizontally
  inside one card that spans two grid cells, with thin vertical separators
  between sub-cells. The standalone "Furthest detected" card is removed
  from the top strip (it remains in the Records section below).
- **`stats.js` adopts the shared `flagBadge()`** — two ad-hoc inline badge
  renderers in the "New aircraft" and "Frequent aircraft" sections were
  replaced with a single `flagBadge(flags, "short")` call so the new
  Anonymous badge propagates without three more copy-paste edits.

### Fixed

- **`icao_ranges._RAW` was missing Qatar (0x06A000–0x06A3FF) and South
  Sudan (0x06A400–0x06A7FF)** — the `FLAG_ANONYMOUS` audit on the live
  35 k-flight DB flagged 60+ Qatar Airways (A7-Bxx) aircraft as anonymous,
  which is a table-gap bug rather than a real anon contact. Added both
  allocations; the anon-flight count on the same DB dropped 181 → 46
  (97 unique → 33 unique aircraft) after the fix. Added a regression
  test pinning the new ranges.

### Operations

- **Perf, measured on the live 35 k-flight DB:** `/api/flights` page-1 with
  `_FLIGHT_COLS` = 22 ms; full-table stats scan with the anon CASE
  = 379 ms (cached 120 s); `_load_notified` startup scan = 133 ms. The
  CASE expression embeds ~10 KB of state-range conditions into every
  flag-projecting query, but SQLite's prepared-statement cache amortises
  parsing across calls because every call site uses the same string literal.
- **Audit the table when this feature surfaces a clean operator.** If
  the gallery shows a familiar callsign (e.g. `OMS681` SalamAir,
  `T7-WHK` San Marino) as anonymous, look up the hex on
  `https://hexdb.io/api/v1/aircraft/<hex>` first to decide whether it's a
  real anonymous contact or a missing state allocation worth adding to
  `_RAW`. The Qatar miss was the canonical example.

### Tests

- +44 Python tests and +7 JS tests. New Python classes / cases:
  `TestIsAnonymousIcao` (10), `TestAnonymousFlagSql` (4 — SQL/Python
  parity), `TestQatar*` (2 regression pins), `TestAnonymousFlagInResponse`
  (4 end-to-end via `/api/flights`),
  `TestApiFlaggedAircraft::test_filter_anonymous_only` +
  `test_all_filter_includes_anonymous`,
  `TestApiStats::test_anonymous_flights_counted_separately` and
  `test_stats_shape` extended,
  `test_close_flight_keeps_anonymous_hex_with_few_positions` +
  `test_enrich_sets_anonymous_flag_for_non_state_hex`,
  `TestLoadNotified::test_loads_anonymous_icao_without_aircraft_db_row` +
  `test_does_not_load_state_allocated_icao_without_flags`,
  `TestDispatchOne` (2 routing cases),
  `TestNotifyAnonymous` (5). JS: 7 `flagBadge` precedence cases in
  `tests/js/test_table_utils.mjs`. Total suite:
  **1152 Python + 69 JS + 35 Playwright UI**, all passing.

## 1.7.1 — 2026-05-12

### Fixed

- **nginx CSP blocks Wikipedia type photos** — `upload.wikimedia.org` was
  missing from the `img-src` directive in `nginx-readsbstats.conf`, causing
  browsers to block the new Wikipedia fallback images with a Content Security
  Policy violation. Added `https://upload.wikimedia.org` to `img-src`.
  Apply by reloading nginx: `sudo nginx -t && sudo systemctl reload nginx`.

### Tests

- Fix race condition in `test_all_three_emergency_squawks_trigger` — the test
  asserted `squawk_calls` immediately after three `_poll()` calls without
  waiting for the async consumer thread to drain the queue, so the last
  notification (7700) was consistently missing on CI. Added
  `_drain_notifications(timeout=1.0)` before the assertion, matching the
  pattern used in every other notification test. Hardened
  `test_emergency_squawk_not_repeated_same_flight` with the same drain call.

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
