import { describe, it, expect } from 'vitest'
import { markRangesInRoot, scrollToFirstMark } from './domHighlight'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeRoot(innerHTML: string): HTMLDivElement {
  const div = document.createElement('div')
  div.innerHTML = innerHTML
  document.body.appendChild(div)
  return div
}

function cleanup(root: HTMLElement) {
  root.remove()
}

// ---------------------------------------------------------------------------
// markRangesInRoot
// ---------------------------------------------------------------------------

describe('markRangesInRoot', () => {
  it('wraps a single-node match in a <mark>', () => {
    const root = makeRoot('<p>Hello world</p>')
    markRangesInRoot(root, ['world'])
    const mark = root.querySelector('mark.search-highlight')
    expect(mark).not.toBeNull()
    expect(mark!.textContent).toBe('world')
    cleanup(root)
  })

  it('handles a match spanning two adjacent text nodes', () => {
    // Build: <p><em>Hello </em>world</p> — "Hello world" crosses the em boundary
    const root = document.createElement('div')
    const p = document.createElement('p')
    const em = document.createElement('em')
    em.textContent = 'Hello '
    p.appendChild(em)
    p.appendChild(document.createTextNode('world'))
    root.appendChild(p)
    document.body.appendChild(root)

    markRangesInRoot(root, ['Hello world'])

    const marks = root.querySelectorAll('mark.search-highlight')
    const markedText = [...marks].map(m => m.textContent).join('')
    expect(markedText).toBe('Hello world')
    cleanup(root)
  })

  it('merges overlapping / adjacent snippet ranges', () => {
    const root = makeRoot('<p>abcdefgh</p>')
    // "abcd" [0,4) and "cdef" [2,6) → merged [0,6) = "abcdef"
    markRangesInRoot(root, ['abcd', 'cdef'])
    const marks = root.querySelectorAll('mark.search-highlight')
    const text = [...marks].map(m => m.textContent).join('')
    expect(text).toBe('abcdef')
    cleanup(root)
  })

  it('is idempotent: calling twice produces the same marks', () => {
    const root = makeRoot('<p>foo bar baz</p>')
    markRangesInRoot(root, ['bar'])
    // Simulate re-render: reset innerHTML then call again
    root.innerHTML = '<p>foo bar baz</p>'
    markRangesInRoot(root, ['bar'])
    const marks = root.querySelectorAll('mark.search-highlight')
    expect(marks.length).toBe(1)
    expect(marks[0].textContent).toBe('bar')
    cleanup(root)
  })

  it('is a no-op when snippets is empty', () => {
    const root = makeRoot('<p>some text</p>')
    const before = root.innerHTML
    markRangesInRoot(root, [])
    expect(root.innerHTML).toBe(before)
    cleanup(root)
  })

  it('is a no-op when no snippet matches', () => {
    const root = makeRoot('<p>some text</p>')
    const before = root.innerHTML
    markRangesInRoot(root, ['zzz'])
    expect(root.innerHTML).toBe(before)
    cleanup(root)
  })
})

// ---------------------------------------------------------------------------
// scrollToFirstMark
// ---------------------------------------------------------------------------

describe('scrollToFirstMark', () => {
  it('does not throw when no marks exist', () => {
    const root = makeRoot('<p>no highlights here</p>')
    expect(() => scrollToFirstMark(root)).not.toThrow()
    cleanup(root)
  })
})
