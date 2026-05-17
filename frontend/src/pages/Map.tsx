import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { PlayIcon, PauseIcon, ArrowRightIcon } from '@radix-ui/react-icons';
import { apiJson } from '@/lib/api';
import { SimpleTooltip } from '@/components/ui/Tooltip';
import { Button } from '@/components/ui/Button';
import { Alert } from '@/components/ui/Alert';
import { Badge } from '@/components/ui/Badge';
import { Skeleton } from '@/components/ui/Skeleton';
import { ToggleGroupRoot, ToggleGroupItem } from '@/components/ui/ToggleGroup';
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
import { fmtTs } from '@/lib/format';
import { cn } from '@/lib/cn';

// Leaflet is lazy-loaded — see /v2/flight pattern.
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

type Mode = 'live' | 'rewind';
type MapWindow = '24h' | '7d' | '30d' | 'all';
type PlaybackSpeed = 1 | 2 | 5 | 10;

const MAX_REWIND_HOURS = 24;
const MAX_REWIND_SEC = MAX_REWIND_HOURS * 3600;
const PLAYBACK_TICK_MS = 1000; // 1 s real time per tick

const WINDOW_OPTIONS: { value: MapWindow; label: string }[] = [
  { value: '24h', label: '24h' },
  { value: '7d', label: '7d' },
  { value: '30d', label: '30d' },
  { value: 'all', label: 'All' },
];

const PLAYBACK_SPEEDS: PlaybackSpeed[] = [1, 2, 5, 10];

export default function MapPage() {
  // ── core state ─────────────────────────────────────────────────────────
  const [mode, setMode] = useState<Mode>('live');
  const [rewindOffsetSec, setRewindOffsetSec] = useState(0);
  const [selectedFlightId, setSelectedFlightId] = useState<number | null>(null);

  // ── overlays ──────────────────────────────────────────────────────────
  const [showHeatmap, setShowHeatmap] = useState(false);
  const [showCoverage, setShowCoverage] = useState(false);
  // 24h is the safe default: fast, small result set, server-side cache TTL
  // 5 min. Larger windows (7d/30d/all) scan millions of positions and can
  // cost > 384 MB peak on a Pi 4 — see CLAUDE.md "heatmap memory" note.
  const [mapWindow, setMapWindow] = useState<MapWindow>('24h');
  const [sidebarOpen, setSidebarOpen] = useState(false);

  // ── playback ──────────────────────────────────────────────────────────
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<PlaybackSpeed>(1);

  const at = useMemo(() => {
    if (mode === 'live') return null;
    return Math.floor(Date.now() / 1000) - rewindOffsetSec;
  }, [mode, rewindOffsetSec]);

  const snapshot = useQuery<SnapshotResp>({
    queryKey: ['map-snapshot', at ?? 'live'],
    queryFn: () => {
      const qs = new URLSearchParams({ trail: '20' });
      if (at != null) qs.set('at', String(at));
      return apiJson<SnapshotResp>(`map/snapshot?${qs.toString()}`);
    },
    refetchInterval: mode === 'live' ? 10_000 : false,
    refetchIntervalInBackground: false,
    placeholderData: (prev) => prev,
    staleTime: 5_000,
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

  // ── playback tick — advance time forward by `speed × tick` per real sec ─
  useEffect(() => {
    if (!playing || mode !== 'rewind') return;
    const id = window.setInterval(() => {
      setRewindOffsetSec((v) => {
        const next = Math.max(0, v - speed * (PLAYBACK_TICK_MS / 1000));
        if (next === 0) {
          // Reached now — switch back to live and stop the timer.
          setPlaying(false);
          setMode('live');
        }
        return next;
      });
    }, PLAYBACK_TICK_MS);
    return () => window.clearInterval(id);
  }, [playing, mode, speed]);

  // <input type="range"> + React + React-Compiler quirk: once the input is
  // "dirty" from user interaction, React's controlled-input commit no longer
  // reliably touches the underlying DOM `.value` property, only the `value`
  // attribute. The thumb position (and Playwright's `input_value()`) reads
  // the property, so during playback the slider visually freezes even though
  // `rewindOffsetSec` advances correctly in state. The fix uses the prototype
  // setter directly (sidesteps React's tracked setter) to push the value onto
  // the DOM node after every state change.
  const sliderRef = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    const el = sliderRef.current;
    if (el && el.value !== String(rewindOffsetSec)) {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype,
        'value',
      )?.set;
      if (setter) setter.call(el, String(rewindOffsetSec));
      else el.value = String(rewindOffsetSec);
    }
  }, [rewindOffsetSec]);

  const aircraft = snapshot.data?.aircraft ?? [];

  const initialCenter: [number, number] | null = useMemo(() => {
    const lat = snapshot.data?.receiver_lat;
    const lon = snapshot.data?.receiver_lon;
    return lat != null && lon != null ? [lat, lon] : null;
  }, [snapshot.data?.receiver_lat, snapshot.data?.receiver_lon]);

  const selectedAircraft = aircraft.find((a) => a.flight_id === selectedFlightId) ?? null;

  return (
    <div
      className="relative h-[calc(100dvh-57px)] w-full overflow-hidden"
      data-testid="page-map"
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
          />
        </Suspense>
      </div>

      {/* Top overlay strip */}
      <div
        className="pointer-events-none absolute left-3 right-3 top-3 z-[400] flex flex-wrap items-start justify-between gap-2"
        data-testid="map-controls-overlay"
      >
        <div className="pointer-events-auto flex flex-col gap-2">
          {/* Row 1: mode toggle + count */}
          <div className="flex flex-wrap items-center gap-2 rounded-lg border border-[var(--color-border-default)] bg-[var(--color-surface)]/95 px-3 py-2 shadow-[var(--shadow-md)] backdrop-blur">
            <ToggleGroupRoot
              type="single"
              value={mode}
              onValueChange={(v) => {
                if (!v) return;
                setMode(v as Mode);
                if (v === 'live') {
                  setRewindOffsetSec(0);
                  setPlaying(false);
                }
              }}
              aria-label="Map mode"
            >
              <ToggleGroupItem value="live" data-testid="map-mode-live">
                Live
              </ToggleGroupItem>
              <ToggleGroupItem value="rewind" data-testid="map-mode-rewind">
                Rewind
              </ToggleGroupItem>
            </ToggleGroupRoot>
            <Badge variant={mode === 'live' ? 'success' : 'warn'} data-testid="map-mode-badge">
              {mode === 'live' ? 'LIVE' : 'HIST'}
            </Badge>
            <span className="tabnum text-xs text-[var(--color-text-dim)]">
              {aircraft.length} aircraft
            </span>
          </div>

          {/* Row 2: layer toggles + sidebar toggle */}
          <div
            className="flex flex-wrap items-center gap-2 rounded-lg border border-[var(--color-border-default)] bg-[var(--color-surface)]/95 px-3 py-1.5 shadow-[var(--shadow-md)] backdrop-blur"
            data-testid="map-layers-overlay"
          >
            <LayerToggle
              testid="map-toggle-heatmap"
              label="Heatmap"
              active={showHeatmap}
              loading={showHeatmap && heatmapQ.isLoading}
              onClick={() => setShowHeatmap((v) => !v)}
            />
            <LayerToggle
              testid="map-toggle-coverage"
              label="Coverage"
              active={showCoverage}
              loading={showCoverage && coverageQ.isLoading}
              onClick={() => setShowCoverage((v) => !v)}
            />
            <LayerToggle
              testid="map-toggle-list"
              label="List"
              active={sidebarOpen}
              onClick={() => setSidebarOpen((v) => !v)}
            />
            {(showHeatmap || showCoverage) && (
              <div className="flex items-center gap-1 border-l border-[var(--color-border-default)] pl-2">
                <ToggleGroupRoot
                  type="single"
                  value={mapWindow}
                  onValueChange={(v) => v && setMapWindow(v as MapWindow)}
                  aria-label="Window"
                  data-testid="map-window-selector"
                >
                  {WINDOW_OPTIONS.map((w) => (
                    <ToggleGroupItem key={w.value} value={w.value} data-testid={`map-window-${w.value}`}>
                      {w.label}
                    </ToggleGroupItem>
                  ))}
                </ToggleGroupRoot>
              </div>
            )}
          </div>
        </div>

        {/* Top-right snapshot timestamp */}
        <div className="pointer-events-auto">
          <div
            className="rounded-lg border border-[var(--color-border-default)] bg-[var(--color-surface)]/95 px-3 py-2 text-xs tabnum text-[var(--color-text)] shadow-[var(--shadow-md)] backdrop-blur"
            data-testid="map-snapshot-ts"
          >
            <span className="mr-1 text-[var(--color-text-dim)] uppercase tracking-wide">
              Snapshot
            </span>
            {snapshot.data ? fmtTs(snapshot.data.at) : '—'}
            {/*
              Audit-12 #158 — `placeholderData: (prev) => prev` keeps the
              previous moment's data visible across rewind fetches so the
              map doesn't flicker. On a failed fetch this means we're
              showing stale aircraft positions; surface that explicitly so
              the user knows the displayed time is not the requested time.
            */}
            {snapshot.isError && snapshot.data && (
              <SimpleTooltip content="The requested moment failed to load — showing the previous snapshot">
                <span
                  tabIndex={0}
                  className="ml-2 rounded bg-[var(--color-warn-bg,_#7c2d12)]/40 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-[var(--color-warn-fg,_#fed7aa)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]"
                  data-testid="map-snapshot-stale"
                >
                  stale
                </span>
              </SimpleTooltip>
            )}
          </div>
        </div>
      </div>

      {/* Rewind pill with playback controls */}
      {mode === 'rewind' && (
        <div
          className="pointer-events-none absolute inset-x-0 bottom-4 z-[400] flex justify-center px-3"
          data-testid="map-rewind-wrap"
        >
          <div
            className="pointer-events-auto flex w-full max-w-2xl flex-wrap items-center gap-2 rounded-full border border-[var(--color-border-default)] bg-[var(--color-surface)]/95 py-2 pl-4 pr-2 shadow-[var(--shadow-md)] backdrop-blur"
            data-testid="map-rewind-controls"
          >
            <span className="tabnum min-w-[5ch] whitespace-nowrap text-xs text-[var(--color-text-dim)]">
              {describeRewind(rewindOffsetSec)}
            </span>
            <button
              type="button"
              onClick={() => setPlaying((v) => !v)}
              aria-label={playing ? 'Pause playback' : 'Play playback'}
              data-testid="map-play-toggle"
              className={cn(
                'flex h-9 w-9 items-center justify-center rounded-full transition-colors',
                playing
                  ? 'bg-[var(--color-accent)] text-white hover:bg-[var(--color-accent-hover)]'
                  : 'border border-[var(--color-border-default)] hover:bg-[var(--color-surface-2)]',
              )}
            >
              {playing ? <PauseIcon /> : <PlayIcon />}
            </button>
            <input
              ref={sliderRef}
              type="range"
              min={0}
              max={MAX_REWIND_SEC}
              // step=1 (not 60). The playback tick decrements by
              // `speed × tick-seconds`, which can produce non-60-multiple
              // values; with step=60 the browser silently snaps them back
              // to the nearest 60-multiple and the slider thumb stops
              // moving until the next "valid" tick. 1-second precision is
              // fine — users dragging the slider don't need 60s detents
              // over a 24-hour range.
              step={1}
              value={rewindOffsetSec}
              onChange={(e) => {
                setRewindOffsetSec(Number(e.target.value));
                setPlaying(false);
              }}
              className="map-rewind-range flex-1 min-w-[120px]"
              aria-label="Rewind offset in seconds"
              data-testid="map-rewind-slider"
            />
            <div className="flex items-center gap-1">
              <JumpButton
                label="−1h"
                testid="map-jump-back-1h"
                onClick={() =>
                  setRewindOffsetSec((v) => Math.min(MAX_REWIND_SEC, v + 3600))
                }
              />
              <JumpButton
                label="−10m"
                testid="map-jump-back-10m"
                onClick={() =>
                  setRewindOffsetSec((v) => Math.min(MAX_REWIND_SEC, v + 600))
                }
              />
              <JumpButton
                label="+10m"
                testid="map-jump-fwd-10m"
                onClick={() => setRewindOffsetSec((v) => Math.max(0, v - 600))}
              />
              <JumpButton
                label="+1h"
                testid="map-jump-fwd-1h"
                onClick={() => setRewindOffsetSec((v) => Math.max(0, v - 3600))}
              />
              <button
                type="button"
                onClick={() => {
                  setRewindOffsetSec(0);
                  setMode('live');
                  setPlaying(false);
                }}
                className="rounded-full bg-[var(--color-accent)] px-3 py-1 text-xs text-white hover:bg-[var(--color-accent-hover)]"
                data-testid="map-jump-now"
              >
                Now
              </button>
            </div>
            <div
              className="flex items-center gap-0.5 rounded-full border border-[var(--color-border-default)] p-0.5"
              role="group"
              aria-label="Playback speed"
              data-testid="map-speed-group"
            >
              {PLAYBACK_SPEEDS.map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => setSpeed(s)}
                  aria-pressed={speed === s}
                  data-testid={`map-speed-${s}x`}
                  className={cn(
                    'tabnum rounded-full px-2 py-0.5 text-xs',
                    speed === s
                      ? 'bg-[var(--color-accent)] text-white'
                      : 'text-[var(--color-text-dim)] hover:bg-[var(--color-surface-2)] hover:text-[var(--color-text)]',
                  )}
                >
                  {s}×
                </button>
              ))}
            </div>
          </div>
        </div>
      )}

      {snapshot.isError && (
        <div className="pointer-events-auto absolute inset-x-3 top-32 z-[400]">
          <Alert variant="error">
            Failed to load snapshot: {(snapshot.error as Error).message}
          </Alert>
        </div>
      )}

      {/* Sidebar — aircraft list (slides in from the left). */}
      <Sheet open={sidebarOpen} onOpenChange={setSidebarOpen}>
        <SheetContent side="left" data-testid="map-sidebar-list">
          <SheetHeader>
            <SheetTitle>
              Aircraft
              <span className="ml-2 text-xs font-normal text-[var(--color-text-dim)] tabnum">
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
// Small subcomponents
// ---------------------------------------------------------------------------

function LayerToggle({
  label,
  active,
  loading,
  onClick,
  testid,
}: {
  label: string;
  active: boolean;
  loading?: boolean;
  onClick: () => void;
  testid: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      data-testid={testid}
      className={cn(
        'inline-flex items-center gap-1.5 rounded px-2.5 py-1 text-xs font-medium transition-colors min-h-[28px]',
        active
          ? 'bg-[var(--color-accent)] text-white'
          : 'text-[var(--color-text-dim)] hover:bg-[var(--color-surface-2)] hover:text-[var(--color-text)]',
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          'inline-block h-1.5 w-1.5 rounded-full',
          active ? 'bg-white' : 'bg-[var(--color-text-dim)]',
          loading && 'animate-pulse',
        )}
      />
      {label}
    </button>
  );
}

function JumpButton({
  label,
  onClick,
  testid,
}: {
  label: string;
  onClick: () => void;
  testid: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      data-testid={testid}
      className="rounded-full border border-[var(--color-border-default)] px-2 py-1 text-xs hover:bg-[var(--color-surface-2)]"
    >
      {label}
    </button>
  );
}

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
              <TD className="font-mono text-xs tabnum">{a.icao_hex}</TD>
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
        <dd className="font-mono tabnum text-xs">
          {ac.lat != null && ac.lon != null ? `${ac.lat.toFixed(4)}, ${ac.lon.toFixed(4)}` : '—'}
        </dd>
        <dt className="text-[var(--color-text-dim)]">Source</dt>
        <dd>
          <SourceBadge source={ac.source_type} />
        </dd>
        <dt className="text-[var(--color-text-dim)]">Updated</dt>
        <dd className="tabnum">{ac.seconds_ago}s ago</dd>
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
