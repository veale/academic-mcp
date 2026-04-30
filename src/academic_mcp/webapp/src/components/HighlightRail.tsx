import type { HighlightChunk } from '../api/article'

interface Props {
  chunks: HighlightChunk[]
  totalChars: number
  totalPages?: number
  viewerType: 'pdf' | 'html' | 'txt'
  onJump: (chunk: HighlightChunk) => void
}

export function HighlightRail({ chunks, totalChars, totalPages, viewerType, onJump }: Props) {
  if (chunks.length === 0) return null

  const getPosition = (chunk: HighlightChunk): number => {
    if (viewerType === 'pdf' && totalPages && chunk.page_rects.length > 0) {
      return chunk.page_rects[0].page / totalPages
    }
    if (totalChars > 0) {
      return chunk.char_start / totalChars
    }
    return 0
  }

  return (
    <div className="w-3 shrink-0 relative border-l bg-gray-50" title="Highlight positions">
      {chunks.map((chunk, i) => {
        const pos = getPosition(chunk)
        return (
          <button
            key={i}
            onClick={() => onJump(chunk)}
            title={chunk.snippet.slice(0, 80)}
            style={{ top: `${pos * 100}%` }}
            className="absolute left-0.5 right-0.5 h-1.5 rounded-sm bg-yellow-400 hover:bg-yellow-500 transition-colors"
          />
        )
      })}
    </div>
  )
}
