import { lazy, Suspense, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowRightIcon } from '@radix-ui/react-icons';
import { errMsg } from '@/lib/errMsg';
import { Button } from '@/components/ui/Button';
import { Alert } from '@/components/ui/Alert';
import { Skeleton } from '@/components/ui/Skeleton';
import {
  Sheet,
  SheetClose,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/Sheet';
import { Table, THead, TBody, TR, TH, TD } from '@/components/ui/Table';
import { FlagBadge, SourceBadge } from '@/components/FlagBadge';
import { useFormat } from '@/hooks/useFormat';
import { cn } from '@/lib/cn';
import { MapCommandBar } from '@/components/map/MapCommandBar';
import { useMapSettings } from '@/hooks/useMapSettings';
import { useMapPlaybackState } from '@/hooks/useMapPlaybackState';
import { useMapDataQueries, type Aircraft } from '@/hooks/useMapDataQueries';

// MapLibre is lazy-loaded — see /v2/flight pattern.
const LiveMap = lazy(() => import('@/components/LiveMap'));

export default function MapPage() {
  // ── composition (settings → playback → data) ──────────────────────────
  // Three hooks with a strict dependency order, set up to avoid a cycle:
  //   useMapSettings           — owns the settings query; exposes maxRewindSec.
  //   useMapPlaybackState(sec) — owns mode/rewind/hist/playback; needs maxRewindSec.
  //   useMapDataQueries(pb)    — owns the snapshot+overlay queries; the snapshot
  //                              query key/fn derive from playback's
  //                              mode/rewindOffsetSec/histAt, so it needs playback.
  // Settings can't live in useMapDataQueries (data needs playback, playback
  // needs settings — that's the cycle), so it's a standalone hook sharing the
  // ['settings'] query key (no extra request).
  const { maxRewindSec } = useMapSettings();
  const playback = useMapPlaybackState(maxRewindSec);
  const data = useMapDataQueries(playback);

  const {
    snapshot,
    aircraft,
    heatmapQ,
    coverageQ,
    vdl2ActiveQ,
    vdl2PositionsQ,
    vdl2Available,
    showHeatmap,
    setShowHeatmap,
    showCoverage,
    setShowCoverage,
    showVdl2,
    setShowVdl2,
    mapWindow,
    setMapWindow,
    acarsActive,
  } = data;
  const { mode, histAt } = playback;
  const [selectedFlightId, setSelectedFlightId] = useState<number | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);

  const initialCenter: [number, number] | null = useMemo(() => {
    const lat = snapshot.data?.receiver_lat;
    const lon = snapshot.data?.receiver_lon;
    return lat != null && lon != null ? [lat, lon] : null;
  }, [snapshot.data?.receiver_lat, snapshot.data?.receiver_lon]);

  const selectedAircraft = aircraft.find((a) => a.flight_id === selectedFlightId) ?? null;

  // ── --map-bar-height for MapLibre control offset ──────────────────────
  const [barHeight, setBarHeight] = useState(0);

  return (
    <div
      className="map-with-bar relative h-[calc(100dvh-57px)] w-full overflow-hidden"
      data-testid="page-map"
      style={{ ['--map-bar-height' as string]: `${barHeight}px` }}
    >
      {/* Map fills viewport below nav */}
      <div className="h-full w-full" data-testid="map-container">
        <Suspense fallback={<Skeleton className="h-full w-full" />}>
          <LiveMap
            aircraft={aircraft}
            receiverLat={snapshot.data?.receiver_lat ?? null}
            receiverLon={snapshot.data?.receiver_lon ?? null}
            selectedFlightId={selectedFlightId}
            onSelect={(a) => setSelectedFlightId(a.flight_id)}
            initialCenter={initialCenter}
            heatmapPoints={showHeatmap ? heatmapQ.data?.points : undefined}
            coveragePolygon={showCoverage ? coverageQ.data?.polygon : undefined}
            vdl2Positions={showVdl2 ? vdl2PositionsQ.data?.points : undefined}
            acarsActive={acarsActive}
          />
        </Suspense>
      </div>

      {/* Placeholder when HIST mode has no date picked yet */}
      {mode === 'hist' && histAt == null && (
        <div
          className="pointer-events-none absolute inset-x-0 top-1/3 z-[10] flex justify-center px-3"
          data-testid="map-hist-pickdate-hint"
        >
          <div className="pointer-events-auto rounded-lg border border-[var(--color-border-default)] bg-[var(--color-surface)]/95 px-4 py-2 text-sm text-[var(--color-text-dim)] shadow-[var(--shadow-md)] backdrop-blur">
            Pick a date and time to view history.
          </div>
        </div>
      )}

      {snapshot.isError && (
        <div className="pointer-events-auto absolute inset-x-3 top-3 z-[10]">
          <Alert variant="error">Failed to load snapshot: {errMsg(snapshot.error)}</Alert>
        </div>
      )}

      {/* VDL2 overlay fetch failed while the toggle is on — surface it instead of
          leaving the toggle on with silently-missing data. */}
      {showVdl2 && (vdl2ActiveQ.isError || vdl2PositionsQ.isError) && (
        <div
          className="pointer-events-auto absolute inset-x-3 top-3 z-[10]"
          data-testid="map-vdl2-error"
        >
          <Alert variant="warn">VDL2 overlay unavailable — couldn’t load ACARS positions.</Alert>
        </div>
      )}

      {/* Bottom command bar */}
      <MapCommandBar
        mode={mode}
        onModeChange={playback.handleModeChange}
        mapWindow={mapWindow}
        onMapWindowChange={setMapWindow}
        showHeatmap={showHeatmap}
        onToggleHeatmap={() => setShowHeatmap((v) => !v)}
        heatmapLoading={showHeatmap && heatmapQ.isLoading}
        showCoverage={showCoverage}
        onToggleCoverage={() => setShowCoverage((v) => !v)}
        coverageLoading={showCoverage && coverageQ.isLoading}
        showVdl2={showVdl2}
        onToggleVdl2={vdl2Available ? () => setShowVdl2((v) => !v) : undefined}
        vdl2Loading={showVdl2 && (vdl2ActiveQ.isLoading || vdl2PositionsQ.isLoading)}
        sidebarOpen={sidebarOpen}
        onToggleSidebar={() => setSidebarOpen((v) => !v)}
        snapshotAt={snapshot.data?.at ?? null}
        snapshotIsError={snapshot.isError}
        snapshotIsStale={snapshot.isError && snapshot.data != null}
        aircraftCount={aircraft.length}
        scrubMin={playback.scrubMin}
        scrubMax={playback.scrubMax}
        scrubValue={playback.scrubValue}
        onScrubChange={playback.onScrubChange}
        onSeek={playback.onSeek}
        onJumpNow={playback.onJumpNow}
        rewindLabel={playback.rewindLabel}
        playing={playback.playing}
        onPlayToggle={() => playback.setPlaying((v) => !v)}
        speed={playback.speed}
        onSpeedChange={playback.setSpeed}
        histDateISO={playback.histDateISO}
        histTimeHHMM={playback.histTimeHHMM}
        onHistDateChange={playback.onHistDateChange}
        onHistTimeChange={playback.onHistTimeChange}
        histMinSec={playback.nowSec - maxRewindSec}
        histMaxSec={playback.nowSec}
        onHeightChange={setBarHeight}
      />

      {/* Sidebar — aircraft list (slides in from the left). */}
      <Sheet open={sidebarOpen} onOpenChange={setSidebarOpen}>
        <SheetContent side="left" data-testid="map-sidebar-list">
          <SheetHeader>
            <SheetTitle>
              Aircraft
              <span className="tabnum ml-2 text-xs font-normal text-[var(--color-text-dim)]">
                {aircraft.length}
              </span>
            </SheetTitle>
            <SheetDescription>
              Click a row to focus the aircraft and open its details.
            </SheetDescription>
          </SheetHeader>
          <AircraftListTable
            aircraft={aircraft}
            onSelect={(a) => setSelectedFlightId(a.flight_id)}
          />
        </SheetContent>
      </Sheet>

      {/* Aircraft detail sheet (right). Coexists with the left list sheet. */}
      <Sheet
        open={selectedAircraft != null}
        onOpenChange={(open) => {
          if (!open) setSelectedFlightId(null);
        }}
      >
        <SheetContent side="right" data-testid="map-detail-sheet">
          {selectedAircraft && <AircraftDetail ac={selectedAircraft} />}
        </SheetContent>
      </Sheet>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Aircraft list (left sheet)
// ---------------------------------------------------------------------------

function AircraftListTable({
  aircraft,
  onSelect,
}: {
  aircraft: Aircraft[];
  onSelect: (a: Aircraft) => void;
}) {
  const { fmtAlt, fmtSpd } = useFormat();
  const sorted = useMemo(
    () => [...aircraft].sort((a, b) => a.seconds_ago - b.seconds_ago),
    [aircraft],
  );
  if (sorted.length === 0) {
    return (
      <p className="mt-3 text-sm text-[var(--color-text-dim)]" data-testid="map-list-empty">
        No aircraft currently tracked.
      </p>
    );
  }
  return (
    <div className="mt-3">
      <Table data-testid="map-aircraft-list">
        <THead>
          <TR>
            <TH>ICAO</TH>
            <TH>Callsign</TH>
            <TH>Reg</TH>
            <TH>Type</TH>
            <TH>Alt</TH>
            <TH>Speed</TH>
            <TH>Src</TH>
            <TH>Ago</TH>
          </TR>
        </THead>
        <TBody>
          {sorted.map((a) => (
            <TR
              key={a.flight_id}
              onClick={() => onSelect(a)}
              className="cursor-pointer"
              data-testid={`map-list-row-${a.flight_id}`}
            >
              <TD className="tabnum font-mono text-xs">{a.icao_hex}</TD>
              <TD className="font-mono text-xs">{a.callsign ?? '—'}</TD>
              <TD className="text-xs">{a.registration ?? '—'}</TD>
              <TD className="text-xs">{a.aircraft_type ?? '—'}</TD>
              <TD className="tabnum text-xs">{fmtAlt(a.alt_baro)}</TD>
              <TD className="tabnum text-xs">{fmtSpd(a.gs)}</TD>
              <TD>
                <SourceBadge source={a.source_type ?? a.primary_source} />
              </TD>
              <TD className="tabnum text-xs text-[var(--color-text-dim)]">
                {fmtAgoCompact(a.seconds_ago)}
              </TD>
            </TR>
          ))}
        </TBody>
      </Table>
    </div>
  );
}

function fmtAgoCompact(sec: number): string {
  // Audit 17: seconds_ago comes from untrusted backend JSON; guard against a
  // null/missing value rendering as "NaNs" (mirrors lib/format.ts).
  if (!Number.isFinite(sec)) return '—';
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m`;
  return `${Math.floor(sec / 3600)}h`;
}

// ---------------------------------------------------------------------------
// Per-aircraft detail (right sheet — unchanged from before)
// ---------------------------------------------------------------------------

function AircraftDetail({ ac }: { ac: Aircraft }) {
  const { fmtAlt, fmtSpd } = useFormat();
  return (
    <div className="space-y-3 text-sm" data-testid="aircraft-detail">
      <SheetHeader>
        <SheetTitle className="flex flex-wrap items-center gap-2">
          <span className="font-mono">{ac.registration ?? ac.icao_hex}</span>
          <FlagBadge flags={ac.flags} />
          <SourceBadge source={ac.primary_source} />
        </SheetTitle>
        <SheetDescription className="flex flex-wrap items-center gap-2 font-mono text-xs">
          <span>{ac.icao_hex}</span>
          {ac.callsign ? <span>· {ac.callsign}</span> : null}
          {ac.aircraft_type ? <span>· {ac.aircraft_type}</span> : null}
        </SheetDescription>
      </SheetHeader>

      <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1.5">
        <dt className="text-[var(--color-text-dim)]">Altitude</dt>
        <dd className="tabnum">{fmtAlt(ac.alt_baro)}</dd>
        <dt className="text-[var(--color-text-dim)]">Speed</dt>
        <dd className="tabnum">{fmtSpd(ac.gs)}</dd>
        <dt className="text-[var(--color-text-dim)]">Track</dt>
        <dd className="tabnum">{ac.track != null ? `${Math.round(ac.track)}°` : '—'}</dd>
        <dt className="text-[var(--color-text-dim)]">Position</dt>
        <dd className="tabnum font-mono text-xs">
          {ac.lat != null && ac.lon != null ? `${ac.lat.toFixed(4)}, ${ac.lon.toFixed(4)}` : '—'}
        </dd>
        <dt className="text-[var(--color-text-dim)]">Source</dt>
        <dd>
          <SourceBadge source={ac.source_type} />
        </dd>
        <dt className="text-[var(--color-text-dim)]">Updated</dt>
        <dd className="tabnum">
          {Number.isFinite(ac.seconds_ago) ? `${ac.seconds_ago}s ago` : '—'}
        </dd>
        {(ac.origin_icao || ac.dest_icao) && (
          <>
            <dt className="text-[var(--color-text-dim)]">Route</dt>
            <dd className="font-mono">
              {ac.origin_icao ?? '???'} → {ac.dest_icao ?? '???'}
            </dd>
          </>
        )}
      </dl>

      <div className="flex flex-wrap items-center gap-2 pt-2">
        <Link
          to={`/flight/${ac.flight_id}`}
          className={cn(
            'inline-flex items-center justify-center rounded bg-[var(--color-accent)] px-3 py-1.5 text-sm text-white shadow-[var(--shadow-sm)]',
            'hover:bg-[var(--color-accent-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]',
          )}
          data-testid="aircraft-detail-flight-link"
        >
          <span>Flight detail</span>
          <ArrowRightIcon aria-hidden="true" className="ml-1" />
        </Link>
        <Link
          to={`/aircraft/${ac.icao_hex}`}
          className="inline-flex items-center justify-center rounded border border-[var(--color-border-default)] px-3 py-1.5 text-sm text-[var(--color-text)] hover:bg-[var(--color-surface-2)]"
        >
          Aircraft history
        </Link>
        <SheetClose asChild>
          <Button size="sm" variant="ghost" data-testid="map-detail-close">
            Close
          </Button>
        </SheetClose>
      </div>
    </div>
  );
}
