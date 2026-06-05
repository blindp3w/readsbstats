/**
 * Phase 2-4 VDL2 integration: flight ACARS panel, history badge column,
 * and the capability gating of the History filter option + Stats section.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { TooltipProvider } from '@/components/ui/Tooltip';
import { AcarsPanel } from '@/components/vdl2/AcarsPanel';
import { FlightsTable, type Flight } from '@/components/FlightsTable';

interface Stub {
  vdl2_enabled?: boolean;
  messages?: unknown[];
}

function stubFetch(s: Stub) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : (input as Request).url ?? String(input);
    let body: unknown = {};
    if (url.includes('/api/settings')) body = { vdl2_enabled: s.vdl2_enabled ?? false };
    else if (url.includes('/api/health'))
      // AcarsPanel now gates on runtime availability (/api/health), not config.
      body = { vdl2: { enabled: s.vdl2_enabled ?? false, available: s.vdl2_enabled ?? false } };
    else if (url.includes('/api/vdl2/messages')) body = { messages: s.messages ?? [], next_before_id: null };
    else if (url.includes('/api/vdl2/stats'))
      body = { total: 0, last_hour: 0, aircraft: 0, top_labels: [], top_airlines: [], hourly: [] };
    else if (url.includes('/api/live')) body = { count: 0, aircraft: [] };
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }) as unknown as typeof fetch;
}

function newClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
}

function wrap(ui: React.ReactNode) {
  return render(
    <QueryClientProvider client={newClient()}>
      <TooltipProvider>
        <MemoryRouter>{ui}</MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  globalThis.localStorage.clear();
});

describe('AcarsPanel', () => {
  it('renders messages within the flight window when enabled', async () => {
    stubFetch({
      vdl2_enabled: true,
      messages: [
        { id: 1, ts: 1_749_065_117, icao_hex: '48e95d', registration: 'SP-LYF',
          flight: 'LO6550', label: 'H1', freq: 136.725, dsta: 'EPWA',
          body: 'depart EPWA gate 12', decoder: 'vdlm2dec' },
      ],
    });
    const { findByText, getByTestId } = wrap(
      <AcarsPanel icao="48e95d" firstSeen={1_749_060_000} lastSeen={1_749_070_000} />,
    );
    expect(await findByText('depart EPWA gate 12')).toBeTruthy();
    expect(getByTestId('flight-acars-card')).toBeTruthy();
  });

  it('caps the message list height with vertical scroll (matches the position log)', async () => {
    stubFetch({
      vdl2_enabled: true,
      messages: [
        { id: 1, ts: 1_749_065_117, icao_hex: '48e95d', label: 'H1',
          freq: 136.725, body: 'one', decoder: 'vdlm2dec' },
      ],
    });
    const { findByTestId } = wrap(
      <AcarsPanel icao="48e95d" firstSeen={1_749_060_000} lastSeen={1_749_070_000} />,
    );
    const scroll = await findByTestId('flight-acars-scroll');
    expect(scroll.className).toContain('overflow-y-auto');
    expect(scroll.className).toContain('max-h-[480px]');
    // The list lives inside the scroll container, not as a sibling.
    expect(scroll.querySelector('[data-testid="vdl2-list"]')).toBeTruthy();
  });

  it('shows empty state when no ACARS for the flight', async () => {
    stubFetch({ vdl2_enabled: true, messages: [] });
    const { findByTestId } = wrap(
      <AcarsPanel icao="48e95d" firstSeen={1} lastSeen={2} />,
    );
    expect(await findByTestId('flight-acars-empty')).toBeTruthy();
  });

  it('renders nothing when the feature is disabled', async () => {
    stubFetch({ vdl2_enabled: false });
    const { container } = wrap(<AcarsPanel icao="48e95d" firstSeen={1} lastSeen={2} />);
    // Settings resolves to disabled → panel returns null.
    await waitFor(() => {
      expect(container.querySelector('[data-testid="flight-acars-card"]')).toBeNull();
    });
  });
});

describe('FlightsTable ACARS badge', () => {
  const base: Flight = {
    id: 1, icao_hex: '48e95d', callsign: 'LO1', registration: 'SP-LYF',
    aircraft_type: 'B38M', flags: 0, primary_source: 'adsb',
    first_seen: 1000, last_seen: 2000, duration_sec: 1000, max_alt_baro: 35000,
    max_gs: 450, max_distance_nm: 100, total_positions: 10,
  };

  function renderTable(flights: Flight[]) {
    return render(
      <MemoryRouter>
        <FlightsTable
          flights={flights}
          isLoading={false}
          error={null}
          sortBy="first_seen"
          sortDir="desc"
          onSortChange={() => {}}
        />
      </MemoryRouter>,
    );
  }

  it('shows the ACARS column + badge when has_acars is present', () => {
    const { getByTestId } = renderTable([
      { ...base, id: 1, has_acars: 1 },
      { ...base, id: 2, icao_hex: 'aabbcc', has_acars: 0 },
    ]);
    expect(getByTestId('flights-row-1-acars').textContent).toContain('acars');
    expect(getByTestId('flights-row-2-acars').textContent).toBe('—');
  });

  it('omits the ACARS column entirely when has_acars is absent (VDL2 off)', () => {
    const { queryByTestId } = renderTable([base]);
    expect(queryByTestId('flights-row-1-acars')).toBeNull();
  });
});
