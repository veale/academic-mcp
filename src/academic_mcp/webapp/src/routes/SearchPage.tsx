import { useState, useEffect, useRef } from 'react'
import { Link, useNavigate } from '@tanstack/react-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { searchPapers, searchInCitations, type SearchResult, type SearchParams } from '../api/search'
import { logout } from '../api/auth'
import { fetchSavedSearches, saveSearch, deleteSavedSearch, type SavedSearch } from '../api/saved'
import { useToast } from '../components/Toast'
import { selectUrl as zoteroSelect } from '../lib/zoteroDeeplink'

function useDebounce<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return debounced
}

const DOMAIN_HINTS = ['general', 'medicine', 'biology', 'cs', 'physics', 'social']

function ResultSkeletons() {
  return (
    <ul className="space-y-4">
      {[1, 2, 3].map((i) => (
        <li key={i} className="border rounded-lg p-4 space-y-2 animate-pulse">
          <div className="h-4 bg-gray-200 rounded w-3/4" />
          <div className="h-3 bg-gray-100 rounded w-1/2" />
          <div className="h-3 bg-gray-100 rounded w-full" />
          <div className="h-3 bg-gray-100 rounded w-5/6" />
        </li>
      ))}
    </ul>
  )
}

export function SearchPage() {
  const navigate = useNavigate()
  const toast = useToast()
  const qc = useQueryClient()
  const [q, setQ] = useState('')
  const [semantic, setSemantic] = useState(false)
  const [zoteroOnly, setZoteroOnly] = useState(false)
  const [includeScite, setIncludeScite] = useState(false)
  const [domainHint, setDomainHint] = useState('general')
  const [savedOpen, setSavedOpen] = useState(false)
  const debouncedQ = useDebounce(q.trim(), 400)

  const params: SearchParams = {
    q: debouncedQ,
    limit: 10,
    zotero_only: zoteroOnly,
    semantic: semantic || undefined,
    include_scite: includeScite,
    domain_hint: domainHint,
  }

  const { data, isFetching, error } = useQuery({
    queryKey: ['search', params],
    queryFn: () => searchPapers(params),
    enabled: debouncedQ.length > 1,
  })

  const { data: savedSearches } = useQuery({
    queryKey: ['saved-searches'],
    queryFn: fetchSavedSearches,
  })

  const saveMut = useMutation({
    mutationFn: () => saveSearch(debouncedQ, params as unknown as Record<string, unknown>),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['saved-searches'] })
      toast.show('Search saved', 'success')
    },
    onError: () => toast.show('Failed to save search', 'error'),
  })

  const deleteMut = useMutation({
    mutationFn: (id: number) => deleteSavedSearch(id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['saved-searches'] }),
    onError: () => toast.show('Failed to delete saved search', 'error'),
  })

  async function handleLogout() {
    await logout()
    localStorage.removeItem('wa_logged_in')
    await navigate({ to: '/login' })
  }

  // Show error toast when search fails
  useEffect(() => {
    if (error) toast.show('Search failed. Please try again.', 'error')
  }, [error, toast])

  const alreadySaved =
    debouncedQ.length > 1 && savedSearches?.some((s) => s.query === debouncedQ)

  return (
    <div className="max-w-3xl mx-auto px-4 py-8 space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Academic Search</h1>
        <div className="flex items-center gap-3">
          <Link to="/health" className="text-sm text-gray-500 hover:text-gray-700">
            Index
          </Link>
          <button
            onClick={() => void handleLogout()}
            className="text-sm text-gray-500 hover:text-gray-700"
          >
            Sign out
          </button>
        </div>
      </div>

      <div className="flex gap-2">
        <input
          type="search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search papers…"
          className="flex-1 border rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          autoFocus
        />
        {debouncedQ.length > 1 && (
          <button
            onClick={() => saveMut.mutate()}
            disabled={saveMut.isPending || alreadySaved}
            title={alreadySaved ? 'Already saved' : 'Save this search'}
            className={[
              'border rounded-lg px-3 py-2.5 text-sm transition-colors',
              alreadySaved
                ? 'text-blue-600 border-blue-300 bg-blue-50'
                : 'hover:bg-gray-50 text-gray-600',
            ].join(' ')}
          >
            {alreadySaved ? '★' : '☆'}
          </button>
        )}
        {(savedSearches?.length ?? 0) > 0 && (
          <button
            onClick={() => setSavedOpen((v) => !v)}
            className={[
              'border rounded-lg px-3 py-2.5 text-sm transition-colors',
              savedOpen ? 'bg-gray-100 text-gray-800' : 'hover:bg-gray-50 text-gray-600',
            ].join(' ')}
            title="Saved searches"
          >
            Saved ({savedSearches!.length})
          </button>
        )}
      </div>

      {savedOpen && savedSearches && savedSearches.length > 0 && (
        <div className="border rounded-lg divide-y">
          {savedSearches.map((s: SavedSearch) => (
            <div key={s.id} className="flex items-center gap-2 px-3 py-2">
              <button
                onClick={() => {
                  setQ(s.query)
                  setSavedOpen(false)
                }}
                className="flex-1 text-left text-sm text-blue-600 hover:underline truncate"
              >
                {s.query}
              </button>
              <span className="text-xs text-gray-400 shrink-0">
                {s.created_at.slice(0, 10)}
              </span>
              <button
                onClick={() => deleteMut.mutate(s.id)}
                className="text-xs text-gray-400 hover:text-red-500 shrink-0"
                title="Delete"
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      )}

      <div className="flex flex-wrap gap-4 text-sm text-gray-700">
        <label className="flex items-center gap-1.5 cursor-pointer">
          <input
            type="checkbox"
            checked={zoteroOnly}
            onChange={(e) => setZoteroOnly(e.target.checked)}
          />
          Zotero only
        </label>
        <label className="flex items-center gap-1.5 cursor-pointer">
          <input
            type="checkbox"
            checked={semantic}
            onChange={(e) => setSemantic(e.target.checked)}
          />
          Semantic
        </label>
        <label className="flex items-center gap-1.5 cursor-pointer">
          <input
            type="checkbox"
            checked={includeScite}
            onChange={(e) => setIncludeScite(e.target.checked)}
          />
          Include scite
        </label>
        <label className="flex items-center gap-1.5">
          Domain:
          <select
            value={domainHint}
            onChange={(e) => setDomainHint(e.target.value)}
            className="border rounded px-1.5 py-0.5"
          >
            {DOMAIN_HINTS.map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
        </label>
      </div>

      {isFetching && !data && <ResultSkeletons />}

      {isFetching && data && (
        <p className="text-xs text-gray-400">Updating…</p>
      )}

      {!isFetching && !error && data && data.results.length === 0 && (
        <div className="py-12 text-center text-sm text-gray-400">
          No results for <span className="font-medium">"{debouncedQ}"</span>. Try different keywords.
        </div>
      )}

      {!isFetching && !data && debouncedQ.length > 1 && !error && (
        <div className="py-12 text-center text-sm text-gray-400 animate-pulse">
          Searching…
        </div>
      )}

      {data && data.results.length > 0 && (
        <ul className="space-y-4">
          {data.results.map((r, i) => (
            <ResultCard key={r.doi ?? r.s2_id ?? i} result={r} />
          ))}
        </ul>
      )}
    </div>
  )
}

function ResultCard({ result: r }: { result: SearchResult }) {
  const articleSearch = {
    doi: r.doi ?? undefined,
    zotero_key: r.zotero_key ?? undefined,
    url: r.url ?? undefined,
  }
  const hasIdentifier = !!(r.doi ?? r.zotero_key ?? r.url)
  const zoteroUrl = r.zotero_key ? zoteroSelect(r.zotero_key) : null
  const [citSearch, setCitSearch] = useState<{ open: boolean; direction: 'in' | 'out' }>({
    open: false,
    direction: 'out',
  })

  return (
    <li className="border rounded-lg p-4 space-y-1.5">
      <div className="flex items-start justify-between gap-2">
        <p className="font-medium text-sm leading-snug">{r.title || '(no title)'}</p>
        {r.score != null && (
          <span className="text-xs text-gray-400 shrink-0">{r.score.toFixed(2)}</span>
        )}
      </div>

      <p className="text-xs text-gray-500">
        {r.authors.slice(0, 3).join(', ')}
        {r.authors.length > 3 ? ' et al.' : ''}
        {r.year ? ` · ${r.year}` : ''}
        {r.venue ? ` · ${r.venue}` : ''}
      </p>

      {r.abstract && (
        <p className="text-xs text-gray-600 line-clamp-3">{r.abstract}</p>
      )}

      <div className="flex flex-wrap items-center gap-2 pt-1">
        {hasIdentifier && (
          <Link
            to="/article"
            search={articleSearch}
            className="text-xs bg-blue-600 text-white rounded px-2.5 py-1 hover:bg-blue-700"
          >
            Open
          </Link>
        )}
        {zoteroUrl && (
          <a
            href={zoteroUrl}
            className="text-xs border rounded px-2.5 py-1 hover:bg-gray-50 text-gray-700"
          >
            Open in Zotero
          </a>
        )}
        {r.doi && (
          <a
            href={`https://doi.org/${r.doi}`}
            target="_blank"
            rel="noreferrer"
            className="text-xs text-blue-500 hover:underline"
          >
            DOI ↗
          </a>
        )}
        {r.doi && (
          <button
            onClick={() =>
              setCitSearch((s) =>
                s.open && s.direction === 'out'
                  ? { open: false, direction: 'out' }
                  : { open: true, direction: 'out' },
              )
            }
            className={[
              'text-xs border rounded px-2.5 py-1 transition-colors',
              citSearch.open && citSearch.direction === 'out'
                ? 'bg-amber-50 border-amber-400 text-amber-700'
                : 'hover:bg-gray-50 text-gray-600',
            ].join(' ')}
          >
            Search its references
          </button>
        )}
        {r.doi && (
          <button
            onClick={() =>
              setCitSearch((s) =>
                s.open && s.direction === 'in'
                  ? { open: false, direction: 'in' }
                  : { open: true, direction: 'in' },
              )
            }
            className={[
              'text-xs border rounded px-2.5 py-1 transition-colors',
              citSearch.open && citSearch.direction === 'in'
                ? 'bg-amber-50 border-amber-400 text-amber-700'
                : 'hover:bg-gray-50 text-gray-600',
            ].join(' ')}
          >
            Search cited by
          </button>
        )}
        <div className="flex gap-1.5 ml-auto">
          {r.in_zotero && (
            <span className="text-xs bg-green-100 text-green-700 rounded px-2 py-0.5">
              Zotero
            </span>
          )}
          {r.has_oa_pdf && (
            <span className="text-xs bg-blue-100 text-blue-700 rounded px-2 py-0.5">
              OA PDF
            </span>
          )}
          {r.found_in.map((s) => (
            <span key={s} className="text-xs bg-gray-100 text-gray-600 rounded px-2 py-0.5">
              {s}
            </span>
          ))}
        </div>
      </div>

      {citSearch.open && r.doi && (
        <CitationSearchPanel
          doi={r.doi}
          direction={citSearch.direction}
          onClose={() => setCitSearch((s) => ({ ...s, open: false }))}
        />
      )}
    </li>
  )
}

function CitationSearchPanel({
  doi,
  direction,
  onClose,
}: {
  doi: string
  direction: 'in' | 'out'
  onClose: () => void
}) {
  const [q, setQ] = useState('')
  const [submitted, setSubmitted] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  const { data, isFetching, error } = useQuery({
    queryKey: ['cit-search', doi, direction, submitted],
    queryFn: () => searchInCitations(doi, submitted, direction),
    enabled: submitted.length > 1,
  })

  return (
    <div className="mt-3 border-t pt-3 space-y-2">
      <div className="flex items-center gap-2">
        <span className="text-xs font-medium text-gray-500">
          {direction === 'out' ? 'Search inside references' : 'Search papers that cite this'}
        </span>
        <button onClick={onClose} className="ml-auto text-xs text-gray-400 hover:text-gray-600">
          ✕ close
        </button>
      </div>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          setSubmitted(q.trim())
        }}
        className="flex gap-2"
      >
        <input
          ref={inputRef}
          type="search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Query within this citation set…"
          className="flex-1 border rounded px-3 py-1.5 text-xs focus:outline-none focus:ring-2 focus:ring-amber-400"
        />
        <button
          type="submit"
          className="text-xs bg-amber-500 text-white rounded px-3 py-1.5 hover:bg-amber-600 disabled:opacity-50"
          disabled={q.trim().length < 2}
        >
          Search
        </button>
      </form>

      {isFetching && <p className="text-xs text-gray-400">Searching…</p>}
      {error && <p className="text-xs text-red-600">Search failed.</p>}

      {data && (
        <ul className="space-y-2 max-h-96 overflow-y-auto pr-1">
          {data.results.length === 0 ? (
            <p className="text-xs text-gray-500">No results in this citation set.</p>
          ) : (
            data.results.map((r, i) => (
              <CitationResultRow key={r.doi ?? i} result={r} />
            ))
          )}
        </ul>
      )}
    </div>
  )
}

function CitationResultRow({ result: r }: { result: SearchResult }) {
  const articleSearch = {
    doi: r.doi ?? undefined,
    zotero_key: r.zotero_key ?? undefined,
    url: r.url ?? undefined,
  }
  const hasIdentifier = !!(r.doi ?? r.zotero_key ?? r.url)

  return (
    <li className="border rounded p-2.5 space-y-0.5 bg-amber-50/40">
      <div className="flex items-start justify-between gap-2">
        <p className="text-xs font-medium leading-snug">{r.title || '(no title)'}</p>
        {r.score != null && r.score > 0 && (
          <span className="text-[10px] text-gray-400 shrink-0">{r.score.toFixed(1)}</span>
        )}
      </div>
      <p className="text-[11px] text-gray-500">
        {r.authors.slice(0, 2).join(', ')}
        {r.authors.length > 2 ? ' et al.' : ''}
        {r.year ? ` · ${r.year}` : ''}
        {r.venue ? ` · ${r.venue}` : ''}
      </p>
      {r.abstract && (
        <p className="text-[11px] text-gray-600 line-clamp-2">{r.abstract}</p>
      )}
      <div className="flex items-center gap-2 pt-0.5">
        {hasIdentifier && (
          <Link
            to="/article"
            search={articleSearch}
            className="text-[11px] bg-blue-600 text-white rounded px-2 py-0.5 hover:bg-blue-700"
          >
            Open
          </Link>
        )}
        {r.doi && (
          <a
            href={`https://doi.org/${r.doi}`}
            target="_blank"
            rel="noreferrer"
            className="text-[11px] text-blue-500 hover:underline"
          >
            DOI ↗
          </a>
        )}
        {r.in_zotero && (
          <span className="text-[10px] bg-green-100 text-green-700 rounded px-1.5 py-0.5 ml-auto">
            Zotero
          </span>
        )}
      </div>
    </li>
  )
}
