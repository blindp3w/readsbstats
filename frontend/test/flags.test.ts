import { describe, it, expect } from 'vitest';

import {
  FLAG_MILITARY,
  FLAG_INTERESTING,
  FLAG_PIA,
  FLAG_LADD,
  FLAG_ANONYMOUS,
  primaryFlagLabel,
} from '@/lib/flags';

// Audit-12 #210 — backend mirrors these constants in config.py. The Python
// side has thorough tests for FLAG_* parity; the JS side had none until now.
// Drift between sides corrupts the ?flags= URL filter (frontend writes
// "military", backend masks differently).

describe('FLAG_* constants', () => {
  it('match the backend bit values in config.py', () => {
    expect(FLAG_MILITARY).toBe(1);
    expect(FLAG_INTERESTING).toBe(2);
    expect(FLAG_PIA).toBe(4);
    expect(FLAG_LADD).toBe(8);
    expect(FLAG_ANONYMOUS).toBe(16);
  });

  it('are powers of 2 (each occupies one distinct bit)', () => {
    for (const v of [FLAG_MILITARY, FLAG_INTERESTING, FLAG_PIA, FLAG_LADD, FLAG_ANONYMOUS]) {
      // Single-bit means v & (v - 1) === 0.
      expect(v & (v - 1)).toBe(0);
    }
  });

  it('are mutually distinct', () => {
    const all = [FLAG_MILITARY, FLAG_INTERESTING, FLAG_PIA, FLAG_LADD, FLAG_ANONYMOUS];
    expect(new Set(all).size).toBe(all.length);
  });
});

describe('primaryFlagLabel — precedence: military > interesting > anonymous > none', () => {
  it('null / undefined / 0 → no label', () => {
    expect(primaryFlagLabel(null)).toBeNull();
    expect(primaryFlagLabel(undefined)).toBeNull();
    expect(primaryFlagLabel(0)).toBeNull();
  });

  it('military bit only', () => {
    expect(primaryFlagLabel(FLAG_MILITARY)).toBe('military');
  });

  it('interesting bit only', () => {
    expect(primaryFlagLabel(FLAG_INTERESTING)).toBe('interesting');
  });

  it('anonymous bit only', () => {
    expect(primaryFlagLabel(FLAG_ANONYMOUS)).toBe('anonymous');
  });

  it('military wins over interesting', () => {
    expect(primaryFlagLabel(FLAG_MILITARY | FLAG_INTERESTING)).toBe('military');
  });

  it('military wins over anonymous', () => {
    expect(primaryFlagLabel(FLAG_MILITARY | FLAG_ANONYMOUS)).toBe('military');
  });

  it('interesting wins over anonymous', () => {
    expect(primaryFlagLabel(FLAG_INTERESTING | FLAG_ANONYMOUS)).toBe('interesting');
  });

  it('military still wins with every other bit also set', () => {
    const all = FLAG_MILITARY | FLAG_INTERESTING | FLAG_PIA | FLAG_LADD | FLAG_ANONYMOUS;
    expect(primaryFlagLabel(all)).toBe('military');
  });

  it('PIA-only / LADD-only return null (not surfaced as a primary label)', () => {
    // PIA and LADD are filterable on the backend but not part of the
    // "primary label" precedence — they're additional context flags.
    expect(primaryFlagLabel(FLAG_PIA)).toBeNull();
    expect(primaryFlagLabel(FLAG_LADD)).toBeNull();
    expect(primaryFlagLabel(FLAG_PIA | FLAG_LADD)).toBeNull();
  });
});
