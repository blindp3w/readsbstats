import { describe, it, expect } from 'vitest';
import { buildTimeline, formatDuration, hhmmToMinutes } from '@/lib/oooiTimeline';

describe('oooiTimeline', () => {
  it('parses HHMM strictly', () => {
    expect(hhmmToMinutes('0030')).toBe(30);
    expect(hhmmToMinutes('2359')).toBe(1439);
    expect(hhmmToMinutes('2400')).toBeNull();
    expect(hhmmToMinutes('0060')).toBeNull();
    expect(hhmmToMinutes('12:30')).toBeNull();
    expect(hhmmToMinutes(null)).toBeNull();
  });
  it('formats durations', () => {
    expect(formatDuration(0)).toBe('0 min');
    expect(formatDuration(12)).toBe('12 min');
    expect(formatDuration(60)).toBe('1 h');
    expect(formatDuration(85)).toBe('1 h 25 min');
  });
  it('keeps unparseable times as points without durations', () => {
    const items = buildTimeline({ out: 'XXXX', off: '0042', on: null, in: null });
    expect(items).toHaveLength(3);
    expect(items[1]).toMatchObject({ type: 'connector', name: null, duration: null });
  });
  it('rejects absurd taxi legs and negative taxi', () => {
    const items = buildTimeline({ out: '0000', off: '1000', on: '1100', in: '1050' });
    const names = items.filter((i) => i.type === 'connector').map((c) => (c as { name: string | null }).name);
    expect(names).toEqual([null, 'Airborne', null]);
  });
});
