import { useId } from 'react'

/**
 * Dual-thumb year range slider built from two overlaid <input type="range">.
 * `min`/`max` are the slider bounds; `from`/`to` are the current selection
 * (undefined = open-ended on that side). Emits the new [from, to] on change,
 * where a value equal to the bound is reported as undefined (no filter).
 */
export function YearRangeSlider({
  min,
  max,
  from,
  to,
  onChange,
}: {
  min: number
  max: number
  from?: number
  to?: number
  onChange: (from: number | undefined, to: number | undefined) => void
}) {
  const id = useId()
  const lo = from ?? min
  const hi = to ?? max
  // Keep the thumbs from crossing.
  const clampLo = (v: number) => Math.min(v, hi)
  const clampHi = (v: number) => Math.max(v, lo)
  const norm = (v: number, side: 'from' | 'to') =>
    side === 'from' ? (v <= min ? undefined : v) : v >= max ? undefined : v

  const pct = (v: number) => ((v - min) / (max - min)) * 100

  return (
    <div className="flex items-center gap-3 text-sm text-gray-700">
      <span className="shrink-0">Years:</span>
      <span className="tabular-nums w-10 text-right shrink-0">{lo}</span>
      <div className="relative flex-1 h-5 min-w-[140px]">
        {/* track */}
        <div className="absolute top-1/2 -translate-y-1/2 h-1 w-full rounded bg-gray-200" />
        {/* selected range */}
        <div
          className="absolute top-1/2 -translate-y-1/2 h-1 rounded bg-blue-500"
          style={{ left: `${pct(lo)}%`, right: `${100 - pct(hi)}%` }}
        />
        <input
          aria-label="Earliest year"
          id={`${id}-from`}
          type="range"
          min={min}
          max={max}
          value={lo}
          onChange={(e) => onChange(norm(clampLo(+e.target.value), 'from'), norm(hi, 'to'))}
          className="year-thumb absolute w-full top-0 appearance-none bg-transparent pointer-events-none"
        />
        <input
          aria-label="Latest year"
          id={`${id}-to`}
          type="range"
          min={min}
          max={max}
          value={hi}
          onChange={(e) => onChange(norm(lo, 'from'), norm(clampHi(+e.target.value), 'to'))}
          className="year-thumb absolute w-full top-0 appearance-none bg-transparent pointer-events-none"
        />
      </div>
      <span className="tabular-nums w-10 shrink-0">{hi}</span>
    </div>
  )
}
