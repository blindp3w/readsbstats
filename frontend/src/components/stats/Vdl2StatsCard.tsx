import { useQuery } from '@tanstack/react-query';
import { apiJson } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { KpiCard } from '@/components/stats/KpiCard';

// VDL2 / ACARS summary for the Stats page. Self-contained; Stats gates whether
// it renders (only when the feature is enabled). Reuses KpiCard (which renders a
// KpiSparkline from the `series` prop) for the 24h trend.
interface TopLabel {
  label: string;
  messages: number;
  aircraft: number;
}
interface TopAirline {
  code: string;
  messages: number;
  name: string | null;
}
interface Vdl2StatsResponse {
  total: number;
  last_hour: number;
  aircraft: number;
  top_labels: TopLabel[];
  top_airlines: TopAirline[];
  hourly: number[];
}

export function Vdl2StatsCard() {
  const { data } = useQuery({
    queryKey: ['vdl2-stats'],
    queryFn: () => apiJson<Vdl2StatsResponse>('vdl2/stats'),
    staleTime: 120_000,
  });

  return (
    <div className="space-y-4" data-testid="stats-vdl2">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
        <KpiCard
          label="ACARS messages (24h trend)"
          value={data ? data.total : '—'}
          series={data?.hourly}
          testid="vdl2-kpi-total"
        />
        <KpiCard label="Last hour" value={data ? data.last_hour : '—'} />
        <KpiCard label="Aircraft (VDL2)" value={data ? data.aircraft : '—'} />
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
                    <span className="font-mono">{l.label}</span>
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
