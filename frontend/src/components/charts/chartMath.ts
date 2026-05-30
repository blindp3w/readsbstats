// Audit-13 Phase 6 round 2: chart-math helpers extracted from
// `Heatmap.tsx` and `PolarRange.tsx` so the page files export only
// their component(s) — react-refresh/only-export-components hygiene
// (same pattern Audit-15 applied to chart option builders).

import { HEATMAP_RAMP } from './theme';

// Bucket a non-zero count into one of the ramp's 5 stops based on its
// fraction of `max`. Linear, inclusive on both ends: count==max → stop 4.
export function rampColor(count: number, max: number): string {
  if (max === 0 || count === 0) return 'transparent';
  const frac = Math.min(1, count / max);
  const idx = Math.min(HEATMAP_RAMP.length - 1, Math.floor(frac * HEATMAP_RAMP.length));
  return HEATMAP_RAMP[idx];
}

// Convert (bearing in degrees from N, clockwise) + distance to SVG
// coords. SVG axes: +x=east, +y=south. The (-90) rotation aligns
// bearing 0 with screen north (cos=0, sin=-1 → r * cos(a) = 0,
// r * sin(a) = -r → up). Bearing 90 (east) → cos(0) = 1, sin(0) = 0 →
// (cx + r, cy). And so on.
export function polarToXY(
  bearingDeg: number,
  distance: number,
  max: number,
  cx: number,
  cy: number,
  radius: number,
): [number, number] {
  const r = (distance / max) * radius;
  const a = ((bearingDeg - 90) * Math.PI) / 180;
  return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
}
