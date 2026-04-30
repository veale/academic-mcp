import { apiFetch } from './client'

export interface SearchResult {
  title: string
  authors: string[]
  year: string | null
  doi: string | null
  zotero_key: string | null
  abstract: string | null
  citations: number | null
  venue: string | null
  found_in: string[]
  in_zotero: boolean
  has_oa_pdf: boolean
  s2_id: string | null
  url: string | null
  scite: Record<string, unknown> | null
  score: number | null
}

export interface SearchResponse {
  results: SearchResult[]
  query: string
}

export interface SearchParams {
  q: string
  limit?: number
  zotero_only?: boolean
  semantic?: boolean
  include_scite?: boolean
  domain_hint?: string
}

export async function searchPapers(params: SearchParams): Promise<SearchResponse> {
  const qs = new URLSearchParams()
  qs.set('q', params.q)
  if (params.limit !== undefined) qs.set('limit', String(params.limit))
  if (params.zotero_only) qs.set('zotero_only', 'true')
  if (params.semantic != null) qs.set('semantic', String(params.semantic))
  if (params.include_scite) qs.set('include_scite', 'true')
  if (params.domain_hint) qs.set('domain_hint', params.domain_hint)
  return apiFetch<SearchResponse>(`/search?${qs}`)
}

export async function searchInCitations(
  doi: string,
  q: string,
  direction: 'in' | 'out' = 'out',
  limit = 25,
): Promise<SearchResponse> {
  const qs = new URLSearchParams({ doi, q, direction, limit: String(limit) })
  return apiFetch<SearchResponse>(`/citations/search?${qs}`)
}
