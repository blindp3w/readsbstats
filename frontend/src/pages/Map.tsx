import { lazy, Suspense, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { ArrowRightIcon } from '@radix-ui/react-icons';
import { apiJson } from '@/lib/api';
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
import { parseYMD } from '@/lib/dateParse';
import { MapCommandBar, type MapWindow } from '@/components/map/MapCommandBar';
import { useVdl2Available } from '@/hooks/useVdl2Enabled';
import type { Vdl2ActiveResponse, Vdl2PositionsResponse } from '@/lib/types';
import { type Mode } from '@/components/map/MapModeControl';
import { type PlaybackSpeed } from '@/components/map/MapRewindControls';

// MapLibre is lazy-loaded — see /v2/flight pattern.
const LiveMap = lazy(() => import('@/components/LiveMap'));

interface Aircraft {
  flight_id: number;
  icao_hex: string;
  callsign: string | null;
  registration: string | null;
  aircraft_type: string | null;
  category: string | null;
  primary_source: string | null;
  flags: number;
  origin_icao: string | null;
  dest_icao: string | null;
  lat: number | null;
  lon: number | null;
  ts: number;
  alt_baro: number | null;
  gs: number | null;
  track: number | null;
  source_type: string | null;
  seconds_ago: number;
  trail: [number, number, number][];
}

interface SnapshotResp {
  at: number;
  is_live: boolean;
  receiver_lat: number | null;
  receiver_lon: number | null;
  aircraft: Aircraft[];
}

interface HeatmapResp {
  points: [number, number, number][];
  window: string;
  count: number;
}

interface CoverageResp {
  polygon: [number, number][];
  max_range_nm: number;
  window: string;
}

const PLAYBACK_TICK_MS = 1000; // 1 s real time per tick

// HIST default time-of-day when the user picks a date without touching the
// time picker. Noon catches the busy mid-day window for most receivers.
const HIST_DEFAULT_HOUR = 12;

export default function MapPage() {
  // ── settings (map_history_hours) ──────────────────────────────────────
  const { data: settings } = useQuery({
    queryKey: ['settings'],
    queryFn: () => apiJson<{ map_history_hours?: number }>('settings'),
    staleTime: 60_000,
  });
  const MAX_REWIND_HOURS = settings?.map_history_hours ?? 24;
  const MAX_REWIND_SEC = MAX_REWIND_HOURS * 3600;

  // ── core state ─────────────────────────────────────────────────────────
  const [mode, setMode] = useState<Mode>('live');
  const [rewindOffsetSec, setRewindOffsetSec] = useState(0);
  const [histAt, setHistAt] = useState<number | null>(null);
  const [selectedFlightId, setSelectedFlightId] = useState<number | null>(null);

  // ── overlays ──────────────────────────────────────────────────────────
  const [showHeatmap, setShowHeatmap] = useState(false);
  const [showCoverage, setShowCoverage] = useState(false);
  const [showVdl2, setShowVdl2] = useState(false);
  // VDL2 overlay is offered only when the feature is on AND vdl2.db is queryable.
  const vdl2Available = useVdl2Available();
  // 24h is the safe default: fast, small result set, server-side cache TTL
  // 5 min. Larger windows (7d/30d/all) scan millions of positions and can
  // cost > 384 MB peak on a Pi 4 — see CLAUDE.md "heatmap memory" note.
  const [mapWindow, setMapWindow] = useState<MapWindow>('24h');
  const [sidebarOpen, setSidebarOpen] = useState(false);

  const { fmtTs } = useFormat();

  // ── playback ──────────────────────────────────────────────────────────
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<PlaybackSpeed>(1);

  // `nowSec` (epoch seconds) drives the scrubber bounds. It must advance over
  // real time so the user can always scrub up to the present, but reading
  // `Date.now()` during render is impure (react-hooks/purity) and produced a
  // fresh value every render. Hold it in state and refresh on an interval —
  // 10 s matches the live snapshot poll; bounds a few seconds stale are
  // imperceptible against a multi-hour HIST window.
  const [nowSec, setNowSec] = useState(0);
  useEffect(() => {
    const tick = () => setNowSec(Math.floor(Date.now() / 1000));
    tick();
    const id = window.setInterval(tick, 10_000);
    return () => window.clearInterval(id);
  }, []);

  // In HIST mode with no date picked, don't fire a snapshot fetch — `at == null`
  // would otherwise be interpreted as Live by the backend.
  const snapshotEnabled = mode !== 'hist' || histAt != null;

  // Audit-13 A13-033 + react-hooks/purity: previously `at` was computed in
  // a useMemo that read `Date.now()` during render. The non-determinism
  // (a) flagged the audit's "extra fetch per slider tick" concern because
  // each render produced a slightly-different query key, and (b) was
  // impure under react-hooks v7. The query key now uses the deterministic
  // inputs (mode + rewindOffsetSec + histAt) and `Date.now()` runs once
  // per actual fetch inside queryFn.
  const snapshot = useQuery<SnapshotResp>({
    queryKey: ['map-snapshot', mode, mode === 'rewind' ? rewindOffsetSec : null, histAt ?? null],
    queryFn: () => {
      const qs = new URLSearchParams({ trail: '20' });
      let at: number | null = null;
      if (mode === 'hist') at = histAt;
      else if (mode === 'rewind') at = Math.floor(Date.now() / 1000) - rewindOffsetSec;
      if (at != null) qs.set('at', String(at));
      return apiJson<SnapshotResp>(`map/snapshot?${qs.toString()}`);
    },
    refetchInterval: mode === 'live' ? 10_000 : false,
    refetchIntervalInBackground: false,
    // Only fall back to stale data in Live mode (smooth 10s polling). In
    // Rewind / HIST modes the queryKey changes on every scrub, and
    // placeholderData would hold the previous timestamp's aircraft on
    // screen while the new one loads — long enough for the user to see
    // markers from BOTH snapshots overlap briefly, which reads as the
    // same physical aircraft appearing at two different positions.
    placeholderData: mode === 'live' ? (prev) => prev : undefined,
    staleTime: 5_000,
    enabled: snapshotEnabled,
  });

  // Overlay data — only fetched when toggled on. Window changes invalidate
  // the queryKey; switching off unmounts the query.
  const heatmapQ = useQuery<HeatmapResp>({
    queryKey: ['map-heatmap', mapWindow],
    queryFn: () => apiJson<HeatmapResp>(`map/heatmap?window=${mapWindow}`),
    enabled: showHeatmap,
    staleTime: 60_000,
  });

  const coverageQ = useQuery<CoverageResp>({
    queryKey: ['map-coverage', mapWindow],
    queryFn: () => apiJson<CoverageResp>(`map/coverage?window=${mapWindow}`),
    enabled: showCoverage,
    staleTime: 60_000,
  });

  // VDL2 overlay data — only fetched while the toggle is on. Kept entirely
  // separate from the live snapshot query so the hot 10s map poll is untouched.
  const vdl2ActiveQ = useQuery<Vdl2ActiveResponse>({
    queryKey: ['map-vdl2-active'],
    queryFn: () => apiJson<Vdl2ActiveResponse>('vdl2/active'),
    enabled: showVdl2 && vdl2Available,
    refetchInterval: 30_000,
    staleTime: 30_000,
  });

  const vdl2PositionsQ = useQuery<Vdl2PositionsResponse>({
    queryKey: ['map-vdl2-positions'],
    queryFn: () => apiJson<Vdl2PositionsResponse>('vdl2/positions'),
    enabled: showVdl2 && vdl2Available,
    refetchInterval: 30_000,
    staleTime: 30_000,
  });

  // Set of recently-transmitting airframes, for the LiveMap "talking now" ring.
  const acarsActive = showVdl2 && vdl2ActiveQ.data ? new Set(vdl2ActiveQ.data.icao_hex) : undefined;

  // If the feature goes unavailable while the overlay is on, drop it — otherwise
  // the toggle pill (gated on availability) disappears and strands a stale layer
  // the user can no longer turn off. React's render-phase "adjust state on prop
  // change" pattern (no effect — matches MapCommandBar's prevMode idiom).
  const [prevVdl2Available, setPrevVdl2Available] = useState(vdl2Available);
  if (vdl2Available !== prevVdl2Available) {
    setPrevVdl2Available(vdl2Available);
    if (!vdl2Available && showVdl2) setShowVdl2(false);
  }

  // ── playback tick — advance scrub forward per real second ──────────────
  useEffect(() => {
    if (!playing) return;
    if (mode === 'live') return;
    const id = window.setInterval(() => {
      if (mode === 'rewind') {
        setRewindOffsetSec((v) => {
          const next = Math.max(0, v - speed * (PLAYBACK_TICK_MS / 1000));
          if (next === 0) {
            // Reached now — switch back to live and stop the timer.
            setPlaying(false);
            setMode('live');
          }
          return next;
        });
      } else {
        // HIST: advance histAt forward toward now. Audit 2026-06-01 S:
        // on catch-up, mirror the rewind branch and flip back to 'live'
        // (otherwise the UI sat in HIST mode with playback stopped).
        // Clamp the non-terminal advance against the same fresh clock used
        // for the catch-up check (matches clampHist's [now-MAX, now] bounds)
        // to avoid one-tick overshoot.
        setHistAt((v) => {
          if (v == null) return v;
          const nowSec = Math.floor(Date.now() / 1000);
          const next = v + speed * (PLAYBACK_TICK_MS / 1000);
          if (next >= nowSec) {
            setPlaying(false);
            setMode('live');
            return nowSec;
          }
          return Math.max(nowSec - MAX_REWIND_SEC, Math.min(nowSec, next));
        });
      }
    }, PLAYBACK_TICK_MS);
    return () => window.clearInterval(id);
  }, [playing, mode, speed, MAX_REWIND_SEC]);

  const aircraft = snapshot.data?.aircraft ?? [];

  const initialCenter: [number, number] | null = useMemo(() => {
    const lat = snapshot.data?.receiver_lat;
    const lon = snapshot.data?.receiver_lon;
    return lat != null && lon != null ? [lat, lon] : null;
  }, [snapshot.data?.receiver_lat, snapshot.data?.receiver_lon]);

  const selectedAircraft = aircraft.find((a) => a.flight_id === selectedFlightId) ?? null;

  // ── mode-transition side effects ──────────────────────────────────────
  const handleModeChange = (next: Mode) => {
    if (next === mode) return;
    const nowSec = Math.floor(Date.now() / 1000);

    if (next === 'live') {
      setRewindOffsetSec(0);
      setHistAt(null);
      setPlaying(false);
      setMode('live');
      return;
    }

    if (next === 'rewind') {
      // If coming from HIST with a chosen time, preserve it as an offset.
      if (mode === 'hist' && histAt != null) {
        setRewindOffsetSec(Math.max(0, nowSec - histAt));
        setHistAt(null);
      }
      setPlaying(false);
      setMode('rewind');
      return;
    }

    // next === 'hist'
    if (mode === 'rewind' && rewindOffsetSec > 0) {
      setHistAt(nowSec - rewindOffsetSec);
      setRewindOffsetSec(0);
    } else {
      // Default to 1h ago so the map renders immediately on entry.
      setHistAt(nowSec - 3600);
    }
    setPlaying(false);
    setMode('hist');
  };

  // ── HIST date/time derivation ─────────────────────────────────────────
  const histDateISO = histAt != null ? unixToISO(histAt) : '';
  const histTimeHHMM = histAt != null ? unixToHHMM(histAt) : '';

  const onHistDateChange = (nextISO: string) => {
    const time = histAt != null ? unixToHHMM(histAt) : `${pad2(HIST_DEFAULT_HOUR)}:00`;
    const composed = composeUnix(nextISO, time);
    if (composed != null) setHistAt(clampHist(composed));
  };
  const onHistTimeChange = (nextHHMM: string) => {
    const date = histAt != null ? unixToISO(histAt) : unixToISO(nowSec);
    const composed = composeUnix(date, nextHHMM);
    if (composed != null) setHistAt(clampHist(composed));
  };

  const nowSecForBounds = nowSec;
  function clampHist(v: number): number {
    return Math.max(nowSecForBounds - MAX_REWIND_SEC, Math.min(nowSecForBounds, v));
  }

  // ── Scrubber bounds + label per mode ──────────────────────────────────
  const { scrubMin, scrubMax, scrubValue, rewindLabel } = useMemo(() => {
    if (mode === 'hist' && histAt != null) {
      const dayStart = startOfDayLocal(histAt);
      const dayEnd = dayStart + 86400 - 1;
      const min = Math.max(dayStart, nowSecForBounds - MAX_REWIND_SEC);
      const max = Math.min(dayEnd, nowSecForBounds);
      return {
        scrubMin: min,
        scrubMax: max,
        scrubValue: Math.max(min, Math.min(max, histAt)),
        rewindLabel: fmtTs(histAt),
      };
    }
    // rewind (or hist with no date — won't show Row 2 but compute anyway)
    return {
      scrubMin: 0,
      scrubMax: MAX_REWIND_SEC,
      scrubValue: rewindOffsetSec,
      rewindLabel: describeRewind(rewindOffsetSec),
    };
  }, [mode, histAt, rewindOffsetSec, MAX_REWIND_SEC, nowSecForBounds, fmtTs]);

  // ── Bar callbacks ─────────────────────────────────────────────────────
  const onScrubChange = (next: number) => {
    if (mode === 'rewind') {
      setRewindOffsetSec(next);
    } else if (mode === 'hist') {
      setHistAt(clampHist(next));
    }
    setPlaying(false);
  };
  const onSeek = (deltaSec: number) => {
    if (mode === 'rewind') {
      // delta>0 = go back; delta<0 = advance
      setRewindOffsetSec((v) => Math.max(0, Math.min(MAX_REWIND_SEC, v + deltaSec)));
    } else if (mode === 'hist') {
      setHistAt((v) => (v == null ? v : clampHist(v - deltaSec)));
    }
  };
  const onJumpNow = () => {
    setRewindOffsetSec(0);
    setHistAt(null);
    setMode('live');
    setPlaying(false);
  };

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
          <Alert variant="error">
            Failed to load snapshot: {(snapshot.error as Error).message}
          </Alert>
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
        onModeChange={handleModeChange}
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
        scrubMin={scrubMin}
        scrubMax={scrubMax}
        scrubValue={scrubValue}
        onScrubChange={onScrubChange}
        onSeek={onSeek}
        onJumpNow={onJumpNow}
        rewindLabel={rewindLabel}
        playing={playing}
        onPlayToggle={() => setPlaying((v) => !v)}
        speed={speed}
        onSpeedChange={setSpeed}
        histDateISO={histDateISO}
        histTimeHHMM={histTimeHHMM}
        onHistDateChange={onHistDateChange}
        onHistTimeChange={onHistTimeChange}
        histMinSec={nowSecForBounds - MAX_REWIND_SEC}
        histMaxSec={nowSecForBounds}
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
// Helpers
// ---------------------------------------------------------------------------

function describeRewind(sec: number): string {
  if (sec === 0) return 'Now';
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const parts: string[] = [];
  if (h > 0) parts.push(`${h}h`);
  if (m > 0) parts.push(`${m}m`);
  if (parts.length === 0) parts.push(`${Math.round(sec)}s`);
  return `${parts.join(' ')} ago`;
}

function pad2(n: number): string {
  return n.toString().padStart(2, '0');
}

function unixToISO(sec: number): string {
  const d = new Date(sec * 1000);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

function unixToHHMM(sec: number): string {
  const d = new Date(sec * 1000);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

function composeUnix(dateISO: string, timeHHMM: string): number | null {
  const p = parseYMD(dateISO);
  const tm = /^(\d{1,2}):(\d{2})$/.exec(timeHHMM);
  if (!p || !tm) return null;
  const d = new Date(p.y, p.mo, p.d, Number(tm[1]), Number(tm[2]), 0, 0);
  return Math.floor(d.getTime() / 1000);
}

function startOfDayLocal(sec: number): number {
  const d = new Date(sec * 1000);
  d.setHours(0, 0, 0, 0);
  return Math.floor(d.getTime() / 1000);
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
