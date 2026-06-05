import { lazy, StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { createBrowserRouter, RouterProvider } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Toaster } from 'sonner';

import App from './App';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { RouteError } from '@/components/RouteError';
import './index.css';

const StatsPage = lazy(() => import('@/pages/Stats'));
const SettingsPage = lazy(() => import('@/pages/Settings'));
const WatchlistPage = lazy(() => import('@/pages/Watchlist'));
const FeedersPage = lazy(() => import('@/pages/Feeders'));
const HistoryPage = lazy(() => import('@/pages/History'));
const GalleryPage = lazy(() => import('@/pages/Gallery'));
const AircraftPage = lazy(() => import('@/pages/Aircraft'));
const MetricsPage = lazy(() => import('@/pages/Metrics'));
const FlightPage = lazy(() => import('@/pages/Flight'));
const MapPage = lazy(() => import('@/pages/Map'));
// Opt-in VDL2/ACARS feature. Route is always registered (lazy chunk only loads
// on navigation); the page self-guards when the feature is disabled, and the
// nav item only appears when /api/settings reports vdl2_enabled.
const Vdl2Page = lazy(() => import('@/pages/Vdl2'));

// TanStack Query defaults — see plan H3.
// - refetchOnWindowFocus off: don't thrash backend on tab focus.
// - retry: 1 so transient blips don't surface immediately, real failures still do.
// - staleTime: 30s — aggregate dashboards tolerate 30s staleness; live polls
//   override per-query via refetchInterval.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: 1,
      staleTime: 30_000,
    },
  },
});

// Router basename:
//   prod: BASE_URL='/stats/' → basename='/stats'
//   dev:  BASE_URL='/'        → basename=''
// React Router expects no trailing slash. The v2.0.0-rc.1 coexistence build
// used `/stats/v2/` as a transitional basename; the v2.0.0 cutover moved
// the SPA to the canonical `/stats/` mount.
const basename = import.meta.env.BASE_URL.replace(/\/$/, '');

const router = createBrowserRouter(
  [
    {
      element: <App />,
      errorElement: <RouteError />,
      children: [
        { index: true, element: <StatsPage />, errorElement: <RouteError /> },
        { path: 'history', element: <HistoryPage />, errorElement: <RouteError /> },
        { path: 'map', element: <MapPage />, errorElement: <RouteError /> },
        { path: 'gallery', element: <GalleryPage />, errorElement: <RouteError /> },
        { path: 'aircraft/:icao', element: <AircraftPage />, errorElement: <RouteError /> },
        { path: 'flight/:id', element: <FlightPage />, errorElement: <RouteError /> },
        { path: 'metrics', element: <MetricsPage />, errorElement: <RouteError /> },
        { path: 'feeders', element: <FeedersPage />, errorElement: <RouteError /> },
        { path: 'watchlist', element: <WatchlistPage />, errorElement: <RouteError /> },
        { path: 'settings', element: <SettingsPage />, errorElement: <RouteError /> },
        { path: 'vdl2', element: <Vdl2Page />, errorElement: <RouteError /> },
      ],
    },
  ],
  { basename },
);

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
        <Toaster theme="dark" richColors position="top-right" />
      </QueryClientProvider>
    </ErrorBoundary>
  </StrictMode>,
);
