// Shared chart styling tokens for ECharts option builders. CHART_COLORS is
// also consumed by the custom-SVG components (Heatmap.tsx, PolarRange.tsx).

import type { EChartsOption } from 'echarts';

// Axis-pointer label formatter param. ECharts' real type
// (`LabelFormatterParams`) is a deep union covering tooltip / mark-line
// / axis-pointer cases; `value` is `ScaleDataValue` (= `string | number
// | Date`). Page-level chart builders here are time-axis only and the
// time axis surfaces ms-since-epoch numbers — callers coerce defensively
// via `Number(p.value)` so a Date or string at runtime still produces
// a sensible value (Date.toString → NaN, string → its numeric form).
export interface AxisPointerLabelFormatterParam {
  value: number | string | Date;
  seriesData?: unknown[];
}

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

// `as const` on `type` keeps the literal discriminant when builders
// spread the result, so the returned shape stays structurally
// compatible with ECharts' axis-union (time-axis vs value-axis etc.)
// without an explicit cast at the call site. Return type is inferred —
// an explicit `XAXisComponentOption` would re-widen the union and
// re-introduce the spread-pattern type errors this helper was added
// to remove.
export function timeAxis() {
  return {
    type: 'time' as const,
    axisLine: { lineStyle: { color: CHART_COLORS.grid } },
    axisLabel: { color: CHART_COLORS.textDim },
    splitLine: { show: false },
  };
}

export function valueAxis(opts?: { formatter?: (v: number) => string }) {
  return {
    type: 'value' as const,
    axisLine: { show: false },
    axisLabel: { color: CHART_COLORS.textDim, formatter: opts?.formatter },
    splitLine: { lineStyle: { color: CHART_COLORS.grid, type: 'dashed' as const } },
  };
}
