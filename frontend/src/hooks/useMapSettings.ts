import { useQuery } from '@tanstack/react-query';
import { apiJson } from '@/lib/api';

// Tiny settings read for the map: `map_history_hours` → max rewind window.
//
// Pulled out of pages/Map.tsx into its own hook to break a composition cycle:
// the playback-state hook needs `maxRewindSec` to clamp the scrubber, while the
// data-queries hook needs the playback state for the snapshot query key. If
// both lived together the page couldn't order them. With settings standalone,
// the page wires: useMapSettings → useMapPlaybackState(maxRewindSec) →
// useMapDataQueries(playback).
//
// Shares the ['settings'] query key (seeded in App.tsx, also read by
// useVdl2Enabled), so this adds no extra request.
export function useMapSettings(): { maxRewindHours: number; maxRewindSec: number } {
  const { data } = useQuery({
    queryKey: ['settings'],
    queryFn: () => apiJson<{ map_history_hours?: number }>('settings'),
    staleTime: 60_000,
  });
  const maxRewindHours = data?.map_history_hours ?? 24;
  return { maxRewindHours, maxRewindSec: maxRewindHours * 3600 };
}
