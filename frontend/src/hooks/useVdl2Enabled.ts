import { useQuery } from '@tanstack/react-query';
import { apiJson } from '@/lib/api';
import type { HealthResponse, Settings } from '@/lib/types';

// Reads the VDL2 capability flag from /api/settings. Shares the ['settings']
// query key with App.tsx (seeded on boot) so this adds no extra request, and
// returns false while loading — callers render the no-VDL2 state until settings
// resolve. Drives the nav item + the Vdl2 page guard (config-level "is the
// feature turned on").
export function useVdl2Enabled(): boolean {
  const { data } = useQuery({
    queryKey: ['settings'],
    queryFn: () => apiJson<Settings>('settings'),
    staleTime: 60_000,
  });
  return data?.vdl2_enabled === true;
}

// Shared runtime-availability read from /api/health. Exposes the two INDEPENDENT
// capability bits plus loading state so callers can avoid an "unavailable" flash
// before the first health response lands. Shares the ['health'] query key (seeded
// in App.tsx on boot), so the bits are usually warm by the time a gated surface
// mounts.
//  - available       — vdl2.db is queryable (Messages tab / Stats VDL2 section)
//  - attachAvailable — the read-only ATTACH is usable (History "Has ACARS"
//                      filter + badge — a different bit: the web_conn can work
//                      while the cross-DB ATTACH fails, e.g. read-only mount).
export function useVdl2Health(): {
  available: boolean;
  attachAvailable: boolean;
  isLoading: boolean;
} {
  const { data, isLoading } = useQuery({
    queryKey: ['health'],
    queryFn: () => apiJson<HealthResponse>('health'),
    staleTime: 30_000,
  });
  return {
    available: data?.vdl2?.available === true,
    attachAvailable: data?.vdl2?.attach_available === true,
    isLoading,
  };
}

// Gate Messages/Stats surfaces (anything reading /api/vdl2/*) on this — false
// while loading, so callers should pair it with useVdl2Health().isLoading if a
// skeleton-vs-unavailable distinction matters.
export function useVdl2Available(): boolean {
  return useVdl2Health().available;
}

// Gate the History "Has ACARS" filter/badge on this — the flights query reads
// the cross-DB ATTACH, which can be unavailable even when `available` is true.
export function useVdl2AttachAvailable(): boolean {
  return useVdl2Health().attachAvailable;
}

// Widen a flight's [first_seen, last_seen] window by this much on each side so
// the detail-page panels catch OOOI/gate traffic just before pushback and after
// landing.
const VDL2_WINDOW_SLACK_SEC = 1800;

// Shared gate + scoped time window for the flight/aircraft detail VDL2 panels
// (AcarsPanel, OooiCard): runtime availability plus the slack-widened window.
// Keeps the SLACK + windowing in one place so the two panels can't drift.
export function useVdl2FlightWindow(
  firstSeen: number,
  lastSeen: number,
): { available: boolean; since: number; until: number } {
  return {
    available: useVdl2Available(),
    since: firstSeen - VDL2_WINDOW_SLACK_SEC,
    until: lastSeen + VDL2_WINDOW_SLACK_SEC,
  };
}
