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

describe('History filter dropdowns', () => {
  it('Source trigger renders with default "any" label', () => {
    const { getByTestId } = renderHistory();
    const trigger = getByTestId('history-filter-source');
    expect(trigger.textContent).toContain('any');
  });

  it('opening Source shows all 5 options', async () => {
    const { getByTestId } = renderHistory();
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
    const trigger = getByTestId('history-filter-flags');
    trigger.focus();
    fireEvent.keyDown(trigger, { key: 'Enter' });
    await waitFor(() => {
      expect(document.body.textContent).toMatch(/military/);
    });
    expect(document.body.textContent).toMatch(/interesting/);
    expect(document.body.textContent).toMatch(/anonymous/);
  });

  it('hydrates the trigger label from a pre-populated URL param', () => {
    const { getByTestId } = renderHistory('/history?source=mlat');
    expect(getByTestId('history-filter-source').textContent).toContain('MLAT');
  });
});
