export type ParameterPortType = 'string' | 'number' | 'boolean' | 'select' | 'model_backend_ref' | 'schema_ref'

export interface ParameterPort {
  id: string
  name: string
  label: string
  description?: string
  type: ParameterPortType
  required: boolean
  default?: unknown
  options?: { label: string; value: string }[]
  multiline?: boolean
}

export interface CompositeDefinition {
  id: string
  name: string
  description?: string
  version: string
  ports: ParameterPort[]
  created_at: string
  updated_at: string
}

export interface PipelineNodeCompositeData {
  compositeRef?: string
  compositeParameterValues?: Record<string, unknown>
  compositeInputMapping?: Record<string, string>
  compositeOutputMapping?: Record<string, string>
}
