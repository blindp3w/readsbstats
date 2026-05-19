import { lazy, Suspense, useMemo } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { ArrowLeftIcon } from '@radix-ui/react-icons';
import type { EChartsOption } from 'echarts';
import { apiJson } from '@/lib/api';
import { safeUrl } from '@/lib/safeUrl';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { Alert } from '@/components/ui/Alert';
import { Badge } from '@/components/ui/Badge';
import { Table, THead, TBody, TR, TH, TD } from '@/components/ui/Table';
import { FlagBadge, SourceBadge } from '@/components/FlagBadge';
import { useFormat } from '@/hooks/useFormat';
import { fmtDur } from '@/lib/format';
import { CHART_COLORS, baseOption, timeAxis, valueAxis } from '@/components/charts/theme';
import { EChart } from '@/components/charts/EChart';

// Heavy bits (Leaflet) lazy-loaded so other pages don't pay for them.
const RouteMap = lazy(() => import('@/components/RouteMap'));

interface Position {
  ts: number;
  lat: number | null;
  lon: number | null;
  alt_baro: number | null;
  alt_geom: number | null;
  gs: number | null;
  track: number | null;
  baro_rate: number | null;
  rssi: number | null;
  source_type: string | null;
}

interface OtherFlight {
  id: number;
  callsign: string | null;
  first_seen: number;
  duration_sec: number;
  primary_source: string | null;
  origin_icao: string | null;
  dest_icao: string | null;
}

interface FlightDetail {
  flight: {
    id: number;
    icao_hex: string;
    callsign: string | null;
    registration: string | null;
    aircraft_type: string | null;
    type_desc: string | null;
    flags: number;
    squawk: string | null;
    primary_source: string | null;
    first_seen: number;
    last_seen: number;
    duration_sec: number;
    max_alt_baro: number | null;
    max_gs: number | null;
    max_distance_nm: number | null;
    total_positions: number;
    adsb_positions: number;
    mlat_positions: number;
    origin_icao: string | null;
    dest_icao: string | null;
    origin_name: string | null;
    dest_name: string | null;
    airline_name: string | null;
  };
  positions: Position[];
  other_flights: OtherFlight[];
  receiver_lat: number | null;
  receiver_lon: number | null;
}

interface PhotoResp {
  thumbnail_url: string | null;
  large_url: string | null;
  link_url: string | null;
  photographer: string | null;
  is_type_photo: boolean;
}

export default function FlightPage() {
  const { id } = useParams<{ id: string }>();
  const flightId = Number(id);

  const detailQ = useQuery<FlightDetail>({
    queryKey: ['flight', flightId],
    queryFn: () => apiJson<FlightDetail>(`flights/${flightId}`),
    enabled: Number.isFinite(flightId),
  });

  const photoQ = useQuery<PhotoResp | null>({
    queryKey: ['flight-photo', flightId],
    queryFn: () => apiJson<PhotoResp | null>(`flights/${flightId}/photo`),
    enabled: Number.isFinite(flightId),
    staleTime: 600_000,
  });

  if (!Number.isFinite(flightId)) {
    return (
      <div className="mx-auto max-w-3xl p-6">
        <Alert variant="error">Invalid flight ID.</Alert>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-7xl space-y-4 px-4 py-6" data-testid="page-flight">
      <header>
        <Link
          to="/history"
          className="inline-flex items-center gap-1 text-xs text-[var(--color-text-dim)] hover:text-[var(--color-text)]"
        >
          <ArrowLeftIcon aria-hidden="true" />
          back to history
        </Link>
      </header>

      {detailQ.isError && (
        <Alert variant="error">Failed to load flight: {(detailQ.error as Error).message}</Alert>
      )}
      {detailQ.isLoading && <Skeleton className="h-40 w-full" />}

      {detailQ.data && (
        <>
          <FlightHeader detail={detailQ.data} photoQ={photoQ} />
          <Card data-testid="flight-map-card">
            <CardHeader>
              <CardTitle>Route</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="h-[420px] w-full lg:h-[520px]" data-testid="flight-map">
                <Suspense fallback={<Skeleton className="h-full w-full" />}>
                  <RouteMap
                    positions={detailQ.data.positions}
                    receiverLat={detailQ.data.receiver_lat}
                    receiverLon={detailQ.data.receiver_lon}
                  />
                </Suspense>
              </div>
            </CardContent>
          </Card>
          <Card data-testid="flight-profile-card">
            <CardHeader>
              <CardTitle>Altitude + speed</CardTitle>
            </CardHeader>
            <CardContent>
              <FlightProfile positions={detailQ.data.positions} />
            </CardContent>
          </Card>
          <Card data-testid="flight-positions-card">
            <CardHeader>
              <CardTitle>Position log ({detailQ.data.positions.length})</CardTitle>
            </CardHeader>
            <CardContent>
              <PositionTable positions={detailQ.data.positions} />
            </CardContent>
          </Card>
          {detailQ.data.other_flights && detailQ.data.other_flights.length > 0 && (
            <Card data-testid="flight-other-flights-card">
              <CardHeader>
                <CardTitle>
                  Other flights by{' '}
                  <Link
                    to={`/aircraft/${detailQ.data.flight.icao_hex}`}
                    className="font-mono text-[var(--color-accent)] hover:underline"
                  >
                    {detailQ.data.flight.registration ?? detailQ.data.flight.icao_hex}
                  </Link>
                </CardTitle>
              </CardHeader>
              <CardContent>
                <OtherFlightsTable rows={detailQ.data.other_flights} />
              </CardContent>
            </Card>
          )}
        </>
      )}
    </div>
  );
}

function OtherFlightsTable({ rows }: { rows: OtherFlight[] }) {
  const { fmtTs } = useFormat();
  return (
    <Table data-testid="flight-other-flights-table">
      <THead>
        <TR>
          <TH>First seen</TH>
          <TH>Callsign</TH>
          <TH>Route</TH>
          <TH>Duration</TH>
          <TH>Source</TH>
        </TR>
      </THead>
      <TBody>
        {rows.map((r) => (
          <TR key={r.id} data-testid={`flight-other-flight-${r.id}`}>
            <TD className="tabnum text-xs">
              <Link
                to={`/flight/${r.id}`}
                className="text-[var(--color-accent)] hover:underline"
              >
                {fmtTs(r.first_seen)}
              </Link>
            </TD>
            <TD className="font-mono text-xs">{r.callsign ?? '—'}</TD>
            <TD className="font-mono text-xs tabnum">
              {r.origin_icao || r.dest_icao
                ? `${r.origin_icao ?? '???'}→${r.dest_icao ?? '???'}`
                : '—'}
            </TD>
            <TD className="tabnum text-xs">{fmtDur(r.duration_sec)}</TD>
            <TD>
              <SourceBadge source={r.primary_source} />
            </TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

// ---------------------------------------------------------------------------
// Header: photo + key stats
// ---------------------------------------------------------------------------

function FlightHeader({
  detail,
  photoQ,
}: {
  detail: FlightDetail;
  photoQ: { data: PhotoResp | null | undefined; isLoading: boolean };
}) {
  const { fmtAlt, fmtSpd, fmtDist, fmtTs } = useFormat();
  const f = detail.flight;
  const photoUrl =
    safeUrl(photoQ.data?.large_url ?? null) || safeUrl(photoQ.data?.thumbnail_url ?? null);

  return (
    <Card data-testid="flight-header-card">
      <CardHeader>
        <CardTitle>
          <span className="flex flex-wrap items-center gap-2">
            <Link
              to={`/aircraft/${f.icao_hex}`}
              className="font-mono text-[var(--color-accent)] hover:underline"
            >
              {f.registration ?? f.icao_hex}
            </Link>
            <span className="text-[var(--color-text-dim)]">·</span>
            <span className="font-mono">{f.callsign ?? '—'}</span>
            <FlagBadge flags={f.flags} />
            <SourceBadge source={f.primary_source} />
            {f.squawk ? <Badge variant="warn">{f.squawk}</Badge> : null}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="grid gap-4 md:grid-cols-[220px_1fr]">
          <div>
            {photoQ.isLoading ? (
              <Skeleton className="aspect-[4/3] w-full" />
            ) : photoUrl ? (
              <div className="space-y-1">
                <div className="aspect-[4/3] overflow-hidden rounded bg-[var(--color-surface-2)]">
                  <img src={photoUrl} alt="" loading="lazy" className="h-full w-full object-cover" />
                </div>
                {photoQ.data?.photographer && (
                  <p className="text-xs text-[var(--color-text-dim)]">
                    © {photoQ.data.photographer}
                    {photoQ.data.is_type_photo ? ' (type photo)' : ''}
                  </p>
                )}
              </div>
            ) : (
              <div className="flex aspect-[4/3] items-center justify-center rounded bg-[var(--color-surface-2)] text-xs text-[var(--color-text-dim)]">
                no photo
              </div>
            )}
          </div>
          <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1.5 text-sm">
            <dt className="text-[var(--color-text-dim)]">Aircraft</dt>
            <dd>
              {f.aircraft_type ?? '—'}
              {f.type_desc ? (
                <span className="ml-1 text-[var(--color-text-dim)]">· {f.type_desc}</span>
              ) : null}
            </dd>
            {f.airline_name ? (
              <>
                <dt className="text-[var(--color-text-dim)]">Operator</dt>
                <dd>{f.airline_name}</dd>
              </>
            ) : null}
            <dt className="text-[var(--color-text-dim)]">Seen</dt>
            <dd className="tabnum">
              {fmtTs(f.first_seen)} → {fmtTs(f.last_seen)}
            </dd>
            <dt className="text-[var(--color-text-dim)]">Duration</dt>
            <dd className="tabnum">{fmtDur(f.duration_sec)}</dd>
            <dt className="text-[var(--color-text-dim)]">Route</dt>
            <dd className="font-mono">
              {f.origin_icao ?? '???'} → {f.dest_icao ?? '???'}
            </dd>
            <dt className="text-[var(--color-text-dim)]">Max alt</dt>
            <dd className="tabnum">{fmtAlt(f.max_alt_baro)}</dd>
            <dt className="text-[var(--color-text-dim)]">Max speed</dt>
            <dd className="tabnum">{fmtSpd(f.max_gs)}</dd>
            <dt className="text-[var(--color-text-dim)]">Max range</dt>
            <dd className="tabnum">{fmtDist(f.max_distance_nm)}</dd>
            <dt className="text-[var(--color-text-dim)]">Positions</dt>
            <dd className="tabnum">
              {f.total_positions.toLocaleString()}
              <span className="ml-1 text-xs text-[var(--color-text-dim)]">
                ({f.adsb_positions} ADS-B / {f.mlat_positions} MLAT)
              </span>
            </dd>
          </dl>
        </div>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Altitude + speed profile chart
// ---------------------------------------------------------------------------

interface ProfileRow {
  ts: number;
  alt: number | null;
  gs: number | null;
}

// Exported for unit tests.
export function buildFlightProfileOption(
  rows: ProfileRow[],
  altLabel: string,
  spdLabel: string,
  fmtAxisTime: (epoch: number) => string,
  fmtTs: (epoch: number) => string,
): EChartsOption {
  const base = baseOption();
  const tAxis = timeAxis() as Exclude<EChartsOption['xAxis'], undefined | unknown[]>;
  const leftAxis = valueAxis() as any;
  const rightAxis = valueAxis() as any;
  return {
    ...base,
    // Legend at the top — at `bottom: 0` it collides with the time-axis
    // tick labels on narrow viewports (and on desktop when the chart is
    // forced into a 1/3-column grid).
    legend: {
      top: 0,
      textStyle: { color: CHART_COLORS.textDim, fontSize: 12 },
      data: [altLabel, spdLabel],
    },
    grid: { top: 32, right: 40, bottom: 28, left: 44, containLabel: false },
    xAxis: {
      ...tAxis,
      axisLabel: {
        ...(tAxis as any).axisLabel,
        formatter: (v: number) => fmtAxisTime(v / 1000),
        hideOverlap: true,
      },
      axisPointer: {
        label: { formatter: (p: any) => fmtTs(p.value / 1000) },
      },
    },
    // No yAxis `name` here — the top legend already carries the series
    // names and adding `name` puts them on the same horizontal strip
    // (would crowd "Alt (m) | legend | Speed (km/h)" into the top edge).
    yAxis: [leftAxis, { ...rightAxis, position: 'right' }],
    dataZoom: [{ type: 'inside' }],
    series: [
      {
        name: altLabel,
        type: 'line',
        yAxisIndex: 0,
        color: CHART_COLORS.orange,
        data: rows.map((r) => [r.ts * 1000, r.alt]),
        showSymbol: false,
        sampling: 'lttb',
        areaStyle: { opacity: 0.4 },
      },
      {
        name: spdLabel,
        type: 'line',
        yAxisIndex: 1,
        color: CHART_COLORS.accent,
        data: rows.map((r) => [r.ts * 1000, r.gs]),
        showSymbol: false,
        sampling: 'lttb',
        lineStyle: { width: 1.5 },
      },
    ],
  };
}

function FlightProfile({ positions }: { positions: Position[] }) {
  const { altLabel, spdLabel, fmtTs, fmtAxisTime } = useFormat();
  const rows = useMemo<ProfileRow[]>(
    () =>
      positions
        .filter((p) => p.alt_baro != null || p.gs != null)
        .map((p) => ({ ts: p.ts, alt: p.alt_baro, gs: p.gs })),
    [positions],
  );
  const option = useMemo(
    () => buildFlightProfileOption(rows, altLabel(), spdLabel(), fmtAxisTime, fmtTs),
    [rows, altLabel, spdLabel, fmtAxisTime, fmtTs],
  );
  if (rows.length === 0) {
    return (
      <div className="flex h-56 items-center justify-center text-sm text-[var(--color-text-dim)]">
        no altitude / speed data
      </div>
    );
  }
  return <EChart option={option} height={260} />;
}

// ---------------------------------------------------------------------------
// Position log
// ---------------------------------------------------------------------------

function rssiColor(rssi: number | null): string {
  if (rssi == null) return CHART_COLORS.textDim;
  if (rssi >= -3) return CHART_COLORS.success;
  if (rssi >= -10) return '#a3e635';
  if (rssi >= -20) return CHART_COLORS.warn;
  if (rssi >= -30) return CHART_COLORS.orange;
  return CHART_COLORS.danger;
}

function PositionTable({ positions }: { positions: Position[] }) {
  const { fmtAlt, fmtSpd, fmtTs } = useFormat();
  if (positions.length === 0) {
    return <p className="text-sm text-[var(--color-text-dim)]">No positions recorded.</p>;
  }
  // Sample if too many — full table would be heavy DOM
  const sampled =
    positions.length > 500
      ? positions.filter((_, i) => i % Math.ceil(positions.length / 500) === 0)
      : positions;
  return (
    <div className="max-h-[480px] overflow-y-auto">
      <Table>
        <THead>
          <TR>
            <TH>Time</TH>
            <TH>Lat</TH>
            <TH>Lon</TH>
            <TH>Alt</TH>
            <TH>Speed</TH>
            <TH>RSSI</TH>
            <TH>Source</TH>
          </TR>
        </THead>
        <TBody>
          {sampled.map((p, i) => (
            <TR key={i}>
              <TD className="tabnum text-xs">{fmtTs(p.ts)}</TD>
              <TD className="font-mono tabnum text-xs">{p.lat?.toFixed(4) ?? '—'}</TD>
              <TD className="font-mono tabnum text-xs">{p.lon?.toFixed(4) ?? '—'}</TD>
              <TD className="tabnum text-xs">{fmtAlt(p.alt_baro)}</TD>
              <TD className="tabnum text-xs">{fmtSpd(p.gs)}</TD>
              <TD className="tabnum text-xs" style={{ color: rssiColor(p.rssi) }}>
                {p.rssi == null ? '—' : `${p.rssi.toFixed(1)} dBFS`}
              </TD>
              <TD>
                <SourceBadge source={p.source_type} />
              </TD>
            </TR>
          ))}
        </TBody>
      </Table>
      {sampled.length < positions.length && (
        <p className="mt-2 text-xs text-[var(--color-text-dim)]">
          Showing {sampled.length} of {positions.length} positions (sampled).
        </p>
      )}
    </div>
  );
}
