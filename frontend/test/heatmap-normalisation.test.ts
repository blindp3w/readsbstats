// Audit-13 Phase 6 round 2: ActivityHeatmap max-normalisation.
//
// `rampColor(count, max)` buckets a non-zero count into one of
// HEATMAP_RAMP's 5 stops based on count/max. This file pins the
// boundary behaviour — empty grid, single non-zero, mixed values —
// so a future refactor of the ramp or the bucketing math can't
// silently shift cell colours.

import { describe, it, expect } from 'vitest';
import { rampColor } from '@/components/charts/chartMath';
import { HEATMAP_RAMP } from '@/components/charts/theme';

describe('rampColor — empty/zero handling', () => {
  it('returns transparent when max is 0 (empty grid)', () => {
    expect(rampColor(0, 0)).toBe('transparent');
  });

  it('returns transparent when count is 0 with non-zero max', () => {
    expect(rampColor(0, 100)).toBe('transparent');
  });

  it('returns transparent for count=0 even when max=0 (degenerate)', () => {
    expect(rampColor(0, 0)).toBe('transparent');
  });
});

describe('rampColor — single-non-zero grid (count==max)', () => {
  it('maps count==max to the top stop', () => {
    // floor((100/100) * 5) = floor(5) = 5, clamped to 4.
    expect(rampColor(100, 100)).toBe(HEATMAP_RAMP[HEATMAP_RAMP.length - 1]);
  });

  it('count==max==1 still resolves to top stop', () => {
    expect(rampColor(1, 1)).toBe(HEATMAP_RAMP[HEATMAP_RAMP.length - 1]);
  });
});

describe('rampColor — mixed values across the ramp', () => {
  // With HEATMAP_RAMP length=5, stops are: [0–20%) [20–40%) [40–60%) [60–80%) [80–100%]
  // floor((count/max) * 5) maps fractional positions to indices 0..4.

  it('count at 0% (just above zero) maps to stop 0', () => {
    // count=1, max=100 → frac=0.01 → idx=floor(0.05)=0.
    expect(rampColor(1, 100)).toBe(HEATMAP_RAMP[0]);
  });

  it('count at 20% maps to stop 1', () => {
    // floor(0.20 * 5) = 1.
    expect(rampColor(20, 100)).toBe(HEATMAP_RAMP[1]);
  });

  it('count at 40% maps to stop 2', () => {
    expect(rampColor(40, 100)).toBe(HEATMAP_RAMP[2]);
  });

  it('count at 60% maps to stop 3', () => {
    expect(rampColor(60, 100)).toBe(HEATMAP_RAMP[3]);
  });

  it('count at 80% maps to stop 4 (top)', () => {
    expect(rampColor(80, 100)).toBe(HEATMAP_RAMP[4]);
  });
});

describe('rampColor — clamps overflow', () => {
  it('count > max never returns past the top stop', () => {
    // Defence-in-depth: rampColor's `Math.min(1, count/max)` and
    // `Math.min(HEATMAP_RAMP.length - 1, ...)` both clamp. A future
    // refactor that drops either should be caught here.
    expect(rampColor(150, 100)).toBe(HEATMAP_RAMP[HEATMAP_RAMP.length - 1]);
  });
});
