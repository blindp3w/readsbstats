/**
 * Audit 17: the Watchlist add-form is the only CRUD surface in the SPA. Pin the
 * client-side ICAO-hex validation so a malformed value is rejected locally and
 * never POSTed (the mutation path was previously untested).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { TooltipProvider } from '@/components/ui/Tooltip';
import WatchlistPage from '@/pages/Watchlist';

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider delayDuration={0}>
        <MemoryRouter>
          <WatchlistPage />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  globalThis.localStorage.clear();
});

describe('Watchlist add-form ICAO validation', () => {
  it('rejects a non-hex ICAO value client-side and does not POST', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      const body = url.includes('/api/watchlist') ? { entries: [] } : {};
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    renderPage();

    // Default match type is ICAO hex; enter a 6-char but non-hex value.
    const input = await screen.findByTestId('watchlist-value');
    fireEvent.change(input, { target: { value: 'NOTHEX' } });
    fireEvent.submit(screen.getByTestId('watchlist-add-form'));

    const err = await screen.findByTestId('watchlist-form-error');
    expect(err.textContent).toContain('6 hexadecimal');

    // No mutating request was made — only GET fetches are allowed through.
    const mutating = fetchMock.mock.calls.filter(([, init]) => {
      const m = (init as RequestInit | undefined)?.method;
      return m != null && m.toUpperCase() !== 'GET';
    });
    expect(mutating.length).toBe(0);
  });
});
