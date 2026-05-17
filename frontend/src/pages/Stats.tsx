import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  TriangleUpIcon,
  TriangleDownIcon,
  DotFilledIcon,
  ArrowRightIcon,
} from '@radix-ui/react-icons';
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from 'recharts';
import { apiJson } from '@/lib/api';
import { useRange, RangePicker } from '@/components/RangePicker';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { Alert } from '@/components/ui/Alert';
import { Badge } from '@/components/ui/Badge';
import { ActivityHeatmap } from '@/components/charts/Heatmap';
import { PolarRange } from '@/components/charts/PolarRange';
import { Table, THead, TBody, TR, TH, TD } from '@/components/ui/Table';
import { SimpleTooltip } from '@/components/ui/Tooltip';
import { useFormat } from '@/hooks/useFormat';
import { fmtBytes, fmtTs } from '@/lib/format';
import { cn } from '@/lib/cn';
import { AXIS_PROPS, CHART_COLORS, TOOLTIP_LABEL_STYLE, TOOLTIP_STYLE } from '@/components/charts/theme';

interface StatsResponse {
  total_flights: number;
  total_positions: number;
  unique_aircraft: number;
  unique_airlines: number;
  db_size_bytes: number | null;
  oldest_flight: number | null;
  flights_last_24h: number;
  flights_last_7d: number;
  source_breakdown: { adsb: number; mlat: number; other: number };
  top_airlines: { airline: string; airline_name: string | null; flights: number }[];
  // SQL aliases the column to `type`, not `aircraft_type`.
  top_aircraft_types: { type: string; type_desc: string | null; flights: number }[];
  hourly_distribution: { hour: number; count: number }[];
  // SQL returns `unique_aircraft`, not `unique`.
  daily_unique_aircraft: { day: string; unique_aircraft: number; flights: number }[];
  altitude_distribution: { band: string; count: number }[];
  military_flights: number;
  interesting_flights: number;
  anonymous_flights: number;
  heatmap: { dow: number; hour: number; count: number }[];
  top_countries: { country: string; flights: number }[];
  trends?: { flights_24h_prev: number; flights_7d_prev: number };
  frequent_aircraft: {
    icao_hex: string;
    registration: string | null;
    aircraft_type: string | null;
    flights: number;
  }[];
  top_routes?: { origin_icao: string; dest_icao: string; flights: number }[];
  top_airports?: { icao_code: string; name?: string | null; appearances?: number; flights?: number }[];
  new_aircraft?: {
    total: number;
    items: {
      icao_hex: string;
      registration: string | null;
      aircraft_type: string | null;
      type_desc: string | null;
      flags?: number;
      first_seen_ever?: number;
    }[];
  };
  squawk_counts?: { '7700'?: number; '7600'?: number; '7500'?: number };
}

interface PolarResponse {
  buckets: { bucket: number; bearing: number; max_distance_nm: number }[];
  window?: string;
  count?: number;
}

interface RecordsResponse {
  fastest: { id?: number; icao_hex: string; callsign: string | null; max_gs: number | null; first_seen: number } | null;
  furthest: { id?: number; icao_hex: string; max_distance_nm: number | null; first_seen: number } | null;
  highest: { id?: number; icao_hex: string; max_alt_baro: number | null; first_seen: number } | null;
  longest: { id?: number; icao_hex: string; duration_sec: number | null; first_seen: number } | null;
}

export default function StatsPage() {
  const { state: range, setPreset, setCustom } = useRange('all');

  const filterQs = new URLSearchParams();
  if (range.from) filterQs.set('from', String(range.from));
  if (range.to) filterQs.set('to', String(range.to));
  const qsStr = filterQs.toString();

  const statsQ = useQuery<StatsResponse>({
    queryKey: ['stats', qsStr],
    queryFn: () => apiJson<StatsResponse>(`stats${qsStr ? '?' + qsStr : ''}`),
    staleTime: 120_000,
  });
  const polarQ = useQuery<PolarResponse>({
    queryKey: ['stats-polar'],
    queryFn: () => apiJson<PolarResponse>('stats/polar'),
    staleTime: 300_000,
  });
  const recordsQ = useQuery<RecordsResponse>({
    queryKey: ['stats-records'],
    queryFn: () => apiJson<RecordsResponse>('stats/records'),
    staleTime: 300_000,
  });

  return (
    <div className="mx-auto max-w-7xl space-y-4 px-4 py-6" data-testid="page-stats">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Statistics</h1>
          <p className="text-sm text-[var(--color-text-dim)]">Receiver-wide flight aggregates.</p>
        </div>
        <RangePicker state={range} onPreset={setPreset} onCustom={setCustom} />
      </header>

      {statsQ.isError && (
        <Alert variant="error">Failed to load stats: {(statsQ.error as Error).message}</Alert>
      )}

      <SummaryCards stats={statsQ.data} loading={statsQ.isLoading} />

      <div className="grid gap-4 lg:grid-cols-2">
        <Card data-testid="stats-hourly-card">
          <CardHeader>
            <CardTitle>Activity by hour</CardTitle>
          </CardHeader>
          <CardContent>
            {statsQ.isLoading ? (
              <Skeleton className="h-56 w-full" />
            ) : (
              <BarChartBlock data={statsQ.data?.hourly_distribution ?? []} xKey="hour" yKey="count" />
            )}
          </CardContent>
        </Card>

        <Card data-testid="stats-daily-card">
          <CardHeader>
            <CardTitle>Daily unique aircraft</CardTitle>
          </CardHeader>
          <CardContent>
            {statsQ.isLoading ? (
              <Skeleton className="h-56 w-full" />
            ) : (
              <BarChartBlock
                data={statsQ.data?.daily_unique_aircraft ?? []}
                xKey="day"
                yKey="unique_aircraft"
              />
            )}
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2" data-testid="stats-heatmap-card">
          <CardHeader>
            <CardTitle>Activity heatmap (DOW × hour)</CardTitle>
          </CardHeader>
          <CardContent>
            {statsQ.isLoading ? (
              <Skeleton className="h-40 w-full" />
            ) : (
              <ActivityHeatmap rows={statsQ.data?.heatmap ?? []} />
            )}
          </CardContent>
        </Card>

        <Card data-testid="stats-polar-card">
          <CardHeader>
            <CardTitle>Polar range</CardTitle>
          </CardHeader>
          <CardContent>
            {polarQ.isLoading ? (
              <Skeleton className="h-64 w-full" />
            ) : (
              <PolarRange buckets={polarQ.data?.buckets} />
            )}
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <TopList
          title="Top aircraft types"
          data={statsQ.data?.top_aircraft_types}
          loading={statsQ.isLoading}
          labelKey="type"
          subLabelKey="type_desc"
          countKey="flights"
          testid="stats-top-types"
        />
        <TopList
          title="Top airlines"
          data={statsQ.data?.top_airlines}
          loading={statsQ.isLoading}
          labelKey="airline"
          subLabelKey="airline_name"
          countKey="flights"
          testid="stats-top-airlines"
        />
        <TopList
          title="Top countries"
          data={statsQ.data?.top_countries}
          loading={statsQ.isLoading}
          labelKey="country"
          countKey="flights"
          testid="stats-top-countries"
        />
        <FrequentAircraft data={statsQ.data?.frequent_aircraft} loading={statsQ.isLoading} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <RouteList data={statsQ.data?.top_routes} loading={statsQ.isLoading} />
        <AirportList data={statsQ.data?.top_airports} loading={statsQ.isLoading} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <NewAircraftList data={statsQ.data?.new_aircraft} loading={statsQ.isLoading} />
        <EmergencySquawks data={statsQ.data?.squawk_counts} loading={statsQ.isLoading} />
      </div>

      <Records q={recordsQ} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Summary cards
// ---------------------------------------------------------------------------

function SummaryCards({ stats, loading }: { stats: StatsResponse | undefined; loading: boolean }) {
  if (loading) {
    return (
      <div className="grid gap-3 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-24 w-full" />
        ))}
      </div>
    );
  }
  if (!stats) return null;
  const oldest = stats.oldest_flight
    ? new Date(stats.oldest_flight * 1000).toLocaleDateString()
    : '—';
  // Plain cards — total counts. v1's trend cards are below as a separate row
  // so they get the prominent treatment they deserve.
  const cards: { label: string; value: string; sub?: string; testid: string }[] = [
    {
      label: 'Total flights',
      value: stats.total_flights.toLocaleString(),
      sub: `since ${oldest}`,
      testid: 'stat-total-flights',
    },
    {
      label: 'Unique aircraft',
      value: stats.unique_aircraft.toLocaleString(),
      sub: `${stats.unique_airlines.toLocaleString()} unique airlines`,
      testid: 'stat-unique-aircraft',
    },
    {
      label: 'Position fixes',
      value: stats.total_positions.toLocaleString(),
      sub: `${stats.source_breakdown.adsb}% ADS-B · ${stats.source_breakdown.mlat}% MLAT`,
      testid: 'stat-position-fixes',
    },
    {
      label: 'DB size',
      value: stats.db_size_bytes != null ? fmtBytes(stats.db_size_bytes) : '—',
      sub: oldest !== '—' ? `oldest ${oldest}` : undefined,
      testid: 'stat-db-size',
    },
  ];
  return (
    <div className="space-y-3" data-testid="stats-summary-cards">
      <div className="grid gap-3 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4">
        {cards.map((c) => (
          <Card key={c.label} className="card-hover" data-testid={c.testid}>
            <CardContent className="space-y-1 pt-4">
              <div className="text-xs font-medium uppercase tracking-wide text-[var(--color-text-dim)]">
                {c.label}
              </div>
              <div className="tabnum text-2xl font-bold">{c.value}</div>
              {c.sub ? <div className="text-xs text-[var(--color-text-dim)]">{c.sub}</div> : null}
            </CardContent>
          </Card>
        ))}
      </div>

      <div className="grid gap-3 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-5">
        <TrendCard
          label="Last 24h"
          value={stats.flights_last_24h}
          prev={stats.trends?.flights_24h_prev ?? null}
          testid="stat-last-24h"
        />
        <TrendCard
          label="Last 7 days"
          value={stats.flights_last_7d}
          prev={stats.trends?.flights_7d_prev ?? null}
          testid="stat-last-7d"
        />
        <FlaggedCard
          label="Military"
          count={stats.military_flights}
          variant="success"
          testid="stat-military"
        />
        <FlaggedCard
          label="Interesting"
          count={stats.interesting_flights}
          variant="warn"
          testid="stat-interesting"
        />
        <FlaggedCard
          label="Anonymous"
          count={stats.anonymous_flights}
          variant="danger"
          testid="stat-anonymous"
        />
      </div>
    </div>
  );
}

function TrendCard({
  label,
  value,
  prev,
  testid,
}: {
  label: string;
  value: number;
  prev: number | null;
  testid: string;
}) {
  const delta = prev == null ? null : value - prev;
  const pct = prev != null && prev > 0 ? ((value - prev) / prev) * 100 : null;
  const ArrowIcon =
    delta == null ? null : delta > 0 ? TriangleUpIcon : delta < 0 ? TriangleDownIcon : DotFilledIcon;
  const tone =
    delta == null || delta === 0
      ? 'text-[var(--color-text-dim)]'
      : delta > 0
        ? 'text-[var(--color-success)]'
        : 'text-[var(--color-danger)]';
  return (
    <Card className="card-hover" data-testid={testid}>
      <CardContent className="space-y-1 pt-4">
        <div className="text-xs font-medium uppercase tracking-wide text-[var(--color-text-dim)]">
          {label}
        </div>
        <div className="tabnum text-2xl font-bold">{value.toLocaleString()}</div>
        <div className={`text-xs tabnum ${tone}`}>
          {delta == null ? (
            <span className="text-[var(--color-text-dim)]">no previous data</span>
          ) : (
            <span className="inline-flex items-center gap-1">
              {ArrowIcon ? <ArrowIcon aria-hidden="true" /> : null}
              <span>
                {Math.abs(delta).toLocaleString()}
                {pct != null ? ` (${pct >= 0 ? '+' : ''}${pct.toFixed(0)}%)` : ''}
              </span>
              <span className="ml-1 text-[var(--color-text-dim)]">vs prev period</span>
            </span>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function FlaggedCard({
  label,
  count,
  variant,
  testid,
}: {
  label: string;
  count: number;
  variant: 'success' | 'warn' | 'danger';
  testid: string;
}) {
  return (
    <Card data-testid={testid}>
      <CardContent className="space-y-1 pt-4">
        <Badge variant={variant}>{label}</Badge>
        <div className="tabnum text-2xl font-bold">{count.toLocaleString()}</div>
        <Link
          to={`/history?flags=${label.toLowerCase()}`}
          className="inline-flex items-center gap-1 text-xs text-[var(--color-accent)] hover:underline"
        >
          See in history
          <ArrowRightIcon aria-hidden="true" />
        </Link>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// BarChart wrapper
// ---------------------------------------------------------------------------

function BarChartBlock({
  data,
  xKey,
  yKey,
}: {
  data: Array<Record<string, string | number>>;
  xKey: string;
  yKey: string;
}) {
  return (
    <div style={{ width: '100%', height: 220 }}>
      <ResponsiveContainer>
        <BarChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid stroke={CHART_COLORS.grid} strokeDasharray="2 4" vertical={false} />
          <XAxis dataKey={xKey} {...AXIS_PROPS} />
          <YAxis allowDecimals={false} {...AXIS_PROPS} />
          <Tooltip
            cursor={{ fill: CHART_COLORS.surface }}
            contentStyle={TOOLTIP_STYLE}
            labelStyle={TOOLTIP_LABEL_STYLE}
          />
          <Bar dataKey={yKey} fill={CHART_COLORS.accent} radius={[3, 3, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Top-N lists
// ---------------------------------------------------------------------------

function TopList<T extends Record<string, unknown>>({
  title,
  data,
  loading,
  labelKey,
  subLabelKey,
  countKey,
  testid,
}: {
  title: string;
  data: T[] | undefined;
  loading: boolean;
  labelKey: keyof T;
  subLabelKey?: keyof T;
  countKey: keyof T;
  testid: string;
}) {
  return (
    <Card data-testid={testid}>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent>
        {loading ? (
          <Skeleton className="h-32 w-full" />
        ) : !data || data.length === 0 ? (
          <p className="text-sm text-[var(--color-text-dim)]">No data.</p>
        ) : (
          <Table>
            <TBody>
              {data.slice(0, 10).map((row, i) => {
                const primary = row[labelKey];
                const sub = subLabelKey ? row[subLabelKey] : null;
                return (
                  <TR key={i}>
                    <TD>
                      <div>
                        <span className="font-medium">
                          {primary != null && String(primary) !== '' ? String(primary) : '—'}
                        </span>
                        {sub && String(sub) !== '' ? (
                          <span className="ml-2 text-xs text-[var(--color-text-dim)]">
                            {String(sub)}
                          </span>
                        ) : null}
                      </div>
                    </TD>
                    <TD className="text-right tabnum">
                      {Number(row[countKey] ?? 0).toLocaleString()}
                    </TD>
                  </TR>
                );
              })}
            </TBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}

function FrequentAircraft({
  data,
  loading,
}: {
  data:
    | {
        icao_hex: string;
        registration: string | null;
        aircraft_type: string | null;
        flights: number;
      }[]
    | undefined;
  loading: boolean;
}) {
  return (
    <Card data-testid="stats-frequent-aircraft">
      <CardHeader>
        <CardTitle>Frequent visitors</CardTitle>
      </CardHeader>
      <CardContent>
        {loading ? (
          <Skeleton className="h-32 w-full" />
        ) : !data || data.length === 0 ? (
          <p className="text-sm text-[var(--color-text-dim)]">No data.</p>
        ) : (
          <Table>
            <THead>
              <TR>
                <TH>Aircraft</TH>
                <TH>Type</TH>
                <TH className="text-right">Flights</TH>
              </TR>
            </THead>
            <TBody>
              {data.slice(0, 10).map((row) => (
                <TR key={row.icao_hex}>
                  <TD className="font-mono">
                    <Link
                      to={`/aircraft/${row.icao_hex}`}
                      className="text-[var(--color-accent)] hover:underline"
                    >
                      {row.registration ?? row.icao_hex}
                    </Link>
                  </TD>
                  <TD>{row.aircraft_type ?? '—'}</TD>
                  <TD className="text-right tabnum">{row.flights.toLocaleString()}</TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Records
// ---------------------------------------------------------------------------

function Records({ q }: { q: { data: RecordsResponse | undefined; isLoading: boolean; isError: boolean } }) {
  const { fmtAlt, fmtSpd, fmtDist } = useFormat();
  if (q.isError) return null;
  return (
    <Card data-testid="stats-records-card">
      <CardHeader>
        <CardTitle>Personal records</CardTitle>
      </CardHeader>
      <CardContent>
        {q.isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : !q.data ? null : (
          <div className="grid gap-3 sm:grid-cols-2 md:grid-cols-4">
            <RecordCell
              label="Furthest"
              icao={q.data.furthest?.icao_hex}
              value={fmtDist(q.data.furthest?.max_distance_nm ?? null)}
              ts={q.data.furthest?.first_seen}
            />
            <RecordCell
              label="Fastest"
              icao={q.data.fastest?.icao_hex}
              value={fmtSpd(q.data.fastest?.max_gs ?? null)}
              ts={q.data.fastest?.first_seen}
            />
            <RecordCell
              label="Highest"
              icao={q.data.highest?.icao_hex}
              value={fmtAlt(q.data.highest?.max_alt_baro ?? null)}
              ts={q.data.highest?.first_seen}
            />
            <RecordCell
              label="Longest"
              icao={q.data.longest?.icao_hex}
              value={formatLongest(q.data.longest?.duration_sec ?? null)}
              ts={q.data.longest?.first_seen}
            />
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function RecordCell({
  label,
  icao,
  value,
  ts,
}: {
  label: string;
  icao: string | undefined;
  value: string;
  ts: number | undefined;
}) {
  return (
    <div className="rounded border border-[var(--color-border-default)] bg-[var(--color-surface-2)]/60 p-3">
      <div className="text-xs uppercase tracking-wide text-[var(--color-text-dim)]">{label}</div>
      <div className="tabnum text-xl font-bold">{value}</div>
      {icao ? (
        <Link
          to={`/aircraft/${icao}`}
          className="font-mono text-xs text-[var(--color-accent)] hover:underline"
        >
          {icao}
        </Link>
      ) : null}
      {ts ? <div className="text-xs text-[var(--color-text-dim)] tabnum">{fmtTs(ts)}</div> : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Top routes, top airports, new aircraft, emergency squawks — sections that
// were in v1 stats.html and are restored here for parity.
// ---------------------------------------------------------------------------

function RouteList({
  data,
  loading,
}: {
  data: { origin_icao: string; dest_icao: string; flights: number }[] | undefined;
  loading: boolean;
}) {
  return (
    <Card data-testid="stats-top-routes">
      <CardHeader>
        <CardTitle>Top routes</CardTitle>
      </CardHeader>
      <CardContent>
        {loading ? (
          <Skeleton className="h-32 w-full" />
        ) : !data || data.length === 0 ? (
          <p className="text-sm text-[var(--color-text-dim)]">No data.</p>
        ) : (
          <Table>
            <TBody>
              {data.slice(0, 10).map((r, i) => (
                <TR key={i}>
                  <TD className="font-mono tabnum">
                    {(r.origin_icao || '???') + '→' + (r.dest_icao || '???')}
                  </TD>
                  <TD className="text-right tabnum">{r.flights.toLocaleString()}</TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}

function AirportList({
  data,
  loading,
}: {
  data: { icao_code: string; name?: string | null; appearances?: number; flights?: number }[] | undefined;
  loading: boolean;
}) {
  return (
    <Card data-testid="stats-top-airports">
      <CardHeader>
        <CardTitle>Top airports</CardTitle>
      </CardHeader>
      <CardContent>
        {loading ? (
          <Skeleton className="h-32 w-full" />
        ) : !data || data.length === 0 ? (
          <p className="text-sm text-[var(--color-text-dim)]">No data.</p>
        ) : (
          <Table>
            <TBody>
              {data.slice(0, 10).map((r) => (
                <TR key={r.icao_code}>
                  <TD>
                    <span className="font-mono">{r.icao_code}</span>
                    {r.name ? (
                      <span className="ml-2 text-xs text-[var(--color-text-dim)]">{r.name}</span>
                    ) : null}
                  </TD>
                  <TD className="text-right tabnum">
                    {(r.appearances ?? r.flights ?? 0).toLocaleString()}
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}

function NewAircraftList({
  data,
  loading,
}: {
  data:
    | {
        total: number;
        items: {
          icao_hex: string;
          registration: string | null;
          aircraft_type: string | null;
          type_desc: string | null;
          first_seen_ever?: number;
        }[];
      }
    | undefined;
  loading: boolean;
}) {
  return (
    <Card data-testid="stats-new-aircraft">
      <CardHeader>
        <CardTitle>
          New aircraft
          {data ? (
            <span className="ml-2 text-xs font-normal text-[var(--color-text-dim)] tabnum">
              {data.total.toLocaleString()} total in window
            </span>
          ) : null}
        </CardTitle>
      </CardHeader>
      <CardContent>
        {loading ? (
          <Skeleton className="h-32 w-full" />
        ) : !data || data.items.length === 0 ? (
          <p className="text-sm text-[var(--color-text-dim)]">
            No new aircraft in the selected window.
          </p>
        ) : (
          <Table>
            <THead>
              <TR>
                <TH>Aircraft</TH>
                <TH>Type</TH>
                <TH className="text-right">First seen</TH>
              </TR>
            </THead>
            <TBody>
              {data.items.map((r) => (
                <TR key={r.icao_hex}>
                  <TD className="font-mono">
                    <Link
                      to={`/aircraft/${r.icao_hex}`}
                      className="text-[var(--color-accent)] hover:underline"
                    >
                      {r.registration ?? r.icao_hex}
                    </Link>
                  </TD>
                  <TD>
                    {r.aircraft_type ?? '—'}
                    {r.type_desc ? (
                      <span className="ml-1 text-xs text-[var(--color-text-dim)]">
                        {r.type_desc}
                      </span>
                    ) : null}
                  </TD>
                  <TD className="text-right text-xs text-[var(--color-text-dim)] tabnum">
                    {fmtTs(r.first_seen_ever ?? null)}
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}

const SQUAWK_LABELS: Record<string, string> = {
  '7700': 'General emergency',
  '7600': 'Radio failure',
  '7500': 'Hijack',
};

function EmergencySquawks({
  data,
  loading,
}: {
  data: { '7700'?: number; '7600'?: number; '7500'?: number } | undefined;
  loading: boolean;
}) {
  return (
    <Card data-testid="stats-emergency-squawks">
      <CardHeader>
        <CardTitle>Emergency squawks</CardTitle>
      </CardHeader>
      <CardContent>
        {loading ? (
          <Skeleton className="h-24 w-full" />
        ) : (
          <div className="grid grid-cols-3 gap-2">
            {(['7700', '7600', '7500'] as const).map((code) => {
              const count = data?.[code] ?? 0;
              return (
                <SimpleTooltip
                  key={code}
                  content={`${count.toLocaleString()} flights with squawk ${code}`}
                >
                  <Link
                    to={`/history?squawk=${code}`}
                    className={cn(
                      'rounded border border-[var(--color-border-default)] bg-[var(--color-surface-2)]/60 p-3 text-center transition-colors',
                      count > 0
                        ? 'hover:border-[var(--color-danger)] hover:bg-[var(--color-surface-3)]'
                        : 'hover:bg-[var(--color-surface-2)]',
                    )}
                    data-testid={`stats-squawk-${code}`}
                  >
                    <div className="font-mono text-xs text-[var(--color-text-dim)]">{code}</div>
                    <div
                      className={cn(
                        'tabnum text-2xl font-bold',
                        count > 0 ? 'text-[var(--color-danger)]' : 'text-[var(--color-text-dim)]',
                      )}
                    >
                      {count.toLocaleString()}
                    </div>
                    <div className="text-[10px] uppercase tracking-wide text-[var(--color-text-dim)]">
                      {SQUAWK_LABELS[code]}
                    </div>
                  </Link>
                </SimpleTooltip>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function formatLongest(seconds: number | null): string {
  if (seconds == null) return '—';
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}
