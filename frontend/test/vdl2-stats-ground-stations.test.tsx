import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { TooltipProvider } from '@/components/ui/Tooltip';
import { Vdl2StatsCard } from '@/components/stats/Vdl2StatsCard';

const BASE = { total: 1, last_hour: 1, aircraft: 1, top_labels: [], top_airlines: [], hourly: [] };

function stub(extra: Record<string, unknown>) {
  const fetchMock = vi.fn(async () =>
    new Response(JSON.stringify({ ...BASE, ...extra }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }),
  );
  globalThis.fetch = fetchMock as unknown as typeof fetch;
  return fetchMock;
}

function renderCard(enabled = true) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider delayDuration={0}>
        <Vdl2StatsCard enabled={enabled} />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

describe('Vdl2StatsCard ground stations', () => {
  it('renders a row per station with formatted freq and em-dash for null SNR', async () => {
    stub({
      ground_stations: [
        { toaddr: '1234AB', freq: 136.725, frames: 1500, aircraft: 12, snr_db: 14.25 },
        { toaddr: null, freq: null, frames: 3, aircraft: 1, snr_db: null },
        { toaddr: '1234AB', freq: 136.725, frames: 2, aircraft: 1, snr_db: null },
      ],
    });
    renderCard();
    const card = await screen.findByTestId('vdl2-ground-stations');
    expect(card.textContent).toContain('Ground stations (24 h)');
    const rows = within(card).getAllByTestId('vdl2-gs-row');
    expect(rows).toHaveLength(3);
    const cells = (r: HTMLElement) => within(r).getAllByRole('cell').map((c) => c.textContent);
    expect(cells(rows[0])).toEqual(['1234AB', '136.725', '1,500', '12', '14.3']);
    expect(cells(rows[1])).toEqual(['—', '—', '3', '1', '—']);
    expect(cells(rows[2])[4]).toBe('—');
  });

  it('is hidden when the array is empty or missing (older backend)', async () => {
    stub({ ground_stations: [] });
    const { unmount } = renderCard();
    await screen.findByTestId('vdl2-kpi-total');
    expect(screen.queryByTestId('vdl2-ground-stations')).toBeNull();
    unmount();
    stub({});
    renderCard();
    await screen.findByTestId('vdl2-kpi-total');
    expect(screen.queryByTestId('vdl2-ground-stations')).toBeNull();
  });

  it('renders nothing when disabled and makes no request', () => {
    const f = stub({ ground_stations: [{ toaddr: 'X', freq: 1, frames: 1, aircraft: 1, snr_db: 1 }] });
    const { container } = renderCard(false);
    expect(container.innerHTML).toBe('');
    expect(f).not.toHaveBeenCalled();
  });
});
