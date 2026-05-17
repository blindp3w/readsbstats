// Hand-typed shared shapes used across multiple pages/components.
// Distinct from `api.types.ts`, which is generated from the OpenAPI spec.
// Anything that drifts on backend response shape changes should be auto-
// generated; types here are SPA-only inventions (e.g. union narrowings)
// or backend shapes that pre-date the OpenAPI export.

// Audit-12 #P6.7 — `WatchlistEntry` was declared in two places (Aircraft.tsx
// and Watchlist.tsx) with divergent shapes. Single source of truth here.

export type WatchlistMatchType = 'icao' | 'registration' | 'callsign_prefix';

export interface WatchlistEntry {
  id: number;
  match_type: WatchlistMatchType;
  value: string;
  label: string | null;
  created_at: number;
  // Live-state field surfaced by /api/watchlist's joins; 0/1 rather than
  // boolean because that's the JSON shape the backend emits.
  airborne: 0 | 1;
}

export interface WatchlistResponse {
  entries: WatchlistEntry[];
}
