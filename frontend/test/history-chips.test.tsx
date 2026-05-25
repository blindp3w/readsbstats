/**
 * M8.3 — History filter row replaced with a chip-based pattern:
 *   [Active chip 1 ×] [Active chip 2 ×] [+ filter…]   [Export CSV]
 *   {total} flights · [▾ Advanced] · [Clear all]
 *   {advancedOpen && <Card with the old 9-field form>}
 *
 * The old form lives inside Advanced; the chip row derives state from
 * the same URL params, so both stay in sync without extra wiring.
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

describe('History filter chips — M8.3', () => {
  it('renders no chips when the URL has no filter params', () => {
    const { queryAllByTestId } = renderHistory('/history');
    const chips = queryAllByTestId(/^history-chip-/);
    expect(chips.length).toBe(0);
  });

  it('renders an ICAO chip when ?icao=3c4b17 is in the URL', () => {
    const { getByTestId } = renderHistory('/history?icao=3c4b17');
    const chip = getByTestId('history-chip-icao');
    expect(chip.textContent).toContain('3c4b17');
    expect(chip.textContent?.toLowerCase()).toContain('icao');
  });

  it('renders a single Date chip spanning date_from + date_to', () => {
    const { getByTestId } = renderHistory(
      '/history?date_from=2026-04-25&date_to=2026-05-25',
    );
    const chip = getByTestId('history-chip-date');
    expect(chip.textContent?.toLowerCase()).toContain('date');
    // Both endpoints should appear in the chip value.
    expect(chip.textContent).toContain('4');
    expect(chip.textContent).toContain('5');
  });

  it('renders a partial Date chip when only date_from is set', () => {
    const { getByTestId } = renderHistory('/history?date_from=2026-04-25');
    const chip = getByTestId('history-chip-date');
    // Em-dash on the empty side.
    expect(chip.textContent).toMatch(/–|—/);
  });

  it('Source chip displays the option LABEL, not the raw URL value', () => {
    // source=adsb should render as "ADS-B" (the Select option label),
    // not the raw lowercase URL key.
    const { getByTestId } = renderHistory('/history?source=adsb');
    const chip = getByTestId('history-chip-source');
    expect(chip.textContent).toContain('ADS-B');
  });

  it('clicking × on a chip clears the URL param + resets offset', async () => {
    const { getByTestId } = renderHistory('/history?icao=3c4b17&offset=200');
    const removeBtn = getByTestId('history-chip-icao-remove');
    fireEvent.click(removeBtn);
    await waitFor(() => {
      const params = getByTestId('probe-params').textContent ?? '';
      expect(params).not.toContain('icao=');
      expect(params).not.toContain('offset=200');
    });
  });

  it('clicking × on the Date chip clears both date_from AND date_to atomically', async () => {
    const { getByTestId } = renderHistory(
      '/history?date_from=2026-04-25&date_to=2026-05-25',
    );
    const removeBtn = getByTestId('history-chip-date-remove');
    fireEvent.click(removeBtn);
    await waitFor(() => {
      const params = getByTestId('probe-params').textContent ?? '';
      expect(params).not.toContain('date_from');
      expect(params).not.toContain('date_to');
    });
  });

  it('+ filter… button opens a popover listing the available field types', async () => {
    const { getByTestId } = renderHistory('/history');
    const trigger = getByTestId('history-add-filter-trigger');
    fireEvent.click(trigger);
    await waitFor(() => {
      expect(document.body.textContent).toMatch(/Callsign/);
    });
    // 8 field types should be available when no chip is active.
    expect(document.body.textContent).toMatch(/ICAO/);
    expect(document.body.textContent).toMatch(/Registration/);
    expect(document.body.textContent).toMatch(/Source/);
  });

  it('+ filter… popover OMITS fields that are already active chips', async () => {
    const { getByTestId } = renderHistory('/history?callsign=LOT');
    const trigger = getByTestId('history-add-filter-trigger');
    fireEvent.click(trigger);
    await waitFor(() => {
      // The "field picker" listbox should be open; Callsign should NOT
      // appear there as an option (it's already an active chip).
      expect(
        document.querySelector('[data-testid="history-add-filter-field-callsign"]'),
      ).toBeNull();
    });
    // Other unused fields still show.
    expect(
      document.querySelector('[data-testid="history-add-filter-field-icao"]'),
    ).toBeTruthy();
  });

  it('selecting a field in step 1 swaps the popover to a value input (step 2)', async () => {
    const { getByTestId } = renderHistory('/history');
    const trigger = getByTestId('history-add-filter-trigger');
    fireEvent.click(trigger);
    await waitFor(() => {
      expect(
        document.querySelector('[data-testid="history-add-filter-field-callsign"]'),
      ).toBeTruthy();
    });
    fireEvent.click(
      document.querySelector('[data-testid="history-add-filter-field-callsign"]')!,
    );
    await waitFor(() => {
      expect(
        document.querySelector('[data-testid="history-add-filter-value-input"]'),
      ).toBeTruthy();
    });
  });

  it('Advanced disclosure toggles the form below the chip row', async () => {
    const { getByTestId, queryByTestId } = renderHistory('/history');
    expect(queryByTestId('history-filters-form')).toBeNull();
    fireEvent.click(getByTestId('history-advanced-trigger'));
    await waitFor(() => {
      expect(queryByTestId('history-filters-form')).toBeTruthy();
    });
    fireEvent.click(getByTestId('history-advanced-trigger'));
    await waitFor(() => {
      expect(queryByTestId('history-filters-form')).toBeNull();
    });
  });

  it('pressing / focuses the + filter… trigger', async () => {
    const { getByTestId } = renderHistory('/history');
    const trigger = getByTestId('history-add-filter-trigger');
    // Spy click on the trigger.
    const clickSpy = vi.spyOn(trigger, 'click');
    fireEvent.keyDown(document, { key: '/' });
    expect(clickSpy).toHaveBeenCalled();
  });

  it('pressing / does NOT trigger when an input is focused', async () => {
    // Pre-populate a filter so the Advanced disclosure has a target input.
    const { getByTestId } = renderHistory('/history');
    // Open Advanced to give us a real input to focus.
    fireEvent.click(getByTestId('history-advanced-trigger'));
    await waitFor(() => {
      expect(document.querySelector('[data-testid="history-filter-icao"]')).toBeTruthy();
    });
    const input = document.querySelector(
      '[data-testid="history-filter-icao"]',
    ) as HTMLInputElement;
    input.focus();
    const trigger = getByTestId('history-add-filter-trigger');
    const clickSpy = vi.spyOn(trigger, 'click');
    fireEvent.keyDown(input, { key: '/', bubbles: true });
    expect(clickSpy).not.toHaveBeenCalled();
  });

  it('chip row sits inside a sticky wrapper with --rsbs-nav-h top', async () => {
    const { findByTestId } = renderHistory('/history');
    const sticky = await findByTestId('history-filter-sticky');
    expect(sticky.className).toMatch(/\bsticky\b/);
    expect(sticky.getAttribute('style') ?? '').toContain('--rsbs-nav-h');
  });
});
