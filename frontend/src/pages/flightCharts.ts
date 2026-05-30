// ECharts option builder for the FlightProfileChart. Lives in its own
// file so the page component file only exports the component itself —
// required by `react-refresh/only-export-components`.
//
// Series carry STABLE keys ('alt' / 'gs') as their `name` so the
// `isolated` lookup doesn't break when the user toggles units (altLabel
// changes from "Alt (m)" to "Alt (ft)").

import type { EChartsOption } from 'echarts';
import {
  CHART_COLORS,
  baseOption,
  timeAxis,
  valueAxis,
  type AxisPointerLabelFormatterParam,
} from '@/components/charts/theme';

export interface ProfileRow {
  ts: number;
  alt: number | null;
  gs: number | null;
}

export function buildFlightProfileOption(
  rows: ProfileRow[],
  altLabel: string,
  spdLabel: string,
  fmtAxisTime: (epoch: number) => string,
  fmtTs: (epoch: number) => string,
  // M3.2: when set to 'alt' or 'gs', the OTHER series fades to 0.2
  // opacity. null = both at full opacity. Companion to the HTML pill
  // row rendered by IsolationPills above the chart.
  isolated?: string | null,
): EChartsOption {
  const base = baseOption();
  const tAxis = timeAxis();
  const leftAxis = valueAxis();
  const rightAxis = valueAxis();
  const altFaded = isolated != null && isolated !== 'alt';
  const gsFaded = isolated != null && isolated !== 'gs';
  // Orange gradient under altitude (top 30% alpha → bottom transparent).
  const altAreaGradient = {
    type: 'linear' as const,
    x: 0,
    y: 0,
    x2: 0,
    y2: 1,
    colorStops: [
      { offset: 0, color: CHART_COLORS.orange + '4d' /* ~30% alpha */ },
      { offset: 1, color: CHART_COLORS.orange + '00' /* fully transparent */ },
    ],
  };
  return {
    ...base,
    grid: { top: 16, right: 40, bottom: 28, left: 44, containLabel: false },
    xAxis: {
      ...tAxis,
      axisLabel: {
        ...tAxis.axisLabel,
        formatter: (v: number) => fmtAxisTime(v / 1000),
        hideOverlap: true,
      },
      axisPointer: {
        label: {
          formatter: (p: AxisPointerLabelFormatterParam) => fmtTs(Number(p.value) / 1000),
        },
      },
    },
    yAxis: [leftAxis, { ...rightAxis, position: 'right' }],
    dataZoom: [{ type: 'inside' }],
    series: [
      {
        name: 'alt',
        tooltip: { valueFormatter: (v: number) => `${v} (${altLabel})` },
        type: 'line',
        yAxisIndex: 0,
        color: CHART_COLORS.orange,
        data: rows.map((r) => [r.ts * 1000, r.alt]),
        showSymbol: false,
        sampling: 'lttb',
        lineStyle: { width: 1.5, opacity: altFaded ? 0.2 : 1 },
        areaStyle: { color: altAreaGradient, opacity: altFaded ? 0.06 : 1 },
      },
      {
        name: 'gs',
        tooltip: { valueFormatter: (v: number) => `${v} (${spdLabel})` },
        type: 'line',
        yAxisIndex: 1,
        color: CHART_COLORS.accent,
        data: rows.map((r) => [r.ts * 1000, r.gs]),
        showSymbol: false,
        sampling: 'lttb',
        lineStyle: { width: 1.5, opacity: gsFaded ? 0.2 : 1 },
      },
    ],
  };
}
