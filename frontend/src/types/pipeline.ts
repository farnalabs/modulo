export type ParameterPortType = 'string' | 'number' | 'boolean' | 'select' | 'model_backend_ref' | 'schema_ref'

export interface TargetInjection {
  mode: string
  node_id: string
  injection_point: string
}

export interface ParameterPort {
  id: string
  name: string
  label: string
  description?: string | null
  type: ParameterPortType
  required: boolean
  default: unknown | null
  options: { label: string; value: string }[] | null
  multiline: boolean
  target_injection: TargetInjection
}

export interface SchemaField {
  name: string
  type: 'string' | 'number' | 'integer' | 'boolean' | 'array' | 'object' | 'null'
  description: string | null
  required: boolean
}

export interface CompositeDefinition {
  id: string
  name: string
  description: string | null
  version: string
  parameter_ports_json: Record<string, unknown>[]
  input_schema_id: string | null
  output_schema_id: string | null
  organisation_id: string
  created_by: string
  created_at: string
  updated_at: string
}

export interface PipelineNodeCompositeData {
  compositeRef?: string
  compositeParameterValues?: Record<string, unknown>
  compositeInputMapping?: Record<string, string>
  compositeOutputMapping?: Record<string, string>
}
