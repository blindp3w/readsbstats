/**
 * VDL2 reception card (Metrics page) — vdlm2dec-only receiver-health card.
 * Locks the per-frequency table, KPI tiles, freshness line + "stale" styling,
 * and the self-gating `enabled` prop (no fetch when the availability gate is off).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, waitFor, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { TooltipProvider } from '@/components/ui/Tooltip';
import { Vdl2ReceptionCard } from '@/components/metrics/Vdl2ReceptionCard';

const FIXTURE = {
  msgs_last_min: 2,
  msgs_last_hour: 12,
  msgs_24h: 240,
  aircraft_last_hour: 5,
  newest_ts: 1000,
  newest_age_sec: 8,
  per_freq: [
    { freq_mhz: 136.725, messages: 100, aircraft: 4 },
    { freq_mhz: 136.975, messages: 50, aircraft: 3 },
  ],
  rate_sparkline: Array.from({ length: 60 }, (_, i) => i % 4),
};

let fixture: Record<string, unknown> = FIXTURE;
let fetchSpy: ReturnType<typeof vi.fn>;

function stubFetch() {
  fetchSpy = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const body = url.includes('/api/vdl2/reception') ? fixture : { ok: true };
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  });
  globalThis.fetch = fetchSpy as unknown as typeof fetch;
}

function renderCard(enabled = true) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider delayDuration={0}>
        <Vdl2ReceptionCard enabled={enabled} />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  fixture = FIXTURE;
  stubFetch();
});

describe('Vdl2ReceptionCard', () => {
  it('renders KPI tiles and the per-frequency table', async () => {
    renderCard();
    await waitFor(() => expect(screen.getAllByTestId('vdl2-freq-row')).toHaveLength(2));
    const rows = screen.getAllByTestId('vdl2-freq-row');
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain('136.725 MHz');
    expect(screen.getByTestId('vdl2-kpi-rate').textContent).toContain('2');
    expect(screen.getByTestId('vdl2-kpi-aircraft').textContent).toContain('5');
  });

  it('shows fresh styling (no warning) when the feed is recent', async () => {
    renderCard();
    await waitFor(() =>
      expect(screen.getByTestId('vdl2-reception-freshness').textContent).toContain(
        'last message 8s ago',
      ),
    );
    expect(screen.getByTestId('vdl2-reception-freshness').textContent).not.toContain('⚠');
  });

  it('flags a stale feed when newest_age_sec exceeds the threshold', async () => {
    fixture = { ...FIXTURE, newest_age_sec: 1200 };
    renderCard();
    await waitFor(() =>
      expect(screen.getByTestId('vdl2-reception-freshness').textContent).toContain('⚠'),
    );
    expect(screen.getByTestId('vdl2-reception-freshness').className).toContain('color-danger');
  });

  it('makes no request and shows the empty state when not enabled', async () => {
    renderCard(false);
    await waitFor(() =>
      expect(screen.getByTestId('vdl2-reception-per-freq').textContent).toContain(
        'No messages received yet',
      ),
    );
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
