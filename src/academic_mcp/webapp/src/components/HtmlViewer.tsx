import { useEffect, useRef, useState } from 'react'
import DOMPurify from 'dompurify'
import { markRangesInRoot, scrollToFirstMark } from '../lib/domHighlight'
import type { HighlightChunk } from '../api/article'

interface Props {
  htmlUrl: string
  highlights: HighlightChunk[]
  totalChars: number
}

export function HtmlViewer({ htmlUrl, highlights: _highlights, totalChars: _totalChars }: Props) {
  const iframeRef = useRef<HTMLIFrameElement>(null)

  // Placeholder: future enhancement — inject highlight marks via contentDocument
  useEffect(() => {
    void iframeRef
  }, [])

  return (
    <iframe
      ref={iframeRef}
      src={htmlUrl}
      sandbox="allow-same-origin"
      style={{ width: '100%', height: '100%', border: 'none' }}
      title="Article HTML"
    />
  )
}

// Standalone HTML renderer with DOM-based highlight marking.
// Fetches HTML from `htmlUrl`, sanitises it, then marks snippet occurrences.
interface HtmlDivProps {
  htmlUrl: string
  highlights?: HighlightChunk[]
}

export function HtmlDivViewer({ htmlUrl, highlights = [] }: HtmlDivProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const cleanHtmlRef = useRef<string>('')
  const [ready, setReady] = useState(false)

  useEffect(() => {
    setReady(false)
    let cancelled = false
    fetch(htmlUrl, { credentials: 'include' })
      .then(r => r.text())
      .then(raw => {
        if (cancelled || !containerRef.current) return
        const clean = DOMPurify.sanitize(raw, {
          FORBID_TAGS: ['script', 'style', 'iframe'],
          FORBID_ATTR: ['onerror', 'onload', 'onclick'],
        })
        cleanHtmlRef.current = clean
        containerRef.current.innerHTML = clean
        setReady(true)
      })
      .catch(() => { if (!cancelled) setReady(true) })
    return () => { cancelled = true }
  }, [htmlUrl])

  useEffect(() => {
    if (!ready || !containerRef.current) return
    // Reset to clean HTML so re-renders don't stack multiple <mark> layers
    containerRef.current.innerHTML = cleanHtmlRef.current
    const snippets = highlights.map(h => h.snippet)
    markRangesInRoot(containerRef.current, snippets)
    scrollToFirstMark(containerRef.current)
  }, [highlights, ready])

  return (
    <div className="h-full overflow-y-auto">
      {!ready && (
        <div className="flex items-center justify-center h-full">
          <p className="text-sm text-gray-400 animate-pulse">Loading…</p>
        </div>
      )}
      <div
        ref={containerRef}
        className="prose max-w-none px-8 py-6 font-serif text-base leading-relaxed"
        style={ready ? undefined : { display: 'none' }}
      />
    </div>
  )
}

