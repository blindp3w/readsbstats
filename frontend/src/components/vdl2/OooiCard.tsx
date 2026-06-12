import { useQuery } from '@tanstack/react-query';
import { CheckIcon, Cross2Icon } from '@radix-ui/react-icons';
import { apiJson } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Badge } from '@/components/ui/Badge';
import { useVdl2FlightWindow } from '@/hooks/useVdl2Enabled';
import type { Vdl2OooiSummary } from '@/lib/types';

// Route confirmation + (when available) OOOI block times for this flight.
// Events come from slash-TEI bodies or are synthesized server-side from
// Q-series QP/QQ/QR/QS compact reports (Ryanair/Wizz style); airline-defined
// label-49 movement reports and the XID `dsta` destination fill route gaps.
// Partial events are normal — TimeRow hides slots the feed didn't report.
// A ✓/✗ chip confirms the reported route against the scheduled origin/dest.
// Renders NOTHING when nothing parsed and there's no `dsta`.

interface Props {
  icao: string;
  firstSeen: number;
  lastSeen: number;
  scheduledOrigin: string | null;
  scheduledDest: string | null;
}

function fmtHHMM(t: string | null): string | null {
  if (!t) return null;
  // Raw OOOI times are HHMM ("0030"); render "00:30" when it looks like one.
  return /^\d{4}$/.test(t) ? `${t.slice(0, 2)}:${t.slice(2)}` : t;
}

function TimeRow({ label, value }: { label: string; value: string | null }) {
  if (!value) return null;
  return (
    <div className="flex items-center justify-between gap-3 text-sm">
      <span className="text-[var(--color-text-dim)]">{label}</span>
      <span className="tabnum font-mono">{value}</span>
    </div>
  );
}

export function OooiCard({ icao, firstSeen, lastSeen, scheduledOrigin, scheduledDest }: Props) {
  const { available, since, until } = useVdl2FlightWindow(firstSeen, lastSeen);
  const q = useQuery({
    queryKey: ['vdl2-oooi', icao, since, until],
    enabled: available && !!icao,
    queryFn: () => {
      const p = new URLSearchParams({ since: String(since), until: String(until) });
      return apiJson<Vdl2OooiSummary>(`vdl2/oooi/${icao}?${p.toString()}`);
    },
    staleTime: 60_000,
  });

  if (!available) return null;
  const data = q.data;
  // Nothing meaningful parsed → don't show an empty card.
  if (!data || (!data.has_oooi && !data.dsta)) return null;

  const reportedDep = data.dep?.dep_icao ?? data.arr?.dep_icao ?? null;
  const reportedDest = data.arr?.dest_icao ?? data.dep?.dest_icao ?? data.dsta ?? null;

  // Route confirmation: only assert a verdict when we have both a scheduled and
  // a reported value to compare.
  const depVerdict = routeVerdict(scheduledOrigin, reportedDep);
  const destVerdict = routeVerdict(scheduledDest, reportedDest);

  return (
    <Card data-testid="flight-oooi-card">
      <CardHeader>
        <CardTitle>ACARS route{data.has_oooi ? ' / OOOI' : ''}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-2">
          <RoutePoint label="Departure" icaoCode={reportedDep} verdict={depVerdict} />
          <RoutePoint label="Destination" icaoCode={reportedDest} verdict={destVerdict} />
        </div>
        {data.has_oooi && (
          <div className="space-y-1">
            <TimeRow label="Out (off gate)" value={fmtHHMM(data.dep?.t_out ?? null)} />
            <TimeRow label="Off (wheels up)" value={fmtHHMM(data.dep?.t_off ?? null)} />
            <TimeRow label="On (wheels down)" value={fmtHHMM(data.arr?.t_on ?? null)} />
            <TimeRow label="In (on gate)" value={fmtHHMM(data.arr?.t_in ?? null)} />
          </div>
        )}
        {!data.has_oooi && data.dsta && (
          <p className="text-xs text-[var(--color-text-dim)]" data-testid="flight-oooi-dsta-only">
            No OOOI block times parsed — destination from link data only.
          </p>
        )}
      </CardContent>
    </Card>
  );
}

type Verdict = 'match' | 'mismatch' | null;

function routeVerdict(scheduled: string | null, reported: string | null): Verdict {
  if (!scheduled || !reported) return null;
  return scheduled.toUpperCase() === reported.toUpperCase() ? 'match' : 'mismatch';
}

function RoutePoint({
  label,
  icaoCode,
  verdict,
}: {
  label: string;
  icaoCode: string | null;
  verdict: Verdict;
}) {
  return (
    <div className="flex items-center justify-between gap-2">
      <span className="text-xs uppercase tracking-wide text-[var(--color-text-dim)]">{label}</span>
      <span className="flex items-center gap-1.5">
        <span className="font-mono text-sm">{icaoCode ?? '—'}</span>
        {verdict === 'match' && (
          <Badge variant="success" data-testid="oooi-route-match">
            <CheckIcon aria-hidden="true" /> matches
          </Badge>
        )}
        {verdict === 'mismatch' && (
          <Badge variant="warn" data-testid="oooi-route-mismatch">
            <Cross2Icon aria-hidden="true" /> differs
          </Badge>
        )}
      </span>
    </div>
  );
}
