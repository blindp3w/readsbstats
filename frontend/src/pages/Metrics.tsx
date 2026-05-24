import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import type { EChartsOption } from 'echarts';
import { apiJson } from '@/lib/api';
import { useRange, RangePicker, type RangeValue } from '@/components/RangePicker';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { Alert } from '@/components/ui/Alert';
import { cn } from '@/lib/cn';
import { EChart } from '@/components/charts/EChart';
import { CHART_COLORS, baseOption, timeAxis, valueAxis } from '@/components/charts/theme';
import { fmtBytes } from '@/lib/format';
import { useFormat } from '@/hooks/useFormat';
import { HealthStripe, type HealthResp } from '@/components/metrics/HealthStripe';

interface MetricsResp {
  bucket_seconds: number;
  metrics: string[];
  data: number[][]; // [[ts...], [m1...], [m2...], ...]
}

// Y-axis tick formatter shape.
type ValueFmt = (v: number) => string;

interface Panel {
  id: string;
  title: string;
  metrics: string[];
  colors: string[];
  // Friendly per-metric labels for the panels that render them visibly
  // (signal small-multiples sub-titles, aircraft isolation pills). Other
  // panels can omit — the metric key is fine as a fallback.
  labels?: string[];
  // Optional Y-axis tick formatter — used for byte/meter values that
  // shouldn't render as raw integers.
  valueFormat?: ValueFmt;
}

// 11 panels = v1 parity. Same metrics, same groupings — see static/js/metrics.js
// CHART_GROUPS for the source-of-truth.
const PANELS: Panel[] = [
  {
    // M2.1: rendered by <SignalSmallMultiples>, not <MetricChart>. 4
    // stacked sub-panels in one ECharts canvas; bottom-most sub-panel
    // owns the shared x-axis.
    id: 'signal',
    title: 'Signal quality (dBFS)',
    metrics: ['peak_signal', 'signal', 'noise', 'strong_signals'],
    colors: [CHART_COLORS.accent, CHART_COLORS.success, CHART_COLORS.danger, CHART_COLORS.warn],
    labels: ['Peak signal', 'Mean signal', 'Noise floor', 'Strong signals'],
  },
  {
    // M2.2: rendered by <IsolatingMetricChart>, not <MetricChart>. HTML
    // pill row above the chart toggles series isolation (fade-not-hide).
    id: 'aircraft',
    title: 'Aircraft count',
    metrics: ['ac_with_pos', 'ac_without_pos', 'ac_adsb', 'ac_mlat'],
    colors: [CHART_COLORS.accent, CHART_COLORS.textDim, CHART_COLORS.success, CHART_COLORS.warn],
    labels: ['With pos', 'No pos', 'ADS-B', 'MLAT'],
  },
  {
    id: 'messages',
    title: 'Messages (accepted)',
    metrics: ['messages', 'local_accepted_0', 'local_accepted_1'],
    colors: [CHART_COLORS.accent, CHART_COLORS.success, CHART_COLORS.warn],
  },
  {
    id: 'range',
    title: 'Max range',
    metrics: ['max_distance_m'],
    colors: [CHART_COLORS.purple],
    valueFormat: (v) => `${(v / 1000).toFixed(0)} km`,
  },
  {
    id: 'positions',
    title: 'Positions',
    metrics: ['positions_total', 'positions_adsb', 'positions_mlat'],
    colors: [CHART_COLORS.accent, CHART_COLORS.success, CHART_COLORS.warn],
  },
  {
    id: 'cpu',
    title: 'CPU usage (ms)',
    metrics: ['cpu_demod', 'cpu_reader', 'cpu_background', 'cpu_aircraft_json', 'cpu_heatmap'],
    colors: [
      CHART_COLORS.orange,
      CHART_COLORS.purple,
      CHART_COLORS.accent,
      CHART_COLORS.success,
      CHART_COLORS.warn,
    ],
  },
  {
    id: 'network',
    title: 'Network',
    metrics: ['remote_bytes_out', 'remote_bytes_in'],
    colors: [CHART_COLORS.accent, CHART_COLORS.purple],
    valueFormat: (v) => fmtBytes(v),
  },
  {
    id: 'tracks',
    title: 'Tracks',
    metrics: ['tracks_new', 'tracks_single'],
    colors: [CHART_COLORS.accent, CHART_COLORS.warn],
  },
  {
    id: 'decoder',
    title: 'Decoder (raw preambles)',
    metrics: ['local_modes', 'local_bad'],
    colors: [CHART_COLORS.success, CHART_COLORS.danger],
  },
  {
    id: 'cpr',
    title: 'CPR decoding',
    metrics: ['cpr_global_ok', 'cpr_airborne', 'cpr_local_ok'],
    colors: [CHART_COLORS.success, CHART_COLORS.accent, CHART_COLORS.purple],
  },
];

const RANGE_OPTIONS: { value: RangeValue; label: string }[] = [
  { value: '24h', label: '24h' },
  { value: '7d', label: '7d' },
  { value: '30d', label: '30d' },
  { value: '90d', label: '90d' },
];

// Audit-12 #159 — PANELS is a module constant; the comma-joined metric list
// is therefore also constant. Hoisting it to module scope means it's computed
// once at import time instead of inside a `useMemo(..., [])` that would
// silently freeze on the first render if anyone ever made PANELS dynamic.
const ALL_METRICS = PANELS.flatMap((p) => p.metrics).join(',');

export default function MetricsPage() {
  const { state: range, setPreset, setCustom } = useRange('24h');
  const now = Math.floor(Date.now() / 1000);
  const from = range.from ?? now - 86400;
  const to = range.to ?? now;

  const qsStr = new URLSearchParams({
    from: String(from),
    to: String(to),
    metrics: ALL_METRICS,
  }).toString();

  const metricsQ = useQuery<MetricsResp>({
    queryKey: ['metrics', qsStr],
    queryFn: () => apiJson<MetricsResp>(`metrics?${qsStr}`),
    placeholderData: (prev) => prev,
    staleTime: 30_000,
  });

  const healthQ = useQuery<HealthResp>({
    queryKey: ['metrics-health'],
    queryFn: () => apiJson<HealthResp>('metrics/health'),
    staleTime: 30_000,
  });

  const hasData =
    !!metricsQ.data && metricsQ.data.data.length > 0 && metricsQ.data.data[0]?.length > 0;

  return (
    <div className="mx-auto max-w-7xl space-y-4 px-4 py-6" data-testid="page-metrics">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Receiver metrics</h1>
          <p className="text-sm text-[var(--color-text-dim)]">
            Auto-downsampled time-series from <code>readsb</code> stats.json.
          </p>
        </div>
        <RangePicker
          state={range}
          onPreset={setPreset}
          onCustom={setCustom}
          options={RANGE_OPTIONS}
          allowAll={false}
        />
      </header>

      <HealthStripe q={healthQ} />

      {metricsQ.isError && (
        <Alert variant="error">Failed to load metrics: {(metricsQ.error as Error).message}</Alert>
      )}

      {metricsQ.data && !hasData && !metricsQ.isLoading && (
        <Alert variant="info" data-testid="metrics-no-data">
          No metrics recorded in the selected range. Either receiver metrics collection is disabled
          (<code>RSBS_METRICS_ENABLED=0</code>) or the window is younger than the poll interval.
        </Alert>
      )}

      <div className="grid gap-4 xl:grid-cols-2">
        {PANELS.map((panel) => (
          <Card key={panel.id} data-testid={`metrics-panel-${panel.id}`}>
            <CardHeader>
              <CardTitle>{panel.title}</CardTitle>
            </CardHeader>
            <CardContent>
              {metricsQ.isLoading ? (
                <Skeleton className="h-56 w-full" />
              ) : panel.id === 'signal' ? (
                <SignalSmallMultiples
                  resp={metricsQ.data}
                  keys={panel.metrics}
                  colors={panel.colors}
                  labels={panel.labels ?? panel.metrics}
                />
              ) : panel.id === 'aircraft' ? (
                <IsolatingMetricChart
                  resp={metricsQ.data}
                  keys={panel.metrics}
                  colors={panel.colors}
                  labels={panel.labels ?? panel.metrics}
                  valueFormat={panel.valueFormat}
                />
              ) : (
                <MetricChart
                  resp={metricsQ.data}
                  keys={panel.metrics}
                  colors={panel.colors}
                  valueFormat={panel.valueFormat}
                />
              )}
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}

// statusColor / StatusIcon / HealthBanner moved to
// `frontend/src/components/metrics/HealthStripe.tsx` in v2.7.0.

// ---------------------------------------------------------------------------
// Metric chart — converts the columnar /api/metrics response into one ECharts
// option object. Skips panels whose metrics aren't in the response (e.g. when
// the server doesn't collect them).
// ---------------------------------------------------------------------------

// Exported for unit tests. Format functions are injected so tests don't
// need to mount a React tree to flip the clock-format store.
//   fmtAxisTime → short "HH:MM" form for x-axis ticks (sub-day spans)
//   fmtAxisDate → short locale-aware "DD/MM" for ticks on multi-day spans;
//                 every tick at midnight reads "00:00" otherwise
//   fmtTs       → full datetime for the cross-series tooltip / pointer label
// The builder picks fmtAxisTime vs fmtAxisDate based on the data span.
const MULTI_DAY_THRESHOLD_S = 36 * 3600; // 36h — wider than 24h to avoid
// flipping formats on a sub-day
// jitter inside the 24h preset.
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
  const tAxis = timeAxis() as Exclude<EChartsOption['xAxis'], undefined | unknown[]>;
  return {
    ...base,
    xAxis: {
      ...tAxis,
      axisLabel: {
        ...(tAxis as any).axisLabel,
        formatter: (v: number) => axisFmt(v / 1000),
        hideOverlap: true,
      },
      // On-hover x-axis bubble shows the full timestamp (tooltip header
      // mirrors this format too).
      axisPointer: {
        label: { formatter: (p: any) => fmtTs(p.value / 1000) },
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
const SMALL_MULT_HEIGHT = 280; // px — chart canvas total
const SMALL_MULT_GRID_H = 50;
const SMALL_MULT_TITLE_H = 14; // small label row above each grid

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

// Exported for unit tests.
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

  const titles: EChartsOption['title'] = [];
  const grids: EChartsOption['grid'] = [];
  const xAxes: EChartsOption['xAxis'] = [];
  const yAxes: EChartsOption['yAxis'] = [];
  const series: EChartsOption['series'] = [];

  keys.forEach((k, i) => {
    const idx = resp.metrics.indexOf(k);
    const valuesCol = idx >= 0 ? (rest[idx] ?? []) : [];
    const data: number[][] = valuesCol.map((v, j) => [tsCol[j] * 1000, v]);
    const lastVal = lastNonNullValue(valuesCol);
    const isBottom = i === keys.length - 1;
    const gridTop = smallMultGridTop(i);

    // Two title entries per sub-panel: label (left) + current value (right).
    (titles as any[]).push(
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
    (grids as any[]).push({
      top: gridTop,
      left: 40,
      right: 12,
      height: SMALL_MULT_GRID_H,
    });
    (xAxes as any[]).push({
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
        label: { formatter: (p: any) => fmtTs(p.value / 1000) },
      },
    });
    (yAxes as any[]).push({
      type: 'value',
      gridIndex: i,
      axisLine: { show: false },
      axisLabel: { color: CHART_COLORS.textDim, fontSize: 9 },
      splitLine: { lineStyle: { color: CHART_COLORS.grid, type: 'dashed' } },
      splitNumber: 2,
    });
    (series as any[]).push({
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

function SignalSmallMultiples({
  resp,
  keys,
  colors,
  labels,
}: {
  resp: MetricsResp | undefined;
  keys: string[];
  colors: string[];
  labels: string[];
}) {
  const { fmtTs, fmtAxisTime, fmtAxisDate } = useFormat();
  const option = useMemo<EChartsOption>(
    () =>
      buildSignalSmallMultiplesOption(resp, keys, colors, labels, fmtAxisTime, fmtAxisDate, fmtTs),
    [resp, keys, colors, labels, fmtAxisTime, fmtAxisDate, fmtTs],
  );
  const hasRows = !!resp && resp.data.length > 0 && (resp.data[0]?.length ?? 0) > 0;
  if (!hasRows) {
    return (
      <div
        className={cn(
          'flex h-[280px] items-center justify-center text-sm text-[var(--color-text-dim)]',
        )}
      >
        no data
      </div>
    );
  }
  return <EChart option={option} group="metrics" height={SMALL_MULT_HEIGHT} />;
}

// ---------------------------------------------------------------------------
// M2.2 — Isolating metric chart: HTML pill row above the chart that toggles
// per-series isolation (fade-not-hide). Uses buildPanelOption with the
// `isolated` arg.
// ---------------------------------------------------------------------------

function IsolatingMetricChart({
  resp,
  keys,
  colors,
  labels,
  valueFormat,
}: {
  resp: MetricsResp | undefined;
  keys: string[];
  colors: string[];
  labels: string[];
  valueFormat?: ValueFmt;
}) {
  const { fmtTs, fmtAxisTime, fmtAxisDate } = useFormat();
  const [isolated, setIsolated] = useState<string | null>(null);
  const option = useMemo<EChartsOption>(
    () =>
      buildPanelOption(resp, keys, colors, fmtAxisTime, fmtAxisDate, fmtTs, valueFormat, isolated),
    [resp, keys, colors, valueFormat, fmtAxisTime, fmtAxisDate, fmtTs, isolated],
  );
  const hasRows = !!resp && resp.data.length > 0 && (resp.data[0]?.length ?? 0) > 0;
  return (
    <div className="space-y-2">
      <div
        role="group"
        aria-label="Series isolation"
        className="flex flex-wrap items-center gap-2"
        data-testid="metrics-aircraft-pills"
      >
        {keys.map((k, i) => {
          const active = isolated === k;
          const color = colors[i] ?? CHART_COLORS.accent;
          return (
            <button
              key={k}
              type="button"
              onClick={() => setIsolated((cur) => (cur === k ? null : k))}
              aria-pressed={active}
              aria-label={`Isolate ${labels[i] ?? k}`}
              data-testid={`metrics-aircraft-pill-${k}`}
              className={cn(
                // 44 px tap target on mobile (Apple HIG), 36 px on
                // desktop where pointer precision makes the smaller
                // pill less awkward in a dense Metrics page.
                'inline-flex min-h-[44px] items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors md:min-h-9',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]',
                active
                  ? 'border-[var(--color-accent)] bg-[var(--color-surface-2)] text-[var(--color-text)]'
                  : 'border-[var(--color-border-default)] text-[var(--color-text-dim)] hover:bg-[var(--color-surface-2)]/60 hover:text-[var(--color-text)]',
              )}
            >
              <span
                aria-hidden="true"
                className="inline-block h-2.5 w-2.5 rounded-full"
                style={{ background: color }}
              />
              {labels[i] ?? k}
            </button>
          );
        })}
      </div>
      {!hasRows ? (
        <div
          className={cn(
            'flex h-56 items-center justify-center text-sm text-[var(--color-text-dim)]',
          )}
        >
          no data
        </div>
      ) : (
        <EChart option={option} group="metrics" height={220} />
      )}
    </div>
  );
}

function MetricChart({
  resp,
  keys,
  colors,
  valueFormat,
}: {
  resp: MetricsResp | undefined;
  keys: string[];
  colors: string[];
  valueFormat?: ValueFmt;
}) {
  const { fmtTs, fmtAxisTime, fmtAxisDate } = useFormat();
  const option = useMemo<EChartsOption>(
    () => buildPanelOption(resp, keys, colors, fmtAxisTime, fmtAxisDate, fmtTs, valueFormat),
    [resp, keys, colors, valueFormat, fmtAxisTime, fmtAxisDate, fmtTs],
  );
  const hasRows = !!resp && resp.data.length > 0 && (resp.data[0]?.length ?? 0) > 0;
  if (!hasRows) {
    return (
      <div
        className={cn('flex h-56 items-center justify-center text-sm text-[var(--color-text-dim)]')}
      >
        no data
      </div>
    );
  }
  return <EChart option={option} group="metrics" height={220} />;
}
