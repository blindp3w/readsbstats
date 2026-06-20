/**
 * Watchlist delete failure-path coverage (audit 2026-06-20 gap). The optimistic
 * delete removes the row in onMutate; a failed DELETE must restore it (onError
 * rollback) — i.e. a server error never silently loses the entry. Only the
 * success path was covered before.
 *
 * The onSettled refetch is forced to FAIL here so the assertion is load-bearing
 * on the rollback alone: if the refetch succeeded and returned the entry, the
 * row would survive even without the onError rollback (the refetch would mask a
 * missing rollback). With the refetch failing, TanStack Query retains the last
 * cached data — so the row is present iff onError restored the snapshot. Proven
 * fail-first: deleting the `onError` handler makes this test fail.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { TooltipProvider } from '@/components/ui/Tooltip';
import WatchlistPage from '@/pages/Watchlist';

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider delayDuration={0}>
        <MemoryRouter><WatchlistPage /></MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => globalThis.localStorage.clear());

describe('Watchlist delete failure', () => {
  it('keeps the entry visible when the DELETE fails (onError rollback)', async () => {
    let getCount = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = (init?.method ?? 'GET').toUpperCase();
      if (method === 'DELETE') {
        return new Response(JSON.stringify({ detail: 'boom' }), { status: 500 });
      }
      if (url.includes('/api/watchlist')) {
        getCount += 1;
        if (getCount === 1) {
          // Initial list load — the entry exists.
          return new Response(
            JSON.stringify({ entries: [
              { id: 1, match_type: 'icao', value: 'abc123', label: 'Test', created_at: 0, airborne: false },
            ] }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          );
        }
        // The onSettled refetch fails, so it can't mask a missing rollback: the
        // row can only survive via the onError rollback restoring the snapshot.
        return new Response(JSON.stringify({ detail: 'refetch boom' }), { status: 500 });
      }
      return new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } });
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    renderPage();
    await screen.findByTestId('watchlist-row-1');

    fireEvent.click(screen.getByTestId('watchlist-delete-1'));            // open confirm dialog
    fireEvent.click(await screen.findByTestId('watchlist-delete-confirm')); // confirm → DELETE (500)

    // Wait for both the failed DELETE and the failed onSettled refetch to land.
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([, i]) => (i as RequestInit | undefined)?.method?.toUpperCase() === 'DELETE',
        ),
      ).toBe(true),
    );
    await waitFor(() => expect(getCount).toBeGreaterThanOrEqual(2));
    // Entry survives the failed delete — restored by the onError rollback, not
    // by the (now-failing) refetch.
    await waitFor(() => expect(screen.getByTestId('watchlist-row-1')).toBeInTheDocument());
  });
});
