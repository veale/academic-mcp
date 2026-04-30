import { useEffect, useRef } from 'react'
import { Viewer, Worker, SpecialZoomLevel } from '@react-pdf-viewer/core'
import { defaultLayoutPlugin } from '@react-pdf-viewer/default-layout'
import { highlightPlugin, Trigger } from '@react-pdf-viewer/highlight'
import type { RenderHighlightsProps, HighlightArea } from '@react-pdf-viewer/highlight'
import '@react-pdf-viewer/core/lib/styles/index.css'
import '@react-pdf-viewer/default-layout/lib/styles/index.css'
import type { HighlightChunk } from '../api/article'

// pdfjs-dist worker bundled locally
const WORKER_URL = new URL(
  'pdfjs-dist/build/pdf.worker.min.js',
  import.meta.url,
).toString()

interface Props {
  url: string
  highlights: HighlightChunk[]
  pageDimensions: Record<number, [number, number]>
  initialPage?: number
  onPageChange?: (page: number) => void
}

function chunksToAreas(
  highlights: HighlightChunk[],
  pageDimensions: Record<number, [number, number]>,
): HighlightArea[] {
  const areas: HighlightArea[] = []
  for (const chunk of highlights) {
    for (const pr of chunk.page_rects) {
      const dims = pageDimensions[pr.page]
      if (!dims) continue
      const [pdfW, pdfH] = dims
      for (const rect of pr.rects) {
        areas.push({
          pageIndex: pr.page,
          left: (rect.x0 / pdfW) * 100,
          top: (rect.y0 / pdfH) * 100,
          width: ((rect.x1 - rect.x0) / pdfW) * 100,
          height: ((rect.y1 - rect.y0) / pdfH) * 100,
        })
      }
    }
  }
  return areas
}

export function PdfViewer({ url, highlights, pageDimensions, initialPage = 0, onPageChange }: Props) {
  const pageNavRef = useRef<{ jumpToPage: (page: number) => void } | null>(null)

  const highlightAreas = chunksToAreas(highlights, pageDimensions)

  const renderHighlights = (props: RenderHighlightsProps) =>
    props.pageIndex >= 0 ? (
      <div>
        {highlightAreas
          .filter((area) => area.pageIndex === props.pageIndex)
          .map((area, idx) => (
            <div
              key={idx}
              style={{
                ...props.getCssProperties(area, props.rotation),
                backgroundColor: 'rgba(250, 204, 21, 0.45)',
                mixBlendMode: 'multiply',
              }}
            />
          ))}
      </div>
    ) : <></>

  const highlight = highlightPlugin({
    trigger: Trigger.None,
    renderHighlights,
  })

  const defaultLayout = defaultLayoutPlugin()

  useEffect(() => {
    if (initialPage > 0 && pageNavRef.current) {
      pageNavRef.current.jumpToPage(initialPage)
    }
  }, [initialPage])

  return (
    <Worker workerUrl={WORKER_URL}>
      <div style={{ height: '100%' }}>
        <Viewer
          fileUrl={url}
          plugins={[defaultLayout, highlight]}
          defaultScale={SpecialZoomLevel.PageWidth}
          initialPage={initialPage}
          onPageChange={(e) => onPageChange?.(e.currentPage)}
        />
      </div>
    </Worker>
  )
}
