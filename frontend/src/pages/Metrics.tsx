import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  Area,
  AreaChart,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from 'recharts';
import { apiJson } from '@/lib/api';
import { useRange, RangePicker, type RangeValue } from '@/components/RangePicker';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { Alert } from '@/components/ui/Alert';
import { Badge } from '@/components/ui/Badge';
import { cn } from '@/lib/cn';
import {
  AXIS_PROPS,
  CHART_COLORS,
  TOOLTIP_LABEL_STYLE,
  TOOLTIP_STYLE,
} from '@/components/charts/theme';
import { fmtBytes } from '@/lib/format';

interface MetricsResp {
  bucket_seconds: number;
  metrics: string[];
  data: number[][]; // [[ts...], [m1...], [m2...], ...]
}

interface HealthCheck {
  name: string;
  status: 'ok' | 'warn' | 'critical' | 'info' | string;
  message?: string;
}

interface HealthResp {
  overall: 'ok' | 'warn' | 'critical' | 'info' | string;
  as_of: number;
  checks: HealthCheck[];
}

// Y-axis tick formatter shape.
type ValueFmt = (v: number) => string;

interface Panel {
  id: string;
  title: string;
  metrics: string[];
  colors: string[];
  // Optional Y-axis tick formatter — used for byte/meter values that
  // shouldn't render as raw integers.
  valueFormat?: ValueFmt;
}

// 11 panels = v1 parity. Same metrics, same groupings — see static/js/metrics.js
// CHART_GROUPS for the source-of-truth.
const PANELS: Panel[] = [
  {
    id: 'signal',
    title: 'Signal quality (dBFS)',
    metrics: ['signal', 'noise', 'peak_signal'],
    colors: [CHART_COLORS.success, CHART_COLORS.danger, CHART_COLORS.accent],
  },
  {
    id: 'aircraft',
    title: 'Aircraft count',
    metrics: ['ac_with_pos', 'ac_without_pos', 'ac_adsb', 'ac_mlat'],
    colors: [CHART_COLORS.accent, CHART_COLORS.textDim, CHART_COLORS.success, CHART_COLORS.warn],
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
    id: 'network-out',
    title: 'Network — feed out',
    metrics: ['remote_bytes_out'],
    colors: [CHART_COLORS.accent],
    valueFormat: (v) => fmtBytes(v),
  },
  {
    id: 'network-in',
    title: 'Network — feed in',
    metrics: ['remote_bytes_in'],
    colors: [CHART_COLORS.purple],
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

      <HealthBanner q={healthQ} />

      {metricsQ.isError && (
        <Alert variant="error">
          Failed to load metrics: {(metricsQ.error as Error).message}
        </Alert>
      )}

      {metricsQ.data && !hasData && !metricsQ.isLoading && (
        <Alert variant="info" data-testid="metrics-no-data">
          No metrics recorded in the selected range. Either receiver metrics
          collection is disabled (<code>RSBS_METRICS_ENABLED=0</code>) or the
          window is younger than the poll interval.
        </Alert>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        {PANELS.map((panel) => (
          <Card key={panel.id} data-testid={`metrics-panel-${panel.id}`}>
            <CardHeader>
              <CardTitle>{panel.title}</CardTitle>
            </CardHeader>
            <CardContent>
              {metricsQ.isLoading ? (
                <Skeleton className="h-56 w-full" />
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

function statusColor(status: string): string {
  if (status === 'ok') return CHART_COLORS.success;
  if (status === 'warn') return CHART_COLORS.warn;
  if (status === 'critical') return CHART_COLORS.danger;
  return CHART_COLORS.textDim; // info / unknown
}

// ---------------------------------------------------------------------------
// Health banner — collapsed by default, click-to-expand per-check detail.
// Mirrors v1's behaviour (templates/metrics.html lines 9–16).
// ---------------------------------------------------------------------------

function HealthBanner({
  q,
}: {
  q: { data: HealthResp | undefined; isLoading: boolean; isError: boolean; error: Error | null };
}) {
  const [open, setOpen] = useState(false);
  if (q.isLoading) return <Skeleton className="h-12 w-full" />;
  if (q.isError || !q.data) return null;
  const variant: 'info' | 'warn' | 'error' =
    q.data.overall === 'critical'
      ? 'error'
      : q.data.overall === 'warn'
        ? 'warn'
        : 'info';
  const badgeVariant =
    q.data.overall === 'ok'
      ? 'success'
      : q.data.overall === 'warn'
        ? 'warn'
        : q.data.overall === 'critical'
          ? 'danger'
          : 'muted';
  const bad = q.data.checks.filter((c) => c.status === 'warn' || c.status === 'critical');
  return (
    <Alert variant={variant} data-testid="metrics-health-banner" className="p-0">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls="metrics-health-detail"
        data-testid="metrics-health-toggle"
        className="flex w-full flex-wrap items-center gap-2 px-3 py-2 text-left text-sm hover:bg-[var(--color-surface-2)]/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]"
      >
        <span className="font-medium">Receiver health:</span>
        <Badge variant={badgeVariant}>{q.data.overall}</Badge>
        {bad.length > 0 && (
          <span className="text-[var(--color-text-dim)]">
            {bad.length} alert{bad.length === 1 ? '' : 's'}
          </span>
        )}
        <span className="ml-auto text-[var(--color-text-dim)]">
          {q.data.checks.length} check{q.data.checks.length === 1 ? '' : 's'}{' '}
          <span aria-hidden="true">{open ? '▴' : '▾'}</span>
        </span>
      </button>
      {open && (
        <ul
          id="metrics-health-detail"
          className="border-t border-[var(--color-border-default)] divide-y divide-[var(--color-border-default)]"
          data-testid="metrics-health-detail"
        >
          {q.data.checks.map((c) => (
            <li
              key={c.name}
              className="flex flex-wrap items-baseline gap-2 border-l-4 pl-3 pr-3 py-2 text-xs"
              style={{ borderLeftColor: statusColor(c.status) }}
              data-testid={`metrics-health-check-${c.name}`}
              data-status={c.status}
            >
              <span className="font-medium">{c.name}</span>
              {c.message ? (
                <span className="text-[var(--color-text-dim)]">{c.message}</span>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </Alert>
  );
}

// ---------------------------------------------------------------------------
// Metric chart — converts the columnar /api/metrics response into Recharts
// rows. Skips panels whose metrics aren't in the response (e.g. when the
// server doesn't collect them).
// ---------------------------------------------------------------------------

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
  const rows = useMemo(() => {
    if (!resp || !resp.data || resp.data.length === 0) return [];
    const [tsCol, ...rest] = resp.data;
    const keyIndexes = keys.map((k) => resp.metrics.indexOf(k));
    if (!tsCol) return [];
    const out: Record<string, number>[] = [];
    for (let i = 0; i < tsCol.length; i++) {
      const row: Record<string, number> = { ts: tsCol[i] };
      keyIndexes.forEach((idx, kIdx) => {
        if (idx >= 0 && rest[idx] != null) row[keys[kIdx]] = rest[idx][i];
      });
      out.push(row);
    }
    return out;
  }, [resp, keys]);

  if (rows.length === 0) {
    return (
      <div className={cn('flex h-56 items-center justify-center text-sm text-[var(--color-text-dim)]')}>
        no data
      </div>
    );
  }
  return (
    <div style={{ width: '100%', height: 220 }}>
      <ResponsiveContainer>
        <AreaChart data={rows} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
          <defs>
            {keys.map((k, i) => (
              <linearGradient key={k} id={`grad-${k}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={colors[i] ?? CHART_COLORS.accent} stopOpacity={0.4} />
                <stop offset="100%" stopColor={colors[i] ?? CHART_COLORS.accent} stopOpacity={0} />
              </linearGradient>
            ))}
          </defs>
          <CartesianGrid stroke={CHART_COLORS.grid} strokeDasharray="2 4" vertical={false} />
          <XAxis
            dataKey="ts"
            type="number"
            domain={['dataMin', 'dataMax']}
            tickFormatter={(v: number) =>
              new Date(v * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
            }
            {...AXIS_PROPS}
          />
          <YAxis
            {...AXIS_PROPS}
            tickFormatter={valueFormat ? (v: number) => valueFormat(v) : undefined}
          />
          <Tooltip
            labelFormatter={(v) => new Date((v as number) * 1000).toLocaleString()}
            formatter={valueFormat ? (v: number) => valueFormat(v) : undefined}
            contentStyle={TOOLTIP_STYLE}
            labelStyle={TOOLTIP_LABEL_STYLE}
          />
          {keys.map((k, i) => (
            <Area
              key={k}
              type="monotone"
              dataKey={k}
              stroke={colors[i] ?? CHART_COLORS.accent}
              fill={`url(#grad-${k})`}
              strokeWidth={1.5}
              isAnimationActive={false}
            />
          ))}
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
