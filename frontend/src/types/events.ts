export interface EventBusEvent {
  type: string
  id: string
  action: 'created' | 'updated' | 'deleted'
  version: number
  org_id: string
}

export type ResourceType =
  | 'run' | 'pipeline' | 'agent' | 'schema'
  | 'connector' | 'model_backend' | 'team'
  | 'trigger' | 'eval' | 'feedback' | 'library'
