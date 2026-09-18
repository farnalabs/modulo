// Shared view-model type for the D5 Runners page (FAR-591).
//
// DERIVED from the generated OpenAPI type (``RunnersStatusResponse``, the
// wire contract) so the page can never drift from the backend — the local
// hand-written mirror was dropped when it drifted on optionality.
import type { components } from './api/schema'

export type RunnersStatus = components['schemas']['RunnersStatusResponse']
export type MachineProbe = NonNullable<RunnersStatus['machines']>[number]
export type ProfileHealth = NonNullable<RunnersStatus['profiles']>[number]
export type ProfileDrift = NonNullable<ProfileHealth['drift']>
export type ConcurrencyContract = RunnersStatus['concurrency']
export type ConcurrencyPreflight = ConcurrencyContract['preflight']

// qa F17: the strip states are a closed wire contract (the backend's
// Literal types) — derive the union here so the frontend switches below
// are exhaustively checkable.
export type StripState = MachineProbe['state']
