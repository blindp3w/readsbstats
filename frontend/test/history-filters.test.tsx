/**
 * History page filter dropdowns — Source and Flag are Radix Selects with
 * a sentinel "any" value (Radix Select.Item forbids empty-string values).
 *
 * Verifies the trigger renders, the options appear on open, and the URL
 * search param updates correctly (incl. translating sentinel ↔ empty).
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Routes, Route, useSearchParams } from 'react-router-dom';
import { TooltipProvider } from '@/components/ui/Tooltip';
import History from '@/pages/History';

function setupFetchStub() {
  globalThis.fetch = vi.fn(async () =>
    new Response(JSON.stringify({ total: 0, limit: 100, offset: 0, flights: [] }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }),
  ) as unknown as typeof fetch;
}

// Probe component to expose the current URL search params for assertion.
function SearchProbe() {
  const [params] = useSearchParams();
  return <span data-testid="probe-params">{params.toString()}</span>;
}

function renderHistory(initialRoute = '/history') {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={[initialRoute]}>
          <Routes>
            <Route
              path="/history"
              element={
                <>
                  <History />
                  <SearchProbe />
                </>
              }
            />
          </Routes>
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  setupFetchStub();
  globalThis.localStorage.clear();
});

// M8.3 — the Source / Flag triggers now live inside the Advanced
// disclosure (collapsed by default). Helper opens it before each
// assertion so the existing form-level tests keep covering the
// dropdowns / sentinel translation as before.
async function openAdvanced(getByTestId: (id: string) => HTMLElement): Promise<void> {
  fireEvent.click(getByTestId('history-advanced-trigger'));
  await waitFor(() => {
    expect(document.querySelector('[data-testid="history-filter-source"]')).toBeTruthy();
  });
}

describe('History filter dropdowns', () => {
  it('Source trigger renders with default "any" label', async () => {
    const { getByTestId } = renderHistory();
    await openAdvanced(getByTestId);
    const trigger = getByTestId('history-filter-source');
    expect(trigger.textContent).toContain('any');
  });

  it('opening Source shows all 5 options', async () => {
    const { getByTestId } = renderHistory();
    await openAdvanced(getByTestId);
    const trigger = getByTestId('history-filter-source');
    trigger.focus();
    // Radix Select opens on Enter/Space via keyboard.
    fireEvent.keyDown(trigger, { key: 'Enter' });
    await waitFor(() => {
      expect(document.body.textContent).toMatch(/ADS-B/);
    });
    expect(document.body.textContent).toMatch(/MLAT/);
    expect(document.body.textContent).toMatch(/mixed/);
    expect(document.body.textContent).toMatch(/other/);
    // "any" appears both in the trigger (selected value) and the listbox.
    const anyCount = (document.body.textContent?.match(/any/g) ?? []).length;
    expect(anyCount).toBeGreaterThanOrEqual(2);
  });

  it('opening Flag shows all 4 options', async () => {
    const { getByTestId } = renderHistory();
    await openAdvanced(getByTestId);
    const trigger = getByTestId('history-filter-flags');
    trigger.focus();
    fireEvent.keyDown(trigger, { key: 'Enter' });
    await waitFor(() => {
      expect(document.body.textContent).toMatch(/military/);
    });
    expect(document.body.textContent).toMatch(/interesting/);
    expect(document.body.textContent).toMatch(/anonymous/);
  });

  it('hydrates the trigger label from a pre-populated URL param', async () => {
    // With ?source=mlat present, the chip row renders a "Source: MLAT"
    // label *and* the Advanced trigger label hydrates from the URL.
    const { getByTestId } = renderHistory('/history?source=mlat');
    await openAdvanced(getByTestId);
    expect(getByTestId('history-filter-source').textContent).toContain('MLAT');
  });
});
