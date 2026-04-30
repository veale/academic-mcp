import { apiFetch } from './client'

export interface IndexStatus {
  available?: boolean
  error?: string
  in_progress?: boolean
  count?: number
  indexed_count?: number
  pending?: number
  last_sync?: string
  started_at?: string
  provider?: string
  configured_provider?: string
  model?: string
  configured_model?: string
  cache_dir?: string
  mirror_last_sync_utc?: string
  mirror_age_seconds?: number
}

export function fetchIndexStatus(): Promise<IndexStatus> {
  return apiFetch('/health/status')
}
