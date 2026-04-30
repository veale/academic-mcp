/**
 * Pure-function helpers for Zotero deep-link URLs.
 *
 * Centralised here so that no raw `zotero://` strings appear in route
 * components.  The URLs are constructed client-side — no API round-trip
 * needed.
 */

/** Open the Zotero item selector for a given item key. */
export function selectUrl(key: string): string {
  return `zotero://select/library/items/${key}`
}

/**
 * Open a PDF in Zotero's built-in reader, optionally at a specific page.
 * `page` is 1-indexed (Zotero convention).
 */
export function openPdfUrl(key: string, page?: number): string {
  const base = `zotero://open-pdf/library/items/${key}`
  return page != null ? `${base}?page=${page}` : base
}
