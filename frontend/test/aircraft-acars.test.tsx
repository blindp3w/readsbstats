/**
 * Aircraft-detail ACARS panel (Phase A2). The reusable AcarsPanel is mounted on
 * the aircraft page scoped to the airframe's whole [first_seen, last_seen]
 * history, with context="aircraft" wording/testids. Gated on RSBS_VDL2_ENABLED.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, waitFor, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { TooltipProvider } from '@/components/ui/Tooltip';
import AircraftPage from '@/pages/Aircraft';

interface Stub {
  vdl2_enabled?: boolean;
  messages?: unknown[];
}

const AIRCRAFT_FIXTURE = {
  total: 1,
  icao_hex: '48e95d',
  aircraft_info: {
    registration: 'SP-LYF',
    type_code: 'B38M',
    first_seen: 1_749_000_000,
    last_seen: 1_749_100_000,
  },
  flights: [],
};

function stubFetch(s: Stub) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    let body: unknown = {};
    if (url.includes('/api/settings')) body = { vdl2_enabled: s.vdl2_enabled ?? false };
    else if (url.includes('/api/vdl2/messages'))
      body = { messages: s.messages ?? [], next_before_id: null };
    else if (url.includes('/flights')) body = AIRCRAFT_FIXTURE;
    else if (url.includes('/photo')) body = null;
    else if (url.includes('/api/watchlist')) body = { entries: [] };
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }) as unknown as typeof fetch;
}

function renderAircraft() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider delayDuration={0}>
        <MemoryRouter initialEntries={['/aircraft/48e95d']}>
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

describe('Aircraft page ACARS panel', () => {
  it('renders ACARS messages for the airframe when VDL2 is enabled', async () => {
    stubFetch({
      vdl2_enabled: true,
      messages: [
        {
          id: 1, ts: 1_749_050_000, icao_hex: '48e95d', registration: 'SP-LYF',
          flight: 'LO6550', label: 'H1', body: 'maintenance report', decoder: 'vdlm2dec',
        },
      ],
    });
    renderAircraft();
    expect(await screen.findByTestId('aircraft-acars-card')).toBeTruthy();
    expect(await screen.findByText('maintenance report')).toBeTruthy();
  });

  it('shows the aircraft-scoped empty state when there are no messages', async () => {
    stubFetch({ vdl2_enabled: true, messages: [] });
    renderAircraft();
    const empty = await screen.findByTestId('aircraft-acars-empty');
    expect(empty.textContent).toContain('for this aircraft');
  });

  it('renders no ACARS card when the feature is disabled', async () => {
    stubFetch({ vdl2_enabled: false });
    renderAircraft();
    // Wait for the page to settle (info card present), then assert no panel.
    await screen.findByTestId('aircraft-info-card');
    await waitFor(() => {
      expect(screen.queryByTestId('aircraft-acars-card')).toBeNull();
    });
  });
});
