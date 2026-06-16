import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { apiJson } from '@/lib/api';
import { type MapWindow } from '@/components/map/MapCommandBar';
import { type Mode } from '@/components/map/MapModeControl';
import { useVdl2Available } from '@/hooks/useVdl2Enabled';
import type { Vdl2ActiveResponse, Vdl2PositionsResponse } from '@/lib/types';

export interface Aircraft {
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

export interface SnapshotResp {
  at: number;
  is_live: boolean;
  receiver_lat: number | null;
  receiver_lon: number | null;
  aircraft: Aircraft[];
}

export interface HeatmapResp {
  points: [number, number, number][];
  window: string;
  count: number;
}

export interface CoverageResp {
  polygon: [number, number][];
  max_range_nm: number;
  window: string;
}

// Subset of the playback state the data queries depend on: the snapshot
// query key/fn is a function of mode/rewindOffsetSec/histAt, which is why this
// hook TAKES the playback state rather than owning it.
export interface MapDataPlayback {
  mode: Mode;
  rewindOffsetSec: number;
  histAt: number | null;
}

export interface MapDataQueries {
  snapshot: ReturnType<typeof useQuery<SnapshotResp>>;
  heatmapQ: ReturnType<typeof useQuery<HeatmapResp>>;
  coverageQ: ReturnType<typeof useQuery<CoverageResp>>;
  vdl2ActiveQ: ReturnType<typeof useQuery<Vdl2ActiveResponse>>;
  vdl2PositionsQ: ReturnType<typeof useQuery<Vdl2PositionsResponse>>;
  vdl2Available: boolean;
  // overlay + window toggle state
  showHeatmap: boolean;
  setShowHeatmap: (next: boolean | ((v: boolean) => boolean)) => void;
  showCoverage: boolean;
  setShowCoverage: (next: boolean | ((v: boolean) => boolean)) => void;
  showVdl2: boolean;
  setShowVdl2: (next: boolean | ((v: boolean) => boolean)) => void;
  mapWindow: MapWindow;
  setMapWindow: (next: MapWindow) => void;
  // derived
  aircraft: Aircraft[];
  acarsActive: Set<string> | undefined;
}

// Owns every map data query plus the overlay/window toggle state, extracted
// verbatim from pages/Map.tsx. Takes `playback` because the snapshot query
// key + queryFn derive from mode/rewindOffsetSec/histAt.
export function useMapDataQueries(playback: MapDataPlayback): MapDataQueries {
  const { mode, rewindOffsetSec, histAt } = playback;

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

  const aircraft = snapshot.data?.aircraft ?? [];

  return {
    snapshot,
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
    aircraft,
    acarsActive,
  };
}
