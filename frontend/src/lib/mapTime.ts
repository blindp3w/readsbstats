// Pure date/time helpers for the map playback bar. Extracted verbatim from
// pages/Map.tsx so they can be unit-tested without mounting the page.
//
// These do LOCAL-time date math on purpose (the date/time pickers and the
// scrubber day-window are in the user's timezone), so they are DST-correct —
// they round-trip through the platform `Date`, never UTC arithmetic.

import { parseYMD } from '@/lib/dateParse';

// Human label for a rewind offset in seconds: "Now", "2h 5m ago", "30s ago".
export function describeRewind(sec: number): string {
  if (sec === 0) return 'Now';
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const parts: string[] = [];
  if (h > 0) parts.push(`${h}h`);
  if (m > 0) parts.push(`${m}m`);
  if (parts.length === 0) parts.push(`${Math.round(sec)}s`);
  return `${parts.join(' ')} ago`;
}

export function pad2(n: number): string {
  return n.toString().padStart(2, '0');
}

export function unixToISO(sec: number): string {
  const d = new Date(sec * 1000);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

export function unixToHHMM(sec: number): string {
  const d = new Date(sec * 1000);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

export function composeUnix(dateISO: string, timeHHMM: string): number | null {
  const p = parseYMD(dateISO);
  const tm = /^(\d{1,2}):(\d{2})$/.exec(timeHHMM);
  if (!p || !tm) return null;
  const d = new Date(p.y, p.mo, p.d, Number(tm[1]), Number(tm[2]), 0, 0);
  return Math.floor(d.getTime() / 1000);
}

export function startOfDayLocal(sec: number): number {
  const d = new Date(sec * 1000);
  d.setHours(0, 0, 0, 0);
  return Math.floor(d.getTime() / 1000);
}

// Clamp a HIST timestamp into the allowed scrub window [nowSec - maxRewindSec,
// nowSec]. Previously a closure over the page's `nowSecForBounds` /
// `MAX_REWIND_SEC`; now takes its bounds as args so it stays pure.
export function clampHist(v: number, nowSec: number, maxRewindSec: number): number {
  return Math.max(nowSec - maxRewindSec, Math.min(nowSec, v));
}
