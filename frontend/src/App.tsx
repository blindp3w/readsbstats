import { Suspense } from 'react';
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
      if (
        !hasStoredClockFormat() &&
        (d.time_format === '12h' || d.time_format === '24h')
      ) {
        useClockStore.getState().setClockFormat(d.time_format);
      }
      return d;
    },
  });

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
