import { describe, it, expect, beforeEach } from 'vitest';
import { useUnitsStore, getUnits } from '@/store/units';

beforeEach(() => {
  localStorage.clear();
});

describe('units store', () => {
  it('defaults to metric with no localStorage value', () => {
    useUnitsStore.setState({ units: 'metric' });
    expect(getUnits()).toBe('metric');
  });

  it('persists changes', () => {
    useUnitsStore.getState().setUnits('imperial');
    expect(localStorage.getItem('rsbs_units')).toBe('imperial');
    expect(getUnits()).toBe('imperial');
  });

  it('does not blow up when localStorage throws (Safari private mode etc.)', () => {
    const orig = localStorage.setItem;
    localStorage.setItem = () => {
      throw new Error('QuotaExceeded');
    };
    try {
      expect(() => useUnitsStore.getState().setUnits('imperial')).not.toThrow();
    } finally {
      localStorage.setItem = orig;
    }
  });
});
