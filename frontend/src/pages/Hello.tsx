import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { toast } from 'sonner';
import { apiJson } from '@/lib/api';

// Phase 0 proof-of-concept page.
//
// Exercises every coexistence-plumbing seam:
//   - import.meta.env.BASE_URL → API URL math (apiUrl)
//   - TanStack Query fetch + loading + error states
//   - Sonner toast (replaces the silent-mutations gap on the watchlist page)
//   - Router basename (rendered from BASE_URL)
//   - lazy() + Suspense (this whole component arrives via dynamic import)
//   - ErrorBoundary fallback (click "Throw a render error")
//
// Replaced by the real Statistics page in Phase 3.
interface LiveResponse {
  count?: number;
  receiver_lat?: number | null;
  receiver_lon?: number | null;
}

function ThrowError() {
  const [boom, setBoom] = useState(false);
  if (boom) throw new Error('Deliberate render error — proves ErrorBoundary catches it');
  return (
    <button
      type="button"
      onClick={() => setBoom(true)}
      className="rounded border border-[var(--color-danger)] px-3 py-1 text-sm text-[var(--color-danger)] hover:bg-[var(--color-surface-2)]"
    >
      Throw a render error
    </button>
  );
}

export default function Hello() {
  const live = useQuery<LiveResponse>({
    queryKey: ['live'],
    queryFn: () => apiJson<LiveResponse>('live'),
    staleTime: 10_000,
  });

  return (
    <div className="mx-auto max-w-2xl p-6">
      <h1 className="mb-2 text-2xl font-semibold">readsbstats — v2 shell</h1>
      <p className="mb-6 text-sm text-[var(--color-text-dim)]">
        Phase 0 proof of concept. Mounted at <code>{import.meta.env.BASE_URL}</code>; API at{' '}
        <code>{`${import.meta.env.BASE_URL.replace(/v2\/?$/, '')}api/`}</code>.
      </p>

      <section className="mb-6 rounded border border-[var(--color-border-default)] bg-[var(--color-surface)] p-4">
        <h2 className="mb-2 text-base font-medium">Live aircraft count</h2>
        {live.isLoading && <p className="text-sm text-[var(--color-text-dim)]">Loading…</p>}
        {live.isError && (
          <p className="text-sm text-[var(--color-danger)]">{(live.error as Error).message}</p>
        )}
        {live.data && (
          <p className="tabnum text-3xl font-bold text-[var(--color-success)]">
            {live.data.count ?? 0}
          </p>
        )}
      </section>

      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => toast.success('Sonner toast works')}
          className="rounded bg-[var(--color-accent)] px-3 py-1 text-sm text-white hover:opacity-90"
        >
          Toast
        </button>
        <button
          type="button"
          onClick={() => live.refetch()}
          className="rounded border border-[var(--color-border-default)] px-3 py-1 text-sm hover:bg-[var(--color-surface-2)]"
        >
          Refetch
        </button>
        <ThrowError />
      </div>
    </div>
  );
}
