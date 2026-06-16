import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import type { EChartsOption } from 'echarts';
import { apiJson } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Alert } from '@/components/ui/Alert';
import { Skeleton } from '@/components/ui/Skeleton';
import { EChart } from '@/components/charts/EChart';
import { CHART_COLORS } from '@/components/charts/theme';
import { useFormat } from '@/hooks/useFormat';
import { fmtAgo } from '@/lib/format';
import {
  buildPanelOption,
  buildSignalSmallMultiplesOption,
  smallMultHeight,
  type MetricsResp,
} from '@/pages/metricsCharts';
import type { Vdl2TimeseriesResp } from '@/lib/types';

// A feed quiet for longer than this reads as "stale" — VDL2/ACARS is bursty, so
// keep it lenient to avoid false alarms during genuine quiet spells.
const STALE_SEC = 600;
// Up to six per-frequency sub-panels (matches the backend top-6).
const FREQ_COLORS = [
  CHART_COLORS.orange,
  CHART_COLORS.accent,
  CHART_COLORS.success,
  CHART_COLORS.purple,
  CHART_COLORS.warn,
  CHART_COLORS.danger,
];

function fmtFreshness(ts: number | null, ageSec: number | null): string {
  if (ts == null || ageSec == null) return 'no data';
  // Feed fmtAgo the server-computed age (now = ts + age) so there's no clock skew.
  return fmtAgo(ts, ts + ageSec);
}

// VDL2 reception: two range-driven ECharts — total message rate (msgs/min) and
// per-frequency small multiples (signal-panel style) — over the Metrics page's
// [from, to] window. Rendered as two side-by-side panels matching the rest of the
// Metrics grid. vdlm2dec-only; NO signal level. Self-gating: renders nothing and
// makes no request when `enabled` is false (the page only mounts it when the
// VDL2 feature is on AND vdl2.db is available — see useVdl2Available).
export function Vdl2ReceptionCard({
  enabled = true,
  from,
  to,
}: {
  enabled?: boolean;
  from: number;
  to: number;
}) {
  const { fmtTs, fmtAxisTime, fmtAxisDate } = useFormat();
  const {
    data: resp,
    isError,
    isLoading,
  } = useQuery<Vdl2TimeseriesResp>({
    queryKey: ['vdl2-timeseries', from, to],
    enabled,
    queryFn: () => apiJson<Vdl2TimeseriesResp>(`vdl2/timeseries?from=${from}&to=${to}`),
    placeholderData: (prev) => prev,
    staleTime: 30_000,
  });

  const freqKeys = useMemo(() => resp?.metrics.slice(1) ?? [], [resp]);

  const rateOption = useMemo<EChartsOption>(
    () =>
      buildPanelOption(
        resp as MetricsResp | undefined,
        ['rate'],
        [CHART_COLORS.orange],
        fmtAxisTime,
        fmtAxisDate,
        fmtTs,
      ),
    [resp, fmtAxisTime, fmtAxisDate, fmtTs],
  );
  const freqOption = useMemo<EChartsOption>(
    () =>
      buildSignalSmallMultiplesOption(
        resp as MetricsResp | undefined,
        freqKeys,
        FREQ_COLORS,
        freqKeys,
        fmtAxisTime,
        fmtAxisDate,
        fmtTs,
      ),
    [resp, freqKeys, fmtAxisTime, fmtAxisDate, fmtTs],
  );

  if (!enabled) return null;
  // Don't render a silent blank on a failed/loading query (audit 2026-06-15).
  if (isError && resp == null)
    return (
      <div className="grid gap-4 xl:grid-cols-2" data-testid="metrics-vdl2-reception">
        <Alert variant="warn" data-testid="vdl2-reception-error">
          Couldn't load VDL2 reception data.
        </Alert>
      </div>
    );
  if (isLoading)
    return (
      <div className="grid gap-4 xl:grid-cols-2" data-testid="metrics-vdl2-reception">
        <Skeleton className="h-56 w-full" data-testid="vdl2-reception-loading" />
      </div>
    );

  const ageSec = resp?.newest_age_sec ?? null;
  const stale = resp != null && (ageSec == null || ageSec > STALE_SEC);

  return (
    <div className="grid gap-4 xl:grid-cols-2" data-testid="metrics-vdl2-reception">
      {resp && (
        <Card data-testid="vdl2-rate-chart">
          <CardHeader className="flex flex-row items-center justify-between gap-2">
            <CardTitle>VDL2 / ACARS message rate</CardTitle>
            <span
              data-testid="vdl2-reception-freshness"
              className={
                stale
                  ? 'text-xs font-medium text-[var(--color-danger)]'
                  : 'text-xs text-[var(--color-text-dim)]'
              }
            >
              {`${resp.total.toLocaleString()} msgs · ${stale ? '⚠ ' : ''}last ${fmtFreshness(
                resp.newest_ts,
                ageSec,
              )}`}
            </span>
          </CardHeader>
          <CardContent>
            <EChart option={rateOption} group="metrics" height={220} />
          </CardContent>
        </Card>
      )}
      {resp && (
        <Card data-testid="vdl2-freq-charts">
          <CardHeader>
            <CardTitle>VDL2 per-frequency</CardTitle>
          </CardHeader>
          <CardContent>
            {freqKeys.length > 0 ? (
              <EChart
                option={freqOption}
                group="metrics"
                height={smallMultHeight(freqKeys.length)}
              />
            ) : (
              <p className="text-sm text-[var(--color-text-dim)]">
                No per-frequency data in this window.
              </p>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
