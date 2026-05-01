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

// Map a normalised [0..1] score to a warm-yellow→deep-orange shade for
// semantic chunks, or a cool blue for literal lexical matches.  The intensity
// makes the body text itself act as a heatmap.
function scoreColor(scoreNorm: number, matchType: 'semantic' | 'lexical'): string {
  const opacity = 0.25 + 0.6 * scoreNorm
  if (matchType === 'semantic') {
    const r = 250
    const g = Math.round(220 - 130 * scoreNorm)
    const b = 60
    return `rgba(${r}, ${g}, ${b}, ${opacity})`
  }
  return `rgba(96, 165, 250, ${opacity})`
}

interface LocalRange {
  s: number
  e: number
  score: number
  matchType: 'semantic' | 'lexical'
}

// Mark highlight ranges in a string, returning React-compatible spans
function markText(
  content: string,
  globalOffset: number,
  highlights: HighlightChunk[],
  scoreMin: number,
  scoreRange: number,
): React.ReactNode[] {
  // Collect ranges that overlap this segment, carrying score + match_type.
  const local: LocalRange[] = []
  for (const chunk of highlights) {
    const s = chunk.char_start - globalOffset
    const e = chunk.char_end - globalOffset
    if (e > 0 && s < content.length) {
      local.push({
        s: Math.max(0, s),
        e: Math.min(content.length, e),
        score: chunk.score,
        matchType: chunk.match_type,
      })
    }
  }
  if (local.length === 0) return [content]

  // Sort and merge overlapping ranges, taking the max-score contributor for colour.
  local.sort((a, b) => a.s - b.s)
  const merged: LocalRange[] = []
  for (const r of local) {
    const last = merged[merged.length - 1]
    if (last && r.s <= last.e) {
      last.e = Math.max(last.e, r.e)
      if (r.score > last.score) {
        last.score = r.score
        last.matchType = r.matchType
      }
    } else {
      merged.push({ ...r })
    }
  }

  const nodes: React.ReactNode[] = []
  let cursor = 0
  for (const r of merged) {
    if (r.s > cursor) nodes.push(content.slice(cursor, r.s))
    const norm = (r.score - scoreMin) / scoreRange
    nodes.push(
      <mark
        key={r.s}
        style={{ backgroundColor: scoreColor(norm, r.matchType) }}
        className="rounded-sm"
      >
        {content.slice(r.s, r.e)}
      </mark>,
    )
    cursor = r.e
  }
  if (cursor < content.length) nodes.push(content.slice(cursor))
  return nodes
}

export function TxtViewer({ text, sections, highlights, scrollToChar }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const segments = useMemo(() => buildSegments(text, sections), [text, sections])

  // Normalise scores once per highlight set so each mark's colour reflects
  // its rank within this article (not raw cosine similarity, which compresses
  // into a tiny visible range).
  const { scoreMin, scoreRange } = useMemo(() => {
    if (highlights.length === 0) return { scoreMin: 0, scoreRange: 1 }
    const scores = highlights.map((h) => h.score)
    const lo = Math.min(...scores)
    const hi = Math.max(...scores)
    return { scoreMin: lo, scoreRange: Math.max(1e-6, hi - lo) }
  }, [highlights])

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
            {markText(seg.content, seg.charStart, highlights, scoreMin, scoreRange)}
          </span>
        )
      })}
    </div>
  )
}
