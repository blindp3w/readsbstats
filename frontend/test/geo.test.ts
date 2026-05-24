import { describe, it, expect } from 'vitest';
import { haversineNm, bearingFromReceiver, EARTH_RADIUS_NM } from '@/lib/geo';

// Known fixtures cross-checked against backend src/readsbstats/geo.py.

describe('haversineNm', () => {
  it('returns 0 for identical points', () => {
    expect(haversineNm(52.0, 21.0, 52.0, 21.0)).toBeCloseTo(0, 6);
  });

  it('Warsaw → Krakow ≈ 135 nm', () => {
    // WAW 52.166°N 20.967°E ; KRK 50.078°N 19.785°E
    const d = haversineNm(52.166, 20.967, 50.078, 19.785);
    expect(d).toBeGreaterThan(130);
    expect(d).toBeLessThan(140);
  });

  it('Warsaw → JFK ≈ 3700 nm', () => {
    // WAW 52.166°N 20.967°E ; JFK 40.640°N -73.778°E
    // Great-circle ~3697 nm. Verified against backend src/readsbstats/geo.py.
    const d = haversineNm(52.166, 20.967, 40.640, -73.778);
    expect(d).toBeGreaterThan(3650);
    expect(d).toBeLessThan(3750);
  });

  it('antipode of a point is half-circumference away', () => {
    const half = Math.PI * EARTH_RADIUS_NM;
    const d = haversineNm(0, 0, 0, 180);
    expect(d).toBeCloseTo(half, 0);
  });
});

describe('bearingFromReceiver', () => {
  it('due North is 0°', () => {
    // From equator, point 10° N stays at the same lon → bearing 0.
    expect(bearingFromReceiver(0, 0, 10, 0)).toBeCloseTo(0, 4);
  });

  it('due South is 180°', () => {
    expect(bearingFromReceiver(0, 0, -10, 0)).toBeCloseTo(180, 4);
  });

  it('due East is 90°', () => {
    expect(bearingFromReceiver(0, 0, 0, 10)).toBeCloseTo(90, 4);
  });

  it('due West is 270°', () => {
    expect(bearingFromReceiver(0, 0, 0, -10)).toBeCloseTo(270, 4);
  });

  it('NE quadrant is between 0 and 90', () => {
    const b = bearingFromReceiver(0, 0, 10, 10);
    expect(b).toBeGreaterThan(0);
    expect(b).toBeLessThan(90);
  });

  it('returns values in [0, 360)', () => {
    for (const [lat, lon] of [[-10, -10], [10, -10], [-10, 10], [10, 10]] as const) {
      const b = bearingFromReceiver(0, 0, lat, lon);
      expect(b).toBeGreaterThanOrEqual(0);
      expect(b).toBeLessThan(360);
    }
  });
});
