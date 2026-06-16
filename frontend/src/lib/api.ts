// API client.
//
// URL layout (post-v2.0.0 cutover):
//   prod: BASE_URL='/stats/'  → API at '/stats/api/...'
//   dev:  BASE_URL='/'        → API at '/api/...' (Vite proxies to uvicorn)
//
// The Vite `base` is the same prefix where the API lives, so apiUrl is just
// BASE_URL + 'api/' + path.
//
// CSRF: web.py _csrf_check requires X-Requested-With on every mutating
// (POST/DELETE) /api/* request. apiFetch attaches it to all of them — over-
// attachment is safe and stops the test matrix from drifting per endpoint.

const BASE = import.meta.env.BASE_URL;

export const apiUrl = (path: string): string => BASE + 'api/' + path.replace(/^\//, '');

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
