import { Suspense } from 'react';
import { Outlet } from 'react-router-dom';
import { Nav } from '@/components/Nav';
import { PageSkeleton } from '@/components/PageSkeleton';
import { TooltipProvider } from '@/components/ui/Tooltip';

// App-shell layout. Permanent across route changes: nav, theme, toaster.
// Lazy route chunks arrive via <Outlet/> + <Suspense/>.
//
// <TooltipProvider> wraps the tree so every <Tooltip> shares the same
// 300 ms open delay; once one tooltip is shown, others within 500 ms open
// immediately (skipDelayDuration) — feels responsive on dense controls.
export default function App() {
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
