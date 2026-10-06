<template>
  <fieldset class="space-y-2" data-testid="api-key-grants-selector">
    <legend class="mb-1 block text-sm font-medium">{{ $t('views.SettingsMcpView.grants_title') }}</legend>
    <label for="api-key-grants-restrict" class="flex items-start gap-3 rounded-lg border p-3">
      <input
        id="api-key-grants-restrict"
        v-model="restricted"
        type="checkbox"
        class="mt-1 h-4 w-4 rounded border-muted-foreground"
        data-testid="api-key-grants-restrict"
      />
      <span>
        <span class="block text-sm">{{ $t('views.SettingsMcpView.grants_restrict_label') }}</span>
        <span class="block text-xs text-muted-foreground">{{ restricted ? $t('views.SettingsMcpView.grants_restricted_hint') : $t('views.SettingsMcpView.grants_role_bundle_hint') }}</span>
      </span>
    </label>
    <div v-if="restricted" class="max-h-64 space-y-3 overflow-y-auto rounded-lg border p-3" data-testid="api-key-grants-list">
      <div v-for="group in groups" :key="group.prefix">
        <p class="mb-1 text-xs font-semibold uppercase text-muted-foreground">{{ groupLabel(group.prefix) }}</p>
        <label
          v-for="perm in group.items"
          :key="perm.name"
          :for="`api-key-grant-${perm.name}`"
          class="flex items-center gap-2 py-0.5"
        >
          <input
            :id="`api-key-grant-${perm.name}`"
            type="checkbox"
            class="h-4 w-4 rounded border-muted-foreground"
            :data-testid="`api-key-grant-${perm.name}`"
            :checked="selected.includes(perm.name)"
            @change="toggle(perm.name)"
          />
          <span class="font-mono text-xs">{{ perm.name }}</span>
        </label>
      </div>
      <p
        v-if="selected.length === 0"
        class="text-sm text-destructive"
        data-testid="api-key-grants-empty-error"
        aria-live="polite"
      >{{ $t('views.SettingsMcpView.grants_select_at_least_one') }}</p>
    </div>
  </fieldset>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

/**
 * Grant-set picker for API key creation (FAR-1477).
 *
 * Tri-state on the wire: `grants` omitted = legacy role bundle (the default,
 * `restricted` off); a non-empty list = exactly those permissions. An EMPTY
 * list (deny-all) is deliberately NOT offered here - turning "restrict" on
 * with nothing ticked is invalid, so the parent keeps Create disabled rather
 * than silently sending `[]`.
 *
 * The permission list comes from the backend (`GET /api/v1/api-keys/grantable-permissions`,
 * already filtered through `is_delegable`) - nothing is hardcoded here.
 */
export interface GrantablePermission {
  name: string
  min_role: string
}

const props = defineProps<{ permissions: GrantablePermission[] }>()
const restricted = defineModel<boolean>('restricted', { default: false })
const selected = defineModel<string[]>('selected', { default: () => [] })
const { t, te } = useI18n()

const groups = computed(() => {
  const byPrefix = new Map<string, GrantablePermission[]>()
  for (const perm of props.permissions) {
    const prefix = perm.name.split('.')[0]
    const bucket = byPrefix.get(prefix) ?? []
    bucket.push(perm)
    byPrefix.set(prefix, bucket)
  }
  return [...byPrefix.entries()].map(([prefix, items]) => ({ prefix, items }))
})

function groupLabel(prefix: string): string {
  const key = `views.SettingsMcpView.grants_group_${prefix}`
  if (te(key)) return t(key)
  return prefix.charAt(0).toUpperCase() + prefix.slice(1).replace(/_/g, ' ')
}

function toggle(name: string) {
  selected.value = selected.value.includes(name)
    ? selected.value.filter((n) => n !== name)
    : [...selected.value, name]
}
</script>
