/**
 * Vitest setup: provide a real in-memory localStorage mock for jsdom.
 *
 * The jsdom build bundled with this version of Vitest ships a non-functional
 * localStorage stub.  This replaces it with a plain Map-backed implementation
 * that supports getItem / setItem / removeItem / clear.
 */

import { beforeEach, vi } from 'vitest'

function makeMockStorage() {
  const store = new Map<string, string>()
  return {
    get length() {
      return store.size
    },
    key(index: number): string | null {
      return [...store.keys()][index] ?? null
    },
    getItem(key: string): string | null {
      return store.get(key) ?? null
    },
    setItem(key: string, value: string): void {
      store.set(key, String(value))
    },
    removeItem(key: string): void {
      store.delete(key)
    },
    clear(): void {
      store.clear()
    },
  }
}

const mockStorage = makeMockStorage()

vi.stubGlobal('localStorage', mockStorage)

// Reset storage between tests so they are fully isolated.
beforeEach(() => mockStorage.clear())
