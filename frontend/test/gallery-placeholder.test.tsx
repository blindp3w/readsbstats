/**
 * M8.1 — Gallery no-photo placeholder. Aircraft with no thumbnail
 * render in one of two variants:
 *
 *   - **Featured**: anonymous / military / interesting (flagged) hex
 *     gets a big mono hex in the flag's accent colour + a coloured
 *     pill labelled with the flag name (`non-ICAO hex`, `military`,
 *     `interesting`). Same precedence as `primaryFlagLabel`
 *     (military > interesting > anonymous).
 *   - **Quiet**: ordinary aircraft (no flags or unknown flags) render
 *     a dim mono hex centred, no "no photo" text.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { TooltipProvider } from '@/components/ui/Tooltip';
import Gallery from '@/pages/Gallery';

interface AircraftStub {
  icao_hex: string;
  registration?: string | null;
  aircraft_type?: string | null;
  type_desc?: string | null;
  flags: number | undefined;
  flight_count: number;
  first_seen: number;
  last_seen: number;
  thumbnail_url: string | null;
  large_url?: string | null;
  link_url?: string | null;
  photographer?: string | null;
  is_type_photo: boolean;
  country?: string | null;
}

function aircraft(overrides: Partial<AircraftStub> & Pick<AircraftStub, 'icao_hex'>): AircraftStub {
  return {
    registration: null,
    aircraft_type: 'A320',
    type_desc: 'Airbus A320',
    flags: 0,
    flight_count: 1,
    first_seen: 1_700_000_000,
    last_seen: 1_700_000_900,
    thumbnail_url: null,
    large_url: null,
    link_url: null,
    photographer: null,
    is_type_photo: false,
    country: null,
    ...overrides,
  };
}

function setupFetchStub(items: AircraftStub[]) {
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

async function waitForGrid(container: HTMLElement): Promise<void> {
  await waitFor(() => {
    const grid = container.querySelector('[data-testid="gallery-grid"]');
    if (!grid) throw new Error('grid not yet rendered');
  });
}

describe('Gallery placeholder (M8.1)', () => {
  it('renders the <img> when thumbnail_url is set; no placeholder testids', async () => {
    setupFetchStub([
      aircraft({ icao_hex: 'aabbcc', thumbnail_url: 'https://example.com/t.jpg' }),
    ]);
    const { container } = renderGallery();
    await waitForGrid(container);
    expect(container.querySelector('img')).toBeTruthy();
    expect(container.querySelector('[data-testid="gallery-placeholder-featured"]')).toBeNull();
    expect(container.querySelector('[data-testid="gallery-placeholder-quiet"]')).toBeNull();
  });

  it('anonymous flagged hex → featured placeholder with "non-ICAO hex" pill', async () => {
    setupFetchStub([aircraft({ icao_hex: 'bf000f', flags: 16 })]);
    const { container } = renderGallery();
    await waitForGrid(container);
    const placeholder = container.querySelector('[data-testid="gallery-placeholder-featured"]');
    expect(placeholder).toBeTruthy();
    const pill = container.querySelector('[data-testid="gallery-placeholder-pill"]');
    expect(pill?.textContent?.toLowerCase()).toContain('non-icao hex');
  });

  it('military flagged hex → featured placeholder with "military" pill', async () => {
    setupFetchStub([aircraft({ icao_hex: 'ae0125', flags: 1 })]);
    const { container } = renderGallery();
    await waitForGrid(container);
    const placeholder = container.querySelector('[data-testid="gallery-placeholder-featured"]');
    expect(placeholder).toBeTruthy();
    const pill = container.querySelector('[data-testid="gallery-placeholder-pill"]');
    expect(pill?.textContent?.toLowerCase()).toContain('military');
  });

  it('interesting flagged hex → featured placeholder with "interesting" pill', async () => {
    setupFetchStub([aircraft({ icao_hex: '4ca123', flags: 2 })]);
    const { container } = renderGallery();
    await waitForGrid(container);
    const placeholder = container.querySelector('[data-testid="gallery-placeholder-featured"]');
    expect(placeholder).toBeTruthy();
    const pill = container.querySelector('[data-testid="gallery-placeholder-pill"]');
    expect(pill?.textContent?.toLowerCase()).toContain('interesting');
  });

  it('military + anonymous → military wins (precedence)', async () => {
    setupFetchStub([aircraft({ icao_hex: 'ae0001', flags: 17 })]); // 1 | 16
    const { container } = renderGallery();
    await waitForGrid(container);
    const pill = container.querySelector('[data-testid="gallery-placeholder-pill"]');
    expect(pill?.textContent?.toLowerCase()).toContain('military');
    expect(pill?.textContent?.toLowerCase()).not.toContain('non-icao');
  });

  it('ordinary unflagged aircraft → quiet placeholder, no pill, no "no photo" text', async () => {
    setupFetchStub([aircraft({ icao_hex: '484ce1', flags: 0 })]);
    const { container } = renderGallery();
    await waitForGrid(container);
    const quiet = container.querySelector('[data-testid="gallery-placeholder-quiet"]');
    expect(quiet).toBeTruthy();
    expect(container.querySelector('[data-testid="gallery-placeholder-featured"]')).toBeNull();
    expect(container.querySelector('[data-testid="gallery-placeholder-pill"]')).toBeNull();
    expect((quiet?.textContent ?? '').toLowerCase()).not.toContain('no photo');
    // The hex itself should be visible.
    expect(quiet?.textContent ?? '').toContain('484ce1');
  });

  it('undefined flags → quiet placeholder (defensive ?? 0 guard)', async () => {
    setupFetchStub([
      aircraft({ icao_hex: '484ce2', flags: undefined as unknown as number }),
    ]);
    const { container } = renderGallery();
    await waitForGrid(container);
    expect(container.querySelector('[data-testid="gallery-placeholder-quiet"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="gallery-placeholder-featured"]')).toBeNull();
  });
});
