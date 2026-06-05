import { useQuery } from '@tanstack/react-query';
import { apiJson } from '@/lib/api';
import type { Settings } from '@/lib/types';

// Reads the VDL2 capability flag from /api/settings. Shares the ['settings']
// query key with App.tsx (seeded on boot) so this adds no extra request, and
// returns false while loading — callers render the no-VDL2 state until settings
// resolve. Used to gate the nav item, the flight ACARS panel, the history
// filter option, and the Stats VDL2 section.
export function useVdl2Enabled(): boolean {
  const { data } = useQuery({
    queryKey: ['settings'],
    queryFn: () => apiJson<Settings>('settings'),
    staleTime: 60_000,
  });
  return data?.vdl2_enabled === true;
}
