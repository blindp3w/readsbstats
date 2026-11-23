// Pure helpers for VDL2-derived position fixes (/api/vdl2/positions[/{icao}]):
// the live-map popup text, the flight page's fix window + leg filtering, and the
// winds-aloft row selection. No React / map imports so they unit-test directly.

import type { Vdl2Position } from '@/lib/types';

// Window the live-map overlay requests and the legend advertises (minutes).
export const VDL2_MAP_WINDOW_MIN = 60;

// Padding around a flight when asking for its ACARS fixes (a fix's receive time can lag
// its embedded time, and the last report often follows touchdown), and the server's cap.
const FLIGHT_FIX_PAD_SEC = 600;
const FLIGHT_FIX_MAX_WINDOW_SEC = 48 * 3600;

export function vdl2SourceLabel(p: Pick<Vdl2Position, 'source' | 'label' | 'precise'>): string {
  switch (p.source) {
    case 'label':
      return p.label ? `ACARS label ${p.label}` : 'ACARS label';
    case 'h1-abs':
      return 'H1 ABS report';
    case 'h1-cf':
      return 'H1 CF report';
    case 'h1-pos':
      return 'H1 position report';
    case 'm1bpos':
      return 'FMS #M1BPOS';
    case '59g':
      return 'LOT 59,G report';
    case 'adsc':
      return 'ADS-C';
    case 'xid':
      return 'VDL2 link (coarse)';
  }
  // No / unknown source (older backend, or a source added later): degrade gracefully.
  if (p.source == null) {
    if (p.precise === false) return 'VDL2 link (coarse)';
    if (p.label) return `ACARS label ${p.label}`;
  }
  return 'ACARS position';
}

export interface Vdl2PointFormatters {
  fmtTs: (epoch: number | null | undefined) => string;
  fmtAlt: (ft: number | null | undefined) => string;
  fmtSpd: (kt: number | null | undefined) => string;
}

export interface Vdl2PointDescription {
  title: string;
  precise: boolean;
  rows: [string, string][];
}

const num = (v: number | null | undefined): v is number => typeof v === 'number' && Number.isFinite(v);

// Text for the map popup / route-map tooltip. Only rows with data are emitted; the
// formatters are injected so the app's unit + clock settings apply (useFormat()).
export function describeVdl2Point(p: Vdl2Position, f: Vdl2PointFormatters): Vdl2PointDescription {
  const rows: [string, string][] = [['Time', f.fmtTs(p.ts)]];
  if (p.label) rows.push(['Label', p.label]);
  if (num(p.alt)) rows.push(['Altitude', f.fmtAlt(p.alt)]);
  if (num(p.wind_dir) || num(p.wind_kt)) {
    const dir = num(p.wind_dir) ? `${Math.round(p.wind_dir)}°` : null;
    const spd = num(p.wind_kt) ? f.fmtSpd(p.wind_kt) : null;
    rows.push(['Wind', [dir, spd].filter(Boolean).join(' / ')]);
  }
  if (num(p.temp_c)) rows.push(['Temp', `${p.temp_c} °C`]);
  if (num(p.track)) rows.push(['Track', `${Math.round(p.track)}°`]);
  if (num(p.gs)) rows.push(['Speed', f.fmtSpd(p.gs)]);
  if (num(p.vs)) rows.push(['V/S', `${Math.round(p.vs)} fpm`]);
  return { title: vdl2SourceLabel(p), precise: p.precise !== false && p.source !== 'xid', rows };
}

// since/until (epoch s) for a flight's fix query: ±10 min around the flight, never wider
// than the server's 48 h cap — a longer flight keeps the LAST 48 h of it.
export function vdl2FlightPositionWindow(
  firstSeen: number,
  lastSeen: number,
): { since: number; until: number } {
  const until = lastSeen + FLIGHT_FIX_PAD_SEC;
  let since = firstSeen - FLIGHT_FIX_PAD_SEC;
  if (until - since > FLIGHT_FIX_MAX_WINDOW_SEC) since = until - FLIGHT_FIX_MAX_WINDOW_SEC;
  return { since, until };
}

// Keep only fixes inside the flight itself so the previous/next leg of the same airframe
// (caught by the ±10 min pad) doesn't leak onto this flight's map.
export function filterFixesToFlight(
  fixes: Vdl2Position[],
  firstSeen: number,
  lastSeen: number,
): Vdl2Position[] {
  return fixes.filter(
    (p) =>
      p.ts != null && p.ts >= firstSeen && p.ts <= lastSeen && p.lat != null && p.lon != null,
  );
}

// Fixes carrying temperature and/or wind (only the h1-abs CSV dialect), oldest first.
export function windsAloftRows(fixes: Vdl2Position[]): Vdl2Position[] {
  return fixes
    .filter((p) => num(p.temp_c) || num(p.wind_dir) || num(p.wind_kt))
    .sort((a, b) => (a.ts ?? 0) - (b.ts ?? 0));
}
