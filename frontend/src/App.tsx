import { Suspense } from 'react';
import { Outlet } from 'react-router-dom';
import { Nav } from '@/components/Nav';
import { PageSkeleton } from '@/components/PageSkeleton';

// App-shell layout. Permanent across route changes: nav, theme, toaster.
// Lazy route chunks arrive via <Outlet/> + <Suspense/>.
export default function App() {
  return (
    <div className="min-h-screen">
      <Nav />
      <main>
        <Suspense fallback={<PageSkeleton />}>
          <Outlet />
        </Suspense>
      </main>
    </div>
  );
}
