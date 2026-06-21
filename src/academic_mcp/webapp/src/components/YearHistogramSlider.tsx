import { useId } from 'react'

/**
 * Year range filter with a live, per-year bar chart of the current results
 * sitting directly above a dual-thumb slider. The x-axis scales dynamically
 * from the earliest year present in the results up to the present day, so the
 * histogram reflects what the query actually returned.
 *
 * `from`/`to` are the current selection (undefined = open-ended on that side);
 * a value equal to a bound is reported back as undefined (no filter).
 */
export function YearHistogramSlider({
  years,
  from,
  to,
  fallbackMin,
  max,
  onChange,
}: {
  years: number[]
  from?: number
  to?: number
  fallbackMin: number
  max: number
  onChange: (from: number | undefined, to: number | undefined) => void
}) {
  const id = useId()
  const valid = years.filter((y) => Number.isFinite(y) && y <= max)
  const baseMin = valid.length ? Math.min(...valid) : fallbackMin
  // The track must always be able to show the current selection.
  const min = Math.min(baseMin, from ?? baseMin)
  const hiMax = Math.max(max, to ?? max)

  const counts = new Map<number, number>()
  for (const y of valid) counts.set(y, (counts.get(y) ?? 0) + 1)
  const maxCount = Math.max(1, ...counts.values())

  const lo = from ?? min
  const hi = to ?? hiMax
  const clampLo = (v: number) => Math.min(v, hi)
  const clampHi = (v: number) => Math.max(v, lo)
  const norm = (v: number, side: 'from' | 'to') =>
    side === 'from' ? (v <= min ? undefined : v) : v >= hiMax ? undefined : v
  const pct = (v: number) => (hiMax === min ? 0 : ((v - min) / (hiMax - min)) * 100)

  const bars: number[] = []
  for (let y = min; y <= hiMax; y++) bars.push(y)

  return (
    <div className="text-sm text-gray-700">
      {/* Histogram */}
      <div className="flex items-end gap-px h-12 mb-1">
        {bars.map((y) => {
          const c = counts.get(y) ?? 0
          const inRange = y >= lo && y <= hi
          return (
            <div
              key={y}
              title={`${y}: ${c} result${c === 1 ? '' : 's'}`}
              className="flex-1 min-w-px rounded-t-sm transition-colors"
              style={{
                height: `${c ? Math.max(8, (c / maxCount) * 100) : 0}%`,
                backgroundColor: c
                  ? inRange
                    ? '#3b82f6' /* blue-500 */
                    : '#cbd5e1' /* slate-300 */
                  : 'transparent',
              }}
            />
          )
        })}
      </div>

      {/* Slider */}
      <div className="flex items-center gap-3">
        <span className="tabular-nums w-10 text-right shrink-0">{lo}</span>
        <div className="relative flex-1 h-5">
          <div className="absolute top-1/2 -translate-y-1/2 h-1 w-full rounded bg-gray-200" />
          <div
            className="absolute top-1/2 -translate-y-1/2 h-1 rounded bg-blue-500"
            style={{ left: `${pct(lo)}%`, right: `${100 - pct(hi)}%` }}
          />
          <input
            aria-label="Earliest year"
            id={`${id}-from`}
            type="range"
            min={min}
            max={hiMax}
            value={lo}
            onChange={(e) => onChange(norm(clampLo(+e.target.value), 'from'), norm(hi, 'to'))}
            className="year-thumb absolute w-full top-0 appearance-none bg-transparent pointer-events-none"
          />
          <input
            aria-label="Latest year"
            id={`${id}-to`}
            type="range"
            min={min}
            max={hiMax}
            value={hi}
            onChange={(e) => onChange(norm(lo, 'from'), norm(clampHi(+e.target.value), 'to'))}
            className="year-thumb absolute w-full top-0 appearance-none bg-transparent pointer-events-none"
          />
        </div>
        <span className="tabular-nums w-10 shrink-0">{hi}</span>
      </div>
    </div>
  )
}
