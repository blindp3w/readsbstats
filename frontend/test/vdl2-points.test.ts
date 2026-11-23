import { describe, it, expect } from 'vitest';
import {
  describeVdl2Point,
  vdl2SourceLabel,
  vdl2FlightPositionWindow,
  filterFixesToFlight,
  windsAloftRows,
  VDL2_MAP_WINDOW_MIN,
} from '@/lib/vdl2Points';
import type { Vdl2Position } from '@/lib/types';

const fmt = {
  fmtTs: (t: number | null | undefined) => (t == null ? '—' : `T${t}`),
  fmtAlt: (ft: number | null | undefined) => (ft == null ? '—' : `${ft} ft`),
  fmtSpd: (kt: number | null | undefined) => (kt == null ? '—' : `${kt} kt`),
};

const pt = (o: Partial<Vdl2Position> = {}): Vdl2Position => ({
  lat: 52,
  lon: 21,
  icao_hex: '4ca123',
  ts: 1000,
  label: '16',
  precise: true,
  ...o,
});

describe('vdl2SourceLabel', () => {
  it.each([
    [{ source: 'label', label: '16' }, 'ACARS label 16'],
    [{ source: 'label', label: null }, 'ACARS label'],
    [{ source: 'h1-abs' }, 'H1 ABS report'],
    [{ source: 'h1-cf' }, 'H1 CF report'],
    [{ source: 'h1-pos' }, 'H1 position report'],
    [{ source: 'm1bpos' }, 'FMS #M1BPOS'],
    [{ source: '59g' }, 'LOT 59,G report'],
    [{ source: 'adsc' }, 'ADS-C'],
    [{ source: 'xid' }, 'VDL2 link (coarse)'],
  ] as [Partial<Vdl2Position>, string][])('%j → %s', (o, expected) => {
    expect(vdl2SourceLabel(pt(o))).toBe(expected);
  });

  it('falls back on precise/label when source is absent (older backend)', () => {
    expect(vdl2SourceLabel(pt({ source: undefined, precise: false, label: null }))).toBe(
      'VDL2 link (coarse)',
    );
    expect(vdl2SourceLabel(pt({ source: undefined, precise: true, label: '17' }))).toBe(
      'ACARS label 17',
    );
  });

  it('treats an unknown future source as a generic position, never throws', () => {
    expect(vdl2SourceLabel(pt({ source: 'zzz' as never }))).toBe('ACARS position');
  });
});

describe('describeVdl2Point', () => {
  it('lists only the fields that are present', () => {
    const d = describeVdl2Point(pt({ source: 'label' }), fmt);
    expect(d.title).toBe('ACARS label 16');
    expect(d.precise).toBe(true);
    expect(d.rows).toEqual([
      ['Time', 'T1000'],
      ['Label', '16'],
    ]);
  });

  it('formats alt / wind / temp / track / gs / vs', () => {
    const d = describeVdl2Point(
      pt({
        source: 'adsc',
        alt: 36000,
        wind_dir: 270,
        wind_kt: 55,
        temp_c: -52.5,
        track: 91.4,
        gs: 480,
        vs: -1200,
      }),
      fmt,
    );
    expect(d.rows).toEqual([
      ['Time', 'T1000'],
      ['Label', '16'],
      ['Altitude', '36000 ft'],
      ['Wind', '270° / 55 kt'],
      ['Temp', '-52.5 °C'],
      ['Track', '91°'],
      ['Speed', '480 kt'],
      ['V/S', '-1200 fpm'],
    ]);
  });

  it('shows a lone wind direction or speed without inventing the other half', () => {
    expect(describeVdl2Point(pt({ wind_dir: 90 }), fmt).rows).toContainEqual(['Wind', '90°']);
    expect(describeVdl2Point(pt({ wind_kt: 12 }), fmt).rows).toContainEqual(['Wind', '12 kt']);
  });

  it('marks coarse fixes and tolerates null ts/label', () => {
    const d = describeVdl2Point(pt({ source: 'xid', precise: false, ts: null, label: null }), fmt);
    expect(d.precise).toBe(false);
    expect(d.rows).toEqual([['Time', '—']]);
  });
});

describe('VDL2_MAP_WINDOW_MIN', () => {
  it('is the 60 min the legend advertises', () => {
    expect(VDL2_MAP_WINDOW_MIN).toBe(60);
  });
});

describe('vdl2FlightPositionWindow', () => {
  it('pads 10 min each side', () => {
    expect(vdl2FlightPositionWindow(10_000, 20_000)).toEqual({ since: 9_400, until: 20_600 });
  });

  it('keeps exactly 48 h when the padded window is 48 h', () => {
    const w = vdl2FlightPositionWindow(100_000, 100_000 + 48 * 3600 - 1200);
    expect(w.until - w.since).toBe(48 * 3600);
  });

  it('clamps a longer flight to the LAST 48 h of it', () => {
    const last = 1_000_000;
    const w = vdl2FlightPositionWindow(last - 100 * 3600, last);
    expect(w.until).toBe(last + 600);
    expect(w.until - w.since).toBe(48 * 3600);
  });
});

describe('filterFixesToFlight', () => {
  it('drops fixes outside [first, last], null ts, and null coords', () => {
    const fixes = [
      pt({ ts: 99 }),
      pt({ ts: 100 }),
      pt({ ts: 150 }),
      pt({ ts: 200 }),
      pt({ ts: 201 }),
      pt({ ts: null }),
      pt({ ts: 120, lat: null as never }),
    ];
    expect(filterFixesToFlight(fixes, 100, 200).map((f) => f.ts)).toEqual([100, 150, 200]);
  });
});

describe('windsAloftRows', () => {
  it('keeps only fixes with temp or wind, sorted by time', () => {
    const rows = windsAloftRows([
      pt({ ts: 300, temp_c: -40 }),
      pt({ ts: 100 }),
      pt({ ts: 200, wind_dir: 180, wind_kt: 30 }),
      pt({ ts: 250, wind_kt: 5 }),
    ]);
    expect(rows.map((r) => r.ts)).toEqual([200, 250, 300]);
  });

  it('returns [] when no fix has atmospherics', () => {
    expect(windsAloftRows([pt(), pt({ alt: 1000 })])).toEqual([]);
  });
});
