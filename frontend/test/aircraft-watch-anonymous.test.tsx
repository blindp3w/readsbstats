/**
 * BUG-10: the Aircraft page strips a leading `~` from the URL icao (anonymous /
 * non-ICAO airframes), but the "am I already watching?" lookup compared the
 * stripped icao against the raw watchlist value. A watchlist entry stored with
 * the `~` prefix therefore never matched, so the button offered "+ Watch" for
 * an airframe already on the list. The lookup must normalize the stored value
 * the same way (lowercase + strip leading `~`).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { TooltipProvider } from '@/components/ui/Tooltip';
import AircraftPage from '@/pages/Aircraft';

const AIRCRAFT_FIXTURE = {
  total: 0,
  icao_hex: 'abc123',
  aircraft_info: { registration: null },
  flights: [],
};

function stubFetch(watchlistEntries: unknown[]) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    let body: unknown = {};
    if (url.includes('/api/settings')) body = { vdl2_enabled: false };
    else if (url.includes('/api/health')) body = { vdl2: { enabled: false, available: false } };
    else if (url.includes('/api/watchlist')) body = { entries: watchlistEntries };
    else if (url.includes('/flights')) body = AIRCRAFT_FIXTURE;
    else if (url.includes('/photo')) body = null;
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }) as unknown as typeof fetch;
}

function renderAircraft(route: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider delayDuration={0}>
        <MemoryRouter initialEntries={[route]}>
          <Routes>
            <Route path="/aircraft/:icao" element={<AircraftPage />} />
          </Routes>
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  globalThis.localStorage.clear();
});

describe('Aircraft watch button — anonymous (~) airframe detection (BUG-10)', () => {
  it('detects a ~-prefixed watchlist entry as already-watched for the stripped icao', async () => {
    // URL icao `~abc123` → stripped to `abc123`. The watchlist stores the
    // anonymous form `~ABC123`; the lookup must still match.
    stubFetch([{ id: 7, match_type: 'icao', value: '~ABC123' }]);
    renderAircraft('/aircraft/~abc123');

    const toggle = await screen.findByTestId('aircraft-watch-toggle');
    await waitFor(() => {
      expect(toggle.getAttribute('aria-pressed')).toBe('true');
    });
    expect(toggle.textContent).toContain('Watching');
  });

  it('still offers +Watch when the airframe is not on the watchlist', async () => {
    stubFetch([{ id: 7, match_type: 'icao', value: '~ZZZ999' }]);
    renderAircraft('/aircraft/~abc123');

    const toggle = await screen.findByTestId('aircraft-watch-toggle');
    await waitFor(() => {
      expect(toggle.getAttribute('aria-pressed')).toBe('false');
    });
    expect(toggle.textContent).toContain('Watch');
  });
});
