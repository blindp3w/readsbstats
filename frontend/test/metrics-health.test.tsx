/**
 * Receiver-health rendering — v2.7.0 HealthStripe.
 *
 * The backend dataclass field is `Check.severity` (see src/readsbstats/
 * health.py). The stripe consumes it directly. These tests guard against
 * field-name drift (`data-severity` attr) AND lock in the new visual
 * contract: per-check squares, summary counts, first-failing inline lines,
 * empty state, error state.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, fireEvent, waitFor, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { TooltipProvider } from '@/components/ui/Tooltip';
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

let stubHealth: unknown = HEALTH_FIXTURE;

function setupFetchStub() {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const path = url.split('?')[0];
    let body: unknown = { ok: true };
    if (path.endsWith('/api/metrics/health')) body = stubHealth;
    else if (path.endsWith('/api/metrics')) body = { bucket_seconds: 60, metrics: [], data: [] };
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
      <TooltipProvider delayDuration={0}>
        <MemoryRouter initialEntries={['/metrics']}>
          <Routes>
            <Route path="/metrics" element={<Metrics />} />
          </Routes>
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  stubHealth = HEALTH_FIXTURE;
  setupFetchStub();
  globalThis.localStorage.clear();
});

describe('HealthStripe — happy path with 4 checks', () => {
  it('renders one square per check with the correct severity attribute', async () => {
    renderMetrics();
    const squares = await waitFor(() => {
      const els = screen.queryAllByTestId('health-stripe-square');
      if (els.length === 0) throw new Error('squares not ready');
      return els;
    });
    expect(squares).toHaveLength(4);
    expect(squares.map((s) => s.getAttribute('data-severity'))).toEqual([
      'ok',
      'warn',
      'critical',
      'info',
    ]);
  });

  it('summary line reports counts per severity', async () => {
    renderMetrics();
    const summary = await waitFor(() => screen.getByTestId('health-stripe-summary'));
    const text = summary.textContent ?? '';
    expect(text).toContain('4 checks');
    expect(text).toContain('1 OK');
    expect(text).toContain('1 warn');
    expect(text).toContain('1 down');
    expect(text).toContain('1 info');
  });

  it('renders first-failing summaries for warn + critical only', async () => {
    renderMetrics();
    await waitFor(() => screen.getByTestId('health-stripe-failing'));
    // Critical and warn entries; not ok or info.
    expect(screen.queryByTestId('health-stripe-failing-cpu_saturation')).toBeTruthy();
    expect(screen.queryByTestId('health-stripe-failing-signal_drop')).toBeTruthy();
    expect(screen.queryByTestId('health-stripe-failing-heartbeat')).toBeNull();
    expect(screen.queryByTestId('health-stripe-failing-message_rate')).toBeNull();
  });

  it('clicking a square opens the detail panel', async () => {
    renderMetrics();
    const squares = await waitFor(() => {
      const els = screen.queryAllByTestId('health-stripe-square');
      if (els.length === 0) throw new Error('squares not ready');
      return els;
    });
    // Detail panel hidden by default.
    expect(screen.queryByTestId('metrics-health-detail')).toBeNull();
    fireEvent.click(squares[1]); // signal_drop (warn)
    await waitFor(() => {
      expect(screen.queryByTestId('metrics-health-detail')).toBeTruthy();
    });
    expect(screen.getByTestId('metrics-health-check-signal_drop').getAttribute('data-status')).toBe(
      'warn',
    );
  });

  it('expanded detail rows carry id + data-status for each severity', async () => {
    renderMetrics();
    await waitFor(() => screen.getByTestId('metrics-health-toggle'));
    fireEvent.click(screen.getByTestId('metrics-health-toggle'));
    await waitFor(() => screen.getByTestId('metrics-health-check-heartbeat'));
    expect(
      screen.getByTestId('metrics-health-check-heartbeat').getAttribute('data-status'),
    ).toBe('ok');
    expect(
      screen.getByTestId('metrics-health-check-cpu_saturation').getAttribute('id'),
    ).toBe('health-check-cpu_saturation');
  });
});

describe('HealthStripe — square-click focus management', () => {
  // Unit twin of the CI Playwright regression lock
  // `test_v2_health_stripe_second_square_click_re_focuses`: catching the
  // focus regression here costs ~1 s instead of a browser run.

  it('first square click expands the panel and focuses the matching row', async () => {
    renderMetrics();
    const squares = await waitFor(() => {
      const els = screen.queryAllByTestId('health-stripe-square');
      if (els.length === 0) throw new Error('squares not ready');
      return els;
    });
    fireEvent.click(squares[1]); // signal_drop
    await waitFor(() => {
      expect(document.activeElement?.id).toBe('health-check-signal_drop');
    });
  });

  it('second square click while the panel is open re-focuses the new row', async () => {
    // The original bug: the [open]-keyed effect did not re-run on the second
    // click (setOpen(true) bails out), so focus stayed on the first row.
    // openAndFocus now takes the synchronous path when already open.
    renderMetrics();
    const squares = await waitFor(() => {
      const els = screen.queryAllByTestId('health-stripe-square');
      if (els.length === 0) throw new Error('squares not ready');
      return els;
    });
    fireEvent.click(squares[1]); // signal_drop — opens panel
    await waitFor(() => {
      expect(document.activeElement?.id).toBe('health-check-signal_drop');
    });
    fireEvent.click(squares[2]); // cpu_saturation — panel already open
    expect(document.activeElement?.id).toBe('health-check-cpu_saturation');
  });

  it('failing-summary click also opens and focuses its check row', async () => {
    renderMetrics();
    const failing = await waitFor(() =>
      screen.getByTestId('health-stripe-failing-cpu_saturation'),
    );
    fireEvent.click(failing);
    await waitFor(() => {
      expect(document.activeElement?.id).toBe('health-check-cpu_saturation');
    });
  });
});


describe('HealthStripe — edge cases', () => {
  it('empty checks array shows "0 checks" and the toggle is disabled', async () => {
    stubHealth = { overall: 'ok', as_of: 0, checks: [] };
    renderMetrics();
    const toggle = await waitFor(() => screen.getByTestId('metrics-health-toggle'));
    expect(toggle.hasAttribute('disabled')).toBe(true);
    expect(screen.getByTestId('health-stripe-summary').textContent).toContain('0 checks');
    // No first-failing lines either.
    expect(screen.queryByTestId('health-stripe-failing')).toBeNull();
  });

  it('all-ok input shows no first-failing block', async () => {
    stubHealth = {
      overall: 'ok',
      as_of: 0,
      checks: [
        { name: 'heartbeat', severity: 'ok' },
        { name: 'cpu_saturation', severity: 'ok' },
      ],
    };
    renderMetrics();
    await waitFor(() => screen.getByTestId('health-stripe-summary'));
    expect(screen.queryByTestId('health-stripe-failing')).toBeNull();
  });
});
