// Flight detail position log table (M3.3). Extracted verbatim from
// pages/Flight.tsx.

import { Fragment, useState } from 'react';
import { Skeleton } from '@/components/ui/Skeleton';
import { Table, THead, TBody, TR, TH, TD } from '@/components/ui/Table';
import { SourceBadge } from '@/components/FlagBadge';
import { useFormat } from '@/hooks/useFormat';
import { KpiSparkline } from '@/components/stats/KpiSparkline';
import { RssiCell } from '@/components/flight/RssiCell';
import { useIsMobile } from '@/hooks/useIsMobile';
import type { Position } from '@/components/flight/types';

// ---------------------------------------------------------------------------
// Position log
// ---------------------------------------------------------------------------

// Per-position source stripe — keyed to source_type (NOT primary_source).
// Same mapping as History flight rows but uses startsWith for the raw
// readsb taxonomy ('adsb_icao', 'adsb_icao_nt', etc.).
function positionSourceStripe(source: string | null): string {
  if (!source) return 'var(--color-border-default)';
  const s = source.toLowerCase();
  if (s.startsWith('adsb')) return 'var(--color-success)';
  if (s === 'mlat') return 'var(--color-warn)';
  return 'var(--color-border-default)';
}

interface RssiStats {
  min: number;
  max: number;
  median: number;
  hasAny: boolean;
}

function computeRssiStats(positions: Position[]): RssiStats {
  // Filter NULLs before computing median — median of a NULL-laden array
  // would be nonsense.
  const vals: number[] = [];
  for (const p of positions) {
    if (p.rssi != null && Number.isFinite(p.rssi)) vals.push(p.rssi);
  }
  if (vals.length === 0) return { min: 0, max: 0, median: 0, hasAny: false };
  vals.sort((a, b) => a - b);
  const mid = vals.length >> 1;
  const median = vals.length % 2 === 0 ? (vals[mid - 1] + vals[mid]) / 2 : vals[mid];
  return { min: vals[0], max: vals[vals.length - 1], median, hasAny: true };
}

export function PositionTable({
  positions,
  total,
  loading,
}: {
  positions: Position[];
  total: number;
  loading: boolean;
}) {
  const { fmtAlt, fmtSpd, fmtTs } = useFormat();
  // Per-row inline disclosure state — iPhone only. Keyed by `${ts}-${index}`
  // since 1 Hz polling can emit two fixes with the same ts (audit 2026-06-15) —
  // a bare-ts key collides in React reconciliation + shares expand state.
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  // Gate the interactive row affordance behind <sm. At md+ all detail
  // columns are visible inline, so the row tap-handler + aria-expanded +
  // role="button" would mislead screen readers ('expanded' but nothing
  // changes visually) and accumulate Set entries indefinitely.
  const isMobile = useIsMobile();

  if (loading) {
    return <Skeleton className="h-40 w-full" />;
  }
  if (positions.length === 0) {
    return <p className="text-sm text-[var(--color-text-dim)]">No positions recorded.</p>;
  }
  // Sample if too many — full table would be heavy DOM. Audit 2026-06-01 S:
  // a pure `i % stride === 0` sampler always keeps positions[0] but generally
  // drops positions[len-1] (landing / last-seen, the most operationally
  // interesting point). Stride-sample as before, then append the last fix if
  // the sampler missed it. Cheap, deterministic, and preserves the modulo
  // sampler's even spacing on the rest.
  const sampled = (() => {
    if (positions.length <= 500) return positions;
    const stride = Math.ceil(positions.length / 500);
    const picks = positions.filter((_, i) => i % stride === 0);
    const last = positions[positions.length - 1];
    if (picks[picks.length - 1] !== last) picks.push(last);
    return picks;
  })();
  const rssi = computeRssiStats(sampled);
  const rssiSpark = rssi.hasAny ? sampled.map((p) => (p.rssi == null ? rssi.median : p.rssi)) : [];

  function toggle(key: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  return (
    <div className="max-h-[480px] overflow-y-auto">
      <Table>
        <THead>
          <TR>
            <TH>Time</TH>
            <TH className="hidden md:table-cell">Lat</TH>
            <TH className="hidden md:table-cell">Lon</TH>
            <TH>Alt</TH>
            <TH>Speed</TH>
            <TH>
              <div className="flex items-center justify-between gap-2">
                <span>RSSI</span>
                {rssiSpark.length >= 7 && (
                  <KpiSparkline
                    data={rssiSpark}
                    width={60}
                    height={16}
                    ariaLabel="RSSI trend across this flight"
                  />
                )}
              </div>
            </TH>
            <TH className="hidden md:table-cell">Source</TH>
          </TR>
        </THead>
        <TBody>
          {sampled.map((p, i) => {
            const rowKey = `${p.ts}-${i}`;
            const isOpen = expanded.has(rowKey);
            return (
              <Fragment key={rowKey}>
                <TR
                  data-testid={`flight-position-row-${p.ts}`}
                  // Interactive affordances ONLY on <sm. Desktop sees all
                  // detail columns inline so there's nothing to disclose.
                  {...(isMobile
                    ? {
                        tabIndex: 0,
                        role: 'button',
                        'aria-expanded': isOpen,
                        onClick: () => toggle(rowKey),
                        onKeyDown: (e: React.KeyboardEvent) => {
                          if (e.key === 'Enter' || e.key === ' ') {
                            e.preventDefault();
                            toggle(rowKey);
                          }
                        },
                        className: 'cursor-pointer',
                      }
                    : {})}
                >
                  <TD
                    className="tabnum border-l-[3px] text-xs text-[var(--color-text-dim)]"
                    style={{ borderLeftColor: positionSourceStripe(p.source_type) }}
                  >
                    {fmtTs(p.ts)}
                  </TD>
                  <TD className="hidden font-mono tabnum text-xs text-[var(--color-text-dim)] md:table-cell">
                    {p.lat?.toFixed(4) ?? '—'}
                  </TD>
                  <TD className="hidden font-mono tabnum text-xs text-[var(--color-text-dim)] md:table-cell">
                    {p.lon?.toFixed(4) ?? '—'}
                  </TD>
                  <TD className="tabnum text-xs text-[var(--color-text-dim)]">
                    {fmtAlt(p.alt_baro)}
                  </TD>
                  <TD className="tabnum text-xs text-[var(--color-text-dim)]">{fmtSpd(p.gs)}</TD>
                  <TD className="tabnum text-xs">
                    <RssiCell value={p.rssi} min={rssi.min} max={rssi.max} median={rssi.median} />
                  </TD>
                  <TD className="hidden md:table-cell">
                    <SourceBadge source={p.source_type} size="sm" />
                  </TD>
                </TR>
                {isOpen && (
                  <TR data-testid={`flight-position-detail-${p.ts}`} className="md:hidden">
                    <TD
                      colSpan={4}
                      className="border-l-[3px] bg-[var(--color-surface-2)]/40 text-xs text-[var(--color-text-dim)]"
                      style={{ borderLeftColor: positionSourceStripe(p.source_type) }}
                    >
                      <div className="grid grid-cols-2 gap-x-3 gap-y-1 py-1">
                        <span>Lat</span>
                        <span className="font-mono tabnum">{p.lat?.toFixed(4) ?? '—'}</span>
                        <span>Lon</span>
                        <span className="font-mono tabnum">{p.lon?.toFixed(4) ?? '—'}</span>
                        <span>Track</span>
                        <span className="tabnum">
                          {p.track != null ? `${Math.round(p.track)}°` : '—'}
                        </span>
                        <span>Source</span>
                        <span>
                          <SourceBadge source={p.source_type} size="sm" />
                        </span>
                      </div>
                    </TD>
                  </TR>
                )}
              </Fragment>
            );
          })}
        </TBody>
      </Table>
      {sampled.length < positions.length && (
        <p className="mt-2 text-xs text-[var(--color-text-dim)]">
          Showing {sampled.length} of {positions.length} positions (sampled).
        </p>
      )}
      {positions.length < total && (
        <p className="mt-2 text-xs text-[var(--color-text-dim)]">
          Position log capped at the first {positions.length} of {total} fixes.
        </p>
      )}
    </div>
  );
}
