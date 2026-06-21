/**
 * Pure-function helpers for Zotero deep-link URLs.
 *
 * Centralised here so that no raw `zotero://` strings appear in route
 * components.  The URLs are constructed client-side — no API round-trip
 * needed.
 *
 * IMPORTANT: the `library` path segment is a literal that maps ONLY to the
 * personal "My Library". Items that live in a group library are a separate
 * database with their own item keys, and `zotero://select/library/items/<key>`
 * will NOT find them — there is no cross-library fallback. Group items must use
 * `zotero://select/groups/<groupID>/items/<key>`. Pass the item's library
 * context so we emit the right form.
 */

export interface ZoteroLibraryCtx {
  /** "user" (My Library) or "group". */
  libraryType?: string | null
  /** Numeric group ID, required for group-library items. */
  groupId?: number | null
}

/** Build the `library`/`groups/<id>` path segment for a deep link. */
function libraryScope(ctx?: ZoteroLibraryCtx): string {
  if (ctx?.libraryType === 'group' && ctx.groupId != null) {
    return `groups/${ctx.groupId}`
  }
  return 'library'
}

/** Open the Zotero item selector for a given item key. */
export function selectUrl(key: string, ctx?: ZoteroLibraryCtx): string {
  return `zotero://select/${libraryScope(ctx)}/items/${key}`
}

/**
 * Open a PDF in Zotero's built-in reader, optionally at a specific page.
 * `page` is 1-indexed (Zotero convention).
 */
export function openPdfUrl(
  key: string,
  page?: number,
  ctx?: ZoteroLibraryCtx,
): string {
  const base = `zotero://open-pdf/${libraryScope(ctx)}/items/${key}`
  return page != null ? `${base}?page=${page}` : base
}
