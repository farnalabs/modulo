import { describe, it, expect } from 'vitest'
import {
  buildJsonSchema,
  coerceDefault,
  createField,
  parseDefinitionToFields,
} from '../utils/schema-definition'

describe('schema-definition', () => {
  describe('createField', () => {
    it('creates a blank string field with a unique key', () => {
      expect(createField(1)).toEqual({
        _key: 1,
        name: '',
        type: 'string',
        required: false,
        description: '',
        defaultValue: '',
      })
    })
  })

  describe('coerceDefault', () => {
    it('coerces numeric defaults', () => {
      expect(coerceDefault('42', 'number')).toBe(42)
      expect(coerceDefault('not-a-number', 'number')).toBe('not-a-number')
    })

    it('coerces boolean defaults', () => {
      expect(coerceDefault('true', 'boolean')).toBe(true)
      expect(coerceDefault('false', 'boolean')).toBe(false)
      expect(coerceDefault('maybe', 'boolean')).toBe('maybe')
    })

    it('passes through string defaults untouched', () => {
      expect(coerceDefault('hello', 'string')).toBe('hello')
    })
  })

  describe('parseDefinitionToFields', () => {
    it('returns an empty list when properties are missing', () => {
      expect(parseDefinitionToFields({}, () => 1)).toEqual([])
    })

    it('maps properties, required flags, and descriptions', () => {
      const fields = parseDefinitionToFields(
        {
          required: ['email'],
          properties: {
            email: { type: 'string', description: 'User email', default: 'a@b.c' },
            age: { type: 'number' },
          },
        },
        () => 7,
      )
      expect(fields).toEqual([
        {
          _key: 7,
          name: 'email',
          type: 'string',
          required: true,
          description: 'User email',
          defaultValue: 'a@b.c',
        },
        {
          _key: 7,
          name: 'age',
          type: 'number',
          required: false,
          description: '',
          defaultValue: '',
        },
      ])
    })
  })

  describe('buildJsonSchema', () => {
    const base = {
      schemaName: 'Test Schema',
      schemaDescription: '',
      fields: [
        { _key: 1, name: 'email', type: 'string', required: true, description: '', defaultValue: '' },
      ],
      untitledLabel: 'Untitled',
    }

    it('builds a JSON-Schema object with required fields and coercions', () => {
      const schema = buildJsonSchema({
        ...base,
        schemaName: '',
        fields: [
          { _key: 1, name: 'count', type: 'number', required: true, description: 'A count', defaultValue: '5' },
          { _key: 2, name: 'flag', type: 'boolean', required: false, description: '', defaultValue: 'true' },
        ],
      })
      expect(schema.title).toBe('Untitled')
      expect(schema.type).toBe('object')
      expect(schema.required).toEqual(['count'])
      expect(schema.properties).toEqual({
        count: { type: 'number', description: 'A count', default: 5 },
        flag: { type: 'boolean', default: true },
      })
    })

    it('skips unnamed fields and omits empty defaults', () => {
      const schema = buildJsonSchema({
        ...base,
        fields: [
          { _key: 1, name: '', type: 'string', required: true, description: '', defaultValue: '' },
          { _key: 2, name: 'name', type: 'string', required: false, description: '', defaultValue: '' },
        ],
      })
      expect(schema.properties).toEqual({ name: { type: 'string' } })
      expect(schema.required).toBeUndefined()
    })
  })
})
