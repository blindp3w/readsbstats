import { Link } from 'react-router-dom';
import { Table, THead, TBody, TR, TH, TD } from '@/components/ui/Table';
import { Skeleton } from '@/components/ui/Skeleton';
import { Alert } from '@/components/ui/Alert';
import { FlagBadge, SourceBadge } from '@/components/FlagBadge';
import { fmtTs, fmtDur } from '@/lib/format';
import { useFormat } from '@/hooks/useFormat';
import { cn } from '@/lib/cn';

// Single source of truth for the flights table — used by /v2/history and
// /v2/aircraft/:icao. Sort + page is owned by the caller (URL-state) so the
// table is pure: receives state via props, emits state changes via callbacks.

export interface Flight {
  id: number;
  icao_hex: string;
  callsign: string | null;
  registration: string | null;
  aircraft_type: string | null;
  type_desc?: string | null;
  flags: number;
  squawk?: string | null;
  primary_source: string | null;
  first_seen: number;
  last_seen: number;
  duration_sec: number;
  max_alt_baro: number | null;
  max_gs: number | null;
  max_distance_nm: number | null;
  total_positions: number;
  origin_icao?: string | null;
  dest_icao?: string | null;
}

export type SortKey =
  | 'first_seen'
  | 'icao_hex'
  | 'callsign'
  | 'registration'
  | 'aircraft_type'
  | 'primary_source'
  | 'duration_sec'
  | 'max_alt_baro'
  | 'max_gs'
  | 'max_distance_nm'
  | 'total_positions'
  | 'origin_icao';

export type SortDir = 'asc' | 'desc';

interface ColDef {
  key: SortKey | null; // null = not sortable
  label: string;
  hideOnMobile?: boolean;
}

interface Props {
  flights: Flight[] | undefined;
  isLoading: boolean;
  error: Error | null;
  sortBy: SortKey;
  sortDir: SortDir;
  onSortChange: (key: SortKey, dir: SortDir) => void;
}

export function FlightsTable({ flights, isLoading, error, sortBy, sortDir, onSortChange }: Props) {
  // Subscribes to the units store. Every fmtAlt/fmtSpd/fmtDist call below
  // uses these closures; a unit toggle re-renders this component end-to-end.
  const { fmtAlt, fmtSpd, fmtDist, altLabel, spdLabel, distLabel } = useFormat();

  const cols: ColDef[] = [
    { key: 'first_seen', label: 'First seen' },
    { key: 'duration_sec', label: 'Duration', hideOnMobile: true },
    { key: 'icao_hex', label: 'ICAO' },
    { key: 'callsign', label: 'Callsign' },
    { key: 'registration', label: 'Reg' },
    { key: 'aircraft_type', label: 'Type' },
    { key: null, label: 'Route', hideOnMobile: true },
    { key: 'primary_source', label: 'Source', hideOnMobile: true },
    { key: 'max_alt_baro', label: altLabel(), hideOnMobile: true },
    { key: 'max_gs', label: spdLabel(), hideOnMobile: true },
    { key: 'max_distance_nm', label: distLabel(), hideOnMobile: true },
    { key: 'total_positions', label: 'Positions', hideOnMobile: true },
  ];

  function toggleSort(key: SortKey | null) {
    if (!key) return;
    if (sortBy === key) {
      onSortChange(key, sortDir === 'asc' ? 'desc' : 'asc');
    } else {
      // Default: descending for first_seen, alphabetical (asc) for textual cols.
      const defaultDir: SortDir = key === 'first_seen' || key === 'duration_sec' || key === 'max_alt_baro' || key === 'max_gs' || key === 'max_distance_nm' || key === 'total_positions' ? 'desc' : 'asc';
      onSortChange(key, defaultDir);
    }
  }

  if (error) {
    return <Alert variant="error">Failed to load flights: {error.message}</Alert>;
  }

  if (isLoading && (!flights || flights.length === 0)) {
    return <Skeleton className="h-64 w-full" />;
  }

  if (flights && flights.length === 0) {
    return (
      <div
        className="py-8 text-center text-sm text-[var(--color-text-dim)]"
        data-testid="flights-empty"
      >
        No flights match the current filters.
      </div>
    );
  }

  return (
    <Table data-testid="flights-table">
      <THead>
        <TR>
          {cols.map((c) => {
            const label = c.label;
            const isSortedHere = c.key === sortBy;
            return (
              <TH
                key={label}
                className={cn(
                  c.hideOnMobile && 'hidden md:table-cell',
                  c.key && 'cursor-pointer select-none',
                )}
              >
                {c.key ? (
                  <button
                    type="button"
                    onClick={() => toggleSort(c.key)}
                    className="flex items-center gap-1 font-medium uppercase tracking-wide hover:text-[var(--color-accent)]"
                    aria-sort={
                      isSortedHere ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'
                    }
                    data-testid={`flights-sort-${c.key}`}
                  >
                    {label}
                    {isSortedHere ? <span aria-hidden="true">{sortDir === 'asc' ? '▲' : '▼'}</span> : null}
                  </button>
                ) : (
                  label
                )}
              </TH>
            );
          })}
        </TR>
      </THead>
      <TBody>
        {(flights ?? []).map((f) => (
          <TR key={f.id} data-testid={`flights-row-${f.id}`}>
            <TD className="text-xs tabnum">
              <Link
                to={`/flight/${f.id}`}
                className="text-[var(--color-accent)] hover:underline"
              >
                {fmtTs(f.first_seen)}
              </Link>
            </TD>
            <TD className="hidden md:table-cell tabnum">{fmtDur(f.duration_sec)}</TD>
            <TD className="font-mono">
              <Link
                to={`/aircraft/${f.icao_hex}`}
                className="text-[var(--color-accent)] hover:underline"
              >
                {f.icao_hex}
              </Link>
            </TD>
            <TD>{f.callsign ?? '—'}</TD>
            <TD>{f.registration ?? '—'}</TD>
            <TD>
              <span className="inline-flex items-center gap-1.5">
                {f.aircraft_type ?? '—'}
                <FlagBadge flags={f.flags} />
              </span>
            </TD>
            <TD className="hidden md:table-cell">
              {f.origin_icao || f.dest_icao ? (
                <span className="font-mono text-xs tabnum">
                  {f.origin_icao ?? '???'}→{f.dest_icao ?? '???'}
                </span>
              ) : (
                '—'
              )}
            </TD>
            <TD className="hidden md:table-cell">
              <SourceBadge source={f.primary_source} />
            </TD>
            <TD className="hidden md:table-cell tabnum">{fmtAlt(f.max_alt_baro)}</TD>
            <TD className="hidden md:table-cell tabnum">{fmtSpd(f.max_gs)}</TD>
            <TD className="hidden md:table-cell tabnum">{fmtDist(f.max_distance_nm)}</TD>
            <TD className="hidden md:table-cell tabnum">{f.total_positions.toLocaleString()}</TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}
