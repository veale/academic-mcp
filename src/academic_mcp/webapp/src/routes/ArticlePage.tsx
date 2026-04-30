import { Link, useSearch } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'
import { fetchArticleMeta } from '../api/article'

export function ArticlePage() {
  const { doi, zotero_key, url } = useSearch({ from: '/article' })
  const identifier = doi ?? zotero_key ?? url ?? ''

  const { data, isLoading, error } = useQuery({
    queryKey: ['article-meta', doi, zotero_key, url],
    queryFn: () => fetchArticleMeta({ doi, zotero_key, url }),
    enabled: !!identifier,
  })

  return (
    <div className="max-w-3xl mx-auto px-4 py-8 space-y-4">
      <Link to="/" className="text-sm text-blue-600 hover:underline">
        ← Back to search
      </Link>

      {!identifier && (
        <p className="text-sm text-red-600">No article identifier provided.</p>
      )}

      {isLoading && <p className="text-sm text-gray-400">Loading article…</p>}

      {error && (
        <p className="text-sm text-red-600">Failed to load article metadata.</p>
      )}

      {data && (
        <div className="space-y-3">
          <h1 className="text-xl font-semibold">
            {typeof data.metadata.title === 'string' ? data.metadata.title : identifier}
          </h1>

          <p className="text-sm text-gray-500">
            Source: {data.source} · {data.word_count.toLocaleString()} words ·{' '}
            {data.section_count} sections
          </p>

          <div className="flex gap-2 text-xs">
            {data.viewers.pdf && (
              <span className="bg-blue-100 text-blue-700 rounded px-2 py-0.5">PDF</span>
            )}
            {data.viewers.html && (
              <span className="bg-green-100 text-green-700 rounded px-2 py-0.5">HTML</span>
            )}
            {data.viewers.text && (
              <span className="bg-gray-100 text-gray-700 rounded px-2 py-0.5">Text</span>
            )}
          </div>

          {data.error && <p className="text-sm text-amber-600">Note: {data.error}</p>}

          <div className="border rounded p-4 bg-gray-50 text-sm text-gray-500">
            Full viewer coming in Phase 5.
            <br />
            Cache key: <code className="font-mono text-xs">{data.cache_key}</code>
          </div>
        </div>
      )}
    </div>
  )
}
