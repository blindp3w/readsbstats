import { describe, it, expect, vi } from 'vitest';

import { buildPanelOption } from '@/pages/Metrics';
import { buildBarOption } from '@/pages/Stats';
import { buildFlightProfileOption } from '@/pages/Flight';
import { buildTopChartOption, type Row } from '@/components/charts/TopChart';

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
    expect(yAxis[0].name).toBe('ft');
    expect(yAxis[1].name).toBe('kt');
    expect(yAxis[1].position).toBe('right');
  });

  it('assigns yAxisIndex per series', () => {
    const opt = buildFlightProfileOption(rows, 'ft', 'kt', fakeFmtAxisTime, fakeFmtTs);
    expect((opt.series as any)[0].yAxisIndex).toBe(0);
    expect((opt.series as any)[1].yAxisIndex).toBe(1);
  });
});
