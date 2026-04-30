import { Link } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'
import { fetchIndexStatus, type IndexStatus } from '../api/health'

function Row({ label, value }: { label: string; value: string | number | boolean | undefined }) {
  if (value === undefined || value === null) return null
  return (
    <tr className="border-b last:border-b-0">
      <td className="py-2 pr-6 text-sm text-gray-500 whitespace-nowrap">{label}</td>
      <td className="py-2 text-sm font-mono">{String(value)}</td>
    </tr>
  )
}

function StatusBadge({ status }: { status: IndexStatus }) {
  if (status.available === false) {
    return (
      <span className="inline-block bg-red-100 text-red-700 text-xs rounded-full px-3 py-1">
        Unavailable
      </span>
    )
  }
  if (status.in_progress) {
    const done = status.count ?? 0
    const pending = status.pending ?? 0
    const total = done + pending
    const pct = total > 0 ? Math.round((100 * done) / total) : 0
    return (
      <span className="inline-block bg-yellow-100 text-yellow-700 text-xs rounded-full px-3 py-1">
        Building — {pct}% ({done.toLocaleString()} / {total.toLocaleString()})
      </span>
    )
  }
  return (
    <span className="inline-block bg-green-100 text-green-700 text-xs rounded-full px-3 py-1">
      Ready
    </span>
  )
}

export function HealthPage() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['health-status'],
    queryFn: fetchIndexStatus,
    refetchInterval: (query) => (query.state.data?.in_progress ? 5000 : false),
  })

  return (
    <div className="max-w-2xl mx-auto px-4 py-8 space-y-6">
      <div className="flex items-center gap-4">
        <Link to="/" className="text-sm text-blue-600 hover:underline">
          ← Search
        </Link>
        <h1 className="text-xl font-semibold">Semantic Index Status</h1>
        <button
          onClick={() => void refetch()}
          className="ml-auto text-xs border rounded px-3 py-1.5 hover:bg-gray-50"
        >
          Refresh
        </button>
      </div>

      {isLoading && (
        <div className="space-y-2">
          {[1, 2, 3].map((i) => (
            <div key={i} className="h-8 bg-gray-100 rounded animate-pulse" />
          ))}
        </div>
      )}

      {error && (
        <p className="text-sm text-red-600">Failed to load status.</p>
      )}

      {data && (
        <div className="space-y-4">
          <div className="flex items-center gap-3">
            <StatusBadge status={data} />
            {data.indexed_count != null && (
              <span className="text-sm text-gray-600">
                {data.indexed_count.toLocaleString()} chunks indexed
                {data.pending ? `, ${data.pending.toLocaleString()} pending` : ''}
              </span>
            )}
          </div>

          {data.error && (
            <p className="text-sm text-red-600">{data.error}</p>
          )}

          <table className="w-full">
            <tbody>
              <Row label="Provider" value={data.configured_provider ?? data.provider} />
              <Row label="Model" value={data.configured_model ?? data.model} />
              <Row label="Last sync" value={data.last_sync} />
              <Row label="Build started" value={data.started_at} />
              <Row label="Cache dir" value={data.cache_dir} />
              <Row label="Mirror last sync" value={data.mirror_last_sync_utc} />
              {data.mirror_age_seconds != null && (
                <Row
                  label="Mirror age"
                  value={`${Math.round(data.mirror_age_seconds / 60)} min`}
                />
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
