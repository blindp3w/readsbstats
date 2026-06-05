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
}

// Subset of /api/settings the SPA reads. Shared so the ['settings'] query has a
// single type across App/Nav/useVdl2Enabled (they previously diverged).
export interface Settings {
  time_format?: string;
  vdl2_enabled?: boolean;
}

// Subset of /api/health the SPA reads for runtime VDL2 availability bits.
// `available` = vdl2.db queryable (Messages tab / Stats); `attach_available` =
// the read-only ATTACH usable (History has_acars filter/badge).
export interface HealthResponse {
  status?: string;
  vdl2?: { enabled?: boolean; available?: boolean; attach_available?: boolean };
}
