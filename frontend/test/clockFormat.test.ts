// Audit-13 Phase 6: `store/clockFormat.ts` direct unit tests.
// Mirrors `units.test.ts` shape. The store backs the user's 24h/12h
// preference, persisted to localStorage; App.tsx seeds it from
// /api/settings.time_format on first boot ONLY when storage is empty.

import { describe, it, expect, beforeEach } from 'vitest';
import {
  useClockStore,
  getClockFormat,
  hasStoredClockFormat,
  CLOCK_FORMAT_KEY,
} from '@/store/clockFormat';

beforeEach(() => {
  localStorage.clear();
  // Reset the in-memory store so a previous test's mutation doesn't leak
  // into the "no localStorage value" test below (the store's initial
  // value is captured at module-import time, not per-test).
  useClockStore.setState({ clockFormat: '24h' });
});

describe('clockFormat store', () => {
  it('defaults to 24h with no localStorage value', () => {
    useClockStore.setState({ clockFormat: '24h' });
    expect(getClockFormat()).toBe('24h');
  });

  it('persists changes to localStorage under the canonical key', () => {
    useClockStore.getState().setClockFormat('12h');
    expect(localStorage.getItem(CLOCK_FORMAT_KEY)).toBe('12h');
    expect(getClockFormat()).toBe('12h');
  });

  it('hasStoredClockFormat returns false on empty storage', () => {
    expect(hasStoredClockFormat()).toBe(false);
  });

  it('hasStoredClockFormat returns true after setClockFormat', () => {
    useClockStore.getState().setClockFormat('24h');
    expect(hasStoredClockFormat()).toBe(true);
  });

  it('does not blow up when localStorage throws (Safari private mode etc.)', () => {
    const orig = localStorage.setItem;
    localStorage.setItem = () => {
      throw new Error('QuotaExceeded');
    };
    try {
      expect(() => useClockStore.getState().setClockFormat('12h')).not.toThrow();
      // In-memory state still flips even if persistence failed.
      expect(getClockFormat()).toBe('12h');
    } finally {
      localStorage.setItem = orig;
    }
  });

  it('hasStoredClockFormat returns false when localStorage.getItem throws', () => {
    const orig = localStorage.getItem;
    localStorage.getItem = () => {
      throw new Error('SecurityError');
    };
    try {
      expect(hasStoredClockFormat()).toBe(false);
    } finally {
      localStorage.getItem = orig;
    }
  });
});
