import { apiFetch } from './client'

export async function login(password: string): Promise<{ ok: boolean }> {
  return apiFetch<{ ok: boolean }>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ password }),
  })
}

export async function logout(): Promise<void> {
  await apiFetch<{ ok: boolean }>('/auth/logout', { method: 'POST' })
}
