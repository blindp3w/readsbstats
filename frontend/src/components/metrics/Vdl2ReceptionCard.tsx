import { useQuery } from '@tanstack/react-query';
import { apiJson } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { KpiCard } from '@/components/stats/KpiCard';
import { fmtAgo } from '@/lib/format';
import type { Vdl2ReceptionResponse } from '@/lib/types';

// A feed quiet for longer than this reads as "stale" — VDL2/ACARS is bursty, so
// keep it lenient to avoid false alarms during genuine quiet spells.
const STALE_SEC = 600;

// Reuse fmtAgo's relative formatting but feed it the SERVER-computed age (pass
// `now = ts + ageSec`) so the label has no client/server clock skew and stays
// pure (no Date.now() in render).
function fmtFreshness(ts: number | null, ageSec: number | null): string {
  if (ts == null || ageSec == null) return 'no data';
  return fmtAgo(ts, ts + ageSec);
}

// VDL2 reception / receiver-health card for the Metrics page. vdlm2dec-only:
// shows message rate, per-frequency activity, distinct aircraft, and feed
// freshness — NO signal level (that field exists only in dumpvdl2). Self-gating
// via the `enabled` prop: when false it makes no /api/vdl2/reception call AND
// renders nothing (the parent still gates mounting on availability).
export function Vdl2ReceptionCard({ enabled = true }: { enabled?: boolean }) {
  const { data } = useQuery({
    queryKey: ['vdl2-reception'],
    queryFn: () => apiJson<Vdl2ReceptionResponse>('vdl2/reception'),
    enabled,
    refetchInterval: 15_000,
    staleTime: 15_000,
  });

  if (!enabled) return null;

  const stale = data != null && (data.newest_age_sec == null || data.newest_age_sec > STALE_SEC);

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
          {stale ? '⚠ ' : ''}
          {data ? `last message ${fmtFreshness(data.newest_ts, data.newest_age_sec)}` : '—'}
        </span>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
          <KpiCard
            label="Messages / min (60m trend)"
            value={data ? data.msgs_last_min : '—'}
            series={data?.rate_sparkline}
            testid="vdl2-kpi-rate"
          />
          <KpiCard
            label="Last hour"
            value={data ? data.msgs_last_hour : '—'}
            testid="vdl2-kpi-hour"
          />
          <KpiCard
            label="Aircraft (last hour)"
            value={data ? data.aircraft_last_hour : '—'}
            testid="vdl2-kpi-aircraft"
          />
        </div>
        <div data-testid="vdl2-reception-per-freq">
          <div className="mb-1 text-xs font-medium uppercase tracking-wide text-[var(--color-text-dim)]">
            Per frequency (24h)
          </div>
          {data && data.per_freq.length > 0 ? (
            <ul className="space-y-1 text-sm">
              {data.per_freq.map((f) => (
                <li
                  key={f.freq_mhz ?? 'unknown'}
                  data-testid="vdl2-freq-row"
                  className="flex items-center justify-between gap-2"
                >
                  <span className="font-mono">
                    {f.freq_mhz != null ? `${f.freq_mhz.toFixed(3)} MHz` : 'unknown'}
                  </span>
                  <span className="tabnum text-[var(--color-text-dim)]">
                    {f.messages.toLocaleString()} msg · {f.aircraft.toLocaleString()} ac
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-[var(--color-text-dim)]">No messages received yet.</p>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
