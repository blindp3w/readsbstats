import { useEffect, useMemo, useRef } from 'react';
import {
  Map,
  Source,
  Layer,
  Marker,
  AttributionControl,
  NavigationControl,
  type MapRef,
} from 'react-map-gl/maplibre';
import { LngLatBounds } from 'maplibre-gl';
import type { StyleSpecification } from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import { routeFitKey } from '@/lib/routeFitKey';

// Flight route map. Each position becomes a vertex of a polyline; ADS-B vs
// MLAT segments are colored differently so users can spot multilateration
// gaps. Receiver location shown as a fixed marker.
//
// This module is lazy-loaded by Flight.tsx so other pages don't pull in
// MapLibre (~200 KB gz).
//
// PR #1 of the MapLibre migration (v2.4). Stack:
//   - maplibre-gl 5 + react-map-gl/maplibre 8
//   - CartoDB Dark Matter raster tiles (native dark, no CSS filter chain)
//   - GeoJSON line layer with data-driven color per segment

interface Position {
  ts: number;
  lat: number | null;
  lon: number | null;
  source_type: string | null;
}

interface Props {
  positions: Position[];
  receiverLat: number | null;
  receiverLon: number | null;
}

const ADSB_COLOR = '#22c55e';
const MLAT_COLOR = '#eab308';
const MIXED_COLOR = '#5b9af9';
const RECEIVER_COLOR = '#5b9af9';
// Route start matches the ADS-B track colour so the start marker
// visually 'belongs' to the dominant line colour. Aliased (not a fresh
// literal) so a future ADS-B colour tweak propagates here automatically.
const START_COLOR = ADSB_COLOR;
const END_COLOR = '#ef4444'; // danger red — "terminate"

function colorForSource(src: string | null | undefined): string {
  if (!src) return MIXED_COLOR;
  if (src.startsWith('adsb')) return ADSB_COLOR;
  if (src === 'mlat') return MLAT_COLOR;
  return MIXED_COLOR;
}

// CartoDB Dark Matter raster basemap — free, no API key, CC-BY 4.0.
// MapLibre does not expand Leaflet's `{s}` subdomain placeholder; list
// the four subdomains explicitly. Background color fills the canvas
// during tile load so there is no white flash.
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
    // touch so the lift doesn't wash the basemap out. Data layers paint on
    // top of the raster layer with their own paint properties and are
    // unaffected.
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

export default function RouteMap({ positions, receiverLat, receiverLon }: Props) {
  const mapRef = useRef<MapRef | null>(null);

  // Split positions into contiguous segments where source_type is constant —
  // gives per-segment color via one GeoJSON FeatureCollection rather than one
  // Source per pair-of-points. The line layer reads the color from each
  // feature's `properties.color` (data-driven expression below).
  //
  // Coordinate-order swap: backend returns `[lat, lon]`, MapLibre/GeoJSON
  // uses `[lng, lat]`. Apply here, exactly once, at the API boundary.
  const segmentsGeoJSON = useMemo(() => {
    const features: GeoJSON.Feature[] = [];
    let curCoords: [number, number][] = [];
    let curColor: string | null = null;
    for (const p of positions) {
      if (p.lat == null || p.lon == null) continue;
      const color = colorForSource(p.source_type);
      if (curColor === null) curColor = color;
      if (color !== curColor) {
        if (curCoords.length >= 2) {
          features.push({
            type: 'Feature',
            properties: { color: curColor },
            geometry: { type: 'LineString', coordinates: curCoords },
          });
        }
        curCoords = [curCoords[curCoords.length - 1] ?? [p.lon, p.lat]];
        curColor = color;
      }
      curCoords.push([p.lon, p.lat]);
    }
    if (curColor && curCoords.length >= 2) {
      features.push({
        type: 'Feature',
        properties: { color: curColor },
        geometry: { type: 'LineString', coordinates: curCoords },
      });
    }
    return { type: 'FeatureCollection' as const, features };
  }, [positions]);

  // Flat [lng, lat] array used for both the initial view center and the
  // post-load fitBounds. Computed once, then referenced.
  const allPoints = useMemo(() => {
    const out: [number, number][] = [];
    for (const p of positions) {
      if (p.lat == null || p.lon == null) continue;
      out.push([p.lon, p.lat]);
    }
    return out;
  }, [positions]);

  // fitBounds replaces today's react-leaflet `FitBounds` useEffect. Runs
  // whenever the track changes shape; a key derived from length + endpoints
  // skips redundant fits on prop identity churn.
  const lastFitKey = useRef<string | null>(null);
  useEffect(() => {
    if (allPoints.length === 0) return;
    // Key on count + both coordinates of the endpoints (see lib/routeFitKey) —
    // longitude alone collided on tracks sharing endpoint longitudes (BUG-2).
    const key = routeFitKey(allPoints);
    if (key === lastFitKey.current) return;
    lastFitKey.current = key;
    const bounds = allPoints.reduce(
      (b, [lng, lat]) => b.extend([lng, lat]),
      new LngLatBounds(allPoints[0], allPoints[0]),
    );
    mapRef.current?.fitBounds(bounds, { padding: 20, duration: 0 });
  }, [allPoints]);

  if (allPoints.length === 0 && receiverLat == null) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-[var(--color-text-dim)]">
        no positions to plot
      </div>
    );
  }

  const initialCenter: [number, number] =
    allPoints[0] ??
    (receiverLat != null && receiverLon != null ? [receiverLon, receiverLat] : [0, 0]);

  return (
    <Map
      ref={mapRef}
      mapStyle={DARK_STYLE}
      initialViewState={{ longitude: initialCenter[0], latitude: initialCenter[1], zoom: 9 }}
      scrollZoom={false}
      attributionControl={false}
      style={{ width: '100%', height: '100%', borderRadius: '0.25rem' }}
    >
      <AttributionControl compact position="bottom-right" />
      {/* Zoom + / − in top-right. Compass hidden — bearing rotation
          isn't relevant for a 2D flight route. */}
      <NavigationControl position="top-right" showCompass={false} />
      {segmentsGeoJSON.features.length > 0 && (
        <Source id="route" type="geojson" data={segmentsGeoJSON}>
          <Layer
            id="route-line"
            type="line"
            paint={{
              'line-color': ['get', 'color'],
              'line-width': 2,
              'line-opacity': 0.85,
            }}
            layout={{ 'line-cap': 'round', 'line-join': 'round' }}
          />
        </Source>
      )}

      {receiverLat != null && receiverLon != null && (
        <Marker longitude={receiverLon} latitude={receiverLat} anchor="center">
          <div
            aria-label="Receiver"
            title="Receiver"
            style={{
              width: 12,
              height: 12,
              borderRadius: '50%',
              backgroundColor: RECEIVER_COLOR,
              border: '2px solid #fff',
              boxSizing: 'border-box',
            }}
          />
        </Marker>
      )}

      {/* Start marker — green circle at the FIRST plotted position.
          Same circular shape as the receiver dot; color (green =
          ADS-B-track convention) distinguishes role. */}
      {allPoints.length >= 1 && (
        <Marker longitude={allPoints[0][0]} latitude={allPoints[0][1]} anchor="center">
          <div
            aria-label="Route start"
            title="Route start"
            data-testid="route-marker-start"
            style={{
              width: 12,
              height: 12,
              borderRadius: '50%',
              backgroundColor: START_COLOR,
              border: '2px solid #fff',
              boxSizing: 'border-box',
            }}
          />
        </Marker>
      )}

      {/* End marker — red circle at the LAST plotted position. */}
      {allPoints.length >= 2 && (
        <Marker
          longitude={allPoints[allPoints.length - 1][0]}
          latitude={allPoints[allPoints.length - 1][1]}
          anchor="center"
        >
          <div
            aria-label="Route end"
            title="Route end"
            data-testid="route-marker-end"
            style={{
              width: 12,
              height: 12,
              borderRadius: '50%',
              backgroundColor: END_COLOR,
              border: '2px solid #fff',
              boxSizing: 'border-box',
            }}
          />
        </Marker>
      )}
    </Map>
  );
}
