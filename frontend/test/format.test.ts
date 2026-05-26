import { describe, it, expect } from 'vitest';
import { fmtAlt, fmtSpd, fmtDist, fmtDur, fmtBytes, fmtAgo, fmtTs } from '@/lib/format';

describe('fmtAlt', () => {
  it('returns — for null/undefined', () => {
    expect(fmtAlt(null, 'metric')).toBe('—');
    expect(fmtAlt(undefined, 'aeronautical')).toBe('—');
  });
  it('metric — ft to m, rounded', () => {
    // 1000 ft = 304.8 m → 305
    expect(fmtAlt(1000, 'metric')).toBe('305 m');
  });
  it('aeronautical — keeps feet', () => {
    expect(fmtAlt(1000, 'aeronautical')).toBe('1,000 ft');
  });
  it('imperial — also feet', () => {
    expect(fmtAlt(1000, 'imperial')).toBe('1,000 ft');
  });
});

describe('fmtSpd', () => {
  it('metric → km/h', () => {
    expect(fmtSpd(100, 'metric')).toBe('185 km/h');
  });
  it('imperial → mph', () => {
    expect(fmtSpd(100, 'imperial')).toBe('115 mph');
  });
  it('aeronautical → kts', () => {
    expect(fmtSpd(100, 'aeronautical')).toBe('100 kts');
  });
});

describe('fmtDist', () => {
  it('metric km', () => {
    expect(fmtDist(100, 'metric')).toBe('185.2 km');
  });
  it('imperial mi', () => {
    expect(fmtDist(100, 'imperial')).toBe('115.1 mi');
  });
  it('aeronautical nm', () => {
    expect(fmtDist(100, 'aeronautical')).toBe('100.0 nm');
  });
});

describe('fmtDur', () => {
  it.each([
    [30, '30s'],
    [60, '1m'],
    [90, '1m 30s'],
    [3600, '1h'],
    [3660, '1h 1m'],
    [7320, '2h 2m'],
  ])('%i sec → %s', (n, expected) => {
    expect(fmtDur(n)).toBe(expected);
  });
  it('returns — for null', () => {
    expect(fmtDur(null)).toBe('—');
  });
});

describe('fmtBytes', () => {
  it('B / KB / MB tiers', () => {
    expect(fmtBytes(512)).toBe('512 B');
    expect(fmtBytes(2048)).toBe('2.0 KB');
    expect(fmtBytes(2 * 1024 * 1024)).toBe('2.0 MB');
  });
});

describe('fmtAgo', () => {
  it('produces relative-time strings', () => {
    const now = 1_700_000_000;
    expect(fmtAgo(now, now)).toBe('0s ago');
    expect(fmtAgo(now - 30, now)).toBe('30s ago');
    expect(fmtAgo(now - 600, now)).toBe('10m ago');
    expect(fmtAgo(now - 7200, now)).toBe('2h ago');
    expect(fmtAgo(now - 86400 * 3, now)).toBe('3d ago');
  });
});

describe('fmtTs', () => {
  it('locale string for non-zero', () => {
    expect(fmtTs(1_700_000_000)).not.toBe('—');
  });
  it('— for null/undefined/NaN', () => {
    expect(fmtTs(null)).toBe('—');
    expect(fmtTs(undefined)).toBe('—');
    expect(fmtTs(Number.NaN)).toBe('—');
  });
  // Audit 2026-05-26: epoch 0 (1970-01-01T00:00:00Z) is a valid
  // timestamp and must format to a real string, not the em dash. The
  // earlier `if (!epoch)` shortcut treated 0 as missing.
  it('formats epoch 0 as a real string, not the missing-data dash', () => {
    expect(fmtTs(0)).not.toBe('—');
    expect(fmtTs(0)).toContain('1970');
  });
});

describe('fmtTs clockFormat', () => {
  // 2023-11-14 22:13:20 UTC — an evening hour in UTC so most timezones
  // (incl. Vitest's default UTC) land in the 13..23 range for 24h.
  const AFTERNOON_EPOCH = 1_700_000_000;

  it('null/undefined return dash regardless of format', () => {
    expect(fmtTs(null, '24h')).toBe('—');
    expect(fmtTs(undefined, '12h')).toBe('—');
  });

  // Audit 2026-05-26: epoch 0 is a valid datetime; the formatter must
  // honour the clock-format selector and emit a real string.
  it('epoch 0 renders with the chosen clock format', () => {
    expect(fmtTs(0, '24h')).not.toBe('—');
    expect(fmtTs(0, '24h')).toContain('1970');
  });

  it('12h and 24h outputs differ for an afternoon/evening epoch', () => {
    expect(fmtTs(AFTERNOON_EPOCH, '12h')).not.toBe(fmtTs(AFTERNOON_EPOCH, '24h'));
  });

  it('24h output contains a two-digit hour 13..23 (locale-robust)', () => {
    expect(fmtTs(AFTERNOON_EPOCH, '24h')).toMatch(/\b(1[3-9]|2[0-3]):/);
  });

  it('24h output does not contain a 1..9-then-colon pattern alone', () => {
    // 12h would emit "10:13:20 PM" or similar; 24h emits "22:13:20".
    // Negative assertion: 24h should NOT have a single-digit hour followed by colon.
    const s = fmtTs(AFTERNOON_EPOCH, '24h');
    expect(s).not.toMatch(/(^|\s)[1-9]:[0-5]\d/);
  });
});
