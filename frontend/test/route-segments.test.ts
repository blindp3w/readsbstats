/**
 * Unit tests for the pure RouteMap transforms (lib/routeSegments) — previously
 * untestable inline in the MapLibre component (mocked to null in jsdom).
 */
import { describe, it, expect } from 'vitest';
import {
  buildRouteSegments,
  colorForSource,
  routePoints,
  ROUTE_ADSB_COLOR,
  ROUTE_MLAT_COLOR,
  ROUTE_MIXED_COLOR,
  type RoutePosition,
} from '@/lib/routeSegments';

function pos(lat: number | null, lon: number | null, source_type: string | null): RoutePosition {
  return { ts: 0, lat, lon, source_type };
}

describe('colorForSource', () => {
  it('maps readsb adsb_* variants to the ADS-B colour', () => {
    expect(colorForSource('adsb_icao')).toBe(ROUTE_ADSB_COLOR);
    expect(colorForSource('adsb_icao_nt')).toBe(ROUTE_ADSB_COLOR);
  });
  it('maps mlat to the MLAT colour', () => {
    expect(colorForSource('mlat')).toBe(ROUTE_MLAT_COLOR);
  });
  it('maps null/unknown to the mixed colour', () => {
    expect(colorForSource(null)).toBe(ROUTE_MIXED_COLOR);
    expect(colorForSource(undefined)).toBe(ROUTE_MIXED_COLOR);
    expect(colorForSource('mode_s')).toBe(ROUTE_MIXED_COLOR);
  });
});

describe('routePoints', () => {
  it('swaps [lat,lon] → [lng,lat] and drops null coords', () => {
    const out = routePoints([pos(1, 10, 'adsb_icao'), pos(null, 20, 'adsb_icao'), pos(3, 30, 'mlat')]);
    expect(out).toEqual([
      [10, 1],
      [30, 3],
    ]);
  });
  it('returns [] for an empty list', () => {
    expect(routePoints([])).toEqual([]);
  });
});

describe('buildRouteSegments', () => {
  it('returns no features for an empty list', () => {
    expect(buildRouteSegments([]).features).toEqual([]);
  });

  it('emits one feature for a single-source track, coords swapped to [lng,lat]', () => {
    const fc = buildRouteSegments([pos(1, 10, 'adsb_icao'), pos(2, 20, 'adsb_icao')]);
    expect(fc.features).toHaveLength(1);
    expect(fc.features[0].properties?.color).toBe(ROUTE_ADSB_COLOR);
    expect((fc.features[0].geometry as GeoJSON.LineString).coordinates).toEqual([
      [10, 1],
      [20, 2],
    ]);
  });

  it('splits on a source change and bridges the gap with the previous last point', () => {
    const fc = buildRouteSegments([
      pos(1, 10, 'adsb_icao'),
      pos(2, 20, 'adsb_icao'),
      pos(3, 30, 'mlat'),
      pos(4, 40, 'mlat'),
    ]);
    expect(fc.features).toHaveLength(2);
    expect(fc.features[0].properties?.color).toBe(ROUTE_ADSB_COLOR);
    expect((fc.features[0].geometry as GeoJSON.LineString).coordinates).toEqual([
      [10, 1],
      [20, 2],
    ]);
    expect(fc.features[1].properties?.color).toBe(ROUTE_MLAT_COLOR);
    // Bridged: the amber segment begins at the green segment's last point.
    expect((fc.features[1].geometry as GeoJSON.LineString).coordinates).toEqual([
      [20, 2],
      [30, 3],
      [40, 4],
    ]);
  });

  it('skips positions with null lat/lon', () => {
    const fc = buildRouteSegments([
      pos(1, 10, 'adsb_icao'),
      pos(null, 20, 'adsb_icao'),
      pos(3, 30, 'adsb_icao'),
    ]);
    expect((fc.features[0].geometry as GeoJSON.LineString).coordinates).toEqual([
      [10, 1],
      [30, 3],
    ]);
  });

  it('documents the A8 limitation: a length-1 leading run loses its own colour', () => {
    // The single leading mlat point can't form a ≥2-vertex LineString, so it is
    // absorbed into the following ADS-B segment rather than getting an amber
    // feature. One feature, all green.
    const fc = buildRouteSegments([
      pos(1, 10, 'mlat'),
      pos(2, 20, 'adsb_icao'),
      pos(3, 30, 'adsb_icao'),
    ]);
    expect(fc.features).toHaveLength(1);
    expect(fc.features[0].properties?.color).toBe(ROUTE_ADSB_COLOR);
  });
});
