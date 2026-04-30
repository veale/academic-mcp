import { describe, it, expect } from 'vitest'
import {
  defaultViewer,
  resolveViewer,
  storeViewer,
  storedViewer,
  type ViewerAvailability,
} from '../lib/viewerSelection'

// ---------------------------------------------------------------------------
// defaultViewer — priority rule: pdf → html → txt
// ---------------------------------------------------------------------------

describe('defaultViewer', () => {
  it('returns pdf when pdf is available', () => {
    expect(defaultViewer({ pdf: true, html: true, text: true })).toBe('pdf')
  })

  it('returns html when pdf is unavailable', () => {
    expect(defaultViewer({ pdf: false, html: true, text: true })).toBe('html')
  })

  it('returns txt when only text is available', () => {
    expect(defaultViewer({ pdf: false, html: false, text: true })).toBe('txt')
  })

  it('returns txt when nothing is available', () => {
    // Degenerate case: all false → fall through to 'txt'
    expect(defaultViewer({ pdf: false, html: false, text: false })).toBe('txt')
  })
})

// ---------------------------------------------------------------------------
// resolveViewer — localStorage override wins if still available
// ---------------------------------------------------------------------------

const KEY = 'test-cache-key-abc'

describe('resolveViewer — no stored preference', () => {
  it('follows defaultViewer when nothing stored', () => {
    const viewers: ViewerAvailability = { pdf: true, html: true, text: true }
    expect(resolveViewer(viewers, KEY)).toBe('pdf')
  })
})

describe('resolveViewer — localStorage override', () => {
  it('honours stored viewer when still available', () => {
    storeViewer(KEY, 'html')
    const viewers: ViewerAvailability = { pdf: true, html: true, text: true }
    expect(resolveViewer(viewers, KEY)).toBe('html')
  })

  it('ignores stored viewer when no longer available', () => {
    storeViewer(KEY, 'pdf')
    const viewers: ViewerAvailability = { pdf: false, html: true, text: true }
    expect(resolveViewer(viewers, KEY)).toBe('html')
  })

  it('honours stored "txt" mapped to "text" availability flag', () => {
    storeViewer(KEY, 'txt')
    const viewers: ViewerAvailability = { pdf: false, html: false, text: true }
    expect(resolveViewer(viewers, KEY)).toBe('txt')
  })

  it('different cache keys are independent', () => {
    storeViewer(KEY, 'html')
    storeViewer('other-key', 'txt')
    const viewers: ViewerAvailability = { pdf: false, html: true, text: true }
    expect(resolveViewer(viewers, KEY)).toBe('html')
    expect(resolveViewer(viewers, 'other-key')).toBe('txt')
  })
})

describe('storedViewer', () => {
  it('returns null when nothing stored', () => {
    expect(storedViewer(KEY)).toBeNull()
  })

  it('round-trips through storeViewer', () => {
    storeViewer(KEY, 'pdf')
    expect(storedViewer(KEY)).toBe('pdf')
  })
})
