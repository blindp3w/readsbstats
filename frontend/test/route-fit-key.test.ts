import { describe, it, expect } from 'vitest';
import { routeFitKey } from '@/lib/routeFitKey';

// BUG-2: the RouteMap fitBounds cache key previously keyed only on point count
// + first/last *longitude*. Two tracks that share endpoint longitudes but
// differ in latitude collided, so the map skipped re-fitting. The key must
// fold in latitude at both endpoints.

describe('routeFitKey', () => {
  it('differs for two point-sets with identical endpoint longitudes but different latitudes', () => {
    // [lng, lat] pairs. Same count, same first/last longitude (10 … 20),
    // only the latitudes differ.
    const a: [number, number][] = [
      [10, 50],
      [15, 51],
      [20, 52],
    ];
    const b: [number, number][] = [
      [10, 40],
      [15, 41],
      [20, 42],
    ];
    expect(routeFitKey(a)).not.toBe(routeFitKey(b));
  });

  it('is identical for identical tracks', () => {
    const a: [number, number][] = [
      [10, 50],
      [20, 52],
    ];
    const b: [number, number][] = [
      [10, 50],
      [20, 52],
    ];
    expect(routeFitKey(a)).toBe(routeFitKey(b));
  });

  it('differs when only the count changes', () => {
    const a: [number, number][] = [
      [10, 50],
      [20, 52],
    ];
    const b: [number, number][] = [
      [10, 50],
      [15, 51],
      [20, 52],
    ];
    expect(routeFitKey(a)).not.toBe(routeFitKey(b));
  });

  it('returns a stable empty-track key for zero points', () => {
    expect(routeFitKey([])).toBe(routeFitKey([]));
  });
});
