import { useQuery } from '@tanstack/react-query';
import { apiJson } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Alert } from '@/components/ui/Alert';
import { Skeleton } from '@/components/ui/Skeleton';
import { KpiCard } from '@/components/stats/KpiCard';
import { labelName } from '@/lib/vdl2Labels';
import type { Vdl2StatsResponse } from '@/lib/types';

// VDL2 / ACARS summary for the Stats page. Reuses KpiCard (which renders a
// KpiSparkline from the `series` prop) for the 24h trend. Self-gating via the
// `enabled` prop so an accidental render outside the Stats gate makes no
// /api/vdl2/stats call.
export function Vdl2StatsCard({ enabled = true }: { enabled?: boolean }) {
  const { data, isError, isLoading } = useQuery({
    queryKey: ['vdl2-stats'],
    queryFn: () => apiJson<Vdl2StatsResponse>('vdl2/stats'),
    enabled,
    staleTime: 120_000,
  });

  // Don't render '—' KPIs indistinguishably on a failed/loading query (audit 2026-06-15).
  // Guard on `data == null` (like Vdl2ReceptionCard) so a transient refetch error
  // keeps showing cached stats instead of flipping to the alert.
  if (isError && data == null)
    return (
      <div className="space-y-4" data-testid="stats-vdl2">
        <Alert variant="warn" data-testid="vdl2-stats-error">
          Couldn't load VDL2 stats.
        </Alert>
      </div>
    );
  if (isLoading)
    return (
      <div className="space-y-4" data-testid="stats-vdl2">
        <Skeleton className="h-40 w-full" data-testid="vdl2-stats-loading" />
      </div>
    );

  // With the optional overlap tile there are 4 KPIs (balance 2×2 / 1×4);
  // without it, 3 (1×3). Avoids a lone 4th tile wrapping under a 3-col grid.
  const hasOverlap = data?.flights_overlap_pct != null;

  return (
    <div className="space-y-4" data-testid="stats-vdl2">
      <div className={`grid grid-cols-2 gap-3 ${hasOverlap ? 'lg:grid-cols-4' : 'md:grid-cols-3'}`}>
        <KpiCard
          label="ACARS messages (24h trend)"
          value={data ? data.total : '—'}
          series={data?.hourly}
          testid="vdl2-kpi-total"
        />
        <KpiCard label="Last hour" value={data ? data.last_hour : '—'} />
        <KpiCard label="Aircraft (VDL2)" value={data ? data.aircraft : '—'} />
        {hasOverlap && (
          <KpiCard
            label="Flights also on ACARS (24h)"
            value={`${data.flights_overlap_pct}%`}
            testid="vdl2-kpi-overlap"
          />
        )}
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        <Card data-testid="vdl2-top-labels">
          <CardHeader>
            <CardTitle>Top message labels</CardTitle>
          </CardHeader>
          <CardContent>
            {data && data.top_labels.length > 0 ? (
              <ul className="space-y-1 text-sm">
                {data.top_labels.map((l) => (
                  <li key={l.label} className="flex items-center justify-between gap-2">
                    <span className="min-w-0 truncate">
                      <span className="font-mono">{l.label}</span>
                      {labelName(l.label) && (
                        <span className="text-[var(--color-text-dim)]">
                          {' '}
                          · {labelName(l.label)}
                        </span>
                      )}
                    </span>
                    <span className="tabnum text-[var(--color-text-dim)]">
                      {l.messages.toLocaleString()}
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-[var(--color-text-dim)]">No data yet.</p>
            )}
          </CardContent>
        </Card>
        <Card data-testid="vdl2-top-airlines">
          <CardHeader>
            <CardTitle>Top airlines</CardTitle>
          </CardHeader>
          <CardContent>
            {data && data.top_airlines.length > 0 ? (
              <ul className="space-y-1 text-sm">
                {data.top_airlines.map((a) => (
                  <li key={a.code} className="flex items-center justify-between gap-2">
                    <span>
                      <span className="font-mono">{a.code}</span>
                      {a.name ? ` · ${a.name}` : ''}
                    </span>
                    <span className="tabnum text-[var(--color-text-dim)]">
                      {a.messages.toLocaleString()}
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-[var(--color-text-dim)]">No data yet.</p>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
