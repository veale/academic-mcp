/**
 * Viewer-selection logic for ArticlePage.
 *
 * Extracted so it can be unit-tested without a DOM.
 */

import type { ArticleViewers } from '../api/article'

export type ViewerType = 'pdf' | 'html' | 'txt'

/** Availability flags returned by the API. */
export interface ViewerAvailability {
  pdf: boolean
  html: boolean
  text: boolean
}

/**
 * Return the preferred viewer given availability flags.
 * Priority: pdf → html → txt.
 */
export function defaultViewer(viewers: ViewerAvailability): ViewerType {
  if (viewers.pdf) return 'pdf'
  if (viewers.html) return 'html'
  return 'txt'
}

/** Read the per-document viewer preference from localStorage. */
export function storedViewer(cacheKey: string): ViewerType | null {
  try {
    return localStorage.getItem(`viewer:${cacheKey}`) as ViewerType | null
  } catch {
    return null
  }
}

/** Write the per-document viewer preference to localStorage. */
export function storeViewer(cacheKey: string, viewer: ViewerType): void {
  try {
    localStorage.setItem(`viewer:${cacheKey}`, viewer)
  } catch {
    // Ignore in environments without localStorage (tests, SSR).
  }
}

/**
 * Resolve the viewer to display for a document.
 *
 * If `storedViewer` returns a value that is still available, honour it.
 * Otherwise fall back to `defaultViewer`.
 */
export function resolveViewer(
  viewers: ViewerAvailability,
  cacheKey: string,
): ViewerType {
  const stored = storedViewer(cacheKey)
  if (stored) {
    // Map the stored 'txt' key back to the availability field name 'text'.
    const availKey = stored === 'txt' ? 'text' : stored
    if (viewers[availKey as keyof ViewerAvailability]) return stored
  }
  return defaultViewer(viewers)
}

// Re-export ArticleViewers so callers can use it without an extra import.
export type { ArticleViewers }
