# MapLibre GL as the SPA map library

- Status: ACCEPTED
- Date: 2026-05-23

## Context

The v2.0.0 SPA shipped with `react-leaflet@5` + `leaflet@1.9` + `leaflet.heat@0.2`
across both map surfaces: the live aircraft view at `/stats/map` and the
per-flight route map at `/stats/flight/:id`. Three concrete problems
accumulated against this stack by v2.3.5:

- The royal-blue `leaflet.heat` overlay flooded the basemap once density
  exceeded ~0.2, making roads, place names, and terrain unreadable.
  Flagged during the v2 design review as a heatmap-legibility blocker.
- `leaflet.heat` has no TypeScript surface, forcing a
  `(L as any).heatLayer` cast in `LiveMap.tsx` — a known type-safety
  gap, deliberately deferred at the time.
- `aircraftIcon.ts` rotated planes via a CSS `transform:rotate(${deg}deg)`
  string interpolation. A prior security review fixed the immediate XSS
  edge by hard-coercing `track` to a number, but the string-template
  surface remained as a defence-in-depth concern.
- Pan/zoom on iPad Safari was choppy when both heatmap and aircraft
  markers were active — Leaflet's CPU/SVG renderer was the bottleneck.

ADR-0008 set the precedent for swapping a foundational frontend library
(Recharts → Apache ECharts): custom thin React wrapper, no second-party
React lib, explicit attribution in `THIRD_PARTY_NOTICES.md`.

## Decision

Migrate to **MapLibre GL JS 5** with **react-map-gl 8** as the React
binding (`react-map-gl/maplibre` endpoint).

- `maplibre-gl@^5.24.0` (BSD-3, ESM-only, WebGL2 required, no API key)
- `react-map-gl@^8.1.1` (MIT). v8 split into per-engine endpoints so
  importing from `react-map-gl/maplibre` does not pull mapbox shims.
- Basemap: **CartoDB Dark Matter** raster tiles (CC-BY 4.0, no key,
  OSM-derived). MapLibre has no `raster-invert` paint property and the
  existing `.map-tiles-dark` CSS filter cannot be reapplied to the
  WebGL canvas without also tinting data layers, so a native-dark tile
  provider is the cleanest path.
- Aircraft markers stay as HTML elements via react-map-gl `<Marker>`
  with a JSX `<svg>` child. Rotation moves from the SVG inline style
  to the typed `Marker.rotation` prop with `rotationAlignment="map"` —
  the prior string-template XSS surface is eliminated by
  construction.
- Heatmap is a native MapLibre `heatmap` style layer with a 6-stop
  **inferno-derived ramp** (perceptually uniform, monotonically
  increasing luminance, colorblind-safe). Replaces `leaflet.heat`.
- Coverage polygon, trail polyline, receiver marker (static ring +
  animated pulse + center dot) become GeoJSON sources + style layers.
  The receiver pulse uses `requestAnimationFrame` + `setPaintProperty`
  on circle-radius and circle-stroke-opacity.
- Coordinate-order swap (`[lat, lon]` Leaflet convention →
  `[lng, lat]` GeoJSON) is centralised in each component's `useMemo`,
  at the API boundary. The backend contract is unchanged.
- Tests: the wrappers (`LiveMap`, `RouteMap`) are globally mocked in
  `frontend/test/setup.ts` — symmetric with the existing `EChart`
  mock — so jsdom never tries to bring up a WebGL2 context.
- Selection (`zIndexOffset` has no MapLibre equivalent) uses
  `style.zIndex` on the `<Marker>` style override prop.
- `frontend/src/lib/aircraftIcon.ts` is renamed to `.tsx` and exposes
  `aircraftIconSvg(flags, type): React.ReactElement` instead of
  `aircraftIcon(track, flags, type): L.DivIcon`.

The migration shipped as three commits on `main` in v2.4.0:
1. RouteMap on MapLibre + new deps + chunk rename + CSP allowlist
2. LiveMap + inferno heatmap + receiver pulse + drop `leaflet.heat`
3. Drop remaining Leaflet deps + ADR + notices + version bump

The v2.4.0 release is the first user-visible cut.

## Consequences

What becomes easier:

- **Heatmap is finally legible.** The native MapLibre `heatmap` layer
  with the inferno ramp shows density across the full luminance range
  without obscuring the basemap. Closes the heatmap-legibility blocker
  from the v2 design review.
- **Aircraft rotation is typed.** No string interpolation anywhere in
  the call chain; `Marker.rotation` accepts a number, and
  `aircraftIcon.tsx` returns a JSX element. The string-template XSS
  surface is structurally gone.
- **Receiver marker pulse animates smoothly on the WebGL canvas**
  via `setPaintProperty` on a GeoJSON-backed circle layer. Implements
  the animated receiver-marker goal from the v2 design review.
- **Pan/zoom is GPU-accelerated.** Touch feel on iPad Safari is
  noticeably smoother with both heatmap and aircraft markers active.
- **`leaflet.heat`'s `(L as any).heatLayer` cast is gone.** Closes the
  deferred type-safety gap.
- **`FirstFitOnce` and `HeatmapLayer` useEffect-driven wrappers go
  away** — they're replaced by declarative `<Map initialViewState>` +
  `<Source><Layer/></Source>` pairs.

What becomes harder / costs:

- **Bundle: the `maps` chunk grew from ~45 KB gz (Leaflet) to ~283 KB gz
  (MapLibre)** — same order of magnitude as the v2.2.0 ECharts addition.
  Lazy-loaded by `/stats/map` and `/stats/flight/:id` only; shell,
  vendor, radix, and charts chunks are untouched.
- **CSP needs explicit `worker-src 'self' blob:`** — MapLibre bootstraps
  its tile-decoder Web Workers from blob URLs. Also `connect-src` (not
  just `img-src`) gates tile fetches, because MapLibre uses `fetch()`
  rather than `<img>`. The nginx CSP header was updated accordingly.
- **CartoDB tile provider is an external dependency** with its own
  attribution requirement and reliability story. If CartoDB ever goes
  away or changes terms, we swap the `tiles[]` URLs in
  `LiveMap.tsx` + `RouteMap.tsx` (single inline `DARK_STYLE` per
  component for now).
- **MapLibre is one large dependency graph.** Tree-shaking applies but
  there is no "core + plugins" split equivalent to ECharts' `echarts/core`.
  We accept the full ~195 KB gz baseline.
- **Custom rotation/anchor sets for HTML markers** mean we maintain the
  `aircraftIcon.tsx` JSX surface ourselves. The 4 SVG shapes (jet, light,
  heli, glider) are lifted unmodified from tar1090 (MIT).
- **jsdom unit tests cannot exercise the real map.** The wrappers are
  mocked at the component boundary; visual + interaction coverage is
  Playwright (e2e suite under `tests/ui/`) and manual deployment checks.
