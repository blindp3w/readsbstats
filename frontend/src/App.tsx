import { Suspense, useEffect } from 'react';
import { Outlet } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { apiJson } from '@/lib/api';
import type { HealthResponse, Settings } from '@/lib/types';
import { Nav } from '@/components/Nav';
import { PageSkeleton } from '@/components/PageSkeleton';
import { TooltipProvider } from '@/components/ui/Tooltip';
import { useClockStore, hasStoredClockFormat } from '@/store/clockFormat';

// App-shell layout. Permanent across route changes: nav, theme, toaster.
// Lazy route chunks arrive via <Outlet/> + <Suspense/>.
//
// <TooltipProvider> wraps the tree so every <Tooltip> shares the same
// 300 ms open delay; once one tooltip is shown, others within 500 ms open
// immediately (skipDelayDuration) — feels responsive on dense controls.
export default function App() {
  // Seed clock format from /api/settings on first boot only. After the user
  // has touched localStorage.rsbs_clock_format, their choice wins. Shares
  // queryKey ['settings'] with the Settings page so we make only one request.
  //
  // Audit 2026-05-26: the seeding lives in a useEffect rather than the
  // React Query `select` because `select` is expected to be a pure
  // transformation. Side-effecting from inside it would run more often
  // than expected and make caching behaviour harder to reason about.
  const settingsQ = useQuery({
    queryKey: ['settings'],
    queryFn: () => apiJson<Settings>('settings'),
    staleTime: 60_000,
  });
  // Seed the ['health'] query on boot so the VDL2 runtime-availability bits
  // (useVdl2Health) are usually warm before a gated surface (Vdl2 page, History
  // "Has ACARS" filter, Stats VDL2 section) mounts — avoids an "unavailable"
  // flash. Shares the query key with useVdl2Health(), so this is the only fetch.
  useQuery({
    queryKey: ['health'],
    queryFn: () => apiJson<HealthResponse>('health'),
    staleTime: 30_000,
  });
  useEffect(() => {
    const fmt = settingsQ.data?.time_format;
    if (!hasStoredClockFormat() && (fmt === '12h' || fmt === '24h')) {
      useClockStore.getState().setClockFormat(fmt);
    }
  }, [settingsQ.data?.time_format]);

  // Keep `--rsbs-nav-h` in sync with the Nav's actual rendered height so
  // sticky elements (Stats RangePicker, Gallery filter tabs, History
  // chip row) dock cleanly under the nav without overlap. The static
  // fallback in index.css covers the pre-hydration paint; this observer
  // refines it as soon as React mounts and re-measures on viewport
  // resize / safe-area changes / nav content swaps.
  useEffect(() => {
    const nav = document.querySelector<HTMLElement>('[data-testid="app-nav"]');
    if (!nav) return;
    const apply = () => {
      const rect = nav.getBoundingClientRect();
      document.documentElement.style.setProperty('--rsbs-nav-h', `${Math.round(rect.height)}px`);
    };
    apply();
    if (typeof ResizeObserver === 'undefined') return; // jsdom shim safety
    const ro = new ResizeObserver(apply);
    ro.observe(nav);
    return () => ro.disconnect();
  }, []);

  return (
    <TooltipProvider delayDuration={300} skipDelayDuration={500}>
      <div className="min-h-screen">
        <Nav />
        <main>
          <Suspense fallback={<PageSkeleton />}>
            <Outlet />
          </Suspense>
        </main>
      </div>
    </TooltipProvider>
  );
}
