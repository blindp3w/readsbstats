import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import type { EChartsOption } from 'echarts';
import { apiJson } from '@/lib/api';
import { RangePicker, type RangeValue } from '@/components/RangePicker';
import { useRange } from '@/components/useRange';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { Alert } from '@/components/ui/Alert';
import { cn } from '@/lib/cn';
import { EChart } from '@/components/charts/EChart';
import { IsolationPills } from '@/components/charts/IsolationPills';
import { CHART_COLORS } from '@/components/charts/theme';
import { fmtBytes } from '@/lib/format';
import { useFormat } from '@/hooks/useFormat';
import { HealthStripe, type HealthResp } from '@/components/metrics/HealthStripe';
import {
  SMALL_MULT_HEIGHT,
  buildPanelOption,
  buildSignalSmallMultiplesOption,
  type MetricsResp,
  type ValueFmt,
} from './metricsCharts';

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
  // Capture `now` once at mount via the useState initialiser so render
  // stays pure (react-hooks/purity). The fallback range is "last 24h
  // relative to first paint"; that's still the intended UX — once the
  // user clicks a preset or sets a custom range, this default no longer
  // matters.
  const [now] = useState(() => Math.floor(Date.now() / 1000));
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
// option object. The option builders (`buildPanelOption`,
// `buildSignalSmallMultiplesOption`) live in ./metricsCharts.
// ---------------------------------------------------------------------------

// Inline builder bodies removed in Audit-15 — they now live in
// ./metricsCharts. Existing tests in
// frontend/test/echarts-option-builders.test.ts import directly from
// the new module.

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
      <IsolationPills
        keys={keys}
        labels={labels}
        colors={colors}
        isolated={isolated}
        onChange={setIsolated}
        testIdPrefix="metrics-aircraft"
      />
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
