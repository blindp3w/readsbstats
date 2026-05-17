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
      <MemoryRouter initialEntries={['/']}>
        <Nav />
      </MemoryRouter>
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
});
