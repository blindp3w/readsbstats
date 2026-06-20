import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { apiUrl, apiFetch, ApiError } from '@/lib/api';

// import.meta.env.BASE_URL is set by Vite. In Vitest under jsdom the default
// is '/'. We assert the URL-math against that default — when Vite builds for
// production with base='/stats/v2/', the same logic must yield '/stats/api/'.
describe('apiUrl', () => {
  it('builds /api/<path> in dev', () => {
    expect(apiUrl('flights')).toBe('/api/flights');
    expect(apiUrl('/flights')).toBe('/api/flights');
  });

  it('handles query strings transparently', () => {
    expect(apiUrl('flights?page=2')).toBe('/api/flights?page=2');
  });
});

describe('apiFetch', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url: string, init?: RequestInit) =>
        new Response(JSON.stringify({ ok: true, method: init?.method, headers: init?.headers }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        }),
      ),
    );
  });
  afterEach(() => vi.unstubAllGlobals());

  it('does NOT add X-Requested-With on GET', async () => {
    await apiFetch('flights');
    const init = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0][1] as RequestInit;
    const headers = new Headers(init.headers);
    expect(headers.has('X-Requested-With')).toBe(false);
  });

  it('adds X-Requested-With on POST (CSRF)', async () => {
    await apiFetch('watchlist', { method: 'POST', body: '{}' });
    const init = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0][1] as RequestInit;
    const headers = new Headers(init.headers);
    expect(headers.get('X-Requested-With')).toBe('XMLHttpRequest');
  });

  it('adds X-Requested-With on DELETE (CSRF)', async () => {
    await apiFetch('watchlist/1', { method: 'DELETE' });
    const init = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0][1] as RequestInit;
    const headers = new Headers(init.headers);
    expect(headers.get('X-Requested-With')).toBe('XMLHttpRequest');
  });

  it('throws ApiError on non-2xx', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('boom', { status: 500, statusText: 'oops' })),
    );
    await expect(apiFetch('flights')).rejects.toBeInstanceOf(ApiError);
  });

  it('surfaces the server {detail} in the ApiError (message + .detail)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(JSON.stringify({ detail: 'invalid registration' }),
          { status: 400, statusText: 'Bad Request' })),
    );
    let err: unknown;
    try {
      await apiFetch('watchlist', { method: 'POST', body: '{}' });
    } catch (e) {
      err = e;
    }
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).detail).toBe('invalid registration');
    // Message keeps the HTTP-status prefix (live-count-badge asserts it) and
    // now also carries the reason.
    expect((err as Error).message).toContain('HTTP 400');
    expect((err as Error).message).toContain('invalid registration');
  });

  it('preserves caller-supplied headers alongside X-Requested-With', async () => {
    await apiFetch('watchlist', {
      method: 'POST', body: '{}', headers: { 'Content-Type': 'application/json' },
    });
    const init = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0][1] as RequestInit;
    const headers = new Headers(init.headers);
    expect(headers.get('X-Requested-With')).toBe('XMLHttpRequest');
    expect(headers.get('Content-Type')).toBe('application/json');
  });
});
