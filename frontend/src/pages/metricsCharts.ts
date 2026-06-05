// ECharts option builders + supporting types for the Metrics page. Lives
// in its own file so the page only exports the component itself —
// required by `react-refresh/only-export-components`.

import type {
  EChartsOption,
  GridComponentOption,
  LineSeriesOption,
  TitleComponentOption,
  XAXisComponentOption,
  YAXisComponentOption,
} from 'echarts';
import {
  CHART_COLORS,
  baseOption,
  timeAxis,
  valueAxis,
  type AxisPointerLabelFormatterParam,
} from '@/components/charts/theme';

export interface MetricsResp {
  bucket_seconds: number;
  metrics: string[];
  data: number[][]; // [[ts...], [m1...], [m2...], ...]
}

// Y-axis tick formatter shape.
export type ValueFmt = (v: number) => string;

// Format functions are injected so tests don't need to mount a React tree
// to flip the clock-format store.
//   fmtAxisTime → short "HH:MM" form for x-axis ticks (sub-day spans)
//   fmtAxisDate → short locale-aware "DD/MM" for ticks on multi-day spans;
//                 every tick at midnight reads "00:00" otherwise
//   fmtTs       → full datetime for the cross-series tooltip / pointer label
// The builder picks fmtAxisTime vs fmtAxisDate based on the data span.
const MULTI_DAY_THRESHOLD_S = 36 * 3600; // 36h — wider than 24h to avoid
// flipping formats on a sub-day jitter inside the 24h preset.

export function buildPanelOption(
  resp: MetricsResp | undefined,
  keys: string[],
  colors: string[],
  fmtAxisTime: (epoch: number) => string,
  fmtAxisDate: (epoch: number) => string,
  fmtTs: (epoch: number) => string,
  valueFormat?: ValueFmt,
  // M2.2: when set to a metric key, that series stays at full opacity and
  // the others fade to 0.2 line / 0.06 area. null = all full opacity.
  isolated?: string | null,
): EChartsOption {
  if (!resp || !resp.data || resp.data.length === 0) return { series: [] };
  const [tsCol, ...rest] = resp.data;
  if (!tsCol || tsCol.length === 0) return { series: [] };
  const spanSeconds = tsCol[tsCol.length - 1] - tsCol[0];
  const axisFmt = spanSeconds >= MULTI_DAY_THRESHOLD_S ? fmtAxisDate : fmtAxisTime;
  const valueFmt: ValueFmt = valueFormat ?? ((v: number) => String(v));
  const series = keys.map((k, i) => {
    const idx = resp.metrics.indexOf(k);
    if (idx < 0) return { type: 'line' as const, name: k, data: [] as number[][] };
    const valuesCol = rest[idx] ?? [];
    const data: number[][] = valuesCol.map((v, j) => [tsCol[j] * 1000, v]);
    const faded = isolated != null && isolated !== k;
    return {
      type: 'line' as const,
      name: k,
      data,
      color: colors[i] ?? CHART_COLORS.accent,
      showSymbol: false,
      sampling: 'lttb' as const,
      lineStyle: { width: 1.5, opacity: faded ? 0.2 : 1 },
      areaStyle: { opacity: faded ? 0.06 : 0.25 },
    };
  });
  const base = baseOption();
  const tAxis = timeAxis();
  return {
    ...base,
    xAxis: {
      ...tAxis,
      axisLabel: {
        ...tAxis.axisLabel,
        formatter: (v: number) => axisFmt(v / 1000),
        hideOverlap: true,
      },
      // On-hover x-axis bubble shows the full timestamp (tooltip header
      // mirrors this format too).
      axisPointer: {
        label: {
          formatter: (p: AxisPointerLabelFormatterParam) => fmtTs(Number(p.value) / 1000),
        },
      },
    },
    yAxis: valueAxis({ formatter: valueFmt }),
    tooltip: { ...base.tooltip, valueFormatter: (v: number) => valueFmt(v) },
    dataZoom: [{ type: 'inside', xAxisIndex: 0, throttle: 50 }],
    series,
  };
}

// ---------------------------------------------------------------------------
// M2.1 — Signal small-multiples: ONE ECharts canvas with 4 stacked grids
// sharing a single x-axis at the bottom. Per-sub-panel title + current value
// rendered as ECharts `title` entries (one canvas, one resize observer).
// ---------------------------------------------------------------------------

// Per-sub-panel layout (top → bottom). The bottom grid reserves room for
// the shared x-axis tick labels.
export const SMALL_MULT_HEIGHT = 280; // px — chart canvas total
const SMALL_MULT_GRID_H = 50;
const SMALL_MULT_TITLE_H = 14; // small label row above each grid

// Total canvas height for an `n`-row small-multiples chart. The existing
// SMALL_MULT_HEIGHT (280) is exactly smallMultHeight(4) — the signal panel.
export function smallMultHeight(n: number): number {
  const rows = Math.max(n, 1);
  return (
    SMALL_MULT_TITLE_H +
    (rows - 1) * (SMALL_MULT_TITLE_H + SMALL_MULT_GRID_H) +
    SMALL_MULT_GRID_H +
    24
  );
}

function smallMultGridTop(i: number): number {
  // Row stride = title + grid; bottom (i=3) has axis label band below.
  return SMALL_MULT_TITLE_H + i * (SMALL_MULT_TITLE_H + SMALL_MULT_GRID_H);
}

function lastNonNullValue(col: number[] | undefined): number | null {
  if (!col) return null;
  for (let i = col.length - 1; i >= 0; i--) {
    const v = col[i];
    if (v != null && Number.isFinite(v)) return v;
  }
  return null;
}

// NOTE: unlike buildPanelOption, this builder does NOT accept a
// `valueFormat` arg — the per-sub-panel "current value" in the title row
// is rendered as a plain number. Fine for the signal panel (dBFS values
// + a small count) where the unit is in the Card title. If this builder
// is ever reused for a panel whose values need a unit suffix (bytes,
// metres, etc.), thread `valueFormat` through and apply it at the
// title-text site below.
export function buildSignalSmallMultiplesOption(
  resp: MetricsResp | undefined,
  keys: string[],
  colors: string[],
  labels: string[],
  fmtAxisTime: (epoch: number) => string,
  fmtAxisDate: (epoch: number) => string,
  fmtTs: (epoch: number) => string,
): EChartsOption {
  if (!resp || !resp.data || resp.data.length === 0) return { series: [] };
  const [tsCol, ...rest] = resp.data;
  if (!tsCol || tsCol.length === 0) return { series: [] };
  const spanSeconds = tsCol[tsCol.length - 1] - tsCol[0];
  const axisFmt = spanSeconds >= MULTI_DAY_THRESHOLD_S ? fmtAxisDate : fmtAxisTime;

  const titles: TitleComponentOption[] = [];
  const grids: GridComponentOption[] = [];
  const xAxes: XAXisComponentOption[] = [];
  const yAxes: YAXisComponentOption[] = [];
  const series: LineSeriesOption[] = [];

  keys.forEach((k, i) => {
    const idx = resp.metrics.indexOf(k);
    const valuesCol = idx >= 0 ? (rest[idx] ?? []) : [];
    const data: number[][] = valuesCol.map((v, j) => [tsCol[j] * 1000, v]);
    const lastVal = lastNonNullValue(valuesCol);
    const isBottom = i === keys.length - 1;
    const gridTop = smallMultGridTop(i);

    // Two title entries per sub-panel: label (left) + current value (right).
    titles.push(
      {
        text: labels[i] ?? k,
        top: gridTop - SMALL_MULT_TITLE_H,
        left: 8,
        textStyle: { color: CHART_COLORS.textDim, fontSize: 11, fontWeight: 'normal' },
      },
      {
        text: lastVal == null ? '—' : String(Math.round(lastVal * 10) / 10),
        top: gridTop - SMALL_MULT_TITLE_H,
        right: 8,
        textStyle: {
          color: CHART_COLORS.text,
          fontSize: 11,
          fontFamily: 'ui-monospace, monospace',
        },
      },
    );
    grids.push({
      top: gridTop,
      left: 40,
      right: 12,
      height: SMALL_MULT_GRID_H,
    });
    xAxes.push({
      type: 'time',
      gridIndex: i,
      axisLine: { lineStyle: { color: CHART_COLORS.grid } },
      axisTick: { show: isBottom },
      axisLabel: isBottom
        ? {
            color: CHART_COLORS.textDim,
            fontSize: 10,
            formatter: (v: number) => axisFmt(v / 1000),
            hideOverlap: true,
          }
        : { show: false },
      splitLine: { show: false },
      axisPointer: {
        label: {
          formatter: (p: AxisPointerLabelFormatterParam) => fmtTs(Number(p.value) / 1000),
        },
      },
    });
    yAxes.push({
      type: 'value',
      gridIndex: i,
      axisLine: { show: false },
      axisLabel: { color: CHART_COLORS.textDim, fontSize: 9 },
      splitLine: { lineStyle: { color: CHART_COLORS.grid, type: 'dashed' } },
      splitNumber: 2,
    });
    series.push({
      type: 'line',
      name: labels[i] ?? k,
      data,
      xAxisIndex: i,
      yAxisIndex: i,
      color: colors[i] ?? CHART_COLORS.accent,
      showSymbol: false,
      sampling: 'lttb',
      lineStyle: { width: 1.5 },
      areaStyle: { opacity: 0.3 },
    });
  });

  const base = baseOption();
  return {
    ...base,
    title: titles,
    grid: grids,
    xAxis: xAxes,
    yAxis: yAxes,
    // axisPointer.link must live at the root (verified via context7).
    // 'all' links every xAxis so the vertical crosshair tracks across all
    // 4 sub-panels on hover.
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    tooltip: {
      ...base.tooltip,
      trigger: 'axis',
      axisPointer: { type: 'line' },
    },
    series,
  };
}
