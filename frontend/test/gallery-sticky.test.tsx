/**
 * M10.5 — Gallery filter tabs row sticks to the bottom of the nav as
 * the user scrolls the photo grid. Same `--rsbs-nav-h` pattern the
 * Stats RangePicker uses (v2.6.0).
 *
 * jsdom doesn't apply CSS so we can't observe sticky behaviour
 * end-to-end; class-presence + style.top assertion via the unique
 * data-testid is the project's standard pattern.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { TooltipProvider } from '@/components/ui/Tooltip';
import Gallery from '@/pages/Gallery';

beforeEach(() => {
  globalThis.fetch = vi.fn(async () => {
    return new Response(JSON.stringify({ total: 0, aircraft: [] }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }) as unknown as typeof fetch;
});

function renderGallery() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/gallery']}>
        <TooltipProvider>
          <Gallery />
        </TooltipProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('Gallery — sticky filter row', () => {
  it('wraps the filter tabs in a sticky container under the nav', async () => {
    const { findByTestId } = renderGallery();
    const sticky = await findByTestId('gallery-filter-sticky');
    expect(sticky.className).toMatch(/\bsticky\b/);
    // top docks under the nav via --rsbs-nav-h.
    expect(sticky.getAttribute('style') ?? '').toContain('--rsbs-nav-h');
  });

  it('keeps the existing filter group reachable inside the sticky wrapper', async () => {
    const { findByTestId } = renderGallery();
    const sticky = await findByTestId('gallery-filter-sticky');
    // The ToggleGroup that drives the flag filter must live inside the
    // sticky wrapper — moving it out would defeat M10.5.
    expect(sticky.querySelector('[data-testid="gallery-filter-group"]')).toBeTruthy();
  });
});
