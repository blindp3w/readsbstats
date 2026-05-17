/**
 * Receiver-health row rendering — guards the API field name.
 *
 * The backend dataclass field is `Check.severity` (see src/readsbstats/
 * health.py). Earlier the frontend type used `c.status`, which TypeScript
 * couldn't validate against the runtime JSON — every row fell through to
 * the dim "info" icon and looked identical. This test renders the Metrics
 * page with a synthetic /api/metrics/health response containing one of
 * each severity and asserts that the `data-status` attribute (the CSS
 * hook for per-row styling) matches the API field value.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import Metrics from '@/pages/Metrics';

const HEALTH_FIXTURE = {
  overall: 'warn',
  as_of: 0,
  checks: [
    { name: 'heartbeat', severity: 'ok', message: 'Metrics fresh' },
    { name: 'signal_drop', severity: 'warn', message: 'Signal soft' },
    { name: 'cpu_saturation', severity: 'critical', message: 'CPU > 90%' },
    { name: 'message_rate', severity: 'info', message: 'Baseline idle' },
  ],
};

function setupFetchStub() {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const path = url.split('?')[0];
    let body: unknown = { ok: true };
    if (path.endsWith('/api/metrics/health')) body = HEALTH_FIXTURE;
    else if (path.endsWith('/api/metrics'))
      body = { bucket_seconds: 60, metrics: [], data: [] };
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }) as unknown as typeof fetch;
}

function renderMetrics() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/metrics']}>
        <Routes>
          <Route path="/metrics" element={<Metrics />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  setupFetchStub();
  globalThis.localStorage.clear();
});

describe('Receiver health rows', () => {
  it('renders one row per check with the correct severity attribute', async () => {
    const { getByTestId } = renderMetrics();
    // Banner is collapsed by default; expand it.
    await waitFor(() => {
      expect(getByTestId('metrics-health-toggle')).toBeTruthy();
    });
    fireEvent.click(getByTestId('metrics-health-toggle'));

    await waitFor(() => {
      expect(getByTestId('metrics-health-check-heartbeat')).toBeTruthy();
    });

    // The data-status attribute reflects c.severity from the API. If the
    // field name drifts again (e.g. someone "renames" to status), all four
    // rows would carry the same fallback string and these assertions fail.
    expect(getByTestId('metrics-health-check-heartbeat').getAttribute('data-status')).toBe('ok');
    expect(getByTestId('metrics-health-check-signal_drop').getAttribute('data-status')).toBe(
      'warn',
    );
    expect(getByTestId('metrics-health-check-cpu_saturation').getAttribute('data-status')).toBe(
      'critical',
    );
    expect(getByTestId('metrics-health-check-message_rate').getAttribute('data-status')).toBe(
      'info',
    );
  });

  it('rows of different severity render distinct status icons', async () => {
    const { getByTestId } = renderMetrics();
    await waitFor(() => {
      expect(getByTestId('metrics-health-toggle')).toBeTruthy();
    });
    fireEvent.click(getByTestId('metrics-health-toggle'));
    await waitFor(() => {
      expect(getByTestId('metrics-health-check-heartbeat')).toBeTruthy();
    });

    // Each StatusIcon is a Radix svg with a class name unique to its glyph
    // (e.g. "radix-icons-check-circled"). We rely on the inline `style.color`
    // being distinct across severities — that's how the icon visually signals
    // which check is degraded.
    const colors = (['heartbeat', 'signal_drop', 'cpu_saturation', 'message_rate'] as const).map(
      (name) => {
        const svg = getByTestId(`metrics-health-check-${name}`).querySelector('svg');
        return svg?.getAttribute('style') ?? '';
      },
    );
    // All four colours must differ — they're the 4 different severities.
    const unique = new Set(colors);
    expect(unique.size).toBe(4);
  });
});
