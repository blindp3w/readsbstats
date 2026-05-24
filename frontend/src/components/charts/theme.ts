// Shared chart styling tokens for ECharts option builders. CHART_COLORS is
// also consumed by the custom-SVG components (Heatmap.tsx, PolarRange.tsx).

import type { EChartsOption } from 'echarts';

export const CHART_COLORS = {
  accent: '#5b9af9',
  success: '#22c55e',
  warn: '#eab308',
  orange: '#f97316',
  purple: '#a855f7',
  danger: '#ef4444',
  text: '#e6ebf5',
  textDim: '#8891aa',
  grid: '#2e3350',
  surface: '#161a26',
};

// 5-stop sequential ramp for the activity heatmap. Uses the project's
// accent blue at five discrete alpha stops — keeps the dashboard's
// single-color palette while giving cells better discrimination than
// the previous continuous (0.18 → 1.0) gradient (M1.2 audit fix).
// `#5b9af9` is CHART_COLORS.accent.
export const HEATMAP_RAMP = [
  '#5b9af933', // 20%
  '#5b9af959', // 35%
  '#5b9af980', // 50%
  '#5b9af9b3', // 70%
  '#5b9af9ff', // 100%
];

export function baseOption(): Partial<EChartsOption> {
  return {
    backgroundColor: 'transparent',
    textStyle: { color: CHART_COLORS.text, fontSize: 11 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: CHART_COLORS.surface,
      borderColor: CHART_COLORS.grid,
      textStyle: { color: CHART_COLORS.text, fontSize: 12 },
    },
    grid: { top: 8, right: 8, bottom: 24, left: 36, containLabel: false },
  };
}

export function timeAxis(): EChartsOption['xAxis'] {
  return {
    type: 'time',
    axisLine: { lineStyle: { color: CHART_COLORS.grid } },
    axisLabel: { color: CHART_COLORS.textDim },
    splitLine: { show: false },
  };
}

export function valueAxis(opts?: { formatter?: (v: number) => string }): EChartsOption['yAxis'] {
  return {
    type: 'value',
    axisLine: { show: false },
    axisLabel: { color: CHART_COLORS.textDim, formatter: opts?.formatter },
    splitLine: { lineStyle: { color: CHART_COLORS.grid, type: 'dashed' } },
  };
}
