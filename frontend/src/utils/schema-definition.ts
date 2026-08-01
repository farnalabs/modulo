export interface SchemaField {
  _key: number
  name: string
  type: string
  required: boolean
  description: string
  defaultValue: string
}

export function createField(key: number): SchemaField {
  return {
    _key: key,
    name: '',
    type: 'string',
    required: false,
    description: '',
    defaultValue: '',
  }
}

export function coerceDefault(value: string, type: string): unknown {
  switch (type) {
    case 'number': {
      const n = Number(value)
      return Number.isNaN(n) ? value : n
    }
    case 'boolean': {
      if (value === 'true') return true
      if (value === 'false') return false
      return value
    }
    default:
      return value
  }
}

export function parseDefinitionToFields(
  def: Record<string, unknown>,
  keyCounter: () => number,
): SchemaField[] {
  const properties = def.properties
  if (!properties || typeof properties !== 'object') return []
  const loadedFields: SchemaField[] = []
  for (const [name, prop] of Object.entries(properties as Record<string, Record<string, unknown>>)) {
    loadedFields.push({
      _key: keyCounter(),
      name,
      type: (prop.type as string | undefined) ?? 'string',
      required: Array.isArray(def.required) && def.required.includes(name),
      description: (prop.description as string | undefined) ?? '',
      defaultValue: prop.default !== undefined ? String(prop.default) : '',
    })
  }
  return loadedFields
}

export function buildJsonSchema(input: {
  schemaName: string
  schemaDescription: string
  fields: SchemaField[]
  untitledLabel: string
}): Record<string, unknown> {
  const properties: Record<string, unknown> = {}
  const requiredFields: string[] = []

  for (const field of input.fields) {
    if (!field.name.trim()) continue
    const prop: Record<string, unknown> = { type: field.type }
    if (field.description) prop.description = field.description
    if (field.defaultValue) {
      prop.default = coerceDefault(field.defaultValue, field.type)
    }
    properties[field.name.trim()] = prop
    if (field.required) requiredFields.push(field.name.trim())
  }

  const schema: Record<string, unknown> = {
    $schema: 'https://json-schema.org/draft/2020-12/schema',
    title: input.schemaName || input.untitledLabel,
    type: 'object',
    properties,
  }
  if (input.schemaDescription) schema.description = input.schemaDescription
  if (requiredFields.length > 0) schema.required = requiredFields

  return schema
}
