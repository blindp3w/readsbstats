// F8 — the feed polls page 1 every 15 s and prepends new rows; a user scrolled
// down would see the content jump. Polling pauses while the viewport is
// scrolled, and an accessible button resumes it (scrolls to top + refetches).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { TooltipProvider } from '@/components/ui/Tooltip';
import { MessageList } from '@/components/vdl2/MessageList';
import Vdl2Page from '@/pages/Vdl2';
import type { Vdl2Message } from '@/lib/types';

const row = (id: number) => ({ id, ts: 1_749_000_000 + id, label: 'H1', body: `body ${id}` }) as Vdl2Message;

describe('MessageList onScrollTopChange', () => {
  it('reports the viewport scrollTop on scroll', () => {
    const onTop = vi.fn();
    render(
      <TooltipProvider delayDuration={0}>
        <MessageList messages={[row(1)]} scrollTestId="sv" onScrollTopChange={onTop} />
      </TooltipProvider>,
    );
    const sv = screen.getByTestId('sv');
    sv.scrollTop = 240;
    fireEvent.scroll(sv);
    expect(onTop).toHaveBeenLastCalledWith(240);
    sv.scrollTop = 0;
    fireEvent.scroll(sv);
    expect(onTop).toHaveBeenLastCalledWith(0);
  });

  it('reports 0 when it unmounts (so a parent never keeps a stale "scrolled" flag)', () => {
    const onTop = vi.fn();
    const { unmount } = render(
      <TooltipProvider delayDuration={0}>
        <MessageList messages={[row(1)]} onScrollTopChange={onTop} />
      </TooltipProvider>,
    );
    unmount();
    expect(onTop).toHaveBeenLastCalledWith(0);
  });
});

describe('Vdl2 page live polling', () => {
  const messageCalls = () =>
    (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls
      .map((c) => String(c[0]))
      .filter((u) => u.includes('/api/vdl2/messages')).length;

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'setTimeout', 'clearTimeout'], shouldAdvanceTime: true });
    globalThis.localStorage.clear();
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : String(input);
      let body: unknown = {};
      if (url.includes('/api/settings')) body = { vdl2_enabled: true };
      else if (url.includes('/api/health')) body = { status: 'ok', vdl2: { enabled: true, available: true } };
      else if (url.includes('/api/vdl2/stats')) body = { total: 1, last_hour: 1, aircraft: 1 };
      else if (url.includes('/api/vdl2/messages')) body = { messages: [row(2), row(1)], next_before_id: null };
      return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }) as unknown as typeof fetch;
  });
  afterEach(() => vi.useRealTimers());

  function renderPage() {
    return render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={['/vdl2']}>
          <TooltipProvider delayDuration={0}>
            <Vdl2Page />
          </TooltipProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it('polls every 15 s at the top, pauses while scrolled, resumes via the button', async () => {
    renderPage();
    await screen.findByTestId('vdl2-list');
    expect(screen.queryByTestId('vdl2-resume-live')).toBeNull();
    const base = messageCalls();

    // At the top: a 15 s tick refetches.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_500);
    });
    expect(messageCalls()).toBe(base + 1);

    // Scroll down: pill appears, polling stops.
    const sv = screen.getByTestId('vdl2-feed-scroll');
    sv.scrollTop = 300;
    fireEvent.scroll(sv);
    const pill = await screen.findByTestId('vdl2-resume-live');
    expect(pill.tagName).toBe('BUTTON');
    expect(pill.textContent).toMatch(/paused/i);
    const paused = messageCalls();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(46_000);
    });
    expect(messageCalls()).toBe(paused);

    // Resume: scrolls to top, pill disappears, fetches immediately.
    fireEvent.click(pill);
    expect(sv.scrollTop).toBe(0);
    await waitFor(() => expect(screen.queryByTestId('vdl2-resume-live')).toBeNull());
    await waitFor(() => expect(messageCalls()).toBeGreaterThan(paused));
  });
});
