import { describe, it, expect } from 'vitest'
import { asRejectDisposition } from '../utils/rejectDisposition'

// FAR-1487: the reject-disposition vocabulary is a closed set shared by the
// briefing summary (HitlBriefing) and the review card (HitlReviewCard), so the
// guard lives in one place and cannot drift between the two surfaces.
describe('reject disposition guard (FAR-1487)', () => {
  it('accepts each member of the closed vocabulary', () => {
    expect(asRejectDisposition('route')).toBe('route')
    expect(asRejectDisposition('terminate')).toBe('terminate')
    expect(asRejectDisposition('proceed')).toBe('proceed')
  })

  it('rejects anything outside the vocabulary', () => {
    expect(asRejectDisposition('rejected')).toBeNull()
    expect(asRejectDisposition('')).toBeNull()
    expect(asRejectDisposition(null)).toBeNull()
    expect(asRejectDisposition(undefined)).toBeNull()
    expect(asRejectDisposition(42)).toBeNull()
  })
})
