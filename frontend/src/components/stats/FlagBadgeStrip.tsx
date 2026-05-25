// Compact horizontal pill strip for the three pilot flags (military /
// interesting / anonymous) and the three emergency squawk codes
// (7700 / 7600 / 7500). Each pill is a <Link> into a filtered History
// view.
//
// Sprint 1 #3: squawk pills only render when their count is > 0 —
// "no emergency" is the default state on almost every page load, and
// dashboards conventionally treat empty state as silence (Datadog /
// New Relic). The three flag pills (military / interesting / anonymous)
// still render at 0 because they're "kinds of contacts" rather than
// "emergency events" — "0 military in this window" is informative.

import { Link } from 'react-router-dom';
import { Badge } from '@/components/ui/Badge';
import { cn } from '@/lib/cn';

const SQUAWK_LABELS: Record<string, string> = {
  '7700': 'Emergency',
  '7600': 'Radio failure',
  '7500': 'Hijack',
};

export interface FlagCounts {
  military: number;
  interesting: number;
  anonymous: number;
  squawks: { '7700'?: number; '7600'?: number; '7500'?: number };
}

interface Props {
  counts: FlagCounts;
}

interface PillProps {
  to: string;
  label: string;
  count: number;
  variant: 'success' | 'warn' | 'danger' | 'muted';
  testid: string;
  ariaTemplate: (n: number) => string;
}

function Pill({ to, label, count, variant, testid, ariaTemplate }: PillProps) {
  const muted = count === 0;
  return (
    <Link
      to={to}
      data-testid={testid}
      aria-label={ariaTemplate(count)}
      // flex (not inline-flex) + w-full so each pill stretches to fill its
      // grid cell. justify-between keeps the count anchored right inside
      // each pill so the row scans as a column of values.
      className={cn(
        'flex min-h-11 w-full items-center justify-between gap-2 rounded-full border px-3 py-1 text-xs font-medium transition-colors',
        muted
          ? 'border-[var(--color-border-default)] text-[var(--color-text-dim)] hover:bg-[var(--color-surface-2)]'
          : 'border-[var(--color-border-default)] hover:bg-[var(--color-surface-2)]',
      )}
    >
      <Badge variant={muted ? 'muted' : variant} className="px-1.5 py-0">
        {label}
      </Badge>
      <span
        className={cn(
          'tabnum',
          muted ? 'text-[var(--color-text-dim)]' : 'text-[var(--color-text)]',
        )}
      >
        {count.toLocaleString()}
      </span>
    </Link>
  );
}

export function FlagBadgeStrip({ counts }: Props) {
  return (
    // Equal-width grid (6 cells at xl, 3 at md, 2 at base). The whole strip
    // spans the same width as the KPI row above so the visual rhythm is
    // consistent — pills don't bunch on the left of the container.
    <div
      className="grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-6"
      data-testid="stats-flag-strip"
      aria-label="Notable activity"
    >
      <Pill
        to="/history?flags=military"
        label="Military"
        count={counts.military}
        variant="success"
        testid="flag-pill-military"
        ariaTemplate={(n) => `View ${n} military flights in history`}
      />
      <Pill
        to="/history?flags=interesting"
        label="Interesting"
        count={counts.interesting}
        variant="warn"
        testid="flag-pill-interesting"
        ariaTemplate={(n) => `View ${n} interesting flights in history`}
      />
      <Pill
        to="/history?flags=anonymous"
        label="Anonymous"
        count={counts.anonymous}
        variant="danger"
        testid="flag-pill-anonymous"
        ariaTemplate={(n) => `View ${n} anonymous flights in history`}
      />
      {(['7700', '7600', '7500'] as const)
        .filter((code) => (counts.squawks?.[code] ?? 0) > 0)
        .map((code) => {
          const n = counts.squawks?.[code] ?? 0;
          return (
            <Pill
              key={code}
              to={`/history?squawk=${code}`}
              label={`Sq ${code} · ${SQUAWK_LABELS[code]}`}
              count={n}
              variant="danger"
              testid={`flag-pill-squawk-${code}`}
              ariaTemplate={(x) => `View ${x} squawk ${code} ${SQUAWK_LABELS[code]} flights`}
            />
          );
        })}
    </div>
  );
}
