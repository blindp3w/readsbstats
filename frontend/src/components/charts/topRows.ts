// Shared rankings primitives for the Statistics page.
// Extracted from TopChart.tsx so both the single-card switcher (md/<sm)
// and the xl small-multiples grid render from the same option builder.

import type { EChartsOption } from 'echarts';
import type { StatsResponse } from '@/pages/Stats';
import { CHART_COLORS, baseOption } from './theme';

export type ViewKey = 'aircraft' | 'airlines' | 'countries' | 'visitors' | 'routes' | 'airports';

export const VIEWS: { key: ViewKey; label: string }[] = [
  { key: 'aircraft', label: 'Aircraft types' },
  { key: 'airlines', label: 'Airlines' },
  { key: 'countries', label: 'Countries' },
  { key: 'visitors', label: 'Visitors' },
  { key: 'routes', label: 'Routes' },
  { key: 'airports', label: 'Airports' },
];

export interface Row {
  label: string;
  fullLabel: string;
  value: number;
  icao_hex?: string;
}

export interface RankingsData {
  top_aircraft_types?: StatsResponse['top_aircraft_types'];
  top_airlines?: StatsResponse['top_airlines'];
  top_countries?: StatsResponse['top_countries'];
  frequent_aircraft?: StatsResponse['frequent_aircraft'];
  top_routes?: StatsResponse['top_routes'];
  top_airports?: StatsResponse['top_airports'];
}

export const trunc = (s: string, n = 14) => (s.length > n ? s.slice(0, n - 1) + '…' : s);

// Compact axis-tick formatter. Long flight counts (>10k, especially in the
// xl small-multiples view at ~280 px wide) overflow the axis and collide
// with their neighbours. Abbreviate to k/M with one decimal where useful.
export function abbreviateAxis(v: number): string {
  if (!Number.isFinite(v)) return '';
  const abs = Math.abs(v);
  if (abs >= 1e6) return (v / 1e6).toFixed(abs >= 1e7 ? 0 : 1).replace(/\.0$/, '') + 'M';
  if (abs >= 1e3) return (v / 1e3).toFixed(abs >= 1e4 ? 0 : 1).replace(/\.0$/, '') + 'k';
  return String(v);
}

export function buildRows(view: ViewKey, props: RankingsData, topN = 15): Row[] {
  switch (view) {
    case 'aircraft':
      return (props.top_aircraft_types ?? []).slice(0, topN).map((r) => ({
        label: trunc(r.type || '—'),
        fullLabel: r.type + (r.type_desc ? ' — ' + r.type_desc : ''),
        value: r.flights,
      }));
    case 'airlines':
      return (props.top_airlines ?? []).slice(0, topN).map((r) => ({
        label: trunc(r.airline || '—'),
        fullLabel: r.airline_name ?? r.airline,
        value: r.flights,
      }));
    case 'countries':
      return (props.top_countries ?? []).slice(0, topN).map((r) => ({
        label: trunc(r.country || '—'),
        fullLabel: r.country,
        value: r.flights,
      }));
    case 'visitors':
      return (props.frequent_aircraft ?? []).slice(0, topN).map((r) => ({
        label: trunc(r.registration ?? r.icao_hex),
        fullLabel:
          (r.registration ?? r.icao_hex) + (r.aircraft_type ? ' (' + r.aircraft_type + ')' : ''),
        value: r.flights,
        icao_hex: r.icao_hex,
      }));
    case 'routes':
      return (props.top_routes ?? []).slice(0, topN).map((r) => ({
        label: (r.origin_icao || '???') + '→' + (r.dest_icao || '???'),
        fullLabel: (r.origin_icao || '???') + ' → ' + (r.dest_icao || '???'),
        value: r.flights,
      }));
    case 'airports':
      return (props.top_airports ?? []).slice(0, topN).map((r) => ({
        label: r.icao_code,
        fullLabel: r.icao_code + (r.name ? ' ' + r.name : ''),
        value: r.appearances ?? r.flights ?? 0,
      }));
  }
}

// Exported for unit tests. clickable=true wires a pointer cursor; the
// caller is responsible for the actual click handler via EChart.onEvents.
export function buildTopChartOption(rows: Row[], clickable: boolean): EChartsOption {
  return {
    ...baseOption(),
    tooltip: {
      trigger: 'item',
      backgroundColor: CHART_COLORS.surface,
      borderColor: CHART_COLORS.grid,
      textStyle: { color: CHART_COLORS.text, fontSize: 12 },
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      formatter: (p: any) => `${p.data.fullLabel} — ${p.data.value}`,
    },
    grid: { top: 8, right: 32, bottom: 8, left: 110, containLabel: false },
    xAxis: {
      type: 'value',
      // splitNumber: 4 hint keeps the tick density low so labels don't
      // collide on narrow cards; hideOverlap is the belt-and-braces fallback
      // when a long number (e.g. 12,000) still overflows.
      splitNumber: 4,
      axisLabel: {
        color: CHART_COLORS.textDim,
        formatter: abbreviateAxis,
        hideOverlap: true,
      },
      splitLine: { lineStyle: { color: CHART_COLORS.grid, type: 'dashed' } },
    },
    yAxis: {
      type: 'category',
      data: rows.map((r) => r.label),
      axisLabel: {
        color: CHART_COLORS.textDim,
        formatter: (v: string) => trunc(v, 14),
        width: 100,
        overflow: 'truncate',
      },
      axisTick: { show: false },
      inverse: true,
    },
    series: [
      {
        type: 'bar',
        cursor: clickable ? 'pointer' : 'default',
        data: rows.map((r) => ({
          value: r.value,
          name: r.label,
          fullLabel: r.fullLabel,
          icao_hex: r.icao_hex,
        })),
        itemStyle: { color: CHART_COLORS.accent, borderRadius: [0, 3, 3, 0] },
      },
    ],
  };
}
