import { useQuery } from '@tanstack/react-query';
import { apiJson, apiUrl } from '@/lib/api';
import { useSearchParam, useSearchParamBatch } from '@/hooks/useSearchParam';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Input, Select } from '@/components/ui/Input';
import { Label } from '@/components/ui/Label';
import { Button, buttonClass } from '@/components/ui/Button';
import {
  FlightsTable,
  type Flight,
  type SortKey,
  type SortDir,
} from '@/components/FlightsTable';
import { Pagination } from '@/components/Pagination';

interface FlightsResponse {
  total: number;
  limit: number;
  offset: number;
  flights: Flight[];
}

const PAGE_SIZE = 100;

const SORT_KEYS: SortKey[] = [
  'first_seen',
  'icao_hex',
  'callsign',
  'registration',
  'aircraft_type',
  'primary_source',
  'duration_sec',
  'max_alt_baro',
  'max_gs',
  'max_distance_nm',
  'total_positions',
  'origin_icao',
];

function isSortKey(s: string): s is SortKey {
  return (SORT_KEYS as string[]).includes(s);
}

export default function HistoryPage() {
  // URL-state — all filters / sort / page survive refresh + back button.
  const [dateFrom, setDateFrom] = useSearchParam('date_from', '');
  const [dateTo, setDateTo] = useSearchParam('date_to', '');
  const [icao, setIcao] = useSearchParam('icao', '');
  const [callsign, setCallsign] = useSearchParam('callsign', '');
  const [registration, setRegistration] = useSearchParam('registration', '');
  const [aircraftType, setAircraftType] = useSearchParam('aircraft_type', '');
  const [source, setSource] = useSearchParam('source', '');
  const [flags, setFlags] = useSearchParam('flags', '');
  const [squawk, setSquawk] = useSearchParam('squawk', '');
  const [sortByRaw] = useSearchParam('sort_by', 'first_seen');
  const [sortDirRaw] = useSearchParam('sort_dir', 'desc');
  const [offset, setOffset] = useSearchParam('offset', 0);
  const update = useSearchParamBatch();

  const sortBy: SortKey = isSortKey(String(sortByRaw)) ? (sortByRaw as SortKey) : 'first_seen';
  const sortDir: SortDir = String(sortDirRaw) === 'asc' ? 'asc' : 'desc';

  const queryParams = new URLSearchParams();
  if (dateFrom) queryParams.set('date_from', dateFrom);
  if (dateTo) queryParams.set('date_to', dateTo);
  if (icao) queryParams.set('icao', icao);
  if (callsign) queryParams.set('callsign', callsign);
  if (registration) queryParams.set('registration', registration);
  if (aircraftType) queryParams.set('aircraft_type', aircraftType);
  if (source) queryParams.set('source', source);
  if (flags) queryParams.set('flags', flags);
  if (squawk) queryParams.set('squawk', squawk);
  queryParams.set('sort_by', sortBy);
  queryParams.set('sort_dir', sortDir);
  queryParams.set('limit', String(PAGE_SIZE));
  queryParams.set('offset', String(offset));
  const qs = queryParams.toString();

  const q = useQuery<FlightsResponse>({
    queryKey: ['flights', qs],
    queryFn: () => apiJson<FlightsResponse>(`flights?${qs}`),
    placeholderData: (prev) => prev, // keep previous page during pagination — no flash to skeleton
  });

  // Multi-param updates go through the batch helper so they commit atomically.
  // (useSearchParam's per-param setter is stale-reads-prone when chained — see
  //  hooks/useSearchParam.ts.)
  const onSortChange = (key: SortKey, dir: SortDir) => {
    update({ sort_by: key, sort_dir: dir, offset: 0 });
  };

  const resetFilters = () => {
    update({
      date_from: null,
      date_to: null,
      icao: null,
      callsign: null,
      registration: null,
      aircraft_type: null,
      source: null,
      flags: null,
      squawk: null,
      offset: 0,
    });
  };

  // CSV export inherits the current filters; sort/limit/offset aren't useful in CSV
  const exportQs = new URLSearchParams();
  for (const [k, v] of queryParams.entries()) {
    if (k === 'limit' || k === 'offset') continue;
    exportQs.set(k, v);
  }

  return (
    <div className="mx-auto max-w-7xl space-y-4 px-4 py-6" data-testid="page-history">
      <header className="space-y-1">
        <h1 className="text-xl font-semibold">Flight History</h1>
        <p className="text-sm text-[var(--color-text-dim)]">
          {q.data
            ? `${q.data.total.toLocaleString()} flights match the current filters.`
            : 'Search and filter recorded flights.'}
        </p>
      </header>

      <Card data-testid="history-filters-card">
        <CardContent className="pt-3 pb-3">
          <form
            className="grid gap-2 sm:grid-cols-2 md:grid-cols-4 lg:grid-cols-6"
            onSubmit={(e) => {
              e.preventDefault();
              setOffset(0);
            }}
            data-testid="history-filters-form"
          >
            <Field label="From" htmlFor="f-date-from">
              <Input
                id="f-date-from"
                type="date"
                value={dateFrom}
                onChange={(e) => setDateFrom(e.target.value)}
                data-testid="history-filter-date-from"
              />
            </Field>
            <Field label="To" htmlFor="f-date-to">
              <Input
                id="f-date-to"
                type="date"
                value={dateTo}
                onChange={(e) => setDateTo(e.target.value)}
                data-testid="history-filter-date-to"
              />
            </Field>
            <Field label="ICAO hex" htmlFor="f-icao">
              <Input
                id="f-icao"
                type="text"
                value={icao}
                onChange={(e) => setIcao(e.target.value)}
                placeholder="3c4b17"
                autoCorrect="off"
                autoCapitalize="off"
                data-testid="history-filter-icao"
              />
            </Field>
            <Field label="Callsign" htmlFor="f-callsign">
              <Input
                id="f-callsign"
                type="text"
                value={callsign}
                onChange={(e) => setCallsign(e.target.value)}
                placeholder="LOT"
                data-testid="history-filter-callsign"
              />
            </Field>
            <Field label="Registration" htmlFor="f-reg">
              <Input
                id="f-reg"
                type="text"
                value={registration}
                onChange={(e) => setRegistration(e.target.value)}
                placeholder="SP-LRF"
                data-testid="history-filter-registration"
              />
            </Field>
            <Field label="Aircraft type" htmlFor="f-type">
              <Input
                id="f-type"
                type="text"
                value={aircraftType}
                onChange={(e) => setAircraftType(e.target.value)}
                placeholder="B738"
                data-testid="history-filter-type"
              />
            </Field>
            <Field label="Source" htmlFor="f-source">
              <Select
                id="f-source"
                value={source}
                onChange={(e) => setSource(e.target.value)}
                data-testid="history-filter-source"
              >
                <option value="">any</option>
                <option value="adsb">ADS-B</option>
                <option value="mlat">MLAT</option>
                <option value="mixed">mixed</option>
                <option value="other">other</option>
              </Select>
            </Field>
            <Field label="Flag" htmlFor="f-flags">
              <Select
                id="f-flags"
                value={flags}
                onChange={(e) => setFlags(e.target.value)}
                data-testid="history-filter-flags"
              >
                <option value="">any</option>
                <option value="military">military</option>
                <option value="interesting">interesting</option>
                <option value="anonymous">anonymous</option>
              </Select>
            </Field>
            <Field label="Squawk" htmlFor="f-squawk">
              <Input
                id="f-squawk"
                type="text"
                value={squawk}
                onChange={(e) => setSquawk(e.target.value)}
                placeholder="7700"
                inputMode="numeric"
                data-testid="history-filter-squawk"
              />
            </Field>
            {/* Both controls go through buttonClass() so heights / padding /
                focus rings line up exactly — no more 40px reset next to a
                32px-tall plain anchor. */}
            <div className="flex items-end gap-2 sm:col-span-2 md:col-span-4 lg:col-span-1 lg:justify-end">
              <Button
                type="button"
                variant="secondary"
                size="sm"
                onClick={resetFilters}
                data-testid="history-reset"
              >
                Reset
              </Button>
              <a
                href={apiUrl(`flights/export.csv?${exportQs.toString()}`)}
                className={buttonClass('secondary', 'sm')}
                data-testid="history-export-csv"
              >
                Export CSV
              </a>
            </div>
          </form>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Results</CardTitle>
        </CardHeader>
        <CardContent>
          <FlightsTable
            flights={q.data?.flights}
            isLoading={q.isLoading}
            error={q.isError ? (q.error as Error) : null}
            sortBy={sortBy}
            sortDir={sortDir}
            onSortChange={onSortChange}
          />
          {q.data && q.data.total > PAGE_SIZE && (
            <Pagination
              total={q.data.total}
              limit={PAGE_SIZE}
              offset={offset}
              onOffsetChange={setOffset}
            />
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function Field({
  label,
  htmlFor,
  children,
}: {
  label: string;
  htmlFor: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <Label htmlFor={htmlFor}>{label}</Label>
      {children}
    </div>
  );
}
