// Collapsible "About this receiver" footer for lifetime totals
// (reference values that don't move with the selected window). Native
// <details> so it's keyboard-actionable for free.

import { Card, CardContent } from '@/components/ui/Card';
import { fmtBytes } from '@/lib/format';
import { useFormat } from '@/hooks/useFormat';

interface Props {
  totalFlights?: number;
  uniqueAirlines?: number;
  totalPositions?: number;
  dbSizeBytes?: number | null;
  oldestFlight?: number | null;
  sourceBreakdown?: { adsb: number; mlat: number; other: number };
}

interface RowProps {
  label: string;
  value: React.ReactNode;
}

function Row({ label, value }: RowProps) {
  return (
    <div className="flex items-baseline justify-between gap-2 py-1">
      <span className="text-xs uppercase tracking-wide text-[var(--color-text-dim)]">{label}</span>
      <span className="tabnum text-sm">{value}</span>
    </div>
  );
}

export function AboutReceiverFooter({
  totalFlights,
  uniqueAirlines,
  totalPositions,
  dbSizeBytes,
  oldestFlight,
  sourceBreakdown,
}: Props) {
  const { fmtTs } = useFormat();
  return (
    <Card data-testid="stats-about-receiver">
      <CardContent className="pt-4">
        <details>
          <summary className="cursor-pointer select-none text-sm font-semibold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]">
            About this receiver
          </summary>
          <div
            className="mt-3 grid gap-x-6 gap-y-1 xs:grid-cols-2 lg:grid-cols-3"
            data-testid="stats-about-rows"
          >
            <Row label="Total flights" value={(totalFlights ?? 0).toLocaleString()} />
            <Row label="Unique airlines" value={(uniqueAirlines ?? 0).toLocaleString()} />
            <Row label="Total positions" value={(totalPositions ?? 0).toLocaleString()} />
            <Row label="DB size" value={dbSizeBytes != null ? fmtBytes(dbSizeBytes) : '—'} />
            <Row label="Oldest flight" value={fmtTs(oldestFlight)} />
            <Row
              label="Sources"
              value={
                sourceBreakdown
                  ? `${sourceBreakdown.adsb}% ADS-B · ${sourceBreakdown.mlat}% MLAT · ${sourceBreakdown.other}% other`
                  : '—'
              }
            />
          </div>
        </details>
      </CardContent>
    </Card>
  );
}
