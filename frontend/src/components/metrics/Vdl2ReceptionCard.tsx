import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import type { EChartsOption } from 'echarts';
import { apiJson } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
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

// VDL2 reception card: two range-driven ECharts — total message rate (msgs/min)
// and per-frequency small multiples (signal-panel style) — over the Metrics
// page's [from, to] window. vdlm2dec-only; NO signal level. Self-gating: renders
// nothing and makes no request when `enabled` is false.
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
  const { data: resp } = useQuery<Vdl2TimeseriesResp>({
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

  const ageSec = resp?.newest_age_sec ?? null;
  const stale = resp != null && (ageSec == null || ageSec > STALE_SEC);

  return (
    <Card data-testid="metrics-vdl2-reception">
      <CardHeader className="flex flex-row items-center justify-between gap-2">
        <CardTitle>VDL2 / ACARS reception</CardTitle>
        <span
          data-testid="vdl2-reception-freshness"
          className={
            stale
              ? 'text-xs font-medium text-[var(--color-danger)]'
              : 'text-xs text-[var(--color-text-dim)]'
          }
        >
          {resp
            ? `${resp.total.toLocaleString()} msgs · ${stale ? '⚠ ' : ''}last ${fmtFreshness(
                resp.newest_ts,
                ageSec,
              )}`
            : '—'}
        </span>
      </CardHeader>
      <CardContent className="space-y-4">
        {resp && (
          <div data-testid="vdl2-rate-chart">
            <div className="mb-1 text-xs font-medium uppercase tracking-wide text-[var(--color-text-dim)]">
              Message rate — msgs/min
            </div>
            <EChart option={rateOption} group="metrics" height={180} />
          </div>
        )}
        {resp && (
          <div data-testid="vdl2-freq-charts">
            <div className="mb-1 text-xs font-medium uppercase tracking-wide text-[var(--color-text-dim)]">
              Per-frequency — msgs/min
            </div>
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
          </div>
        )}
      </CardContent>
    </Card>
  );
}
