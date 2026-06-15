import { describe, it, expect } from 'vitest';
import { parseYMD } from '@/lib/dateParse';

describe('parseYMD', () => {
  it('parses a valid YYYY-MM-DD into zero-based-month parts', () => {
    expect(parseYMD('2026-05-18')).toEqual({ y: 2026, mo: 4, d: 18 });
  });

  it('rejects an impossible date (Feb 31) instead of rolling over to March', () => {
    expect(parseYMD('2026-02-31')).toBeNull();
  });

  it('rejects a bad format', () => {
    expect(parseYMD('2026-5-1')).toBeNull();
    expect(parseYMD('foo')).toBeNull();
    expect(parseYMD('')).toBeNull();
  });

  it('rejects a 2-digit-year rollover (0099 → 1999 in JS Date)', () => {
    expect(parseYMD('0099-01-01')).toBeNull();
  });
});
