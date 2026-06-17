// Hand-typed shared shapes used across multiple pages/components.
//
// The backend publishes a typed OpenAPI contract for the hot endpoints
// (FastAPI `response_model=`, see `src/readsbstats/schemas.py`) at
// `/openapi.json`. There is no codegen step wired up yet, so these remain
// hand-maintained — when a backend response shape changes, update the
// matching interface here (or generate from the spec). Types here are the
// single source of truth for SPA-only narrowings and the backend shapes the
// SPA depends on.

// Audit-12 #P6.7 — `WatchlistEntry` was declared in two places (Aircraft.tsx
// and Watchlist.tsx) with divergent shapes. Single source of truth here.

export type WatchlistMatchType = 'icao' | 'registration' | 'callsign_prefix';

export interface WatchlistEntry {
  id: number;
  match_type: WatchlistMatchType;
  value: string;
  label: string | null;
  // Audit-12 P8 — `created_at` and `airborne` are populated by the LIST
  // endpoint (`GET /api/watchlist`) but NOT by the CREATE endpoint
  // (`POST /api/watchlist` returns only id/match_type/value/label on
  // 201). Marked optional so callers reading the mutation result can't
  // be lulled into a non-null assertion that would surprise at runtime.
  created_at?: number;
  // Live-state field surfaced by /api/watchlist's joins; 0/1 rather than
  // boolean because that's the JSON shape the backend emits.
  airborne?: 0 | 1;
}

export interface WatchlistResponse {
  entries: WatchlistEntry[];
}

// VDL2 / ACARS message as returned by /api/vdl2/messages* (the `raw` column is
// intentionally omitted from list responses). Mirrors api/vdl2._LIST_COLS and
// the schemas.Vdl2Message contract. Shared by the Vdl2 page and the flight-detail
// ACARS panel via components/vdl2/MessageList.tsx.
export interface Vdl2Message {
  id: number;
  ts: number;
  icao_hex: string | null;
  registration: string | null;
  flight: string | null;
  label: string | null;
  mode: string | null;
  block_id: string | null;
  ack: string | null;
  msgno: string | null;
  freq: number | null;
  station_id: string | null;
  toaddr: string | null;
  dsta: string | null;
  lat: number | null;
  lon: number | null;
  alt: number | null;
  epu: number | null;
  app_name: string | null;
  app_ver: string | null;
  body: string | null;
  decoder: string | null;
  filed_route?: {
    dep: string;
    arr: string;
    company_route?: string;
    sid?: string;
    star?: string;
    approach?: string;
  };
}

export interface Vdl2MessagesResponse {
  messages: Vdl2Message[];
  next_before_id: number | null;
}

export interface Vdl2TopLabel {
  label: string | null;
  messages: number;
  aircraft: number;
}

export interface Vdl2TopAirline {
  code: string | null;
  messages: number;
  name: string | null;
}

export interface Vdl2StatsResponse {
  total: number;
  last_hour: number;
  aircraft: number;
  top_labels: Vdl2TopLabel[];
  top_airlines: Vdl2TopAirline[];
  hourly: number[];
  // % of last-24h flights also seen on VDL2. Null when the cross-DB join is
  // unavailable — the tile is hidden in that case.
  flights_overlap_pct?: number | null;
}

// Bucketed VDL2 reception series for the Metrics page's two charts. Extends the
// columnar /api/metrics shape (MetricsResp) so buildPanelOption /
// buildSignalSmallMultiplesOption consume it directly.
export interface Vdl2TimeseriesResp {
  bucket_seconds: number;
  metrics: string[];
  data: number[][];
  freqs: number[];
  total: number;
  newest_ts: number | null;
  newest_age_sec: number | null;
}

// Per-frequency signal level (dBFS) + SNR (dB) for the Metrics page. dumpvdl2-only:
// `metrics` is empty on a vdlm2dec feed → the charts self-hide. `metrics` indexes
// both matrices (column 0 is ts; signal[i+1]/snr[i+1] align with metrics[i]). null
// = an empty bucket (gap). Each matrix is sliced into a MetricsResp for the builder.
export interface Vdl2SignalResp {
  bucket_seconds: number;
  metrics: string[];
  freqs: number[];
  samples: number;
  newest_ts: number | null;
  newest_age_sec: number | null;
  signal: (number | null)[][];
  snr: (number | null)[][];
}

// Map overlay: airframes that transmitted ACARS recently ("transmitting now").
export interface Vdl2ActiveResponse {
  icao_hex: string[];
  count: number;
}

export interface Vdl2Position {
  lat: number;
  lon: number;
  icao_hex: string | null;
  ts: number | null;
  label: string | null;
  // true = precise (~0.001°) Label-16 AUTPOS body fix; false = coarse (~0.1°) XID fix.
  precise?: boolean | null;
}

export interface Vdl2PositionsResponse {
  points: Vdl2Position[];
  count: number;
}

export interface Vdl2OooiEvent {
  type: 'DEP' | 'ARR' | null;
  registration: string | null;
  flight: string | null;
  dep_icao: string | null;
  dest_icao: string | null;
  t_out: string | null;
  t_off: string | null;
  t_on: string | null;
  t_in: string | null;
  ts: number | null;
}

export interface Vdl2OooiSummary {
  dep: Vdl2OooiEvent | null;
  arr: Vdl2OooiEvent | null;
  dsta: string | null;
  has_oooi: boolean;
}

// Subset of /api/settings the SPA reads. Shared so the ['settings'] query has a
// single type across App/Nav/useVdl2Enabled (they previously diverged).
export interface Settings {
  time_format?: string;
  vdl2_enabled?: boolean;
  map_history_hours?: number;
}

// Subset of /api/health the SPA reads for runtime VDL2 availability bits.
// `available` = vdl2.db queryable (Messages tab / Stats); `attach_available` =
// the read-only ATTACH usable (History has_acars filter/badge).
export interface HealthResponse {
  status?: string;
  vdl2?: { enabled?: boolean; available?: boolean; attach_available?: boolean };
}
