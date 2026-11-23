import { useQuery } from '@tanstack/react-query';
import { CheckIcon, Cross2Icon } from '@radix-ui/react-icons';
import { apiJson } from '@/lib/api';
import { errMsg } from '@/lib/errMsg';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Badge } from '@/components/ui/Badge';
import { useVdl2FlightWindow } from '@/hooks/useVdl2Enabled';
import { buildTimeline, formatDuration, timelineSummary, type TimelineItem } from '@/lib/oooiTimeline';
import type { Vdl2OooiRoute, Vdl2OooiSummary } from '@/lib/types';

// Route confirmation + (when available) OOOI block times for this flight.
// Events come from slash-TEI bodies or are synthesized server-side from
// Q-series QP/QQ/QR/QS compact reports (Ryanair/Wizz style); airline-defined
// label-49 movement reports and the XID `dsta` destination fill route gaps.
// `route` is the fallback when events lack dep/dest: the aircraft's own ATN CM
// logon (FMS flight + dep + dest) or a label-49 movement report (dep + dest).
// Partial events are normal — TimeRow hides slots the feed didn't report.
// A ✓/✗ chip confirms the reported route against the scheduled origin/dest.
// Renders NOTHING when nothing parsed and there's no `dsta` or `route`; a failed
// query renders a one-line muted error instead.

interface Props {
  icao: string;
  firstSeen: number;
  lastSeen: number;
  scheduledOrigin: string | null;
  scheduledDest: string | null;
}

function fmtHHMM(t: string | null): string | null {
  if (!t) return null;
  // Raw OOOI times are HHMM UTC ("0030"); render "00:30 UTC" when it looks like one.
  return /^\d{4}$/.test(t) ? `${t.slice(0, 2)}:${t.slice(2)} UTC` : t;
}

// Horizontal OUT -> OFF -> ON -> IN block-time bar. Points are always labelled in
// text (never colour-only); the visual is aria-hidden and a sr-only sentence
// carries the same information.
function Timeline({ items }: { items: TimelineItem[] }) {
  return (
    <div data-testid="oooi-timeline" role="group" aria-label="Block time timeline">
      <p className="sr-only" data-testid="oooi-timeline-summary">
        {timelineSummary(items, (t) => fmtHHMM(t) ?? t)}
      </p>
      <div className="flex items-start" aria-hidden="true">
        {items.map((it, i) =>
          it.type === 'point' ? (
            <div
              key={i}
              className="flex min-w-0 shrink-0 flex-col items-center text-center"
              data-testid={`oooi-point-${it.key}`}
            >
              <span className="text-[10px] font-semibold uppercase tracking-wide text-[var(--color-text-dim)]">
                {it.label}
              </span>
              <span className="mt-0.5 h-2.5 w-2.5 rounded-full border-2 border-[var(--color-accent)] bg-[var(--color-surface-2)]" />
              <span className="tabnum mt-0.5 max-w-[4.5rem] font-mono text-[11px] leading-tight">
                {fmtHHMM(it.time)}
              </span>
            </div>
          ) : (
            <div
              key={i}
              className="flex min-w-[1.5rem] flex-1 flex-col items-center px-0.5"
              data-testid="oooi-leg"
            >
              <span className="block h-4" />
              <span
                className={`mt-[5px] block w-full ${
                  it.airborne
                    ? 'h-1 rounded bg-[var(--color-accent)]'
                    : it.name
                      ? 'h-1 rounded bg-[var(--color-border-default)]'
                      : 'mt-[7px] h-0 border-t border-dashed border-[var(--color-border-default)]'
                }`}
              />
              {it.name && it.duration !== null && (
                <span className="mt-0.5 text-center text-[10px] leading-tight text-[var(--color-text-dim)]">
                  {it.name}
                  <br />
                  <span className="tabnum text-[var(--color-text)]">{formatDuration(it.duration)}</span>
                </span>
              )}
            </div>
          ),
        )}
      </div>
    </div>
  );
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
  // A failed fetch is not "nothing parsed": say so (muted, no card chrome) instead
  // of silently vanishing. Cached data wins over a transient refetch error.
  if (q.isError && !q.data) {
    return (
      <p className="text-xs text-[var(--color-text-dim)]" data-testid="flight-oooi-error">
        Failed to load ACARS route: {errMsg(q.error)}
      </p>
    );
  }
  const data = q.data;
  // Nothing meaningful parsed → don't show an empty card.
  if (!data || (!data.has_oooi && !data.dsta && !data.route)) return null;

  const reportedDep = data.dep?.dep_icao ?? data.arr?.dep_icao ?? data.route?.dep_icao ?? null;
  const reportedDest =
    data.arr?.dest_icao ?? data.dep?.dest_icao ?? data.route?.dest_icao ?? data.dsta ?? null;

  // Route confirmation: only assert a verdict when we have both a scheduled and
  // a reported value to compare.
  const depVerdict = routeVerdict(scheduledOrigin, reportedDep);
  const destVerdict = routeVerdict(scheduledDest, reportedDest);

  // >= 2 known points (3 items incl. the connector) -> timeline; else plain rows.
  const timeline = buildTimeline({
    out: data.dep?.t_out,
    off: data.dep?.t_off,
    on: data.arr?.t_on,
    in: data.arr?.t_in,
  });

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
        {data.has_oooi && timeline.length >= 3 && <Timeline items={timeline} />}
        {data.has_oooi && timeline.length < 3 && (
          <div className="space-y-1">
            <TimeRow label="Out (off gate)" value={fmtHHMM(data.dep?.t_out ?? null)} />
            <TimeRow label="Off (wheels up)" value={fmtHHMM(data.dep?.t_off ?? null)} />
            <TimeRow label="On (wheels down)" value={fmtHHMM(data.arr?.t_on ?? null)} />
            <TimeRow label="In (on gate)" value={fmtHHMM(data.arr?.t_in ?? null)} />
          </div>
        )}
        {!data.has_oooi && data.route && (
          <p className="text-xs text-[var(--color-text-dim)]" data-testid="flight-oooi-route-only">
            No OOOI block times parsed — route from {ROUTE_SOURCE_NOTE[data.route.source]}
          </p>
        )}
        {!data.has_oooi && !data.route && data.dsta && (
          <p className="text-xs text-[var(--color-text-dim)]" data-testid="flight-oooi-dsta-only">
            No OOOI block times parsed — destination from link data only.
          </p>
        )}
      </CardContent>
    </Card>
  );
}

const ROUTE_SOURCE_NOTE: Record<Vdl2OooiRoute['source'], string> = {
  cm: "the aircraft's ATN logon.",
  label49: 'an airline movement report.',
  label26: 'an Air France ETA report.',
  label20: 'a KLM route report.',
  r64: 'an Air France engine report (ACARS).',
  abs: 'an ACMS position report header.',
  b44: 'a take-off report.',
  fst01: 'a label 15 flight status report.',
};

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
