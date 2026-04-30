/**
 * DOM-based text highlight marking using TreeWalker.
 *
 * markRangesInRoot() finds every occurrence of each snippet string in the
 * text content of `root`, then wraps the matching text nodes (or sub-ranges
 * of them) with <mark class="search-highlight"> elements.
 *
 * Strategy — "pre-split then wrap":
 *   1. Collect all text nodes in document order and record their cumulative
 *      offsets in the concatenated text.
 *   2. Find all snippet occurrences → collect a sorted, merged list of
 *      [start, end) character ranges.
 *   3. Pre-split any text node whose boundary falls inside a range, so that
 *      after splitting every text node either lies entirely inside a range or
 *      entirely outside one.
 *   4. Wrap the "inside" nodes by inserting a <mark> before them and
 *      re-parenting them into the mark.
 *
 * Processing split-points in descending order ensures that lower offsets
 * (not yet processed) remain valid after each splitText() call.
 */
export function markRangesInRoot(root: HTMLElement, snippets: string[]): void {
  const patterns = snippets
    .map(s => s.trim().replace(/\s+/g, ' '))
    .filter(Boolean)
  if (patterns.length === 0) return

  // --- Phase 1: collect text nodes ---
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  const nodes: Text[] = []
  let n: Node | null
  while ((n = walker.nextNode())) nodes.push(n as Text)
  if (nodes.length === 0) return

  const offsets: number[] = []
  let pos = 0
  for (const node of nodes) {
    offsets.push(pos)
    pos += (node.textContent ?? '').length
  }
  const full = nodes.map(nd => nd.textContent ?? '').join('')

  // --- Phase 2: find match ranges ---
  const rawRanges: Array<[number, number]> = []
  for (const pattern of patterns) {
    let p = 0
    while (p <= full.length - pattern.length) {
      const idx = full.indexOf(pattern, p)
      if (idx === -1) break
      rawRanges.push([idx, idx + pattern.length])
      p = idx + pattern.length
    }
  }
  if (rawRanges.length === 0) return

  // Sort and merge overlapping/adjacent ranges
  rawRanges.sort((a, b) => a[0] - b[0])
  const ranges: Array<[number, number]> = [rawRanges[0]]
  for (const [s, e] of rawRanges.slice(1)) {
    const last = ranges[ranges.length - 1]
    if (s <= last[1]) last[1] = Math.max(last[1], e)
    else ranges.push([s, e])
  }

  // --- Phase 3: pre-split text nodes at range boundaries ---
  const splitSet = new Set<number>()
  for (const [s, e] of ranges) {
    splitSet.add(s)
    splitSet.add(e)
  }
  // Process descending so earlier offsets stay valid
  const splitPoints = [...splitSet].sort((a, b) => b - a)

  for (const point of splitPoints) {
    const idx = nodes.findIndex((node, i) => {
      const nodeEnd = offsets[i] + (node.textContent ?? '').length
      return offsets[i] < point && point < nodeEnd
    })
    if (idx === -1) continue
    const node = nodes[idx]
    const relOffset = point - offsets[idx]
    const after = node.splitText(relOffset)
    nodes.splice(idx + 1, 0, after)
    offsets.splice(idx + 1, 0, point)
  }

  // --- Phase 4: wrap nodes that fall entirely within a highlight range ---
  for (let i = 0; i < nodes.length; i++) {
    const nodeStart = offsets[i]
    const nodeEnd = offsets[i] + (nodes[i].textContent ?? '').length
    if (nodeStart >= nodeEnd) continue
    const inRange = ranges.some(([s, e]) => nodeStart >= s && nodeEnd <= e)
    if (!inRange) continue
    const mark = document.createElement('mark')
    mark.className = 'search-highlight'
    nodes[i].parentNode?.insertBefore(mark, nodes[i])
    mark.appendChild(nodes[i])
  }
}

/** Scroll the first marked highlight into view. No-op if there are none. */
export function scrollToFirstMark(root: HTMLElement): void {
  const first = root.querySelector<HTMLElement>('mark.search-highlight')
  first?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}
