import { QueryClient } from '@tanstack/react-query'

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: false, staleTime: 30_000 },
  },
})

const BASE = '/webapp/api'

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message)
  }
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init.headers ?? {}),
    },
    credentials: 'include',
  })

  if (resp.status === 401) {
    // `next` must be relative to the router basepath (/webapp), otherwise the
    // post-login navigate() double-prefixes it (→ /webapp/webapp/... → 404).
    const rel = (window.location.pathname + window.location.search).replace(
      /^\/webapp/,
      '',
    )
    const next = encodeURIComponent(rel || '/')
    window.location.href = `/webapp/login?next=${next}`
    return new Promise(() => {})
  }

  if (!resp.ok) {
    const body = await resp.text().catch(() => '')
    throw new ApiError(resp.status, body || resp.statusText)
  }

  return resp.json() as Promise<T>
}
