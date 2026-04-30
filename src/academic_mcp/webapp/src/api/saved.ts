import { apiFetch } from './client'

export interface SavedSearch {
  id: number
  query: string
  params: Record<string, unknown>
  created_at: string
}

export function fetchSavedSearches(): Promise<SavedSearch[]> {
  return apiFetch('/saved-searches')
}

export function saveSearch(query: string, params: Record<string, unknown>): Promise<SavedSearch> {
  return apiFetch('/saved-searches', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query, params }),
  })
}

export function deleteSavedSearch(id: number): Promise<void> {
  return apiFetch(`/saved-searches/${id}`, { method: 'DELETE' })
}
