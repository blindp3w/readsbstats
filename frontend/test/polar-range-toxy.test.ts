// Audit-13 Phase 6 round 2: PolarRange bearing→XY math.
//
// `polarToXY(bearing, distance, max, cx, cy, radius)` converts a
// compass bearing (0°=N, clockwise) + distance to SVG coordinates
// (+x=east, +y=south). Pinning the four cardinal directions plus a
// 45° intermediate locks the (-90°) rotation that aligns bearing-0
// with screen-north.

import { describe, it, expect } from 'vitest';
import { polarToXY } from '@/components/charts/chartMath';

// A standard test geometry: 200×200 canvas, centred, full-radius hit.
const CX = 100;
const CY = 100;
const RADIUS = 80;
const MAX = 100;

function expectClose(actual: [number, number], expected: [number, number]) {
  expect(actual[0]).toBeCloseTo(expected[0], 6);
  expect(actual[1]).toBeCloseTo(expected[1], 6);
}

describe('polarToXY — cardinal directions at full distance', () => {
  it('bearing 0 (north) sits directly above centre (smaller y)', () => {
    // North = (cx, cy - radius) in SVG coords.
    expectClose(polarToXY(0, MAX, MAX, CX, CY, RADIUS), [CX, CY - RADIUS]);
  });

  it('bearing 90 (east) sits to the right of centre (larger x)', () => {
    expectClose(polarToXY(90, MAX, MAX, CX, CY, RADIUS), [CX + RADIUS, CY]);
  });

  it('bearing 180 (south) sits below centre (larger y)', () => {
    expectClose(polarToXY(180, MAX, MAX, CX, CY, RADIUS), [CX, CY + RADIUS]);
  });

  it('bearing 270 (west) sits to the left of centre (smaller x)', () => {
    expectClose(polarToXY(270, MAX, MAX, CX, CY, RADIUS), [CX - RADIUS, CY]);
  });
});

describe('polarToXY — 45° intermediates', () => {
  it('bearing 45 (NE) sits in the upper-right quadrant', () => {
    const [x, y] = polarToXY(45, MAX, MAX, CX, CY, RADIUS);
    expect(x).toBeGreaterThan(CX); // east
    expect(y).toBeLessThan(CY); // north
    // |x - cx| ≈ |y - cy| ≈ radius / sqrt(2) ≈ 56.57
    expect(Math.abs(x - CX)).toBeCloseTo(RADIUS / Math.SQRT2, 4);
    expect(Math.abs(y - CY)).toBeCloseTo(RADIUS / Math.SQRT2, 4);
  });

  it('bearing 135 (SE) sits in the lower-right quadrant', () => {
    const [x, y] = polarToXY(135, MAX, MAX, CX, CY, RADIUS);
    expect(x).toBeGreaterThan(CX);
    expect(y).toBeGreaterThan(CY);
  });

  it('bearing 225 (SW) sits in the lower-left quadrant', () => {
    const [x, y] = polarToXY(225, MAX, MAX, CX, CY, RADIUS);
    expect(x).toBeLessThan(CX);
    expect(y).toBeGreaterThan(CY);
  });

  it('bearing 315 (NW) sits in the upper-left quadrant', () => {
    const [x, y] = polarToXY(315, MAX, MAX, CX, CY, RADIUS);
    expect(x).toBeLessThan(CX);
    expect(y).toBeLessThan(CY);
  });
});

describe('polarToXY — distance scaling', () => {
  it('zero distance returns the centre regardless of bearing', () => {
    for (const b of [0, 45, 90, 180, 270, 359]) {
      expectClose(polarToXY(b, 0, MAX, CX, CY, RADIUS), [CX, CY]);
    }
  });

  it('half distance produces half the radial offset', () => {
    expectClose(polarToXY(0, MAX / 2, MAX, CX, CY, RADIUS), [CX, CY - RADIUS / 2]);
    expectClose(polarToXY(90, MAX / 2, MAX, CX, CY, RADIUS), [CX + RADIUS / 2, CY]);
  });
});
