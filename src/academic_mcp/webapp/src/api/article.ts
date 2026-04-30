import { apiFetch } from './client'

export interface ArticleViewers {
  pdf: boolean
  html: boolean
  text: boolean
}

export interface ArticleMeta {
  doi: string
  cache_key: string
  source: string
  word_count: number
  section_count: number
  section_detection: string
  metadata: Record<string, unknown>
  viewers: ArticleViewers
  error: string | null
  failure_hints: string[]
}

export interface ArticleIdentifier {
  doi?: string
  zotero_key?: string
  url?: string
}

export async function fetchArticleMeta(id: ArticleIdentifier): Promise<ArticleMeta> {
  const qs = new URLSearchParams()
  if (id.doi) qs.set('doi', id.doi)
  else if (id.zotero_key) qs.set('zotero_key', id.zotero_key)
  else if (id.url) qs.set('url', id.url)
  return apiFetch<ArticleMeta>(`/article?${qs}`)
}
