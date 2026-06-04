/**
 * VDL2 / ACARS page + nav capability gating.
 *
 * The feature is opt-in: the nav item and page content appear only when
 * /api/settings reports vdl2_enabled. Verifies the feed renders, the disabled
 * notice shows when off, and the nav link follows the capability flag.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { TooltipProvider } from '@/components/ui/Tooltip';
import Vdl2Page from '@/pages/Vdl2';
import { Nav } from '@/components/Nav';

interface StubResponses {
  vdl2_enabled?: boolean;
  messages?: unknown[];
  stats?: { total: number; last_hour: number; aircraft: number };
}

function stubFetch(r: StubResponses) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : (input as Request).url ?? String(input);
    let body: unknown = {};
    if (url.includes('/api/settings')) body = { vdl2_enabled: r.vdl2_enabled ?? false };
    else if (url.includes('/api/vdl2/stats')) body = r.stats ?? { total: 0, last_hour: 0, aircraft: 0 };
    else if (url.includes('/api/vdl2/messages')) body = { messages: r.messages ?? [], next_before_id: null };
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

function renderPage() {
  return render(
    <QueryClientProvider client={newClient()}>
      <MemoryRouter initialEntries={['/vdl2']}>
        <Vdl2Page />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  globalThis.localStorage.clear();
});

describe('Vdl2 page', () => {
  it('renders the message feed when enabled', async () => {
    stubFetch({
      vdl2_enabled: true,
      stats: { total: 5, last_hour: 2, aircraft: 3 },
      messages: [
        {
          id: 1, ts: 1_749_065_117, icao_hex: '48e95d', registration: 'SP-LYF',
          flight: 'LO6550', label: 'H1', freq: 136.725, dsta: 'EPWA',
          body: 'depart EPWA gate 12', decoder: 'vdlm2dec',
        },
      ],
    });
    const { findByText, getByTestId } = renderPage();
    expect(await findByText('depart EPWA gate 12')).toBeTruthy();
    await waitFor(() => expect(getByTestId('vdl2-stat-total').textContent).toContain('5'));
    expect(getByTestId('vdl2-row-hex').textContent).toBe('48e95d');
  });

  it('shows the disabled notice when the feature is off', async () => {
    stubFetch({ vdl2_enabled: false });
    const { findByTestId } = renderPage();
    expect(await findByTestId('vdl2-disabled')).toBeTruthy();
  });

  it('shows the empty state when enabled with no messages', async () => {
    stubFetch({ vdl2_enabled: true, messages: [] });
    const { findByTestId } = renderPage();
    expect(await findByTestId('vdl2-empty')).toBeTruthy();
  });
});

describe('Nav VDL2 capability', () => {
  function renderNav(enabled: boolean) {
    stubFetch({ vdl2_enabled: enabled });
    return render(
      <QueryClientProvider client={newClient()}>
        <TooltipProvider>
          <MemoryRouter initialEntries={['/']}>
            <Nav />
          </MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>,
    );
  }

  it('shows the VDL2 link when enabled', async () => {
    const { findByTestId } = renderNav(true);
    expect(await findByTestId('nav-desktop-vdl2')).toBeTruthy();
  });

  it('hides the VDL2 link when disabled', async () => {
    renderNav(false);
    // Settings resolves to disabled; the nav must never show a VDL2 item.
    await waitFor(() => {
      expect(document.querySelector('[data-testid="nav-desktop-settings"]')).toBeTruthy();
    });
    expect(document.querySelector('[data-testid="nav-desktop-vdl2"]')).toBeNull();
  });
});
