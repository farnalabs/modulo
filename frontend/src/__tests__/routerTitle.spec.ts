import { describe, expect, it } from 'vitest'
import { documentTitleFor } from '../router'

// FAR-1812 review: routes with neither a `meta.title` nor a manifest
// breadcrumb used to render the duplicated "Modulo | Modulo".
describe('documentTitleFor', () => {
  it('uses the route title when present', () => {
    expect(documentTitleFor({ title: 'Runs', breadcrumb: 'Ignored' })).toBe('Runs | Modulo')
  })

  it('falls back to the manifest breadcrumb', () => {
    expect(documentTitleFor({ breadcrumb: 'Pipelines' })).toBe('Pipelines | Modulo')
  })

  it('renders plain "Modulo" when the route supplies no label', () => {
    expect(documentTitleFor({})).toBe('Modulo')
    expect(documentTitleFor({ title: '', breadcrumb: '' })).toBe('Modulo')
  })
})
