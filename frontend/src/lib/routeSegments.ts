// Pure transforms for the flight RouteMap, extracted so they're unit-testable
// (the map component itself is mocked to null in jsdom — see test/setup.ts).
//
// Coordinate-order swap: the backend returns positions as [lat, lon]; GeoJSON
// and MapLibre use [lng, lat]. The swap happens here, once, at this boundary.
//
// Known cosmetic limitation (audit 2026-06-15, A8): an isolated position whose
// source colour differs from its neighbours — or a length-1 run at the very
// start — is dropped from the line, because a LineString needs ≥2 vertices and
// a 1-vertex LineString is invalid GeoJSON. The effect is one ~pixel of the
// "wrong" colour on the adjacent segment; negligible for a flight track. If it
// ever matters, render isolated fixes as a CircleLayer rather than forcing a
// degenerate LineString.

export interface RoutePosition {
  ts: number;
  lat: number | null;
  lon: number | null;
  source_type: string | null;
}

export const ROUTE_ADSB_COLOR = '#22c55e';
export const ROUTE_MLAT_COLOR = '#eab308';
export const ROUTE_MIXED_COLOR = '#5b9af9';

// ADS-B vs MLAT vs mixed/unknown — MLAT gaps render in amber so users can spot
// multilateration stretches. `startsWith('adsb')` covers the readsb taxonomy
// (adsb_icao, adsb_icao_nt, …); bare 'mlat' is amber; everything else is mixed.
export function colorForSource(src: string | null | undefined): string {
  if (!src) return ROUTE_MIXED_COLOR;
  if (src.startsWith('adsb')) return ROUTE_ADSB_COLOR;
  if (src === 'mlat') return ROUTE_MLAT_COLOR;
  return ROUTE_MIXED_COLOR;
}

// Split a position list into contiguous same-source segments, each a coloured
// LineString feature. A colour change bridges the gap by seeding the next
// segment with the previous segment's last point, so adjacent segments touch.
export function buildRouteSegments(positions: RoutePosition[]): GeoJSON.FeatureCollection {
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
  return { type: 'FeatureCollection', features };
}

// Flat [lng, lat] list (lat/lon-null positions dropped) used for the initial
// view centre and the post-load fitBounds.
export function routePoints(positions: RoutePosition[]): [number, number][] {
  const out: [number, number][] = [];
  for (const p of positions) {
    if (p.lat == null || p.lon == null) continue;
    out.push([p.lon, p.lat]);
  }
  return out;
}
