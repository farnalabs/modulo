export type ParameterPortType = 'string' | 'number' | 'boolean' | 'select' | 'model_backend_ref' | 'schema_ref'

export interface ParameterPort {
  id: string
  name: string
  label: string
  description: string | null
  type: ParameterPortType
  required: boolean
  default: unknown | null
  options: { label: string; value: string }[] | null
  multiline: boolean | null
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
  ports: ParameterPort[]
  input_schema_id: string | null
  output_schema_id: string | null
  created_at: string
  updated_at: string
}

export interface PipelineNodeCompositeData {
  compositeRef?: string
  compositeParameterValues?: Record<string, unknown>
  compositeInputMapping?: Record<string, string>
  compositeOutputMapping?: Record<string, string>
}
