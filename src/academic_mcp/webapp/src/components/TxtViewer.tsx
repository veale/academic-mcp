import { useMemo, useRef, useEffect } from 'react'
import type { ArticleSection, HighlightChunk } from '../api/article'

interface Props {
  text: string
  sections: ArticleSection[]
  highlights: HighlightChunk[]
  scrollToChar?: number | null
}

interface Segment {
  type: 'heading' | 'text'
  content: string
  charStart: number
  level?: number
  id?: string
}

function buildSegments(text: string, sections: ArticleSection[]): Segment[] {
  if (sections.length === 0) {
    return [{ type: 'text', content: text, charStart: 0 }]
  }

  const segs: Segment[] = []
  let pos = 0

  for (const sec of sections) {
    if (sec.char_start > pos) {
      segs.push({ type: 'text', content: text.slice(pos, sec.char_start), charStart: pos })
    }
    segs.push({
      type: 'heading',
      content: sec.title,
      charStart: sec.char_start,
      level: sec.level,
      id: `sec-${sec.char_start}`,
    })
    pos = sec.char_start + sec.title.length
  }

  if (pos < text.length) {
    segs.push({ type: 'text', content: text.slice(pos), charStart: pos })
  }

  return segs
}

// Mark highlight ranges in a string, returning React-compatible spans
function markText(
  content: string,
  globalOffset: number,
  highlights: HighlightChunk[],
): React.ReactNode[] {
  // Collect all (start, end) ranges that overlap this segment
  const local: Array<[number, number]> = []
  for (const chunk of highlights) {
    const s = chunk.char_start - globalOffset
    const e = chunk.char_end - globalOffset
    if (e > 0 && s < content.length) {
      local.push([Math.max(0, s), Math.min(content.length, e)])
    }
  }
  if (local.length === 0) return [content]

  // Sort and merge overlapping ranges
  local.sort((a, b) => a[0] - b[0])
  const merged: Array<[number, number]> = []
  for (const [s, e] of local) {
    if (merged.length && s <= merged[merged.length - 1][1]) {
      merged[merged.length - 1][1] = Math.max(merged[merged.length - 1][1], e)
    } else {
      merged.push([s, e])
    }
  }

  const nodes: React.ReactNode[] = []
  let cursor = 0
  for (const [s, e] of merged) {
    if (s > cursor) nodes.push(content.slice(cursor, s))
    nodes.push(
      <mark key={s} className="bg-yellow-200 rounded-sm">
        {content.slice(s, e)}
      </mark>,
    )
    cursor = e
  }
  if (cursor < content.length) nodes.push(content.slice(cursor))
  return nodes
}

export function TxtViewer({ text, sections, highlights, scrollToChar }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const segments = useMemo(() => buildSegments(text, sections), [text, sections])

  useEffect(() => {
    if (scrollToChar == null || !containerRef.current) return
    const target = containerRef.current.querySelector(`[data-char="${scrollToChar}"]`)
    if (target) {
      target.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }
  }, [scrollToChar])

  return (
    <div ref={containerRef} className="px-8 py-6 max-w-3xl mx-auto font-mono text-sm leading-relaxed whitespace-pre-wrap break-words">
      {segments.map((seg, i) => {
        if (seg.type === 'heading') {
          const Tag = (seg.level === 1 ? 'h1' : seg.level === 2 ? 'h2' : 'h3') as
            | 'h1'
            | 'h2'
            | 'h3'
          return (
            <Tag
              key={i}
              id={seg.id}
              data-char={seg.charStart}
              className={
                seg.level === 1
                  ? 'font-bold text-lg mt-8 mb-3'
                  : seg.level === 2
                    ? 'font-semibold text-base mt-6 mb-2'
                    : 'font-medium text-sm mt-4 mb-1'
              }
            >
              {seg.content}
            </Tag>
          )
        }
        return (
          <span key={i} data-char={seg.charStart}>
            {markText(seg.content, seg.charStart, highlights)}
          </span>
        )
      })}
    </div>
  )
}
