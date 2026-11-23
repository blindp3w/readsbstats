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

  it('shows an error alert when the stats query fails (no silent —/empty)', async () => {
    globalThis.fetch = vi.fn(
      async () => new Response('err', { status: 500 }),
    ) as unknown as typeof fetch;
    renderCard();
    await waitFor(() => screen.getByTestId('vdl2-stats-error'));
  });

  it('shows a loading skeleton while the query is pending', async () => {
    // A never-resolving fetch keeps react-query in isLoading, so the skeleton
    // path renders instead of a row of indistinguishable '—' KPIs.
    globalThis.fetch = vi.fn(() => new Promise<Response>(() => {})) as unknown as typeof fetch;
    renderCard();
    await waitFor(() => screen.getByTestId('vdl2-stats-loading'));
  });
});

describe('Vdl2StatsCard null codes and disabled state (batch 3 F5)', () => {
  it('renders placeholders and no duplicate-key warnings for null label/airline codes', async () => {
    const err = vi.spyOn(console, 'error').mockImplementation(() => {});
    globalThis.fetch = vi.fn(async () =>
      new Response(
        JSON.stringify({
          ...FIXTURE,
          top_labels: [
            { label: null, messages: 9, aircraft: 2 },
            { label: null, messages: 4, aircraft: 1 },
          ],
          top_airlines: [
            { code: null, messages: 8, name: null },
            { code: null, messages: 3, name: null },
          ],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    ) as unknown as typeof fetch;
    renderCard();
    await waitFor(() => screen.getByTestId('vdl2-top-labels'));
    await waitFor(() =>
      expect(screen.getByTestId('vdl2-top-labels').textContent).toContain('9'),
    );
    expect(screen.getByTestId('vdl2-top-labels').textContent).toContain('—');
    expect(screen.getByTestId('vdl2-top-airlines').textContent).toContain('—');
    expect(
      err.mock.calls.some((c) => String(c[0]).includes('same key') || String(c[0]).includes('unique "key"')),
    ).toBe(false);
    err.mockRestore();
  });

  it('renders nothing when disabled', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    const { container } = render(
      <QueryClientProvider client={qc}>
        <Vdl2StatsCard enabled={false} />
      </QueryClientProvider>,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
