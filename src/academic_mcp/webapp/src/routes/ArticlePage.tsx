import { useState, useEffect, useCallback } from 'react'
import { Link, useSearch } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'
import {
  fetchArticleMeta,
  fetchArticleText,
  fetchHighlights,
  articlePdfUrl,
  articleHtmlUrl,
  type HighlightChunk,
} from '../api/article'
import { PdfViewer } from '../components/PdfViewer'
import { HtmlDivViewer } from '../components/HtmlViewer'
import { TxtViewer } from '../components/TxtViewer'
import { SectionOutline } from '../components/SectionOutline'
import { HighlightRail } from '../components/HighlightRail'
import { useToast } from '../components/Toast'
import { openPdfUrl as zoteroOpenPdf, selectUrl as zoteroSelect } from '../lib/zoteroDeeplink'
import { resolveViewer, storeViewer, type ViewerType } from '../lib/viewerSelection'

function ViewerSwitcher({
  available,
  current,
  onChange,
}: {
  available: { pdf: boolean; html: boolean; text: boolean }
  current: ViewerType
  onChange: (v: ViewerType) => void
}) {
  const options: Array<{ key: ViewerType; label: string; avail: boolean }> = [
    { key: 'pdf', label: 'PDF', avail: available.pdf },
    { key: 'html', label: 'HTML', avail: available.html },
    { key: 'txt', label: 'Text', avail: available.text },
  ]
  return (
    <div className="flex rounded border overflow-hidden text-xs">
      {options.map(({ key, label, avail }) => (
        <button
          key={key}
          disabled={!avail}
          onClick={() => onChange(key)}
          className={[
            'px-3 py-1.5 border-r last:border-r-0 transition-colors',
            !avail
              ? 'text-gray-300 bg-gray-50 cursor-not-allowed'
              : current === key
                ? 'bg-blue-600 text-white'
                : 'hover:bg-gray-100 text-gray-700',
          ].join(' ')}
        >
          {label}
        </button>
      ))}
    </div>
  )
}

export function ArticlePage() {
  const { doi, zotero_key, url, q } = useSearch({ from: '/article' })
  const identifier = doi ?? zotero_key ?? url ?? ''
  const toast = useToast()

  const { data: meta, isLoading: metaLoading, error: metaError } = useQuery({
    queryKey: ['article-meta', doi, zotero_key, url],
    queryFn: () => fetchArticleMeta({ doi, zotero_key, url }),
    enabled: !!identifier,
  })

  const [viewer, setViewer] = useState<ViewerType | null>(null)
  const [currentPage, setCurrentPage] = useState(0)
  const [scrollToChar, setScrollToChar] = useState<number | null>(null)

  // Set viewer once meta is loaded
  useEffect(() => {
    if (!meta) return
    setViewer(resolveViewer(meta.viewers, meta.cache_key))
  }, [meta])

  const handleViewerChange = useCallback(
    (v: ViewerType) => {
      if (!meta) return
      storeViewer(meta.cache_key, v)
      setViewer(v)
    },
    [meta],
  )

  // Text + sections (needed for txt viewer and section outline)
  const needsText = viewer === 'txt' || (meta && meta.section_count > 0)
  const { data: articleText } = useQuery({
    queryKey: ['article-text', meta?.cache_key],
    queryFn: () => fetchArticleText(meta!.cache_key),
    enabled: !!(meta?.cache_key && needsText),
  })

  // Highlights when q is present
  const { data: highlightsData } = useQuery({
    queryKey: ['highlights', meta?.cache_key, q],
    queryFn: () => fetchHighlights(meta!.cache_key, q!),
    enabled: !!(meta?.cache_key && q),
  })

  const highlights: HighlightChunk[] = highlightsData?.chunks ?? []
  const pageDimensions = highlightsData?.page_dimensions ?? {}

  // Figure out Zotero deep-link
  const zoteroKey = (meta?.metadata?.key as string | undefined) ?? zotero_key
  function zoteroOpenPdfUrl() {
    if (!zoteroKey) return null
    const page = currentPage + 1 // Zotero is 1-indexed
    return zoteroOpenPdf(zoteroKey, page)
  }
  function zoteroSelectUrl() {
    if (!zoteroKey) return null
    return zoteroSelect(zoteroKey)
  }

  function handleRailJump(chunk: HighlightChunk) {
    if (viewer === 'pdf' && chunk.page_rects.length > 0) {
      setCurrentPage(chunk.page_rects[0].page)
    } else {
      setScrollToChar(chunk.char_start)
    }
  }

  function handleSectionSelect(charStart: number, id: string) {
    if (viewer === 'txt') {
      setScrollToChar(charStart)
    } else if (viewer === 'html') {
      document.getElementById(id)?.scrollIntoView({ behavior: 'smooth' })
    }
  }

  const sections = articleText?.sections ?? []
  const totalChars = articleText ? articleText.text.length : 0

  // Max page index across all highlights + 1
  const totalPages = highlights.reduce((max, c) => {
    const maxPage = c.page_rects.reduce((m, pr) => Math.max(m, pr.page), 0)
    return Math.max(max, maxPage + 1)
  }, 1)

  if (!identifier) {
    return (
      <div className="max-w-3xl mx-auto px-4 py-8">
        <p className="text-sm text-red-600">No article identifier provided.</p>
      </div>
    )
  }

  if (metaLoading) {
    return (
      <div className="max-w-3xl mx-auto px-4 py-8">
        <p className="text-sm text-gray-400 animate-pulse">Loading article…</p>
      </div>
    )
  }

  if (metaError || !meta) {
    return (
      <div className="max-w-3xl mx-auto px-4 py-8">
        <Link to="/" className="text-sm text-blue-600 hover:underline">← Back</Link>
        <p className="text-sm text-red-600 mt-4">Failed to load article.</p>
      </div>
    )
  }

  const title =
    typeof meta.metadata.title === 'string' ? meta.metadata.title : identifier

  function copyHighlightsAsMarkdown() {
    const titleStr = typeof meta?.metadata?.title === 'string' ? meta.metadata.title : identifier
    const doiStr = doi ? `https://doi.org/${doi}` : ''
    const lines: string[] = [`# ${titleStr}`, doiStr ? `DOI: ${doiStr}` : '', '']
    highlights.forEach((chunk, i) => {
      lines.push(`## Snippet ${i + 1}`)
      lines.push(chunk.snippet.trim())
      lines.push('')
    })
    const md = lines.filter((l, i) => !(l === '' && lines[i - 1] === '')).join('\n')
    navigator.clipboard
      .writeText(md)
      .then(() => toast.show('Copied to clipboard', 'success'))
      .catch(() => toast.show('Copy failed', 'error'))
  }

  const openPdfUrl = zoteroOpenPdfUrl()
  const selectUrl = zoteroSelectUrl()

  return (
    <div className="flex flex-col h-screen overflow-hidden">
      {/* Header bar */}
      <header className="flex items-center gap-3 px-4 py-2 border-b bg-white shrink-0 min-w-0">
        <Link to="/" className="text-sm text-blue-600 hover:underline shrink-0">← Search</Link>
        <h1 className="text-sm font-medium truncate min-w-0 flex-1">{title}</h1>
        <div className="flex items-center gap-2 shrink-0">
          {q && (
            <span className="text-xs bg-yellow-100 text-yellow-800 rounded px-2 py-0.5 max-w-[180px] truncate">
              {highlights.length} match{highlights.length !== 1 ? 'es' : ''} for "{q}"
            </span>
          )}
          {highlights.length > 0 && (
            <button
              onClick={copyHighlightsAsMarkdown}
              title="Copy highlights as markdown"
              className="text-xs border rounded px-2.5 py-1 hover:bg-gray-50 text-gray-700 shrink-0"
            >
              Copy snippets
            </button>
          )}
          {viewer && (
            <ViewerSwitcher
              available={meta.viewers}
              current={viewer}
              onChange={handleViewerChange}
            />
          )}
          {openPdfUrl && (
            <a
              href={openPdfUrl}
              className="text-xs border rounded px-2.5 py-1 hover:bg-gray-50 text-gray-700 shrink-0"
            >
              Open in Zotero
            </a>
          )}
          {!openPdfUrl && selectUrl && (
            <a
              href={selectUrl}
              className="text-xs border rounded px-2.5 py-1 hover:bg-gray-50 text-gray-700 shrink-0"
            >
              Select in Zotero
            </a>
          )}
        </div>
      </header>

      {/* Body: outline + viewer + rail */}
      <div className="flex flex-1 overflow-hidden">
        {/* Section outline */}
        {sections.length > 0 && (
          <SectionOutline
            sections={sections}
            activeChar={scrollToChar}
            onSelect={handleSectionSelect}
          />
        )}

        {/* Viewer */}
        <main className="flex-1 overflow-hidden">
          {viewer === 'pdf' && (
            <PdfViewer
              url={articlePdfUrl(meta.cache_key)}
              highlights={highlights}
              pageDimensions={pageDimensions as Record<number, [number, number]>}
              initialPage={currentPage}
              onPageChange={setCurrentPage}
            />
          )}

          {viewer === 'html' && (
            <HtmlDivViewer
              htmlUrl={articleHtmlUrl(meta.cache_key)}
              highlights={highlights}
            />
          )}

          {viewer === 'txt' && articleText && (
            <div className="h-full overflow-y-auto">
              <TxtViewer
                text={articleText.text}
                sections={sections}
                highlights={highlights}
                scrollToChar={scrollToChar}
              />
            </div>
          )}

          {viewer === 'txt' && !articleText && (
            <div className="flex items-center justify-center h-full">
              <p className="text-sm text-gray-400 animate-pulse">Loading text…</p>
            </div>
          )}
        </main>

        {/* Highlight heatmap rail */}
        {highlights.length > 0 && viewer && (
          <HighlightRail
            chunks={highlights}
            totalChars={totalChars}
            totalPages={totalPages}
            viewerType={viewer}
            onJump={handleRailJump}
          />
        )}
      </div>
    </div>
  )
}
