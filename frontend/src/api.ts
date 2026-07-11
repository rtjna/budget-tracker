// All API calls go through this wrapper. The backend rejects state-changing
// requests without the X-Budget-App header (CSRF protection: HTML forms
// can't set custom headers, and cross-origin fetch() with one forces a CORS
// preflight), so every non-GET request must carry it.
export function api(input: string, init: RequestInit = {}): Promise<Response> {
  const method = (init.method ?? 'GET').toUpperCase()
  if (method === 'GET' || method === 'HEAD') return fetch(input, init)
  return fetch(input, {
    ...init,
    headers: { 'X-Budget-App': '1', ...(init.headers ?? {}) },
  })
}
