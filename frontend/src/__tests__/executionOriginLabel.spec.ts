import { describe, it, expect } from 'vitest'
import { executionOriginLabel } from '../utils/runUtils'

// FAR-1141 review MAJOR 3 / I18N-1: the analytics execution_origin dimension
// must never surface a raw backend bucket key.
describe('executionOriginLabel (FAR-1141)', () => {
  const t = (key: string) => (key === 'common.execution_origin.dispatched' ? 'Dispatched' : key)

  it('maps a known execution_origin key through i18n', () => {
    expect(executionOriginLabel('dispatched', t)).toBe('Dispatched')
  })

  it('falls back to the raw value for an unmapped key', () => {
    expect(executionOriginLabel('direct', t)).toBe('direct')
  })

  it('renders a dash for a missing key', () => {
    expect(executionOriginLabel(null, t)).toBe('—')
    expect(executionOriginLabel('', t)).toBe('—')
  })
})
