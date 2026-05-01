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

export interface ArticleSection {
  title: string
  char_start: number
  char_end: number
  level: number
  keywords: string[]
  word_count: number
  is_infill: boolean
}

export interface ArticleText {
  doi: string
  cache_key: string
  text: string
  sections: ArticleSection[]
  section_detection: string
  word_count: number
}

export interface Rect {
  x0: number
  y0: number
  x1: number
  y1: number
}

export interface PageRects {
  page: number
  rects: Rect[]
}

export interface HighlightChunk {
  score: number
  char_start: number
  char_end: number
  snippet: string
  page_rects: PageRects[]
  match_type: 'semantic' | 'lexical'
}

export interface HighlightsResponse {
  cache_key: string
  chunks: HighlightChunk[]
  page_dimensions: Record<number, [number, number]>
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

export async function fetchArticleText(cacheKey: string): Promise<ArticleText> {
  return apiFetch<ArticleText>(`/article/text?cache_key=${encodeURIComponent(cacheKey)}`)
}

export async function fetchHighlights(
  cacheKey: string,
  q: string,
  k = 20,
  zoteroKey?: string,
): Promise<HighlightsResponse> {
  const qs = new URLSearchParams({ cache_key: cacheKey, q, k: String(k) })
  if (zoteroKey) qs.set('zotero_key', zoteroKey)
  return apiFetch<HighlightsResponse>(`/article/highlights?${qs}`)
}

export function articleHtmlUrl(cacheKey: string): string {
  return `/webapp/api/article/html?cache_key=${encodeURIComponent(cacheKey)}`
}

export function articlePdfUrl(cacheKey: string): string {
  return `/webapp/api/article/pdf?cache_key=${encodeURIComponent(cacheKey)}`
}
