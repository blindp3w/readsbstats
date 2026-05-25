import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import type { EChartsOption } from 'echarts';
import { apiJson } from '@/lib/api';
import { useRange, RangePicker, type RangeValue } from '@/components/RangePicker';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { Alert } from '@/components/ui/Alert';
import { ActivityHeatmap } from '@/components/charts/Heatmap';
import { PolarRange } from '@/components/charts/PolarRange';
import { Table, THead, TBody, TR, TH, TD } from '@/components/ui/Table';
import { useFormat } from '@/hooks/useFormat';
import { cn } from '@/lib/cn';
import { CHART_COLORS, baseOption, valueAxis } from '@/components/charts/theme';
import { EChart } from '@/components/charts/EChart';
import { TopChart } from '@/components/charts/TopChart';
import { KpiCard } from '@/components/stats/KpiCard';
import { FlagBadgeStrip } from '@/components/stats/FlagBadgeStrip';
import { RangeContextLine } from '@/components/stats/RangeContextLine';
import { AboutReceiverFooter } from '@/components/stats/AboutReceiverFooter';
import { SectionAnchors } from '@/components/stats/SectionAnchors';
import { TopChartMultiples } from '@/components/stats/TopChartMultiples';

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
  // SQL returns `unique_aircraft`, not `unique`. Ordered ASC by day.
  daily_unique_aircraft: { day: string; unique_aircraft: number; flights: number }[];
  altitude_distribution: { band: string; count: number }[];
  military_flights: number;
  interesting_flights: number;
  anonymous_flights: number;
  heatmap: { dow: number; hour: number; count: number }[];
  top_countries: { country: string; flights: number }[];
  trends?: { flights_24h_prev: number; flights_7d_prev: number };
  // Totals for the period of equal length immediately preceding the
  // requested window. Backend returns this only when `from`/`to` are
  // supplied; unfiltered (all-time) requests get `null`. Drives the
  // delta chip on every numeric KPI card.
  previous_window?: {
    from_ts: number;
    to_ts: number;
    total_flights: number;
    total_positions: number;
    unique_aircraft: number;
  } | null;
  frequent_aircraft: {
    icao_hex: string;
    registration: string | null;
    aircraft_type: string | null;
    flights: number;
  }[];
  top_routes?: { origin_icao: string; dest_icao: string; flights: number }[];
  top_airports?: {
    icao_code: string;
    name?: string | null;
    appearances?: number;
    flights?: number;
  }[];
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
  // Window-scoped furthest flight. Backend returns the full flight row
  // (web.py:1636), or null for empty windows.
  furthest_aircraft?: {
    icao_hex: string;
    callsign: string | null;
    registration?: string | null;
    aircraft_type: string | null;
    type_desc: string | null;
    max_distance_nm: number | null;
  } | null;
  // Lifetime block — receiver-wide totals that DO NOT change when the
  // user picks a window. Consumed by the "About this receiver" footer.
  // Always present; the same values as the top-level fields when the
  // request is unfiltered.
  lifetime?: {
    total_flights: number;
    total_positions: number;
    unique_aircraft: number;
    unique_airlines: number;
    oldest_flight: number | null;
    db_size_bytes: number | null;
    source_breakdown: { adsb: number; mlat: number; other: number };
  };
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
  fastest: (RecordEntry & { max_gs: number | null }) | null;
  furthest: (RecordEntry & { max_distance_nm: number | null }) | null;
  highest: (RecordEntry & { max_alt_baro: number | null }) | null;
  longest: (RecordEntry & { duration_sec: number | null }) | null;
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
    placeholderData: (prev) => prev,
  });
  const polarQ = useQuery<PolarResponse>({
    queryKey: ['stats-polar'],
    queryFn: () => apiJson<PolarResponse>('stats/polar'),
    staleTime: 300_000,
    placeholderData: (prev) => prev,
  });
  const recordsQ = useQuery<RecordsResponse>({
    queryKey: ['stats-records'],
    queryFn: () => apiJson<RecordsResponse>('stats/records'),
    staleTime: 300_000,
    placeholderData: (prev) => prev,
  });

  const stats = statsQ.data;
  const isRefetching = statsQ.isFetching && !statsQ.isLoading;

  // Sparkline series — derived per-KPI from already-fetched data. See plan §
  // "Data flow" for the window→source mapping.
  const flightsSeries = pickFlightsSeries(range.value, stats);
  const uniqueSeries = pickUniqueSeries(range.value, stats);
  const positionsSeries = stats?.hourly_distribution?.map((h) => h.count) ?? [];

  const flightsDelta = pickFlightsDelta(range.value, stats);

  return (
    <div
      className="mx-auto max-w-7xl space-y-4 px-4 py-6 [&_section]:scroll-mt-[calc(var(--rsbs-nav-h,41px)+60px)]"
      data-testid="page-stats"
    >
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Statistics</h1>
          <p className="text-sm text-[var(--color-text-dim)]">Receiver-wide flight aggregates.</p>
        </div>
      </header>

      <RangePicker
        state={range}
        onPreset={setPreset}
        onCustom={setCustom}
        sticky
        right={<RangeContextLine state={range} isFetching={isRefetching} />}
      />

      <SectionAnchors />

      {statsQ.isError && (
        <Alert variant="error">Failed to load stats: {(statsQ.error as Error).message}</Alert>
      )}

      <section id="overview" className="space-y-3" aria-labelledby="overview-heading">
        <h2 id="overview-heading" className="sr-only">
          Overview
        </h2>
        {statsQ.isLoading ? (
          <KpiSkeletons />
        ) : (
          <div className="grid grid-cols-1 gap-3 xs:grid-cols-2 xl:grid-cols-4">
            <KpiCard
              label="Flights"
              value={stats?.total_flights ?? 0}
              prev={stats?.previous_window?.total_flights ?? flightsDelta?.prev ?? null}
              series={flightsSeries}
              testid="kpi-flights"
            />
            <KpiCard
              label="Unique aircraft"
              value={stats?.unique_aircraft ?? 0}
              prev={stats?.previous_window?.unique_aircraft ?? null}
              series={uniqueSeries}
              sublabel={`${(stats?.unique_airlines ?? 0).toLocaleString()} unique airlines`}
              testid="kpi-unique-aircraft"
            />
            <KpiCard
              label="Position fixes"
              value={stats?.total_positions ?? 0}
              prev={stats?.previous_window?.total_positions ?? null}
              series={positionsSeries}
              sublabel={
                stats
                  ? `${stats.source_breakdown.adsb}% ADS-B · ${stats.source_breakdown.mlat}% MLAT`
                  : undefined
              }
              testid="kpi-positions"
            />
            <MaxRangeCard furthest={stats?.furthest_aircraft ?? null} />
          </div>
        )}

        {statsQ.isLoading ? (
          <div className="flex flex-wrap items-center gap-2">
            {Array.from({ length: 6 }).map((_, i) => (
              <Skeleton key={i} className="h-9 w-28 rounded-full" />
            ))}
          </div>
        ) : stats ? (
          <FlagBadgeStrip
            counts={{
              military: stats.military_flights,
              interesting: stats.interesting_flights,
              anonymous: stats.anonymous_flights,
              squawks: stats.squawk_counts ?? {},
            }}
          />
        ) : null}
      </section>

      <section id="activity" className="space-y-4" aria-labelledby="activity-heading">
        <h2 id="activity-heading" className="sr-only">
          Activity
        </h2>
        <div className="grid gap-4 lg:grid-cols-2">
          <Card data-testid="stats-hourly-card">
            <CardHeader>
              <CardTitle>Activity by hour</CardTitle>
            </CardHeader>
            <CardContent>
              {statsQ.isLoading ? (
                <Skeleton className="h-56 w-full" />
              ) : (stats?.hourly_distribution?.length ?? 0) === 0 ? (
                <EmptyChartNote />
              ) : (
                <BarChartBlock data={stats?.hourly_distribution ?? []} xKey="hour" yKey="count" />
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
              ) : (stats?.daily_unique_aircraft?.length ?? 0) === 0 ? (
                <EmptyChartNote />
              ) : (
                <BarChartBlock
                  data={stats?.daily_unique_aircraft ?? []}
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
              <ActivityHeatmap rows={stats?.heatmap ?? []} />
            )}
          </CardContent>
        </Card>
      </section>

      <section id="rankings" className="space-y-4" aria-labelledby="rankings-heading">
        <h2 id="rankings-heading" className="sr-only">
          Rankings
        </h2>
        <div className="xl:hidden">
          <TopChart
            loading={statsQ.isLoading}
            top_aircraft_types={stats?.top_aircraft_types}
            top_airlines={stats?.top_airlines}
            top_countries={stats?.top_countries}
            frequent_aircraft={stats?.frequent_aircraft}
            top_routes={stats?.top_routes}
            top_airports={stats?.top_airports}
          />
        </div>
        <div className="hidden xl:block">
          <TopChartMultiples
            loading={statsQ.isLoading}
            top_aircraft_types={stats?.top_aircraft_types}
            top_airlines={stats?.top_airlines}
            top_countries={stats?.top_countries}
            frequent_aircraft={stats?.frequent_aircraft}
            top_routes={stats?.top_routes}
            top_airports={stats?.top_airports}
          />
        </div>

        <Records q={recordsQ} />
      </section>

      <section id="coverage" className="space-y-4" aria-labelledby="coverage-heading">
        <h2 id="coverage-heading" className="sr-only">
          Coverage
        </h2>
        <div className="grid gap-4 lg:grid-cols-2">
          <NewAircraftList data={stats?.new_aircraft} loading={statsQ.isLoading} />
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
      </section>

      <AboutReceiverFooter
        totalFlights={stats?.lifetime?.total_flights}
        uniqueAirlines={stats?.lifetime?.unique_airlines}
        totalPositions={stats?.lifetime?.total_positions}
        dbSizeBytes={stats?.lifetime?.db_size_bytes}
        oldestFlight={stats?.lifetime?.oldest_flight}
        sourceBreakdown={stats?.lifetime?.source_breakdown}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// KPI helpers
// ---------------------------------------------------------------------------

function KpiSkeletons() {
  return (
    <div className="grid grid-cols-1 gap-3 xs:grid-cols-2 xl:grid-cols-4">
      {Array.from({ length: 4 }).map((_, i) => (
        <Skeleton key={i} className="h-28 w-full" />
      ))}
    </div>
  );
}

function EmptyChartNote() {
  return (
    <p className="py-12 text-center text-sm text-[var(--color-text-dim)]">
      No flights in this window.
    </p>
  );
}

function pickFlightsSeries(range: RangeValue, stats: StatsResponse | undefined): number[] {
  if (!stats) return [];
  if (range === '24h') return stats.hourly_distribution?.map((h) => h.count) ?? [];
  return stats.daily_unique_aircraft?.map((d) => d.flights) ?? [];
}

function pickUniqueSeries(range: RangeValue, stats: StatsResponse | undefined): number[] {
  if (!stats) return [];
  if (range === '24h') return []; // No per-hour unique-aircraft series available.
  return stats.daily_unique_aircraft?.map((d) => d.unique_aircraft) ?? [];
}

function pickFlightsDelta(
  range: RangeValue,
  stats: StatsResponse | undefined,
): { current: number; prev: number } | null {
  if (!stats) return null;
  if (range === '24h' && stats.trends) {
    return { current: stats.flights_last_24h, prev: stats.trends.flights_24h_prev };
  }
  if (range === '7d' && stats.trends) {
    return { current: stats.flights_last_7d, prev: stats.trends.flights_7d_prev };
  }
  return null;
}

function MaxRangeCard({ furthest }: { furthest: StatsResponse['furthest_aircraft'] }) {
  const { fmtDist } = useFormat();
  if (!furthest || furthest.max_distance_nm == null) {
    return <KpiCard label="Max range" value="—" testid="kpi-max-range" />;
  }
  const linkLabel = furthest.callsign ?? furthest.registration ?? furthest.icao_hex;
  return (
    <KpiCard
      label="Max range"
      value={fmtDist(furthest.max_distance_nm)}
      testid="kpi-max-range"
      sublabel={
        <Link
          to={`/aircraft/${furthest.icao_hex}`}
          className="font-mono text-[var(--color-accent)] hover:underline"
        >
          {linkLabel}
        </Link>
      }
    />
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

function Records({
  q,
}: {
  q: { data: RecordsResponse | undefined; isLoading: boolean; isError: boolean };
}) {
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
          <div className="grid gap-3 xs:grid-cols-2 md:grid-cols-4">
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

function formatLongest(seconds: number | null): string {
  if (seconds == null) return '—';
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}
