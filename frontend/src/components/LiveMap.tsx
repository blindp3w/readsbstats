import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Map,
  Source,
  Layer,
  Marker,
  AttributionControl,
  NavigationControl,
  type MapRef,
  type MarkerEvent,
} from 'react-map-gl/maplibre';
import type { StyleSpecification } from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import { aircraftIconSvg, getIconType } from '@/lib/aircraftIcon';

// Live aircraft layer for /v2/map. Receives the latest snapshot from the
// parent; renders aircraft as HTML <Marker>s with inline SVG, plus
// data-driven layers for the trail, coverage polygon, heatmap, and
// animated receiver pulse.
//
// Heavy: lazy-loaded by Map.tsx so other pages don't import MapLibre.
//
// PR #2 of the v2.4 MapLibre migration. Stack:
//   - maplibre-gl 5 + react-map-gl/maplibre 8
//   - CartoDB Dark Matter raster basemap (no CSS filter chain)
//   - Inferno heatmap palette via native `heatmap` layer
//   - Receiver pulse via static-ring + animated-pulse circle layers
//   - Aircraft markers via Marker.rotation + Marker.rotationAlignment="map"
//     (eliminates the audit-12 #176 string-template surface entirely)

export interface Aircraft {
  flight_id: number;
  icao_hex: string;
  callsign: string | null;
  registration: string | null;
  aircraft_type: string | null;
  category: string | null;
  primary_source: string | null;
  flags: number;
  origin_icao: string | null;
  dest_icao: string | null;
  lat: number | null;
  lon: number | null;
  ts: number;
  alt_baro: number | null;
  gs: number | null;
  track: number | null;
  source_type: string | null;
  seconds_ago: number;
  trail: [number, number, number][];
}

interface Props {
  aircraft: Aircraft[];
  receiverLat: number | null;
  receiverLon: number | null;
  selectedFlightId: number | null;
  onSelect: (a: Aircraft) => void;
  // Initial view; we only auto-fit on first load to avoid yanking the user
  // around mid-pan.
  initialCenter: [number, number] | null;
  // Optional overlays — when undefined / empty the layer doesn't render.
  // Coordinates use Leaflet convention [lat, lon, weight]; swapped to
  // GeoJSON [lng, lat] inside this component (single boundary).
  heatmapPoints?: [number, number, number][];
  coveragePolygon?: [number, number][];
  // VDL2 overlay (opt-in). `vdl2Positions` are structured ACARS position
  // reports (lat/lon already in [lat, lon] order from the API). `acarsActive`
  // is the set of icao_hex that transmitted ACARS recently — live aircraft in
  // the set get a 'talking now' ring. Both undefined ⇒ nothing renders.
  vdl2Positions?: {
    lat: number;
    lon: number;
    icao_hex: string | null;
    ts: number | null;
    label: string | null;
    // true = precise (~0.001°) Label-16 body fix; false = coarse (~0.1°) XID fix.
    precise?: boolean | null;
  }[];
  acarsActive?: Set<string>;
}

// CartoDB Dark Matter raster basemap — free, no API key, CC-BY 4.0.
// MapLibre does not expand Leaflet's `{s}` subdomain placeholder; list
// the four subdomains explicitly. Background layer fills the canvas
// between tile loads so there is no white flash.
const DARK_STYLE: StyleSpecification = {
  version: 8,
  sources: {
    'carto-dark': {
      type: 'raster',
      tiles: [
        'https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png',
        'https://b.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png',
        'https://c.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png',
        'https://d.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png',
      ],
      tileSize: 256,
      minzoom: 0,
      maxzoom: 20,
      attribution:
        '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> ' +
        'contributors, © <a href="https://carto.com/attributions">CARTO</a>',
    },
  },
  layers: [
    { id: 'bg', type: 'background', paint: { 'background-color': '#0b0b0d' } },
    // Lift the blacks on Dark Matter — its default range bottoms out at near-
    // pitch-black which is hard to read at any zoom. raster-brightness-min
    // pushes the floor up to a mid-charcoal; raster-contrast pulls back a
    // touch so the lift doesn't wash the basemap out. Data layers (heatmap,
    // polylines, circles, markers) are unaffected — they paint on top of
    // the raster layer with their own paint properties.
    {
      id: 'carto-dark',
      type: 'raster',
      source: 'carto-dark',
      paint: {
        'raster-brightness-min': 0.18,
        'raster-contrast': -0.1,
      },
    },
  ],
};

// Inferno-derived 6-stop heatmap ramp. Perceptually uniform, monotonically
// increasing luminance — colorblind-safe (encodes density via brightness
// not hue alone, per CLAUDE_DESIGN_BRIEF.md §M4.2 / §M1.2). Replaces today's
// near-monochromatic blue ramp from leaflet.heat.
const HEATMAP_PAINT = {
  'heatmap-weight': ['coalesce', ['get', 'weight'], 1] as unknown as number,
  'heatmap-color': [
    'interpolate',
    ['linear'],
    ['heatmap-density'],
    0,
    'rgba(0,0,0,0)',
    0.1,
    '#1b0c41',
    0.3,
    '#781c6d',
    0.5,
    '#bb3754',
    0.7,
    '#ed6925',
    1,
    '#fcffa4',
  ] as unknown as string,
  'heatmap-radius': 18,
  'heatmap-intensity': ['interpolate', ['linear'], ['zoom'], 0, 1, 9, 3] as unknown as number,
  'heatmap-opacity': ['interpolate', ['linear'], ['zoom'], 7, 0.85, 15, 0.4] as unknown as number,
};

const RECEIVER_COLOR = '#5b9af9';
const TRAIL_COLOR = '#5b9af9';
const COVERAGE_COLOR = '#a855f7';
const VDL2_COLOR = '#f59e0b'; // amber — distinct from receiver blue / coverage purple

export default function LiveMap({
  aircraft,
  receiverLat,
  receiverLon,
  selectedFlightId,
  onSelect,
  initialCenter,
  heatmapPoints,
  coveragePolygon,
  vdl2Positions,
  acarsActive,
}: Props) {
  const mapRef = useRef<MapRef | null>(null);

  // ─── First-fit-once ────────────────────────────────────────────────────
  // Today's <FirstFitOnce> set the view exactly once when a non-null
  // center first arrived. <Map initialViewState> only applies on mount —
  // if the receiver location arrives late, mirror the prior behavior with
  // an effect + `done` ref guard.
  // mapReady gates the first-fit on the map's load event: the effect must not
  // bail-and-give-up when mapRef isn't attached yet (the old code returned
  // early and never retried, leaving the map at the fallback view if the
  // receiver location resolved before the map mounted). audit 2026-06-15.
  const [mapReady, setMapReady] = useState(false);
  const firstFitDone = useRef(false);
  useEffect(() => {
    if (firstFitDone.current || !initialCenter || !mapReady) return;
    const map = mapRef.current;
    if (!map) return;
    // initialCenter is [lat, lon] (Leaflet convention); MapLibre wants
    // [lng, lat] for jumpTo.
    map.jumpTo({ center: [initialCenter[1], initialCenter[0]], zoom: 8 });
    firstFitDone.current = true;
  }, [initialCenter, mapReady]);

  // ─── Heatmap source (GeoJSON Points with weight property) ──────────────
  const heatmapGeoJSON = useMemo<GeoJSON.FeatureCollection>(() => {
    if (!heatmapPoints || heatmapPoints.length === 0) {
      return { type: 'FeatureCollection', features: [] };
    }
    return {
      type: 'FeatureCollection',
      features: heatmapPoints.map(([lat, lon, weight]) => ({
        type: 'Feature',
        properties: { weight },
        geometry: { type: 'Point', coordinates: [lon, lat] },
      })),
    };
  }, [heatmapPoints]);

  // ─── Coverage polygon ──────────────────────────────────────────────────
  // Backend returns [lat, lon][] (Leaflet); swap once at the boundary.
  // GeoJSON Polygon's outer ring must be closed (first point == last).
  const coverageGeoJSON = useMemo<GeoJSON.Feature | null>(() => {
    if (!coveragePolygon || coveragePolygon.length < 3) return null;
    const ring: [number, number][] = coveragePolygon.map(([lat, lon]) => [lon, lat]);
    if (
      ring.length > 0 &&
      (ring[0][0] !== ring[ring.length - 1][0] || ring[0][1] !== ring[ring.length - 1][1])
    ) {
      ring.push([ring[0][0], ring[0][1]]);
    }
    return {
      type: 'Feature',
      properties: {},
      geometry: { type: 'Polygon', coordinates: [ring] },
    };
  }, [coveragePolygon]);

  // ─── Defensive dedup by icao_hex ───────────────────────────────────────
  // The snapshot endpoint groups by flight_id (web.py::api_map_snapshot),
  // which is correct for ordinary live polling. But during Rewind / HIST
  // an aircraft can legitimately have multiple flight_id rows visible
  // within the 600s snapshot window — if the collector closed one flight
  // and opened a new one for the same icao_hex inside that window, both
  // would render as separate markers at potentially-different positions.
  // Keep only the freshest row per icao_hex.
  const dedupedAircraft = useMemo<Aircraft[]>(() => {
    // Plain object instead of `new Map(...)`: the `Map` identifier is
    // shadowed at the top of this file by the `Map` component from
    // react-map-gl/maplibre, and aliasing the global just for this
    // micro-optimization isn't worth it.
    const byIcao: Record<string, Aircraft> = {};
    for (const ac of aircraft) {
      const prev = byIcao[ac.icao_hex];
      if (!prev || ac.ts > prev.ts) byIcao[ac.icao_hex] = ac;
    }
    return Object.values(byIcao);
  }, [aircraft]);

  // ─── VDL2 position overlay ─────────────────────────────────────────────
  // Structured ACARS positions from /api/vdl2/positions ([lat, lon]); swap to
  // GeoJSON [lng, lat] once at the boundary. Sparse on an H1-dominated feed.
  const vdl2PositionsGeoJSON = useMemo<GeoJSON.FeatureCollection | null>(() => {
    if (!vdl2Positions || vdl2Positions.length === 0) return null;
    return {
      type: 'FeatureCollection',
      features: vdl2Positions
        .filter((p) => p.lat != null && p.lon != null)
        .map((p) => ({
          type: 'Feature',
          properties: { icao_hex: p.icao_hex, label: p.label, precise: !!p.precise },
          geometry: { type: 'Point', coordinates: [p.lon, p.lat] },
        })),
    };
  }, [vdl2Positions]);

  // ─── "Transmitting ACARS now" highlight ────────────────────────────────
  // A ring under each live aircraft whose icao_hex transmitted ACARS recently.
  // Built from the deduped live positions ∩ acarsActive — no marker DOM change
  // (the ring is a canvas circle layer, so it never rotates with the icon).
  const acarsHighlightGeoJSON = useMemo<GeoJSON.FeatureCollection | null>(() => {
    if (!acarsActive || acarsActive.size === 0) return null;
    const features = dedupedAircraft
      .filter((a) => a.lat != null && a.lon != null && acarsActive.has(a.icao_hex))
      .map((a) => ({
        type: 'Feature' as const,
        properties: {},
        geometry: { type: 'Point' as const, coordinates: [a.lon as number, a.lat as number] },
      }));
    return features.length > 0 ? { type: 'FeatureCollection', features } : null;
  }, [acarsActive, dedupedAircraft]);

  // ─── Selected aircraft trail ───────────────────────────────────────────
  // trail tuples are [lat, lon, ts]; convert + swap. Only the selected
  // aircraft gets a trail — drawing trails for all 30 markers spikes the
  // JS thread on every poll (audit-12 perf).
  const trailGeoJSON = useMemo<GeoJSON.Feature | null>(() => {
    if (selectedFlightId == null) return null;
    const sel = dedupedAircraft.find((a) => a.flight_id === selectedFlightId);
    if (!sel || sel.trail.length < 2) return null;
    return {
      type: 'Feature',
      properties: {},
      geometry: {
        type: 'LineString',
        coordinates: sel.trail.map(([lat, lon]) => [lon, lat]),
      },
    };
  }, [dedupedAircraft, selectedFlightId]);

  // ─── Receiver marker (GeoJSON Point) ───────────────────────────────────
  const receiverGeoJSON = useMemo<GeoJSON.Feature | null>(() => {
    if (receiverLat == null || receiverLon == null) return null;
    return {
      type: 'Feature',
      properties: {},
      geometry: { type: 'Point', coordinates: [receiverLon, receiverLat] },
    };
  }, [receiverLat, receiverLon]);

  // ─── Receiver pulse animation (rAF tied to map lifecycle) ──────────────
  // Mutates the `receiver-pulse` layer's circle-radius and
  // circle-stroke-opacity on every frame. CLAUDE_DESIGN_BRIEF M4.3:
  // 1.8s period, radius 12→36, stroke-opacity 0.6→0.
  useEffect(() => {
    if (receiverGeoJSON == null) return;
    let raf = 0;
    const PERIOD_MS = 1800;
    const start = performance.now();
    const tick = (now: number) => {
      const map = mapRef.current?.getMap();
      const t = ((now - start) % PERIOD_MS) / PERIOD_MS; // 0..1
      const radius = 12 + (36 - 12) * t;
      const opacity = 0.6 * (1 - t);
      if (map && map.isStyleLoaded() && map.getLayer('receiver-pulse')) {
        map.setPaintProperty('receiver-pulse', 'circle-radius', radius);
        map.setPaintProperty('receiver-pulse', 'circle-stroke-opacity', opacity);
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [receiverGeoJSON]);

  const initialView = useMemo(() => {
    const fallback: [number, number] =
      receiverLat != null && receiverLon != null ? [receiverLat, receiverLon] : [52, 21];
    const [lat, lon] = initialCenter ?? fallback;
    return { longitude: lon, latitude: lat, zoom: 8 };
  }, [initialCenter, receiverLat, receiverLon]);

  return (
    <Map
      ref={mapRef}
      mapStyle={DARK_STYLE}
      initialViewState={initialView}
      onLoad={() => setMapReady(true)}
      style={{ width: '100%', height: '100%' }}
      attributionControl={false}
    >
      <AttributionControl compact position="bottom-left" />
      <NavigationControl position="bottom-right" showCompass={false} />

      {/* Heatmap below all other overlays so polygons + markers paint on top. */}
      {heatmapGeoJSON.features.length > 0 && (
        <Source id="heatmap" type="geojson" data={heatmapGeoJSON}>
          {/* @ts-expect-error MapLibre paint expression types are looser than our tuples */}
          <Layer id="heatmap-layer" type="heatmap" paint={HEATMAP_PAINT} />
        </Source>
      )}

      {/* Coverage polygon — purple outline + 8% fill. */}
      {coverageGeoJSON && (
        <Source id="coverage" type="geojson" data={coverageGeoJSON}>
          <Layer
            id="coverage-fill"
            type="fill"
            paint={{ 'fill-color': COVERAGE_COLOR, 'fill-opacity': 0.08 }}
          />
          <Layer
            id="coverage-line"
            type="line"
            paint={{ 'line-color': COVERAGE_COLOR, 'line-width': 2 }}
          />
        </Source>
      )}

      {/* VDL2-derived positions — small amber dots (opt-in overlay). */}
      {vdl2PositionsGeoJSON && (
        <Source id="vdl2-positions" type="geojson" data={vdl2PositionsGeoJSON}>
          {/* Precise Label-16 body fixes render as solid amber dots; coarse
              (~11 km) XID fixes render as smaller hollow rings to signal lower
              confidence. Data-driven via the per-point `precise` property. */}
          <Layer
            id="vdl2-positions-dot"
            type="circle"
            paint={{
              'circle-radius': ['case', ['get', 'precise'], 4, 3],
              'circle-color': VDL2_COLOR,
              'circle-opacity': ['case', ['get', 'precise'], 0.9, 0],
              'circle-stroke-width': ['case', ['get', 'precise'], 1, 1.5],
              'circle-stroke-color': ['case', ['get', 'precise'], '#1c1917', VDL2_COLOR],
              'circle-stroke-opacity': ['case', ['get', 'precise'], 1, 0.7],
            }}
          />
        </Source>
      )}

      {/* "Transmitting ACARS now" ring under matching live aircraft. */}
      {acarsHighlightGeoJSON && (
        <Source id="vdl2-active" type="geojson" data={acarsHighlightGeoJSON}>
          <Layer
            id="vdl2-active-ring"
            type="circle"
            paint={{
              'circle-radius': 14,
              'circle-color': 'transparent',
              'circle-stroke-width': 2,
              'circle-stroke-color': VDL2_COLOR,
              'circle-stroke-opacity': 0.9,
            }}
          />
        </Source>
      )}

      {/* Selected aircraft trail. */}
      {trailGeoJSON && (
        <Source id="trail" type="geojson" data={trailGeoJSON}>
          <Layer
            id="trail-line"
            type="line"
            paint={{ 'line-color': TRAIL_COLOR, 'line-width': 2, 'line-opacity': 0.8 }}
            layout={{ 'line-cap': 'round', 'line-join': 'round' }}
          />
        </Source>
      )}

      {/* Receiver: animated pulse + static ring + filled dot, in that order. */}
      {receiverGeoJSON && (
        <Source id="receiver" type="geojson" data={receiverGeoJSON}>
          <Layer
            id="receiver-pulse"
            type="circle"
            paint={{
              'circle-radius': 12,
              'circle-color': 'transparent',
              'circle-stroke-width': 1.5,
              'circle-stroke-color': RECEIVER_COLOR,
              'circle-stroke-opacity': 0.6,
            }}
          />
          <Layer
            id="receiver-ring"
            type="circle"
            paint={{
              'circle-radius': 12,
              'circle-color': 'transparent',
              'circle-stroke-width': 2,
              'circle-stroke-color': RECEIVER_COLOR,
            }}
          />
          <Layer
            id="receiver-dot"
            type="circle"
            paint={{
              'circle-radius': 3,
              'circle-color': RECEIVER_COLOR,
            }}
          />
        </Source>
      )}

      {/* Aircraft markers — one Marker per craft. The component re-uses
          its DOM element across re-renders keyed by flight_id, so changing
          position / rotation / selection just diffs props. */}
      {dedupedAircraft.map((ac) => {
        if (ac.lat == null || ac.lon == null) return null;
        const iconType = getIconType(ac.category, ac.aircraft_type);
        const isSelected = ac.flight_id === selectedFlightId;
        return (
          <Marker
            key={ac.flight_id}
            longitude={ac.lon}
            latitude={ac.lat}
            // Hard-coerce to a number — TS says `track: number | null` but
            // upstream JSON could drift. NaN → 0 keeps the marker upright
            // and never reaches a CSS string interpolation (the API is
            // now a typed number prop).
            rotation={Number.isFinite(Number(ac.track)) ? Number(ac.track) : 0}
            rotationAlignment="map"
            anchor="center"
            onClick={(e: MarkerEvent<MouseEvent>) => {
              // Stop the click from also being a map click (which would
              // deselect via the page-level handler if we ever add one).
              e.originalEvent.stopPropagation();
              onSelect(ac);
            }}
            style={{ cursor: 'pointer', zIndex: isSelected ? 10 : 'auto' }}
          >
            {aircraftIconSvg(ac.flags, iconType)}
          </Marker>
        );
      })}
    </Map>
  );
}
