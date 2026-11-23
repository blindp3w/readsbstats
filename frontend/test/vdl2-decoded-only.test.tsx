// "Decoded only" filter + decode-rate KPI on the VDL2 feed (client-side over
// loaded rows; URL param `dec=1`).
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { TooltipProvider } from '@/components/ui/Tooltip';
import Vdl2Page from '@/pages/Vdl2';
import type { Vdl2Message } from '@/lib/types';

// Stable module-level decode fn (like the real hook): decodes bodies starting "dec".
const decodeFn = (m: Vdl2Message) =>
  m.body?.startsWith('dec') ? { description: 'Decoded thing', items: [{ label: 'K', value: 'V' }] } : null;
vi.mock('@/hooks/useAcarsDecoder', () => ({
  useAcarsDecoder: () => decodeFn,
  useAcarsDecoderStatus: () => 'ready',
}));

const row = (id: number, body: string) =>
  ({ id, ts: 1_749_000_000 + id, label: 'H1', body }) as Vdl2Message;

function stub(messages: Vdl2Message[]) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : String(input);
    let body: unknown = {};
    if (url.includes('/api/settings')) body = { vdl2_enabled: true };
    else if (url.includes('/api/health')) body = { status: 'ok', vdl2: { enabled: true, available: true } };
    else if (url.includes('/api/vdl2/stats')) body = { total: 1, last_hour: 1, aircraft: 1 };
    else if (url.includes('/api/vdl2/messages')) body = { messages, next_before_id: null };
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }) as unknown as typeof fetch;
}

function renderPage(url = '/vdl2') {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })}>
      <MemoryRouter initialEntries={[url]}>
        <TooltipProvider delayDuration={0}>
          <Vdl2Page />
        </TooltipProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => globalThis.localStorage.clear());

describe('Vdl2 page decoded-only + decode rate', () => {
  it('shows the decode rate and filters undecoded rows', async () => {
    stub([row(4, 'dec a'), row(3, 'plain b'), row(2, 'plain c'), row(1, 'dec d')]);
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId('vdl2-message-row')).toHaveLength(4));
    expect(screen.getByTestId('vdl2-decode-rate').textContent).toMatch(/Decoded 2 of 4 loaded \(\s*50%\)/);

    fireEvent.click(screen.getByTestId('vdl2-decoded-only'));
    await waitFor(() => expect(screen.getAllByTestId('vdl2-message-row')).toHaveLength(2));
    expect(screen.queryByText('plain b')).toBeNull();
    expect(screen.getByText('dec a')).toBeTruthy();
    expect(screen.getByTestId('vdl2-decode-rate').textContent).toContain('decoded only');
    // Counts the loaded set, not the filtered view.
    expect(screen.getByTestId('vdl2-decode-rate').textContent).toContain('2 of 4');

    // Clear resets it.
    fireEvent.click(screen.getByTestId('vdl2-clear'));
    await waitFor(() => expect(screen.getAllByTestId('vdl2-message-row')).toHaveLength(4));
    expect((screen.getByTestId('vdl2-decoded-only') as HTMLInputElement).checked).toBe(false);
  });

  it('reads dec=1 from the URL and counts as an active filter', async () => {
    stub([row(2, 'dec a'), row(1, 'plain b')]);
    renderPage('/vdl2?dec=1');
    await waitFor(() => expect(screen.getAllByTestId('vdl2-message-row')).toHaveLength(1));
    expect((screen.getByTestId('vdl2-decoded-only') as HTMLInputElement).checked).toBe(true);
    expect(screen.getByTestId('vdl2-clear')).toBeTruthy();
  });

  it('explains when nothing loaded is decodable', async () => {
    stub([row(2, 'plain a'), row(1, 'plain b')]);
    renderPage('/vdl2?dec=1');
    await screen.findByTestId('vdl2-none-decoded');
    expect(screen.queryByTestId('vdl2-message-row')).toBeNull();
    expect(screen.getByTestId('vdl2-decode-rate').textContent).toContain('0 of 2');
  });
});
