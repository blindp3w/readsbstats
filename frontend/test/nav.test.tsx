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

function renderNav() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={['/']}>
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
