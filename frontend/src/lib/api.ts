// API client.
//
// URL layout:
//   prod: BASE_URL='/stats/v2/'  → API at '/stats/api/...'
//   dev:  BASE_URL='/'           → API at '/api/...' (Vite proxies to uvicorn)
//
// We strip the trailing 'v2/' segment from BASE_URL to derive API_BASE; this
// works for both environments without an extra env var.
//
// CSRF: web.py _csrf_check (line 390) requires X-Requested-With on POST/DELETE
// under /api/watchlist. We attach it to every mutating request unconditionally
// — Telegram-bot equivalent endpoints don't reject it, so over-attachment is
// safe and stops the test matrix from drifting per endpoint.

const BASE = import.meta.env.BASE_URL;
const API_BASE = BASE.replace(/v2\/?$/, '');

export const apiUrl = (path: string): string =>
  API_BASE + 'api/' + path.replace(/^\//, '');

export class ApiError extends Error {
  status: number;
  body?: string;
  constructor(res: Response, body?: string) {
    super(`HTTP ${res.status} ${res.statusText}`);
    this.status = res.status;
    this.body = body;
  }
}

export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  const method = (init.method ?? 'GET').toUpperCase();
  if (method !== 'GET' && method !== 'HEAD') {
    headers.set('X-Requested-With', 'XMLHttpRequest');
  }
  const res = await fetch(apiUrl(path), { ...init, method, headers });
  if (!res.ok) {
    let body: string | undefined;
    try {
      body = await res.text();
    } catch {
      /* ignore */
    }
    throw new ApiError(res, body);
  }
  return res;
}

export async function apiJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await apiFetch(path, init);
  return (await res.json()) as T;
}
