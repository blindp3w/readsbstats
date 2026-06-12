/**
 * Stats VDL2 card — "Top message labels" list shows the human-readable label
 * name inline next to known codes, and just the bare code for unknown ones.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, waitFor, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { TooltipProvider } from '@/components/ui/Tooltip';
import { Vdl2StatsCard } from '@/components/stats/Vdl2StatsCard';

const FIXTURE = {
  total: 240,
  last_hour: 12,
  aircraft: 5,
  top_labels: [
    { label: 'Q0', messages: 100, aircraft: 40 },
    { label: 'ZZ', messages: 7, aircraft: 3 },
  ],
  top_airlines: [],
  hourly: [],
};

function stubFetch() {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const body = url.includes('/api/vdl2/stats') ? FIXTURE : { ok: true };
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
  stubFetch();
});

describe('Vdl2StatsCard top-label names', () => {
  it('renders the label name next to known codes', async () => {
    renderCard();
    await waitFor(() =>
      expect(screen.getByTestId('vdl2-top-labels').textContent).toContain('Q0'),
    );
    expect(screen.getByTestId('vdl2-top-labels').textContent).toContain('Link test');
  });

  it('renders bare code for unknown labels', async () => {
    renderCard();
    await waitFor(() =>
      expect(screen.getByTestId('vdl2-top-labels').textContent).toContain('ZZ'),
    );
    // No " · " separator after the unknown code — i.e. no invented name.
    expect(screen.getByTestId('vdl2-top-labels').textContent).not.toContain('ZZ ·');
  });
});
