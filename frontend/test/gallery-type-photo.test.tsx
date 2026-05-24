/**
 * v2.8.0 M8.2 — type-photo indicator moved from a caption line in
 * CardContent (where it read as data) into a small corner stamp on the
 * photo itself. These tests pin the new location and confirm the old
 * caption no longer renders.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { TooltipProvider } from '@/components/ui/Tooltip';
import Gallery from '@/pages/Gallery';

const aircraft = (icao: string, isTypePhoto: boolean) => ({
  icao_hex: icao,
  registration: 'SP-TEST',
  aircraft_type: 'A320',
  type_desc: 'Airbus A320',
  flags: 0,
  flight_count: 5,
  first_seen: 1_700_000_000,
  last_seen: 1_700_000_900,
  thumbnail_url: 'https://example.com/thumb.jpg',
  large_url: 'https://example.com/large.jpg',
  link_url: 'https://example.com',
  photographer: 'test',
  is_type_photo: isTypePhoto,
  country: 'Poland',
});

function setupFetchStub(items: ReturnType<typeof aircraft>[]) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const path = url.split('?')[0];
    let body: unknown = { ok: true };
    if (path.endsWith('/api/aircraft/flagged')) {
      body = { total: items.length, aircraft: items };
    }
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }) as unknown as typeof fetch;
}

function renderGallery() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider delayDuration={0}>
        <MemoryRouter initialEntries={['/gallery']}>
          <Gallery />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  globalThis.localStorage.clear();
});

describe('Gallery — type-photo corner stamp (M8.2)', () => {
  it('renders the corner stamp when is_type_photo is true', async () => {
    setupFetchStub([aircraft('aabbcc', true)]);
    const { container } = renderGallery();
    await waitFor(() => {
      const stamp = container.querySelector('[data-testid="gallery-type-photo-stamp"]');
      if (!stamp) throw new Error('stamp not yet rendered');
    });
    const stamp = container.querySelector('[data-testid="gallery-type-photo-stamp"]')!;
    expect(stamp.getAttribute('aria-label')).toBe('Type photo');
    expect((stamp.textContent ?? '').toLowerCase()).toContain('type');
  });

  it('does NOT render the stamp when is_type_photo is false', async () => {
    setupFetchStub([aircraft('aabbcc', false)]);
    const { container } = renderGallery();
    // Wait for the grid to appear, then verify no stamp.
    await waitFor(() => {
      const grid = container.querySelector('[data-testid="gallery-grid"]');
      if (!grid) throw new Error('grid not yet rendered');
    });
    expect(container.querySelector('[data-testid="gallery-type-photo-stamp"]')).toBeNull();
  });

  it('removes the old "type photo" caption line from CardContent', async () => {
    setupFetchStub([aircraft('aabbcc', true)]);
    const { container } = renderGallery();
    await waitFor(() => {
      const stamp = container.querySelector('[data-testid="gallery-type-photo-stamp"]');
      if (!stamp) throw new Error('stamp not yet rendered');
    });
    // The previous design rendered "type photo" twice if the move was
    // accidentally additive. With the stamp moved into PhotoBox, the
    // metadata block should contain the literal "type photo" zero times.
    const card = container.querySelector('[data-testid="gallery-card-aabbcc"]')!;
    // Count matches in CardContent only (everything OUTSIDE the photo).
    // PhotoBox stamp lives inside the photo container which is the FIRST
    // child of the card link; CardContent is the second. We scope by
    // excluding the stamp text from the count.
    const cardText = card.textContent ?? '';
    // The stamp's text is "type" (just 4 chars). The old caption was
    // "type photo" (a full word pair). Assert the pair is gone.
    expect(cardText.toLowerCase()).not.toContain('type photo');
  });
});
