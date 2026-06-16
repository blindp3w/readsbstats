import { lazy, Suspense, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { ArrowLeftIcon } from '@radix-ui/react-icons';
import { apiJson } from '@/lib/api';
import { errMsg } from '@/lib/errMsg';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { Alert } from '@/components/ui/Alert';
import { Table, THead, TBody, TR, TH, TD } from '@/components/ui/Table';
import { SourceBadge } from '@/components/FlagBadge';
import { useFormat } from '@/hooks/useFormat';
import { fmtDur } from '@/lib/format';
import { CHART_COLORS } from '@/components/charts/theme';
import { EChart } from '@/components/charts/EChart';
import { buildFlightProfileOption, type ProfileRow } from './flightCharts';
import { IsolationPills } from '@/components/charts/IsolationPills';
import { AcarsPanel } from '@/components/vdl2/AcarsPanel';
import { OooiCard } from '@/components/vdl2/OooiCard';
import { FlightHeader } from '@/components/flight/FlightHeader';
import { PositionTable } from '@/components/flight/PositionTable';
import type { FlightDetail, OtherFlight, PhotoResp, Position } from '@/components/flight/types';

// Heavy bits (MapLibre GL) lazy-loaded so other pages don't pay for them.
const RouteMap = lazy(() => import('@/components/RouteMap'));

// BE-10/FE-1: the raw position timeline is no longer embedded in the
// detail payload. Chart + map consume the LTTB-downsampled series; the
// position-log table consumes the paginated raw endpoint.
interface PositionChartResp {
  total: number;
  target: number;
  positions: Position[];
}
interface PositionPageResp {
  total: number;
  limit: number;
  offset: number;
  positions: Position[];
}

export default function FlightPage() {
  const { id } = useParams<{ id: string }>();
  const flightId = Number(id);

  const detailQ = useQuery<FlightDetail>({
    queryKey: ['flight', flightId],
    queryFn: () => apiJson<FlightDetail>(`flights/${flightId}`),
    enabled: Number.isFinite(flightId),
  });

  // The profile chart and the map both consume the LTTB-downsampled endpoint
  // (target=2000) so long flights (>5k positions) stay responsive; the detail
  // payload no longer embeds the timeline (BE-10). The chart thins this series
  // further at render via ECharts `sampling: 'lttb'`, so a separate target=500
  // fetch is redundant (audit 2026-06-15). The header's at-max sublabels also
  // derive from this series (baro_rate/track/lat/lon).
  const mapPositionsQ = useQuery<PositionChartResp>({
    queryKey: ['flight-chart', flightId, 'map'],
    queryFn: () => apiJson<PositionChartResp>(`flights/${flightId}/positions/chart?target=2000`),
    enabled: Number.isFinite(flightId),
    staleTime: 300_000,
  });
  // Paginated raw positions for the inspection table (capped server-side
  // at 2000/page). `total` drives the position-log count.
  const positionPageQ = useQuery<PositionPageResp>({
    queryKey: ['flight-positions', flightId],
    queryFn: () => apiJson<PositionPageResp>(`flights/${flightId}/positions?limit=2000&offset=0`),
    enabled: Number.isFinite(flightId),
    staleTime: 300_000,
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
        <Alert variant="error">Failed to load flight: {errMsg(detailQ.error)}</Alert>
      )}
      {detailQ.isLoading && <Skeleton className="h-40 w-full" />}

      {detailQ.data && (
        <>
          <FlightHeader
            detail={detailQ.data}
            photoQ={photoQ}
            positions={mapPositionsQ.data?.positions ?? []}
            receiverLat={detailQ.data.receiver_lat}
            receiverLon={detailQ.data.receiver_lon}
          />
          <Card data-testid="flight-map-card">
            <CardHeader>
              <CardTitle>Route</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="h-[420px] w-full lg:h-[520px]" data-testid="flight-map">
                <Suspense fallback={<Skeleton className="h-full w-full" />}>
                  <RouteMap
                    positions={mapPositionsQ.data?.positions ?? []}
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
              <FlightProfileChart positions={mapPositionsQ.data?.positions ?? []} />
            </CardContent>
          </Card>
          <Card data-testid="flight-positions-card">
            <CardHeader>
              <CardTitle>Position log ({positionPageQ.data?.total ?? 0})</CardTitle>
            </CardHeader>
            <CardContent>
              <PositionTable
                positions={positionPageQ.data?.positions ?? []}
                total={positionPageQ.data?.total ?? 0}
                loading={positionPageQ.isLoading}
              />
            </CardContent>
          </Card>
          <OooiCard
            icao={detailQ.data.flight.icao_hex}
            firstSeen={detailQ.data.flight.first_seen}
            lastSeen={detailQ.data.flight.last_seen}
            scheduledOrigin={detailQ.data.flight.origin_icao}
            scheduledDest={detailQ.data.flight.dest_icao}
          />
          <AcarsPanel
            icao={detailQ.data.flight.icao_hex}
            firstSeen={detailQ.data.flight.first_seen}
            lastSeen={detailQ.data.flight.last_seen}
          />
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
          <TH className="hidden md:table-cell">Source</TH>
        </TR>
      </THead>
      <TBody>
        {rows.map((r) => (
          <TR key={r.id} data-testid={`flight-other-flight-${r.id}`}>
            <TD className="tabnum text-xs">
              <Link to={`/flight/${r.id}`} className="text-[var(--color-accent)] hover:underline">
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
            <TD className="hidden md:table-cell">
              <SourceBadge source={r.primary_source} />
            </TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

// ---------------------------------------------------------------------------
// Altitude + speed profile chart — option builder lives in ./flightCharts.
// ---------------------------------------------------------------------------

function FlightProfileChart({ positions }: { positions: Position[] }) {
  const { altLabel, spdLabel, fmtTs, fmtAxisTime } = useFormat();
  const [isolated, setIsolated] = useState<string | null>(null);
  const rows = useMemo<ProfileRow[]>(
    () =>
      positions
        .filter((p) => p.alt_baro != null || p.gs != null)
        .map((p) => ({ ts: p.ts, alt: p.alt_baro, gs: p.gs })),
    [positions],
  );
  const option = useMemo(
    () => buildFlightProfileOption(rows, altLabel(), spdLabel(), fmtAxisTime, fmtTs, isolated),
    [rows, altLabel, spdLabel, fmtAxisTime, fmtTs, isolated],
  );
  if (rows.length === 0) {
    return (
      <div className="flex h-56 items-center justify-center text-sm text-[var(--color-text-dim)]">
        no altitude / speed data
      </div>
    );
  }
  return (
    <div className="space-y-2">
      <IsolationPills
        keys={['alt', 'gs']}
        labels={[altLabel(), spdLabel()]}
        colors={[CHART_COLORS.orange, CHART_COLORS.accent]}
        isolated={isolated}
        onChange={setIsolated}
        testIdPrefix="flight-profile"
      />
      <EChart option={option} height={260} />
    </div>
  );
}
