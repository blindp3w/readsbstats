/**
 * VDL2 reception card — two side-by-side panels (message rate + per-frequency
 * small multiples) matching the rest of the Metrics grid; the rate panel header
 * carries the freshness/total badge. ECharts is globally mocked to null (jsdom
 * has no canvas), so we assert the panel wrappers + header, not chart internals.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, waitFor, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Vdl2ReceptionCard } from '@/components/metrics/Vdl2ReceptionCard';

const FIXTURE = {
  bucket_seconds: 60,
  metrics: ['rate', '136.725', '136.875'],
  freqs: [136.725, 136.875],
  total: 556,
  newest_ts: 1000,
  newest_age_sec: 8,
  data: [
    [1000, 1060],
    [2, 1],
    [1.5, 0.5],
    [0.5, 0.5],
  ],
};

// Per-frequency signal + SNR payload (dumpvdl2). metrics indexes both matrices;
// column 0 is the ts row; null = an empty bucket (gap). The default stubFetch
// returns {ok:true} for /api/vdl2/signal, so the existing tests see no signal
// row (the vdlm2dec / no-data case) — only the tests below opt into this fixture.
const SIGNAL_FIXTURE = {
  bucket_seconds: 60,
  metrics: ['136.725', '136.975'],
  freqs: [136.725, 136.975],
  samples: 42,
  newest_ts: 1000,
  newest_age_sec: 8,
  signal: [
    [1000, 1060],
    [-46.0, -47.0],
    [-50.0, null],
  ],
  snr: [
    [1000, 1060],
    [7.0, 6.0],
    [2.0, null],
  ],
};

let fixture: Record<string, unknown> = FIXTURE;
let fetchSpy: ReturnType<typeof vi.fn>;

function stubFetch() {
  fetchSpy = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const body = url.includes('/api/vdl2/timeseries') ? fixture : { ok: true };
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
      <Vdl2ReceptionCard enabled={enabled} from={900} to={1100} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  fixture = FIXTURE;
  stubFetch();
});

describe('Vdl2ReceptionCard', () => {
  it('renders both chart wrappers and the header total + freshness', async () => {
    renderCard();
    await waitFor(() => screen.getByTestId('vdl2-rate-chart'));
    expect(screen.getByTestId('vdl2-freq-charts')).toBeTruthy();
    const fresh = screen.getByTestId('vdl2-reception-freshness');
    expect(fresh.textContent).toContain('556');
    expect(fresh.textContent).toContain('8s ago');
    expect(fresh.textContent).not.toContain('⚠');
  });

  it('flags a stale feed', async () => {
    fixture = { ...FIXTURE, newest_age_sec: 1200 };
    renderCard();
    await waitFor(() =>
      expect(screen.getByTestId('vdl2-reception-freshness').textContent).toContain('⚠'),
    );
    expect(screen.getByTestId('vdl2-reception-freshness').className).toContain('color-danger');
  });

  it('renders nothing and makes no request when not enabled', async () => {
    const { container } = renderCard(false);
    await waitFor(() => expect(fetchSpy).not.toHaveBeenCalled());
    expect(screen.queryByTestId('metrics-vdl2-reception')).toBeNull();
    expect(container.firstChild).toBeNull();
  });

  it('shows an error alert when the timeseries query fails (no silent blank)', async () => {
    globalThis.fetch = vi.fn(
      async () => new Response('err', { status: 500 }),
    ) as unknown as typeof fetch;
    renderCard();
    await waitFor(() => screen.getByTestId('vdl2-reception-error'));
  });

  it('shows a loading skeleton while the query is pending', async () => {
    // A never-resolving fetch keeps react-query in isLoading, so the
    // skeleton path (not a silent blank) is what renders.
    globalThis.fetch = vi.fn(() => new Promise<Response>(() => {})) as unknown as typeof fetch;
    renderCard();
    await waitFor(() => screen.getByTestId('vdl2-reception-loading'));
  });

  it('renders the per-frequency signal + SNR charts when signal data is present', async () => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      const body = url.includes('/api/vdl2/signal')
        ? SIGNAL_FIXTURE
        : url.includes('/api/vdl2/timeseries')
          ? FIXTURE
          : { ok: true };
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }) as unknown as typeof fetch;
    renderCard();
    await waitFor(() => screen.getByTestId('vdl2-signal-chart'));
    expect(screen.getByTestId('vdl2-snr-chart')).toBeTruthy();
  });

  it('hides the signal + SNR charts when there is no signal data (vdlm2dec feed)', async () => {
    // Default stubFetch returns {ok:true} for /api/vdl2/signal → empty metrics.
    renderCard();
    await waitFor(() => screen.getByTestId('vdl2-rate-chart'));
    expect(screen.queryByTestId('vdl2-signal-chart')).toBeNull();
    expect(screen.queryByTestId('vdl2-snr-chart')).toBeNull();
  });
});
