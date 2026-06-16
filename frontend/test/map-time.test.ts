/**
 * Unit tests for lib/mapTime — the pure date/time helpers extracted from
 * pages/Map.tsx. They do local-time math, so the round-trip assertions go
 * through composeUnix/unixToISO/unixToHHMM rather than hard-coding epoch
 * seconds (which would be timezone-dependent).
 */
import { describe, it, expect } from 'vitest';
import {
  describeRewind,
  pad2,
  unixToISO,
  unixToHHMM,
  composeUnix,
  startOfDayLocal,
  clampHist,
} from '@/lib/mapTime';

describe('describeRewind', () => {
  it('labels zero as Now', () => {
    expect(describeRewind(0)).toBe('Now');
  });
  it('formats hours and minutes', () => {
    expect(describeRewind(2 * 3600 + 5 * 60)).toBe('2h 5m ago');
  });
  it('formats hours only', () => {
    expect(describeRewind(3 * 3600)).toBe('3h ago');
  });
  it('falls back to seconds under a minute', () => {
    expect(describeRewind(30)).toBe('30s ago');
  });
});

describe('pad2', () => {
  it('zero-pads single digits', () => {
    expect(pad2(0)).toBe('00');
    expect(pad2(7)).toBe('07');
    expect(pad2(12)).toBe('12');
  });
});

describe('composeUnix round-trips with unixToISO / unixToHHMM', () => {
  it('parses a date+time and reads it back identically (local time)', () => {
    const sec = composeUnix('2026-05-18', '14:30');
    expect(sec).not.toBeNull();
    expect(unixToISO(sec!)).toBe('2026-05-18');
    expect(unixToHHMM(sec!)).toBe('14:30');
  });

  it('round-trips midnight', () => {
    const sec = composeUnix('2026-01-01', '00:00');
    expect(sec).not.toBeNull();
    expect(unixToISO(sec!)).toBe('2026-01-01');
    expect(unixToHHMM(sec!)).toBe('00:00');
  });

  it('returns null on a bad date or time', () => {
    expect(composeUnix('2026-02-31', '12:00')).toBeNull();
    expect(composeUnix('2026-05-18', 'nope')).toBeNull();
  });
});

describe('startOfDayLocal', () => {
  it('snaps a timestamp to local midnight (00:00 same date)', () => {
    const noon = composeUnix('2026-05-18', '12:34')!;
    const start = startOfDayLocal(noon);
    expect(unixToISO(start)).toBe('2026-05-18');
    expect(unixToHHMM(start)).toBe('00:00');
  });

  it('is idempotent (midnight maps to itself)', () => {
    const midnight = composeUnix('2026-05-18', '00:00')!;
    expect(startOfDayLocal(midnight)).toBe(midnight);
  });
});

describe('clampHist', () => {
  const now = 1_000_000;
  const maxRewind = 24 * 3600;

  it('passes through a value inside the window', () => {
    const v = now - 3600;
    expect(clampHist(v, now, maxRewind)).toBe(v);
  });

  it('clamps a future value down to nowSec', () => {
    expect(clampHist(now + 5000, now, maxRewind)).toBe(now);
  });

  it('clamps a too-old value up to nowSec - maxRewindSec', () => {
    expect(clampHist(now - maxRewind - 5000, now, maxRewind)).toBe(now - maxRewind);
  });

  it('keeps the exact bounds', () => {
    expect(clampHist(now, now, maxRewind)).toBe(now);
    expect(clampHist(now - maxRewind, now, maxRewind)).toBe(now - maxRewind);
  });
});
