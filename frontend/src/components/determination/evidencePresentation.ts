import type { components } from '../../lib/api/schema'

/**
 * Presentation helpers for the determination evidence surface (FAR-2472).
 *
 * Two responsibilities, both deliberately free of Vue state so they can be
 * unit-tested directly:
 *
 * 1. Keep the observed / derived distinction visible. A reference's own
 *    ``provenance`` and the kind of proposal element it supports are separate
 *    facts, and neither may be presented as the other: an ``inferred``
 *    reference is styled as an inference, never as an observation.
 * 2. Refuse to surface anything that looks like a credential. The locator and
 *    reason fields are free text produced by connectors, so a token can end up
 *    inside them; the field is still shown, but its value is replaced with an
 *    explicit "withheld" notice rather than rendered.
 */

export type EvidenceReference = components['schemas']['EvidenceReference']

/** The three selectable proposal elements the drill-down panel can show. */
export type EvidenceElementKind = 'observed' | 'derived' | 'assumption'

/** One proposal element flattened for the drill-down panel. */
export interface EvidenceElementView {
  id: string
  kind: EvidenceElementKind
  statement: string
  confidence: string
  evidenceRefs: EvidenceReference[]
  /**
   * The element's own uncertainty. Only an unresolved assumption carries one
   * in the contract (its ``reason``); the contract has no per-fact
   * uncertainty field, so ``null`` here means "not recorded", never "none".
   */
  uncertainty: string | null
}

/** A free-text value that is either safe to render or withheld. */
export interface DisplayText {
  withheld: boolean
  text: string
}

const SECRET_VALUE_PATTERNS: readonly RegExp[] = [
  // Credential prefixes with a recognisable shape.
  /\bgh[pousr]_[A-Za-z0-9]{20,}\b/,
  /\bsk-[A-Za-z0-9_-]{16,}\b/,
  /\bxox[baprs]-[A-Za-z0-9-]{10,}\b/,
  /\bAKIA[0-9A-Z]{16}\b/,
  /-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----/,
]

// A field named like a credential followed by a value: ``api_key=...``,
// ``token: ...``, ``client_secret = ...``. The value must be non-empty.
const SECRET_ASSIGNMENT =
  /(?:api[_-]?key|apikey|client[_-]?secret|secret|password|passwd|token|access[_-]?token|refresh[_-]?token|auth[_-]?token|private[_-]?key|authorization)\s*[:=]\s*\S+/i

// ``https://user:password@host`` — password-bearing URL userinfo.
const URL_USERINFO_PASSWORD = /\/\/[^/\s:@]+:[^/\s@]+@/

const BEARER_TOKEN = /\bbearer\s+[A-Za-z0-9._~+/=-]{16,}/i

// A long run of token-safe characters. A plain hex digest (a commit id, a
// sha256) is deliberately NOT treated as a secret: it is a locator, and hiding
// it would make the evidence unreviewable.
const TOKEN_LIKE_BLOB = /[A-Za-z0-9+/=_-]{40,}/g

function isHexDigest(value: string): boolean {
  return /^[0-9a-fA-F]+$/.test(value)
}

function hasMixedEntropy(value: string): boolean {
  return /[A-Z]/.test(value) && /[a-z]/.test(value) && /[0-9]/.test(value)
}

/** True when a free-text value looks like it carries a credential. */
export function looksLikeSecret(value: string): boolean {
  if (!value) return false
  if (SECRET_VALUE_PATTERNS.some((pattern) => pattern.test(value))) return true
  if (SECRET_ASSIGNMENT.test(value)) return true
  if (URL_USERINFO_PASSWORD.test(value)) return true
  if (BEARER_TOKEN.test(value)) return true
  for (const candidate of value.match(TOKEN_LIKE_BLOB) ?? []) {
    if (!isHexDigest(candidate) && hasMixedEntropy(candidate)) return true
  }
  return false
}

/** The value to render for a free-text field, withholding a credential. */
export function displayEvidenceText(value: string): DisplayText {
  return looksLikeSecret(value) ? { withheld: true, text: '' } : { withheld: false, text: value }
}

/** Badge classes that keep observed, derived, inferred and unavailable distinct. */
export function provenanceBadgeClass(provenance: string): string {
  switch (provenance) {
    case 'observed':
      return 'bg-success/10 text-success-text'
    case 'derived':
    case 'inferred':
      return 'bg-warning/10 text-warning-text'
    case 'unavailable':
      return 'bg-destructive/10 text-destructive-text'
    default:
      return 'bg-muted text-muted-foreground'
  }
}

/** Badge classes for the kind of proposal element being inspected. */
export function elementKindBadgeClass(kind: EvidenceElementKind): string {
  switch (kind) {
    case 'observed':
      return 'bg-success/10 text-success-text'
    case 'derived':
      return 'bg-warning/10 text-warning-text'
    default:
      return 'bg-muted text-muted-foreground'
  }
}

/**
 * A stable key for an unavailable-evidence row.
 *
 * ``UnavailableEvidence`` carries no id in the contract, so the only identity
 * it has is its own content. Keying by the tuple (never by array position)
 * keeps the row stable while another entry is removed.
 */
export function unavailableEvidenceKey(entry: {
  source_id: string
  requested_resource: string
  reason: string
}): string {
  return JSON.stringify([entry.source_id, entry.requested_resource, entry.reason])
}
