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
  detail?: string;
  constructor(res: Response, body?: string) {
    // Surface a FastAPI `{"detail": ...}` reason in the message — kept AFTER the
    // `HTTP {status}` prefix so existing status-line assertions still hold — and
    // expose it as `.detail` for callers that want just the reason. errMsg()
    // returns `.message`, so error toasts now show the server's reason.
    let detail: string | undefined;
    if (body) {
      try {
        const parsed = JSON.parse(body);
        if (parsed && typeof parsed.detail === 'string') detail = parsed.detail;
      } catch {
        /* non-JSON body — leave detail undefined */
      }
    }
    super(detail ? `HTTP ${res.status}: ${detail}` : `HTTP ${res.status} ${res.statusText}`);
    this.status = res.status;
    this.body = body;
    this.detail = detail;
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
