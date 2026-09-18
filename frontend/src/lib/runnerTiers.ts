// Agent execution tiers (ADR 029): the Runner tier has three packagings,
// identified by the EnvironmentProfile `provider_type`. Labels resolve through
// the `components.RunnerTier` locale namespace so no tier name is hardcoded.
// Wire values (provider_type, node_type) are frozen identifiers - do not rename.

export type RunnerTier = 'bundled_docker' | 'external_e2b' | 'local'

const TIER_BY_PROVIDER: Record<string, RunnerTier> = {
  runner_docker: 'bundled_docker',
  local_docker: 'bundled_docker',
  e2b: 'external_e2b',
  local: 'local',
}

export function runnerTierForProvider(providerType?: string | null): RunnerTier | null {
  if (!providerType) return null
  return TIER_BY_PROVIDER[providerType] ?? null
}

export function runnerTierLabelKey(tier: RunnerTier): string {
  return `components.RunnerTier.${tier}`
}

export function runnerTierLabelKeyForProvider(providerType?: string | null): string | null {
  const tier = runnerTierForProvider(providerType)
  return tier ? runnerTierLabelKey(tier) : null
}
