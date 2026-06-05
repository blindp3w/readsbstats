import { useEffect, useRef, useState } from 'react';
import { ChevronUpIcon, ChevronDownIcon } from '@radix-ui/react-icons';
import { SimpleTooltip } from '@/components/ui/Tooltip';
import { Badge } from '@/components/ui/Badge';
import { ToggleGroupRoot, ToggleGroupItem } from '@/components/ui/ToggleGroup';
import { cn } from '@/lib/cn';
import { useFormat } from '@/hooks/useFormat';
import { MapModeControl, type Mode } from './MapModeControl';
import { MapLayersControl } from './MapLayersControl';
import { MapHistDatePicker } from './MapHistDatePicker';
import { MapRewindControls, type PlaybackSpeed } from './MapRewindControls';

export type MapWindow = '24h' | '7d' | '30d' | 'all';

const WINDOW_OPTIONS: { value: MapWindow; label: string }[] = [
  { value: '24h', label: '24h' },
  { value: '7d', label: '7d' },
  { value: '30d', label: '30d' },
  { value: 'all', label: 'All' },
];

interface Props {
  mode: Mode;
  onModeChange: (next: Mode) => void;

  mapWindow: MapWindow;
  onMapWindowChange: (next: MapWindow) => void;

  showHeatmap: boolean;
  onToggleHeatmap: () => void;
  heatmapLoading?: boolean;
  showCoverage: boolean;
  onToggleCoverage: () => void;
  coverageLoading?: boolean;
  // Optional VDL2 overlay toggle — only passed when the feature is available.
  showVdl2?: boolean;
  onToggleVdl2?: () => void;
  vdl2Loading?: boolean;
  sidebarOpen: boolean;
  onToggleSidebar: () => void;

  snapshotAt: number | null;
  snapshotIsError: boolean;
  snapshotIsStale: boolean;
  aircraftCount: number;

  // Rewind / playback (Row 2). Bounds + value are interpreted by the parent.
  scrubMin: number;
  scrubMax: number;
  scrubValue: number;
  onScrubChange: (next: number) => void;
  onSeek: (deltaSec: number) => void;
  onJumpNow: () => void;
  rewindLabel: string;
  playing: boolean;
  onPlayToggle: () => void;
  speed: PlaybackSpeed;
  onSpeedChange: (next: PlaybackSpeed) => void;

  // HIST date+time picker (only rendered when mode === 'hist').
  histDateISO: string;
  histTimeHHMM: string;
  onHistDateChange: (next: string) => void;
  onHistTimeChange: (next: string) => void;
  histMinSec: number;
  histMaxSec: number;

  // Called whenever the bar's measured height changes so the parent can set
  // --map-bar-height on the map container (shifts MapLibre native controls).
  onHeightChange?: (heightPx: number) => void;
}

// Bottom command bar. Two rows of controls overlaid on the map.
//
// Layout per viewport:
// - lg+: Row 1 has inline Layer toggles + range pills + mode + snapshot.
//        Row 2 (Rewind/HIST only) has scrubber + seek + speed.
// - md:  Layers fold into an icon Popover; everything else inline.
// - <sm: Condensed to mode + chevron; tap chevron to reveal the rest stacked
//        vertically. Auto-expands when mode changes to Rewind/HIST so the
//        scrubber is reachable without an extra tap.
export function MapCommandBar(props: Props) {
  const {
    mode,
    onModeChange,
    mapWindow,
    onMapWindowChange,
    showHeatmap,
    onToggleHeatmap,
    heatmapLoading,
    showCoverage,
    onToggleCoverage,
    coverageLoading,
    showVdl2,
    onToggleVdl2,
    vdl2Loading,
    sidebarOpen,
    onToggleSidebar,
    snapshotAt,
    snapshotIsError,
    snapshotIsStale,
    aircraftCount,
    scrubMin,
    scrubMax,
    scrubValue,
    onScrubChange,
    onSeek,
    onJumpNow,
    rewindLabel,
    playing,
    onPlayToggle,
    speed,
    onSpeedChange,
    histDateISO,
    histTimeHHMM,
    onHistDateChange,
    onHistTimeChange,
    histMinSec,
    histMaxSec,
    onHeightChange,
  } = props;

  const { fmtTs } = useFormat();
  const rootRef = useRef<HTMLDivElement | null>(null);

  // iPhone condensed/expanded state.
  const [mobileExpanded, setMobileExpanded] = useState(false);
  // Auto-expand the mobile bar when entering Rewind/HIST. Uses React's
  // documented "reset state when prop/state changes" render-phase pattern
  // (https://react.dev/reference/react/useState#storing-information-from-previous-renders)
  // so the bar updates in a single render without a cascading effect.
  const [prevMode, setPrevMode] = useState<Mode>(mode);
  if (mode !== prevMode) {
    setPrevMode(mode);
    if (mode !== 'live') {
      setMobileExpanded(true);
    }
  }

  // Measure height and surface to the parent so MapLibre's bottom controls
  // can be shifted out from under the bar. We use getBoundingClientRect()
  // (border-box, includes padding) rather than ResizeObserverEntry.contentRect
  // (content-box only) — the bar has pb-[env(safe-area-inset-bottom)], so
  // contentRect undercounts by the safe-area inset and the zoom +/− buttons
  // end up sitting partially under the bar.
  useEffect(() => {
    const el = rootRef.current;
    if (!el || !onHeightChange) return;
    const measure = () => onHeightChange(el.getBoundingClientRect().height);
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    measure();
    return () => ro.disconnect();
  }, [onHeightChange]);

  const snapshotTs = snapshotAt != null ? fmtTs(snapshotAt) : '—';
  const showRow2 = mode !== 'live';

  // Pieces that appear in multiple variants — define once.
  // Single source of truth for the layer-toggle props, spread into all three
  // MapLayersControl call sites (inline-lg / popover-md-sm / mobile-expanded).
  // Built once so a new toggle can't be added to some sites and forgotten on
  // others (which is exactly how the mobile VDL2 toggle went missing).
  const layersProps = {
    showHeatmap,
    onToggleHeatmap,
    heatmapLoading,
    showCoverage,
    onToggleCoverage,
    coverageLoading,
    showVdl2,
    onToggleVdl2,
    vdl2Loading,
    sidebarOpen,
    onToggleSidebar,
  };

  const rangePills = (
    <ToggleGroupRoot
      type="single"
      value={mapWindow}
      onValueChange={(v) => v && onMapWindowChange(v as MapWindow)}
      aria-label="Window"
      data-testid="map-window-selector"
    >
      {WINDOW_OPTIONS.map((w) => (
        <ToggleGroupItem key={w.value} value={w.value} data-testid={`map-window-${w.value}`}>
          {w.label}
        </ToggleGroupItem>
      ))}
    </ToggleGroupRoot>
  );

  // The picker remounts each time mode flips into 'hist', so defaultOpen
  // re-fires the auto-open behavior on entry without any extra plumbing.
  const histChip = mode === 'hist' && (
    <MapHistDatePicker
      dateISO={histDateISO}
      timeHHMM={histTimeHHMM}
      onDateChange={onHistDateChange}
      onTimeChange={onHistTimeChange}
      minSec={histMinSec}
      maxSec={histMaxSec}
      defaultOpen
    />
  );

  const snapshotBlock = (
    <div
      className="tabnum flex items-center gap-2 whitespace-nowrap text-xs text-[var(--color-text)]"
      data-testid="map-snapshot-ts"
    >
      <Badge variant={mode === 'live' ? 'success' : 'warn'} data-testid="map-mode-badge">
        {mode === 'live' ? 'LIVE' : 'HIST'}
      </Badge>
      <span className="text-[var(--color-text-dim)]" data-testid="map-aircraft-count">
        {aircraftCount} ac
      </span>
      <span aria-live="polite" className="text-[var(--color-text-dim)]">
        {snapshotTs}
      </span>
      {snapshotIsError && snapshotIsStale && (
        <SimpleTooltip content="The requested moment failed to load — showing the previous snapshot">
          <span
            tabIndex={0}
            className="rounded bg-[var(--color-warn-bg,_#7c2d12)]/40 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-[var(--color-warn-fg,_#fed7aa)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]"
            data-testid="map-snapshot-stale"
          >
            stale
          </span>
        </SimpleTooltip>
      )}
    </div>
  );

  const rewindRow = showRow2 && (
    <MapRewindControls
      scrubMin={scrubMin}
      scrubMax={scrubMax}
      scrubValue={scrubValue}
      onScrubChange={onScrubChange}
      onSeek={onSeek}
      onJumpNow={onJumpNow}
      label={rewindLabel}
      playing={playing}
      onPlayToggle={onPlayToggle}
      speed={speed}
      onSpeedChange={onSpeedChange}
    />
  );

  return (
    <div
      ref={rootRef}
      className={cn(
        'pointer-events-auto absolute inset-x-0 bottom-0 z-[20]',
        'border-t border-[var(--color-border-default)]',
        'bg-[var(--color-surface)]/95 backdrop-blur',
        'pb-[max(0.5rem,env(safe-area-inset-bottom))]',
      )}
      data-testid="map-command-bar"
      data-mode={mode}
    >
      {/* ── Desktop / tablet Row 1 (sm and up) ─────────────────────────── */}
      <div className="hidden sm:flex sm:items-center sm:gap-3 sm:px-3 sm:pt-2">
        <MapModeControl mode={mode} onChange={onModeChange} />
        {rangePills}
        {/* Layers: inline pills at lg+, popover at md/sm */}
        <div className="hidden lg:flex">
          <MapLayersControl variant="inline" {...layersProps} />
        </div>
        <div className="flex lg:hidden">
          <MapLayersControl variant="popover" {...layersProps} />
        </div>
        {histChip}
        <div className="ml-auto">{snapshotBlock}</div>
      </div>

      {/* Row 2 (sm and up) — only when scrubbing */}
      {showRow2 && (
        <div className="hidden sm:flex sm:items-center sm:gap-2 sm:px-3 sm:pb-1 sm:pt-2">
          {rewindRow}
        </div>
      )}

      {/* ── iPhone condensed bar (<sm) ─────────────────────────────────── */}
      <div className="flex items-center justify-between gap-2 px-3 pt-2 sm:hidden">
        <MapModeControl mode={mode} onChange={onModeChange} />
        <button
          type="button"
          onClick={() => setMobileExpanded((v) => !v)}
          aria-label={mobileExpanded ? 'Collapse controls' : 'Expand controls'}
          aria-expanded={mobileExpanded}
          data-testid="map-mobile-expand"
          className="inline-flex h-9 w-9 items-center justify-center rounded border border-[var(--color-border-default)] hover:bg-[var(--color-surface-2)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]"
        >
          {mobileExpanded ? <ChevronDownIcon /> : <ChevronUpIcon />}
        </button>
      </div>

      {mobileExpanded && (
        <div
          className="flex flex-col gap-2 overflow-y-auto px-3 pb-1 pt-2 sm:hidden"
          style={{ maxHeight: 'min(60vh, 320px)' }}
          data-testid="map-mobile-expanded"
        >
          {/* Landscape-friendly ordering: scrub + seek + speed first so they
              are visible without scrolling on iPhone landscape. */}
          {rewindRow}
          <div className="flex flex-wrap items-center gap-2">
            <MapLayersControl variant="popover" {...layersProps} />
            {rangePills}
          </div>
          {histChip}
          <div>{snapshotBlock}</div>
        </div>
      )}
    </div>
  );
}
