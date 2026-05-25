/**
 * Nav behaviour — mobile DropdownMenu and desktop horizontal list.
 *
 * Guards against accidental regressions when refactoring the nav (e.g.
 * dropping a link, breaking the asChild/Slot composition between Radix
 * DropdownMenu.Item and react-router NavLink).
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { Nav } from '@/components/Nav';
import { TooltipProvider } from '@/components/ui/Tooltip';

const EXPECTED_LABELS = [
  'statistics',
  'history',
  'map',
  'gallery',
  'watchlist',
  'feeders',
  'metrics',
  'settings',
];

// Sprint 1 #1 (M10.1): the last 4 labels live behind a `More ▾` dropdown
// at md/lg viewports. They're still rendered inline in the DOM at xl
// (and in jsdom because no CSS is loaded), but the dropdown is the
// reachable surface at iPad-portrait width.
const OVERFLOW_LABELS = ['watchlist', 'feeders', 'metrics', 'settings'];

function renderNav(initialEntry: string = '/') {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={[initialEntry]}>
          <Nav />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  // LiveCountBadge polls /api/live; stub fetch so the query resolves to
  // a benign shape and doesn't pollute test output.
  globalThis.fetch = vi.fn(async () =>
    new Response(JSON.stringify({ count: 0, aircraft: [] }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }),
  ) as unknown as typeof fetch;
  globalThis.localStorage.clear();
});

describe('Nav', () => {
  it('renders all 8 links in the desktop list', () => {
    const { getByTestId } = renderNav();
    for (const label of EXPECTED_LABELS) {
      expect(getByTestId(`nav-desktop-${label}`)).toBeTruthy();
    }
  });

  it('mobile menu is closed by default — items not in DOM', () => {
    renderNav();
    // Radix DropdownMenu renders content lazily into a Portal; closed
    // state means the items haven't mounted yet.
    expect(document.querySelector('[data-testid="nav-statistics"]')).toBeNull();
  });

  it('opening the hamburger trigger via keyboard renders all 8 links', async () => {
    const { getByTestId } = renderNav();
    const trigger = getByTestId('nav-toggle');
    // Radix DropdownMenu opens on pointerdown OR Enter/Space keypress.
    // jsdom + fireEvent.click does not synthesise pointerdown, so we use
    // the keyboard path which is also the screen-reader-equivalent open.
    trigger.focus();
    fireEvent.keyDown(trigger, { key: 'Enter' });
    await waitFor(() => {
      expect(document.querySelector('[data-testid="nav-statistics"]')).toBeTruthy();
    });
    for (const label of EXPECTED_LABELS) {
      expect(document.querySelector(`[data-testid="nav-${label}"]`)).toBeTruthy();
    }
  });

  it('trigger button has aria-label for screen readers', () => {
    const { getByTestId } = renderNav();
    expect(getByTestId('nav-toggle').getAttribute('aria-label')).toBe('Open navigation menu');
  });

  it('units selector reveals a unit-table tooltip on focus', async () => {
    const { getByTestId } = renderNav();
    const trigger = getByTestId('nav-units-select');
    trigger.focus();
    await waitFor(() => {
      const tip = document.querySelector('[data-testid="nav-units-tooltip"]');
      if (!tip) throw new Error('tooltip not ready');
    });
    const tip = document.querySelector('[data-testid="nav-units-tooltip"]')!;
    const text = tip.textContent ?? '';
    // All three unit systems present with their unit lists.
    expect(text).toContain('Aeronautical');
    expect(text).toContain('nm · ft · kts');
    expect(text).toContain('Metric');
    expect(text).toContain('km · m · km/h');
    expect(text).toContain('Imperial');
    expect(text).toContain('mi · ft · mph');
  });

  it('renders a More ▾ overflow trigger with menu ARIA attributes', () => {
    // Sprint 1 #1: at md/lg the last 4 nav items collapse into a
    // dropdown menu so the row fits on iPad portrait.
    const { getByTestId } = renderNav();
    const trigger = getByTestId('nav-more-trigger');
    expect(trigger.getAttribute('aria-haspopup')).toBe('menu');
    expect(trigger.tagName.toLowerCase()).toBe('button');
  });

  it('opening More ▾ via keyboard reveals the 4 overflow items', async () => {
    const { getByTestId } = renderNav();
    const trigger = getByTestId('nav-more-trigger');
    trigger.focus();
    fireEvent.keyDown(trigger, { key: 'Enter' });
    await waitFor(() => {
      expect(document.querySelector('[data-testid="nav-more-item-settings"]')).toBeTruthy();
    });
    for (const label of OVERFLOW_LABELS) {
      expect(
        document.querySelector(`[data-testid="nav-more-item-${label}"]`),
      ).toBeTruthy();
    }
  });

  it('More ▾ trigger highlights when the current route is one of the overflow items', () => {
    const { getByTestId } = renderNav('/settings');
    const trigger = getByTestId('nav-more-trigger');
    // Active marker: shares the same border-b-2 + accent class the
    // inline desktop links use for the active state.
    expect(trigger.className).toMatch(/border-b-2/);
  });

  it('More ▾ trigger is NOT highlighted when the current route is an inline item', () => {
    const { getByTestId } = renderNav('/history');
    const trigger = getByTestId('nav-more-trigger');
    expect(trigger.className).not.toMatch(/border-b-2/);
  });

  it('open units dropdown also shows unit-list subtitles (touch fallback)', async () => {
    // Radix Tooltip + Select hover handling on tap doesn't fire on mobile,
    // so the brief mandates "same content via the dropdown's open state".
    // This test guards the touch-fallback path independently of the
    // desktop tooltip path above — if anyone deletes `subtitle={o.units}`
    // from Nav.tsx, this assertion catches it.
    const { getByTestId } = renderNav();
    const trigger = getByTestId('nav-units-select');
    // Radix Select opens on Enter / Space / pointer; jsdom doesn't fire
    // pointerdown for click, so use keyboard.
    trigger.focus();
    fireEvent.keyDown(trigger, { key: 'Enter' });
    await waitFor(() => {
      const items = document.querySelectorAll('[role="option"]');
      if (items.length === 0) throw new Error('dropdown not open');
    });
    const allText = Array.from(document.querySelectorAll('[role="option"]'))
      .map((el) => el.textContent ?? '')
      .join(' | ');
    expect(allText).toContain('nm · ft · kts');
    expect(allText).toContain('km · m · km/h');
    expect(allText).toContain('mi · ft · mph');
  });
});
