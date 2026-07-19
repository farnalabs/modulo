export type ParameterPortType = 'string' | 'number' | 'boolean' | 'select' | 'model_backend_ref' | 'schema_ref'

export interface TargetInjection {
  mode: 'prompt_replace' | 'header_replace' | 'body_append' | 'system_prompt'
  node_id: string
  injection_point: 'prompt_template' | 'before_output' | 'system_prompt'
}

export interface ParameterPort {
  id: string
  name: string
  label: string
  description?: string | null
  type: ParameterPortType
  required: boolean
  default_value?: unknown
  options?: { label: string; value: string }[] | null
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
  sub_pipeline_graph_json: Record<string, unknown>
  parameter_ports_json: Record<string, unknown>[]
  input_schema_id: string | null
  output_schema_id: string | null
  organisation_id: string
  created_by: string
  created_at: string
  updated_at: string
}


