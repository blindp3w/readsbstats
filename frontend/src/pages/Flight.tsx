import { Fragment, lazy, Suspense, useMemo, useState } from 'react';
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
import { IsolationPills } from '@/components/charts/IsolationPills';
import { KpiSparkline } from '@/components/stats/KpiSparkline';
import { MetricCell } from '@/components/flight/MetricCell';
import { RssiCell } from '@/components/flight/RssiCell';
import { haversineNm, bearingFromReceiver } from '@/lib/geo';
import { useIsMobile } from '@/hooks/useIsMobile';
import { cn } from '@/lib/cn';

// Heavy bits (MapLibre GL) lazy-loaded so other pages don't pay for them.
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
          <FlightHeader
            detail={detailQ.data}
            photoQ={photoQ}
            positions={detailQ.data.positions}
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
              <FlightProfileChart positions={detailQ.data.positions} />
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

// At-max position lookups for the M3.1 header sub-labels. Computed
// client-side from `positions` (NOT via equality against the flight-level
// aggregates) because `max_gs` is REAL and `max_distance_nm` requires
// per-position haversine. Single pass, O(n).
interface AtMax {
  altRate: number | null;
  speedTrack: number | null;
  distBearing: number | null;
}

function computeAtMax(positions: Position[], recLat: number | null, recLon: number | null): AtMax {
  if (positions.length === 0) return { altRate: null, speedTrack: null, distBearing: null };
  let maxAltIdx = -1;
  let maxAlt = -Infinity;
  let maxGsIdx = -1;
  let maxGs = -Infinity;
  let maxDistIdx = -1;
  let maxDist = -Infinity;
  for (let i = 0; i < positions.length; i++) {
    const p = positions[i];
    if (p.alt_baro != null && p.alt_baro > maxAlt) {
      maxAlt = p.alt_baro;
      maxAltIdx = i;
    }
    if (p.gs != null && p.gs > maxGs) {
      maxGs = p.gs;
      maxGsIdx = i;
    }
    if (p.lat != null && p.lon != null && recLat != null && recLon != null) {
      const d = haversineNm(recLat, recLon, p.lat, p.lon);
      if (d > maxDist) {
        maxDist = d;
        maxDistIdx = i;
      }
    }
  }
  return {
    altRate: maxAltIdx >= 0 ? positions[maxAltIdx].baro_rate : null,
    speedTrack: maxGsIdx >= 0 ? positions[maxGsIdx].track : null,
    distBearing:
      maxDistIdx >= 0 && recLat != null && recLon != null
        ? bearingFromReceiver(
            recLat,
            recLon,
            positions[maxDistIdx].lat!,
            positions[maxDistIdx].lon!,
          )
        : null,
  };
}

function FlightHeader({
  detail,
  photoQ,
  positions,
  receiverLat,
  receiverLon,
}: {
  detail: FlightDetail;
  photoQ: { data: PhotoResp | null | undefined; isLoading: boolean };
  positions: Position[];
  receiverLat: number | null;
  receiverLon: number | null;
}) {
  const { fmtAlt, fmtSpd, fmtDist, fmtTs } = useFormat();
  const f = detail.flight;
  const photoUrl =
    safeUrl(photoQ.data?.large_url ?? null) || safeUrl(photoQ.data?.thumbnail_url ?? null);
  const atMax = computeAtMax(positions, receiverLat, receiverLon);

  // Subtitle joins aircraft_type · type_desc · operator · route. Each
  // segment omitted if null so the line never reads ' ·  · '.
  const subtitleParts = [
    f.aircraft_type,
    f.type_desc,
    f.airline_name,
    f.origin_icao || f.dest_icao ? `${f.origin_icao ?? '???'} → ${f.dest_icao ?? '???'}` : null,
  ].filter(Boolean);

  const squawkVariant: 'danger' | 'warn' =
    f.squawk === '7700' || f.squawk === '7600' || f.squawk === '7500' ? 'danger' : 'warn';

  return (
    <Card data-testid="flight-header-card">
      <CardContent className="pt-4">
        {/*
          Laptop / iPad / iPad-portrait: photo left (fixed 140 px), content right.
          iPhone (<sm): photo first (full width 16:9), content below.
        */}
        <div className="flex flex-col gap-3 sm:grid sm:grid-cols-[140px_1fr] sm:gap-3">
          {/* Photo box */}
          <div className="space-y-1">
            {photoQ.isLoading ? (
              <Skeleton className="aspect-[16/9] w-full sm:h-[100px] sm:w-[140px]" />
            ) : photoUrl ? (
              <>
                <div
                  className={cn(
                    'overflow-hidden rounded bg-[var(--color-surface-2)]',
                    'aspect-[16/9] sm:aspect-auto sm:h-[100px] sm:w-[140px]',
                  )}
                >
                  <img
                    src={photoUrl}
                    alt={f.registration ?? f.icao_hex}
                    loading="lazy"
                    className="h-full w-full object-cover"
                  />
                </div>
                {photoQ.data?.photographer && (
                  <p
                    className="text-[10px] text-[var(--color-text-dim)]"
                    data-testid="flight-photo-credit"
                  >
                    © {photoQ.data.photographer}
                    {photoQ.data.is_type_photo ? ' · type photo' : ''}
                  </p>
                )}
              </>
            ) : (
              <div
                className={cn(
                  'flex items-center justify-center rounded bg-[var(--color-surface-2)] text-xs text-[var(--color-text-dim)]',
                  'aspect-[16/9] sm:aspect-auto sm:h-[100px] sm:w-[140px]',
                )}
              >
                no photo
              </div>
            )}
          </div>

          {/* Identity + subtitle + metric grid */}
          <div className="space-y-2">
            {/* Identity row */}
            <div className="flex flex-wrap items-center gap-2" data-testid="flight-identity">
              <span className="text-base font-semibold">
                <Link
                  to={`/aircraft/${f.icao_hex}`}
                  className="font-mono text-[var(--color-accent)] hover:underline"
                  aria-label={`View aircraft ${f.icao_hex} history`}
                >
                  {f.registration ?? f.icao_hex}
                </Link>
                <span className="mx-1 text-[var(--color-text-dim)]">·</span>
                <span className="font-mono">{f.callsign ?? '—'}</span>
              </span>
              <span className="ml-auto flex flex-wrap items-center gap-1.5">
                <Link
                  to={`/aircraft/${f.icao_hex}`}
                  className="font-mono text-xs text-[var(--color-text-dim)] hover:text-[var(--color-text)]"
                >
                  {f.icao_hex}
                </Link>
                {f.squawk ? <Badge variant={squawkVariant}>{f.squawk}</Badge> : null}
                <FlagBadge flags={f.flags} />
                <SourceBadge source={f.primary_source} size="sm" />
              </span>
            </div>

            {/* Subtitle line */}
            {subtitleParts.length > 0 ? (
              <div className="text-xs text-[var(--color-text-dim)]" data-testid="flight-subtitle">
                {subtitleParts.join(' · ')}
              </div>
            ) : null}

            {/* 4×2 metric grid (2×4 on iPhone) */}
            <div
              className="grid grid-cols-2 gap-x-4 gap-y-2 pt-1 sm:grid-cols-4"
              data-testid="flight-metric-grid"
            >
              <MetricCell
                label="Max alt"
                value={fmtAlt(f.max_alt_baro)}
                sublabel={
                  atMax.altRate != null
                    ? `vert ${atMax.altRate > 0 ? '+' : ''}${Math.round(atMax.altRate)} ft/min`
                    : undefined
                }
                testid="flight-metric-alt"
              />
              <MetricCell
                label="Max speed"
                value={fmtSpd(f.max_gs)}
                sublabel={
                  atMax.speedTrack != null ? `track ${Math.round(atMax.speedTrack)}°` : undefined
                }
                testid="flight-metric-speed"
              />
              <MetricCell
                label="Max distance"
                value={fmtDist(f.max_distance_nm)}
                sublabel={
                  atMax.distBearing != null
                    ? `bearing ${Math.round(atMax.distBearing)}°`
                    : undefined
                }
                testid="flight-metric-dist"
              />
              <MetricCell
                label="Window"
                value={
                  <span>
                    {fmtTs(f.first_seen)}
                    <span className="mx-1 text-[var(--color-text-dim)]">↘</span>
                    {fmtTs(f.last_seen)}
                  </span>
                }
                valueText={`${fmtTs(f.first_seen)} to ${fmtTs(f.last_seen)}`}
                sublabel={`duration ${fmtDur(f.duration_sec)}`}
                testid="flight-metric-window"
              />
            </div>
          </div>
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

// Exported for unit tests. Series carry STABLE keys ('alt' / 'gs') as
// their `name` so the `isolated` lookup doesn't break when the user
// toggles units (altLabel changes from "Alt (m)" to "Alt (ft)").
export function buildFlightProfileOption(
  rows: ProfileRow[],
  altLabel: string,
  spdLabel: string,
  fmtAxisTime: (epoch: number) => string,
  fmtTs: (epoch: number) => string,
  // M3.2: when set to 'alt' or 'gs', the OTHER series fades to 0.2
  // opacity. null = both at full opacity. Companion to the HTML pill
  // row rendered by IsolationPills above the chart.
  isolated?: string | null,
): EChartsOption {
  const base = baseOption();
  const tAxis = timeAxis() as Exclude<EChartsOption['xAxis'], undefined | unknown[]>;
  const leftAxis = valueAxis() as any;
  const rightAxis = valueAxis() as any;
  const altFaded = isolated != null && isolated !== 'alt';
  const gsFaded = isolated != null && isolated !== 'gs';
  // Orange gradient under altitude (top 30% alpha → bottom transparent).
  // Replaces the previous solid 40% area flood.
  const altAreaGradient = {
    type: 'linear' as const,
    x: 0,
    y: 0,
    x2: 0,
    y2: 1,
    colorStops: [
      { offset: 0, color: CHART_COLORS.orange + '4d' /* ~30% alpha */ },
      { offset: 1, color: CHART_COLORS.orange + '00' /* fully transparent */ },
    ],
  };
  return {
    ...base,
    // Legend removed — the HTML pill row above the chart replaces it.
    grid: { top: 16, right: 40, bottom: 28, left: 44, containLabel: false },
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
    yAxis: [leftAxis, { ...rightAxis, position: 'right' }],
    dataZoom: [{ type: 'inside' }],
    series: [
      {
        // STABLE name for isolation lookup; the pill row renders the
        // unit-dependent label separately.
        name: 'alt',
        // Friendly tooltip / external label still uses the unit-aware string.
        tooltip: { valueFormatter: (v: number) => `${v} (${altLabel})` },
        type: 'line',
        yAxisIndex: 0,
        color: CHART_COLORS.orange,
        data: rows.map((r) => [r.ts * 1000, r.alt]),
        showSymbol: false,
        sampling: 'lttb',
        lineStyle: { width: 1.5, opacity: altFaded ? 0.2 : 1 },
        areaStyle: { color: altAreaGradient, opacity: altFaded ? 0.06 : 1 },
      },
      {
        name: 'gs',
        tooltip: { valueFormatter: (v: number) => `${v} (${spdLabel})` },
        type: 'line',
        yAxisIndex: 1,
        color: CHART_COLORS.accent,
        data: rows.map((r) => [r.ts * 1000, r.gs]),
        showSymbol: false,
        sampling: 'lttb',
        lineStyle: { width: 1.5, opacity: gsFaded ? 0.2 : 1 },
      },
    ],
  };
}

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

function PositionTable({ positions }: { positions: Position[] }) {
  const { fmtAlt, fmtSpd, fmtTs } = useFormat();
  // Per-row inline disclosure state — iPhone only. Keyed by ts (positions
  // sorted by ts and ts is unique-ish per flight).
  const [expanded, setExpanded] = useState<Set<number>>(() => new Set());
  // Gate the interactive row affordance behind <sm. At md+ all detail
  // columns are visible inline, so the row tap-handler + aria-expanded +
  // role="button" would mislead screen readers ('expanded' but nothing
  // changes visually) and accumulate Set entries indefinitely.
  const isMobile = useIsMobile();

  if (positions.length === 0) {
    return <p className="text-sm text-[var(--color-text-dim)]">No positions recorded.</p>;
  }
  // Sample if too many — full table would be heavy DOM.
  const sampled =
    positions.length > 500
      ? positions.filter((_, i) => i % Math.ceil(positions.length / 500) === 0)
      : positions;
  const rssi = computeRssiStats(sampled);
  const rssiSpark = rssi.hasAny ? sampled.map((p) => (p.rssi == null ? rssi.median : p.rssi)) : [];

  function toggle(ts: number) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(ts)) next.delete(ts);
      else next.add(ts);
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
          {sampled.map((p) => {
            const isOpen = expanded.has(p.ts);
            return (
              <Fragment key={p.ts}>
                <TR
                  data-testid={`flight-position-row-${p.ts}`}
                  // Interactive affordances ONLY on <sm. Desktop sees all
                  // detail columns inline so there's nothing to disclose.
                  {...(isMobile
                    ? {
                        tabIndex: 0,
                        role: 'button',
                        'aria-expanded': isOpen,
                        onClick: () => toggle(p.ts),
                        onKeyDown: (e: React.KeyboardEvent) => {
                          if (e.key === 'Enter' || e.key === ' ') {
                            e.preventDefault();
                            toggle(p.ts);
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
    </div>
  );
}
