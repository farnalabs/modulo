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
