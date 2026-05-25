import { Suspense, useEffect } from 'react';
import { Outlet } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { apiJson } from '@/lib/api';
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
  useQuery({
    queryKey: ['settings'],
    queryFn: () => apiJson<{ time_format?: string }>('settings'),
    staleTime: 60_000,
    select: (d) => {
      if (!hasStoredClockFormat() && (d.time_format === '12h' || d.time_format === '24h')) {
        useClockStore.getState().setClockFormat(d.time_format);
      }
      return d;
    },
  });

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
