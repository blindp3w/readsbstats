import { describe, it, expect, vi } from 'vitest';

import { buildPanelOption, buildSignalSmallMultiplesOption } from '@/pages/Metrics';
import { buildBarOption } from '@/pages/Stats';
import { buildFlightProfileOption } from '@/pages/Flight';
import { buildTopChartOption, type Row } from '@/components/charts/TopChart';
import { abbreviateAxis } from '@/components/charts/topRows';

const fakeFmtTs = (epoch: number) => new Date(epoch * 1000).toISOString();
const fakeFmtAxisTime = (epoch: number) =>
  new Date(epoch * 1000).toISOString().slice(11, 16);
const fakeFmtAxisDate = (epoch: number) =>
  new Date(epoch * 1000).toISOString().slice(5, 10);

describe('buildPanelOption (Metrics)', () => {
  it('emits one line series per metric key with LTTB sampling', () => {
    const resp = {
      bucket_seconds: 0,
      metrics: ['signal', 'noise'],
      data: [
        [1000, 2000],
        [-10, -11],
        [-30, -31],
      ],
    };
    const opt = buildPanelOption(resp, ['signal', 'noise'], ['#1', '#2'], fakeFmtAxisTime, fakeFmtAxisDate, fakeFmtTs);
    expect(opt.series).toHaveLength(2);
    const s0 = (opt.series as any)[0];
    expect(s0).toMatchObject({ type: 'line', sampling: 'lttb', name: 'signal' });
    expect(s0.data).toEqual([
      [1_000_000, -10],
      [2_000_000, -11],
    ]);
  });

  it('returns an empty series array when resp is undefined / empty', () => {
    expect((buildPanelOption(undefined, ['signal'], ['#1'], fakeFmtAxisTime, fakeFmtAxisDate, fakeFmtTs).series as any[]).length).toBe(0);
    expect(
      (
        buildPanelOption(
          { bucket_seconds: 0, metrics: [], data: [] },
          ['signal'],
          ['#1'],
          fakeFmtAxisTime,
          fakeFmtAxisDate,
          fakeFmtTs,
        ).series as any[]
      ).length,
    ).toBe(0);
  });

  it('threads valueFormat into yAxis + tooltip', () => {
    const fmt = vi.fn((v: number) => `${v}!`);
    const opt = buildPanelOption(
      { bucket_seconds: 0, metrics: ['x'], data: [[1], [42]] },
      ['x'],
      ['#1'],
      fakeFmtAxisTime,
      fakeFmtAxisDate,
      fakeFmtTs,
      fmt,
    );
    const yAxis = opt.yAxis as any;
    expect(yAxis.axisLabel.formatter(42)).toBe('42!');
    const tt = opt.tooltip as any;
    expect(tt.valueFormatter(7)).toBe('7!');
  });

  it('uses HH:MM tick formatter when data span is < 36h', () => {
    const day = 86_400;
    const opt = buildPanelOption(
      { bucket_seconds: 0, metrics: ['s'], data: [[1000, 1000 + day], [1, 2]] },
      ['s'],
      ['#1'],
      fakeFmtAxisTime,
      fakeFmtAxisDate,
      fakeFmtTs,
    );
    const fmt = (opt.xAxis as any).axisLabel.formatter;
    // Returns the shape produced by fakeFmtAxisTime (HH:MM slice).
    expect(fmt(1_000_000)).toMatch(/^\d{2}:\d{2}$/);
  });

  it('switches to DD/MM tick formatter when data span is >= 36h', () => {
    const days = 7 * 86_400;
    const opt = buildPanelOption(
      { bucket_seconds: 0, metrics: ['s'], data: [[1000, 1000 + days], [1, 2]] },
      ['s'],
      ['#1'],
      fakeFmtAxisTime,
      fakeFmtAxisDate,
      fakeFmtTs,
    );
    const fmt = (opt.xAxis as any).axisLabel.formatter;
    // Returns the shape produced by fakeFmtAxisDate (MM-DD ISO slice).
    expect(fmt(1_000_000)).toMatch(/^\d{2}-\d{2}$/);
  });

  it('emits empty-data series for metric keys not in the response', () => {
    const opt = buildPanelOption(
      { bucket_seconds: 0, metrics: ['x'], data: [[1], [9]] },
      ['x', 'missing'],
      ['#1', '#2'],
      fakeFmtAxisTime,
      fakeFmtAxisDate,
      fakeFmtTs,
    );
    expect((opt.series as any[])).toHaveLength(2);
    expect((opt.series as any[])[1].data).toEqual([]);
  });
});

describe('buildBarOption (Stats)', () => {
  it('builds a category x-axis + value y-axis bar series', () => {
    const data = [
      { hour: 0, count: 3 },
      { hour: 1, count: 5 },
    ];
    const opt = buildBarOption(data, 'hour', 'count');
    expect((opt.xAxis as any).type).toBe('category');
    expect((opt.xAxis as any).data).toEqual(['0', '1']);
    expect((opt.series as any)[0].type).toBe('bar');
    expect((opt.series as any)[0].data).toEqual([3, 5]);
  });
});

describe('buildTopChartOption', () => {
  const rows: Row[] = [
    { label: 'AAA', fullLabel: 'AAA — full', value: 7, icao_hex: 'abc123' },
    { label: 'BBB', fullLabel: 'BBB — other', value: 4 },
  ];

  it('uses item-trigger tooltip with the full label', () => {
    const opt = buildTopChartOption(rows, true);
    const tt = opt.tooltip as any;
    expect(tt.trigger).toBe('item');
    expect(tt.formatter({ data: rows[0] })).toContain('AAA — full');
  });

  it('reflects clickable flag in series.cursor', () => {
    expect(((buildTopChartOption(rows, true).series as any)[0]).cursor).toBe('pointer');
    expect(((buildTopChartOption(rows, false).series as any)[0]).cursor).toBe('default');
  });

  it('carries icao_hex in series.data for click navigation', () => {
    const opt = buildTopChartOption(rows, true);
    const first = (opt.series as any)[0].data[0];
    expect(first.icao_hex).toBe('abc123');
    expect(first.value).toBe(7);
  });

  it('uses category yAxis with inverse=true (top-N descending)', () => {
    const opt = buildTopChartOption(rows, false);
    const yAxis = opt.yAxis as any;
    expect(yAxis.type).toBe('category');
    expect(yAxis.inverse).toBe(true);
    expect(yAxis.data).toEqual(['AAA', 'BBB']);
  });
});

describe('buildFlightProfileOption (Flight)', () => {
  const rows = [
    { ts: 100, alt: 1000, gs: 250 },
    { ts: 200, alt: 1100, gs: 260 },
  ];

  it('emits two series — altitude (area) + speed (line)', () => {
    const opt = buildFlightProfileOption(rows, 'ft', 'kt', fakeFmtAxisTime, fakeFmtTs);
    expect((opt.series as any[])).toHaveLength(2);
    expect((opt.series as any)[0].areaStyle).toBeDefined();
    expect((opt.series as any)[1].areaStyle).toBeUndefined();
  });

  it('uses dual y-axis: left for altitude, right for speed', () => {
    const opt = buildFlightProfileOption(rows, 'ft', 'kt', fakeFmtAxisTime, fakeFmtTs);
    const yAxis = opt.yAxis as any[];
    expect(yAxis).toHaveLength(2);
    // yAxis[0] uses default left position; yAxis[1] explicitly 'right'.
    expect(yAxis[1].position).toBe('right');
  });

  it('omits the built-in legend (v2.9.0 HTML IsolationPills replaces it)', () => {
    const opt = buildFlightProfileOption(rows, 'ft', 'kt', fakeFmtAxisTime, fakeFmtTs);
    expect(opt.legend).toBeUndefined();
  });

  it('series carry stable string keys (not unit-dependent labels)', () => {
    // Critical for isolation: series.name must NOT change when units flip.
    const optMetric = buildFlightProfileOption(rows, 'm', 'km/h', fakeFmtAxisTime, fakeFmtTs);
    const optImperial = buildFlightProfileOption(rows, 'ft', 'kt', fakeFmtAxisTime, fakeFmtTs);
    expect((optMetric.series as any)[0].name).toBe('alt');
    expect((optMetric.series as any)[1].name).toBe('gs');
    expect((optImperial.series as any)[0].name).toBe('alt');
    expect((optImperial.series as any)[1].name).toBe('gs');
  });

  it('altitude area uses an orange gradient (not a solid 40% flood)', () => {
    const opt = buildFlightProfileOption(rows, 'ft', 'kt', fakeFmtAxisTime, fakeFmtTs);
    const area = (opt.series as any)[0].areaStyle;
    expect(area.color).toBeDefined();
    expect(area.color.type).toBe('linear');
    expect(area.color.colorStops).toBeDefined();
    expect(area.color.colorStops.length).toBe(2);
  });

  it('isolated="alt" fades the speed series, alt stays full opacity', () => {
    const opt = buildFlightProfileOption(rows, 'ft', 'kt', fakeFmtAxisTime, fakeFmtTs, 'alt');
    const series = opt.series as any[];
    expect(series[0].lineStyle.opacity).toBe(1);
    expect(series[0].areaStyle.opacity).toBe(1);
    expect(series[1].lineStyle.opacity).toBe(0.2);
  });

  it('isolated="gs" fades the altitude series (both line + area)', () => {
    const opt = buildFlightProfileOption(rows, 'ft', 'kt', fakeFmtAxisTime, fakeFmtTs, 'gs');
    const series = opt.series as any[];
    expect(series[0].lineStyle.opacity).toBe(0.2);
    expect(series[0].areaStyle.opacity).toBeLessThanOrEqual(0.1);
    expect(series[1].lineStyle.opacity).toBe(1);
  });

  it('isolated=null keeps both series at full opacity', () => {
    const opt = buildFlightProfileOption(rows, 'ft', 'kt', fakeFmtAxisTime, fakeFmtTs, null);
    const series = opt.series as any[];
    expect(series[0].lineStyle.opacity).toBe(1);
    expect(series[1].lineStyle.opacity).toBe(1);
  });

  it('assigns yAxisIndex per series', () => {
    const opt = buildFlightProfileOption(rows, 'ft', 'kt', fakeFmtAxisTime, fakeFmtTs);
    expect((opt.series as any)[0].yAxisIndex).toBe(0);
    expect((opt.series as any)[1].yAxisIndex).toBe(1);
  });
});

describe('abbreviateAxis', () => {
  it('returns small integers verbatim', () => {
    expect(abbreviateAxis(0)).toBe('0');
    expect(abbreviateAxis(999)).toBe('999');
  });

  it('abbreviates thousands with k, dropping trailing .0', () => {
    expect(abbreviateAxis(1000)).toBe('1k');
    expect(abbreviateAxis(1500)).toBe('1.5k');
    expect(abbreviateAxis(10000)).toBe('10k');
    expect(abbreviateAxis(12345)).toBe('12k');
  });

  it('abbreviates millions with M, dropping trailing .0', () => {
    expect(abbreviateAxis(1_000_000)).toBe('1M');
    expect(abbreviateAxis(1_500_000)).toBe('1.5M');
    expect(abbreviateAxis(10_000_000)).toBe('10M');
  });

  it('handles negative values with the same scale', () => {
    expect(abbreviateAxis(-5000)).toBe('-5k');
  });

  it('returns empty string for non-finite input', () => {
    expect(abbreviateAxis(Number.NaN)).toBe('');
    expect(abbreviateAxis(Number.POSITIVE_INFINITY)).toBe('');
  });

  it('does NOT strip trailing zero in non-decimal numbers (regex anchors)', () => {
    // Regression guard: the .0$ replacement must not collapse "10" → "1".
    expect(abbreviateAxis(10000)).toBe('10k');
    expect(abbreviateAxis(20_000_000)).toBe('20M');
  });
});

// ---------------------------------------------------------------------------
// v2.8.0 — M2.2 isolation arg on buildPanelOption
// ---------------------------------------------------------------------------

describe('buildPanelOption isolated arg', () => {
  const resp = {
    bucket_seconds: 60,
    metrics: ['ac_adsb', 'ac_mlat'],
    data: [
      [1000, 2000, 3000],
      [10, 20, 30],
      [1, 2, 3],
    ],
  };
  it('full opacity for both series when isolated is null', () => {
    const opt = buildPanelOption(
      resp,
      ['ac_adsb', 'ac_mlat'],
      ['#1', '#2'],
      fakeFmtAxisTime,
      fakeFmtAxisDate,
      fakeFmtTs,
      undefined,
      null,
    );
    const series = opt.series as any[];
    expect(series[0].lineStyle.opacity).toBe(1);
    expect(series[1].lineStyle.opacity).toBe(1);
    expect(series[0].areaStyle.opacity).toBeCloseTo(0.25);
  });
  it('fades non-isolated series to 0.2 line / 0.06 area', () => {
    const opt = buildPanelOption(
      resp,
      ['ac_adsb', 'ac_mlat'],
      ['#1', '#2'],
      fakeFmtAxisTime,
      fakeFmtAxisDate,
      fakeFmtTs,
      undefined,
      'ac_adsb',
    );
    const series = opt.series as any[];
    // ac_adsb at full opacity
    expect(series[0].lineStyle.opacity).toBe(1);
    expect(series[0].areaStyle.opacity).toBeCloseTo(0.25);
    // ac_mlat faded
    expect(series[1].lineStyle.opacity).toBe(0.2);
    expect(series[1].areaStyle.opacity).toBeCloseTo(0.06);
  });
});

// ---------------------------------------------------------------------------
// v2.8.0 — M2.1 signal small-multiples builder
// ---------------------------------------------------------------------------

describe('buildSignalSmallMultiplesOption (M2.1)', () => {
  const resp = {
    bucket_seconds: 60,
    metrics: ['peak_signal', 'signal', 'noise', 'strong_signals'],
    data: [
      [1000, 2000, 3000],
      [-30, -29, -28],
      [-35, -34, -33],
      [-40, -39, -38],
      [5, 6, 7],
    ],
  };

  it('emits 4 grids, 4 xAxes, 4 yAxes, 4 series — one per metric', () => {
    const opt = buildSignalSmallMultiplesOption(
      resp,
      ['peak_signal', 'signal', 'noise', 'strong_signals'],
      ['#a', '#b', '#c', '#d'],
      ['Peak', 'Mean', 'Noise', 'Strong'],
      fakeFmtAxisTime,
      fakeFmtAxisDate,
      fakeFmtTs,
    );
    expect((opt.grid as any[]).length).toBe(4);
    expect((opt.xAxis as any[]).length).toBe(4);
    expect((opt.yAxis as any[]).length).toBe(4);
    expect((opt.series as any[]).length).toBe(4);
  });

  it('hides x-axis labels on the top 3 grids, shows on the bottom', () => {
    const opt = buildSignalSmallMultiplesOption(
      resp,
      ['peak_signal', 'signal', 'noise', 'strong_signals'],
      ['#a', '#b', '#c', '#d'],
      ['Peak', 'Mean', 'Noise', 'Strong'],
      fakeFmtAxisTime,
      fakeFmtAxisDate,
      fakeFmtTs,
    );
    const xAxes = opt.xAxis as any[];
    // Top 3 hide label
    expect(xAxes[0].axisLabel.show).toBe(false);
    expect(xAxes[1].axisLabel.show).toBe(false);
    expect(xAxes[2].axisLabel.show).toBe(false);
    // Bottom shows label (no `show: false`)
    expect(xAxes[3].axisLabel.show).not.toBe(false);
  });

  it('links axisPointer across all xAxes for synchronized crosshair', () => {
    const opt = buildSignalSmallMultiplesOption(
      resp,
      ['peak_signal', 'signal', 'noise', 'strong_signals'],
      ['#a', '#b', '#c', '#d'],
      ['Peak', 'Mean', 'Noise', 'Strong'],
      fakeFmtAxisTime,
      fakeFmtAxisDate,
      fakeFmtTs,
    );
    expect((opt.axisPointer as any).link).toBeDefined();
    expect((opt.axisPointer as any).link[0].xAxisIndex).toBe('all');
  });

  it('renders the latest value in the title row per sub-panel', () => {
    const opt = buildSignalSmallMultiplesOption(
      resp,
      ['peak_signal', 'signal', 'noise', 'strong_signals'],
      ['#a', '#b', '#c', '#d'],
      ['Peak', 'Mean', 'Noise', 'Strong'],
      fakeFmtAxisTime,
      fakeFmtAxisDate,
      fakeFmtTs,
    );
    const titles = opt.title as any[];
    // 4 sub-panels × 2 entries each (label + value) = 8 titles
    expect(titles.length).toBe(8);
    // Last value of peak_signal is -28 → rendered with 1-dp rounding
    const valueTitles = titles.filter((t) => t.right === 8);
    expect(valueTitles.length).toBe(4);
    expect(valueTitles[0].text).toBe('-28');
  });

  it('returns empty series when resp is undefined', () => {
    const opt = buildSignalSmallMultiplesOption(
      undefined,
      ['peak_signal'],
      ['#a'],
      ['Peak'],
      fakeFmtAxisTime,
      fakeFmtAxisDate,
      fakeFmtTs,
    );
    expect((opt.series as any[]).length).toBe(0);
  });
});
