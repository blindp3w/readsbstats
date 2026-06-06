/**
 * BUG-15: LiveCountBadge's error title previously did an unchecked
 * `(q.error as Error).message` cast. A rejection that isn't an Error instance
 * (e.g. a thrown string/object from a future code path) would render
 * `undefined`/`[object Object]` or throw on `.message`. The fix guards with
 * `instanceof Error ? … : String(q.error)`. This test exercises the error
 * branch end-to-end and asserts a usable title.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { TooltipProvider } from '@/components/ui/Tooltip';
import { LiveCountBadge } from '@/components/LiveCountBadge';

function renderBadge() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider delayDuration={0}>
        <MemoryRouter>
          <LiveCountBadge />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  globalThis.localStorage.clear();
});

describe('LiveCountBadge error title (BUG-15)', () => {
  it('renders a usable failure title from an Error rejection', async () => {
    globalThis.fetch = vi.fn(async () =>
      new Response('boom', { status: 500, statusText: 'Internal Server Error' }),
    ) as unknown as typeof fetch;

    renderBadge();
    const badge = await screen.findByTestId('nav-live-badge');
    await waitFor(
      () => {
        expect(badge.getAttribute('aria-label')).toContain('Live poll failed');
      },
      { timeout: 4000 },
    );
    const title = badge.getAttribute('aria-label') ?? '';
    // The message comes from ApiError (an Error subclass) — should be the
    // HTTP status line, never `[object Object]` or `undefined`.
    expect(title).toContain('HTTP 500');
    expect(title).not.toContain('[object Object]');
    expect(title).not.toContain('undefined');
  });

  it('does not throw when the rejection is a non-Error value', async () => {
    // Force a non-Error rejection: the unguarded `(q.error as Error).message`
    // would have produced `undefined`; the guard falls back to String(...).
    globalThis.fetch = vi.fn(async () => {
      throw 'network-down';
    }) as unknown as typeof fetch;

    renderBadge();
    const badge = await screen.findByTestId('nav-live-badge');
    await waitFor(
      () => {
        expect(badge.getAttribute('aria-label')).toContain('Live poll failed');
      },
      { timeout: 4000 },
    );
    const title = badge.getAttribute('aria-label') ?? '';
    expect(title).toContain('network-down');
    expect(title).not.toContain('undefined');
  });
});
