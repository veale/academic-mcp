import { useState, useEffect } from 'react'
import { Link, useNavigate } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'
import { searchPapers, type SearchResult, type SearchParams } from '../api/search'
import { logout } from '../api/auth'

function useDebounce<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return debounced
}

const DOMAIN_HINTS = ['general', 'medicine', 'biology', 'cs', 'physics', 'social']

export function SearchPage() {
  const navigate = useNavigate()
  const [q, setQ] = useState('')
  const [semantic, setSemantic] = useState(false)
  const [zoteroOnly, setZoteroOnly] = useState(false)
  const [includeScite, setIncludeScite] = useState(false)
  const [domainHint, setDomainHint] = useState('general')
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

  async function handleLogout() {
    await logout()
    localStorage.removeItem('wa_logged_in')
    await navigate({ to: '/login' })
  }

  return (
    <div className="max-w-3xl mx-auto px-4 py-8 space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Academic Search</h1>
        <button
          onClick={() => void handleLogout()}
          className="text-sm text-gray-500 hover:text-gray-700"
        >
          Sign out
        </button>
      </div>

      <input
        type="search"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Search papers…"
        className="w-full border rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        autoFocus
      />

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

      {isFetching && <p className="text-sm text-gray-400">Searching…</p>}
      {error && <p className="text-sm text-red-600">Search failed.</p>}

      {data && (
        <ul className="space-y-4">
          {data.results.length === 0 ? (
            <p className="text-sm text-gray-500">No results.</p>
          ) : (
            data.results.map((r, i) => (
              <ResultCard key={r.doi ?? r.s2_id ?? i} result={r} />
            ))
          )}
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
  const zoteroUrl = r.zotero_key ? `zotero://select/library/items/${r.zotero_key}` : null

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
    </li>
  )
}
