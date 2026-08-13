export type StageType = 'modulo' | 'external' | 'manual' | 'placeholder'
export type TriggerType = 'pipeline_completed' | 'webhook' | 'cron' | 'manual' | 'external'
export type EstimatedFrequency = 'daily' | 'per-pr' | 'hourly' | 'custom'

export interface LifecycleStage {
  id: string
  name: string
  description: string
  stage_type: StageType
  pipeline_id: string | null
  external_url: string | null
  owner: string | null
  graduated: boolean
}

export interface LifecycleEdge {
  id: string
  source_stage_id: string
  target_stage_id: string
  trigger_type: TriggerType
  description: string
  condition_expression: string | null
  trigger_link: string | null
  estimated_frequency: EstimatedFrequency | null
}

export interface LifecycleMapVersion {
  id: string
  lifecycle_map_id: string
  version_number: number
  stages: LifecycleStage[]
  edges: LifecycleEdge[]
  created_by: string
  created_at: string
  notes: string
}

export interface LifecycleMap {
  id: string
  name: string
  description: string
  organisation_id: string
  current_version_id: string | null
  created_by: string
  created_at: string
  updated_at: string
}

export interface PipelineSummary {
  id: string
  name: string
  visibility: string
  created_at: string
}

export interface JourneyCurrentStage {
  map_id: string
  version: number | null
  stage_id: string
  stage_name: string | null
  position: number | null
}

export interface JourneySummary {
  kind: string
  ref: string
  canonical_work_item_id: string
  current_stage: JourneyCurrentStage | null
  status: string | null
  provenance: string | null
  run_count: number
  unattributed?: boolean
  latest_run_id: string | null
  updated_at: string
}

export interface JourneyRunHistoryItem {
  run_id: string
  status: string | null
  completed_at: string | null
  provenance: string | null
}

export interface JourneyDetail extends JourneySummary {
  runs: JourneyRunHistoryItem[]
}

export interface JourneyListResponse {
  items: JourneySummary[]
  next_cursor: string | null
}

export interface LifecycleMapTransfer {
  primitive_type: 'lifecycle_map'
  format_version: string
  name: string
  description: string | null
  content_json: Record<string, unknown>
}
