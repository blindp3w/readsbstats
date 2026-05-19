import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  TriangleUpIcon,
  TriangleDownIcon,
  DotFilledIcon,
} from '@radix-ui/react-icons';
import type { EChartsOption } from 'echarts';
import { apiJson } from '@/lib/api';
import { useRange, RangePicker } from '@/components/RangePicker';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { Alert } from '@/components/ui/Alert';
import { Badge } from '@/components/ui/Badge';
import { ActivityHeatmap } from '@/components/charts/Heatmap';
import { PolarRange } from '@/components/charts/PolarRange';
import { Table, THead, TBody, TR, TH, TD } from '@/components/ui/Table';
import { useFormat } from '@/hooks/useFormat';
import { fmtBytes } from '@/lib/format';
import { cn } from '@/lib/cn';
import { CHART_COLORS, baseOption, valueAxis } from '@/components/charts/theme';
import { EChart } from '@/components/charts/EChart';
import { TopChart } from '@/components/charts/TopChart';
import { SimpleTooltip } from '@/components/ui/Tooltip';

export interface StatsResponse {
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

interface RecordEntry {
  id?: number;
  icao_hex: string;
  callsign: string | null;
  aircraft_type: string | null;
  type_desc: string | null;
  first_seen: number;
}

interface RecordsResponse {
  fastest:  (RecordEntry & { max_gs: number | null }) | null;
  furthest: (RecordEntry & { max_distance_nm: number | null }) | null;
  highest:  (RecordEntry & { max_alt_baro: number | null }) | null;
  longest:  (RecordEntry & { duration_sec: number | null }) | null;
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

      <Card data-testid="stats-heatmap-card">
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

      <div className="grid gap-4 lg:grid-cols-2">
        <NewAircraftList data={statsQ.data?.new_aircraft} loading={statsQ.isLoading} />
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

      <TopChart
        loading={statsQ.isLoading}
        top_aircraft_types={statsQ.data?.top_aircraft_types}
        top_airlines={statsQ.data?.top_airlines}
        top_countries={statsQ.data?.top_countries}
        frequent_aircraft={statsQ.data?.frequent_aircraft}
        top_routes={statsQ.data?.top_routes}
        top_airports={statsQ.data?.top_airports}
      />

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
      <div className="space-y-3" data-testid="stats-summary-cards">
        <div className="grid gap-3 grid-cols-2 sm:grid-cols-3 lg:grid-cols-6">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-24 w-full" />
          ))}
        </div>
        <div className="grid gap-3 grid-cols-2 sm:grid-cols-3 lg:grid-cols-6">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-24 w-full" />
          ))}
        </div>
      </div>
    );
  }
  if (!stats) return null;
  const oldest = stats.oldest_flight
    ? new Date(stats.oldest_flight * 1000).toLocaleDateString()
    : '—';
  return (
    <div className="space-y-3" data-testid="stats-summary-cards">
      {/* Row 1: core metrics with trend cards inserted after Total flights */}
      <div className="grid gap-3 grid-cols-2 sm:grid-cols-3 lg:grid-cols-6">
        <Card className="card-hover" data-testid="stat-total-flights">
          <CardContent className="space-y-1 pt-4">
            <div className="text-xs font-medium uppercase tracking-wide text-[var(--color-text-dim)]">
              Total flights
            </div>
            <div className="tabnum text-2xl font-bold">{stats.total_flights.toLocaleString()}</div>
            <div className="text-xs text-[var(--color-text-dim)]">since {oldest}</div>
          </CardContent>
        </Card>
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
        <Card className="card-hover" data-testid="stat-unique-aircraft">
          <CardContent className="space-y-1 pt-4">
            <div className="text-xs font-medium uppercase tracking-wide text-[var(--color-text-dim)]">
              Unique aircraft
            </div>
            <div className="tabnum text-2xl font-bold">{stats.unique_aircraft.toLocaleString()}</div>
            <div className="text-xs text-[var(--color-text-dim)]">
              {stats.unique_airlines.toLocaleString()} unique airlines
            </div>
          </CardContent>
        </Card>
        <Card className="card-hover" data-testid="stat-position-fixes">
          <CardContent className="space-y-1 pt-4">
            <div className="text-xs font-medium uppercase tracking-wide text-[var(--color-text-dim)]">
              Position fixes
            </div>
            <div className="tabnum text-2xl font-bold">{stats.total_positions.toLocaleString()}</div>
            <div className="text-xs text-[var(--color-text-dim)]">
              {stats.source_breakdown.adsb}% ADS-B · {stats.source_breakdown.mlat}% MLAT
            </div>
          </CardContent>
        </Card>
        <Card className="card-hover" data-testid="stat-db-size">
          <CardContent className="space-y-1 pt-4">
            <div className="text-xs font-medium uppercase tracking-wide text-[var(--color-text-dim)]">
              DB size
            </div>
            <div className="tabnum text-2xl font-bold">
              {stats.db_size_bytes != null ? fmtBytes(stats.db_size_bytes) : '—'}
            </div>
            {oldest !== '—' ? (
              <div className="text-xs text-[var(--color-text-dim)]">oldest {oldest}</div>
            ) : null}
          </CardContent>
        </Card>
      </div>

      {/* Row 2: flags + squawks */}
      <div className="grid gap-3 grid-cols-2 sm:grid-cols-3 lg:grid-cols-6">
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
        {(['7700', '7600', '7500'] as const).map((code) => {
          const count = stats.squawk_counts?.[code] ?? 0;
          return (
            <Link
              key={code}
              to={`/history?squawk=${code}`}
              data-testid={`stat-squawk-${code}`}
              className={cn(
                'block rounded-lg border border-[var(--color-border-default)]',
                'bg-[var(--color-surface-2)]/60 p-4 text-center shadow-[var(--shadow-sm)]',
                'transition-colors',
                count > 0
                  ? 'hover:border-[var(--color-danger)] hover:bg-[var(--color-surface-3)]'
                  : 'hover:bg-[var(--color-surface-2)]',
              )}
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
          );
        })}
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
  const tooltipContent =
    delta == null ? (
      'No previous period data'
    ) : (
      <span className="inline-flex items-center gap-1">
        {ArrowIcon ? <ArrowIcon aria-hidden="true" /> : null}
        <span className={tone}>
          {delta >= 0 ? '+' : '−'}{Math.abs(delta).toLocaleString()}
          {pct != null ? ` (${pct >= 0 ? '+' : ''}${pct.toFixed(0)}%)` : ''}
        </span>
        <span className="text-[var(--color-text-dim)]">vs previous period</span>
      </span>
    );
  return (
    <SimpleTooltip content={tooltipContent} delayDuration={300}>
      <div>
        <Card className="card-hover" data-testid={testid}>
          <CardContent className="space-y-1 pt-4">
            <div className="text-xs font-medium uppercase tracking-wide text-[var(--color-text-dim)]">
              {label}
            </div>
            <div className="tabnum text-2xl font-bold">{value.toLocaleString()}</div>
            <div className={`text-xs tabnum ${tone}`}>
              {delta == null ? (
                <span className="text-[var(--color-text-dim)]">—</span>
              ) : (
                <span className="inline-flex items-center gap-1">
                  {ArrowIcon ? <ArrowIcon aria-hidden="true" /> : null}
                  <span>
                    {delta >= 0 ? '+' : '−'}{Math.abs(delta).toLocaleString()}
                    {pct != null ? ` (${pct >= 0 ? '+' : ''}${pct.toFixed(0)}%)` : ''}
                  </span>
                </span>
              )}
            </div>
          </CardContent>
        </Card>
      </div>
    </SimpleTooltip>
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
    <Link
      to={`/history?flags=${label.toLowerCase()}`}
      data-testid={testid}
      aria-label={`View ${label} flights in history`}
      className="block rounded-lg border border-[var(--color-border-default)] bg-[var(--color-surface-2)]/60 p-4 shadow-[var(--shadow-sm)] transition-colors hover:bg-[var(--color-surface-2)]"
    >
      <Badge variant={variant}>{label}</Badge>
      <div className="tabnum text-2xl font-bold mt-1">{count.toLocaleString()}</div>
    </Link>
  );
}

// ---------------------------------------------------------------------------
// BarChart wrapper
// ---------------------------------------------------------------------------

// Exported for unit tests.
export function buildBarOption(
  data: Array<Record<string, string | number>>,
  xKey: string,
  yKey: string,
): EChartsOption {
  return {
    ...baseOption(),
    xAxis: {
      type: 'category',
      data: data.map((d) => String(d[xKey])),
      axisLine: { lineStyle: { color: CHART_COLORS.grid } },
      axisLabel: { color: CHART_COLORS.textDim },
    },
    yAxis: valueAxis(),
    series: [
      {
        type: 'bar',
        data: data.map((d) => Number(d[yKey] ?? 0)),
        itemStyle: { color: CHART_COLORS.accent, borderRadius: [3, 3, 0, 0] },
      },
    ],
  };
}

function BarChartBlock({
  data,
  xKey,
  yKey,
}: {
  data: Array<Record<string, string | number>>;
  xKey: string;
  yKey: string;
}) {
  const option = useMemo(() => buildBarOption(data, xKey, yKey), [data, xKey, yKey]);
  return <EChart option={option} height={220} />;
}


// ---------------------------------------------------------------------------
// Records
// ---------------------------------------------------------------------------

function Records({ q }: { q: { data: RecordsResponse | undefined; isLoading: boolean; isError: boolean } }) {
  const { fmtAlt, fmtSpd, fmtDist, fmtTs } = useFormat();
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
              callsign={q.data.furthest?.callsign}
              typeDesc={q.data.furthest?.type_desc}
              aircraftType={q.data.furthest?.aircraft_type}
              value={fmtDist(q.data.furthest?.max_distance_nm ?? null)}
              ts={q.data.furthest?.first_seen}
              fmtTs={fmtTs}
            />
            <RecordCell
              label="Fastest"
              icao={q.data.fastest?.icao_hex}
              callsign={q.data.fastest?.callsign}
              typeDesc={q.data.fastest?.type_desc}
              aircraftType={q.data.fastest?.aircraft_type}
              value={fmtSpd(q.data.fastest?.max_gs ?? null)}
              ts={q.data.fastest?.first_seen}
              fmtTs={fmtTs}
            />
            <RecordCell
              label="Highest"
              icao={q.data.highest?.icao_hex}
              callsign={q.data.highest?.callsign}
              typeDesc={q.data.highest?.type_desc}
              aircraftType={q.data.highest?.aircraft_type}
              value={fmtAlt(q.data.highest?.max_alt_baro ?? null)}
              ts={q.data.highest?.first_seen}
              fmtTs={fmtTs}
            />
            <RecordCell
              label="Longest"
              icao={q.data.longest?.icao_hex}
              callsign={q.data.longest?.callsign}
              typeDesc={q.data.longest?.type_desc}
              aircraftType={q.data.longest?.aircraft_type}
              value={formatLongest(q.data.longest?.duration_sec ?? null)}
              ts={q.data.longest?.first_seen}
              fmtTs={fmtTs}
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
  callsign,
  typeDesc,
  aircraftType,
  value,
  ts,
  fmtTs,
}: {
  label: string;
  icao: string | undefined;
  callsign?: string | null;
  typeDesc?: string | null;
  aircraftType?: string | null;
  value: string;
  ts: number | undefined;
  fmtTs: (epoch: number | null | undefined) => string;
}) {
  const linkLabel = callsign ?? icao;
  const typeLabel = typeDesc ?? aircraftType ?? null;
  return (
    <div className="rounded border border-[var(--color-border-default)] bg-[var(--color-surface-2)]/60 p-3">
      <div className="flex items-baseline justify-between gap-2">
        <div className="text-xs uppercase tracking-wide text-[var(--color-text-dim)]">{label}</div>
        {ts && <div className="tabnum text-xs text-[var(--color-text-dim)]">{fmtTs(ts)}</div>}
      </div>
      <div className="tabnum mt-1 text-xl font-bold">{value}</div>
      {icao && (
        <div className="mt-0.5 flex items-baseline gap-1.5 text-xs tabnum text-[var(--color-text-dim)]">
          <Link
            to={`/aircraft/${icao}`}
            className={cn('text-[var(--color-accent)] hover:underline', !callsign && 'font-mono')}
          >
            {linkLabel}
          </Link>
          {typeLabel && <span>· {typeLabel}</span>}
        </div>
      )}
    </div>
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
  const { fmtTs } = useFormat();
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
  '7700': 'Emergency',
  '7600': 'Radio failure',
  '7500': 'Hijack',
};

function formatLongest(seconds: number | null): string {
  if (seconds == null) return '—';
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}
