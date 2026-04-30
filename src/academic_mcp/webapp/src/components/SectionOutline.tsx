import type { ArticleSection } from '../api/article'

interface Props {
  sections: ArticleSection[]
  onSelect: (charStart: number, id: string) => void
  activeChar?: number | null
}

export function SectionOutline({ sections, onSelect, activeChar }: Props) {
  if (sections.length === 0) return null

  return (
    <nav className="w-52 shrink-0 overflow-y-auto border-r text-xs leading-snug py-4 pr-2 pl-3 space-y-0.5">
      <p className="text-gray-400 font-medium uppercase tracking-wide mb-2 text-[10px]">Outline</p>
      {sections.map((sec) => {
        const isActive =
          activeChar != null &&
          activeChar >= sec.char_start &&
          activeChar < sec.char_end
        return (
          <button
            key={sec.char_start}
            onClick={() => onSelect(sec.char_start, `sec-${sec.char_start}`)}
            className={[
              'block w-full text-left truncate rounded px-1.5 py-0.5 transition-colors',
              sec.level === 1 ? 'font-semibold' : sec.level === 2 ? 'pl-3' : 'pl-5 text-gray-500',
              isActive
                ? 'bg-blue-50 text-blue-700'
                : 'hover:bg-gray-100 text-gray-700',
            ].join(' ')}
          >
            {sec.title}
          </button>
        )
      })}
    </nav>
  )
}
