/**
 * Stats VDL2 card — "Flights also on ACARS (24h)" overlap KPI (Phase A3).
 * The tile appears only when the backend returns a non-null flights_overlap_pct
 * (the cross-DB join is available); otherwise it's hidden.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, waitFor, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { TooltipProvider } from '@/components/ui/Tooltip';
import { Vdl2StatsCard } from '@/components/stats/Vdl2StatsCard';

const BASE = {
  total: 240,
  last_hour: 12,
  aircraft: 5,
  top_labels: [],
  top_airlines: [],
  hourly: [],
};

let fixture: Record<string, unknown> = BASE;

function stubFetch() {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const body = url.includes('/api/vdl2/stats') ? fixture : { ok: true };
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }) as unknown as typeof fetch;
}

function renderCard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider delayDuration={0}>
        <Vdl2StatsCard />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  fixture = BASE;
  stubFetch();
});

describe('Vdl2StatsCard overlap KPI', () => {
  it('shows the overlap tile when flights_overlap_pct is present', async () => {
    fixture = { ...BASE, flights_overlap_pct: 42.5 };
    renderCard();
    await waitFor(() => expect(screen.getByTestId('vdl2-kpi-overlap').textContent).toContain('42.5%'));
  });

  it('hides the overlap tile when flights_overlap_pct is null', async () => {
    fixture = { ...BASE, flights_overlap_pct: null };
    renderCard();
    // Wait for the card to render (total tile present), then assert no overlap tile.
    await waitFor(() => screen.getByTestId('vdl2-kpi-total'));
    expect(screen.queryByTestId('vdl2-kpi-overlap')).toBeNull();
  });
});
