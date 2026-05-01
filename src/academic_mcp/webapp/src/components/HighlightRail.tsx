import type { HighlightChunk } from '../api/article'

interface Props {
  chunks: HighlightChunk[]
  totalChars: number
  totalPages?: number
  viewerType: 'pdf' | 'html' | 'txt'
  onJump: (chunk: HighlightChunk) => void
}

// Each bar's vertical span reflects the chunk's actual length, and its colour
// intensity reflects the chunk's score relative to the strongest match. The
// result is a true heatmap: the eye is drawn to the densest, highest-scoring
// regions of the article.
export function HighlightRail({ chunks, totalChars, totalPages, viewerType, onJump }: Props) {
  if (chunks.length === 0) return null

  const scores = chunks.map((c) => c.score)
  const minScore = Math.min(...scores)
  const maxScore = Math.max(...scores)
  const scoreRange = Math.max(1e-6, maxScore - minScore)

  const getSpan = (chunk: HighlightChunk): { top: number; height: number } => {
    if (viewerType === 'pdf' && totalPages && chunk.page_rects.length > 0) {
      const firstPage = Math.min(...chunk.page_rects.map((p) => p.page))
      const lastPage = Math.max(...chunk.page_rects.map((p) => p.page))
      return {
        top: (firstPage / totalPages) * 100,
        height: Math.max(0.6, ((lastPage - firstPage + 1) / totalPages) * 100),
      }
    }
    if (totalChars > 0) {
      const top = (chunk.char_start / totalChars) * 100
      const span = ((chunk.char_end - chunk.char_start) / totalChars) * 100
      return { top, height: Math.max(0.6, span) }
    }
    return { top: 0, height: 0.6 }
  }

  const isSemantic = chunks.some((c) => c.match_type === 'semantic')

  return (
    <div className="w-3 shrink-0 relative border-l bg-gray-50" title="Relevance heatmap">
      {chunks.map((chunk, i) => {
        const { top, height } = getSpan(chunk)
        const norm = (chunk.score - minScore) / scoreRange
        const opacity = 0.25 + 0.75 * norm
        // Semantic = warm yellow→red gradient; lexical = blue
        const baseColor = chunk.match_type === 'semantic'
          ? `rgba(${230 + Math.round(25 * (1 - norm))}, ${180 - Math.round(120 * norm)}, 30, ${opacity})`
          : `rgba(96, 165, 250, ${opacity})`
        return (
          <button
            key={i}
            onClick={() => onJump(chunk)}
            title={`${chunk.match_type} ${(chunk.score * 100).toFixed(0)}% — ${chunk.snippet.slice(0, 100)}`}
            style={{
              top: `${top}%`,
              height: `${height}%`,
              backgroundColor: baseColor,
            }}
            className="absolute left-0.5 right-0.5 rounded-sm hover:brightness-110 transition-all"
          />
        )
      })}
      {/* Tiny legend dot bottom-left */}
      <div
        className="absolute bottom-1 left-0 right-0 text-[8px] text-center text-gray-400 pointer-events-none"
        title={isSemantic ? 'semantic match' : 'lexical match'}
      >
        {isSemantic ? '∼' : 'A'}
      </div>
    </div>
  )
}
