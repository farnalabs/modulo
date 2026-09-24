export interface EventBusEvent {
  type: ResourceType
  id: string
  action: 'created' | 'updated' | 'deleted'
  version: number
  org_id: string
  timestamp?: string
  /**
   * FAR-250: notification events carry ONLY these three fields on top of the
   * envelope — never title/body/content (content is fetched through the
   * RLS- and preference-filtered REST API at read time).
   */
  notification_id?: string
  category?: string
  created_at?: string | null
  /** Deterministic id used by the relay's reconnect-backfill dedupe. */
  event_id?: string
}

export type ResourceType =
  | 'run' | 'pipeline' | 'agent' | 'schema'
  | 'connector' | 'model_backend' | 'team'
  | 'trigger' | 'eval' | 'feedback' | 'library'
  | 'license' | 'plan' | 'notification'
