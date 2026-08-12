<template>
  <span
    v-if="provenance"
    :class="badgeClass"
    :data-testid="`provenance-badge-${provenance}`"
    class="badge"
  >
    {{ label }}
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

const props = defineProps<{
  provenance: string | null
}>()

const { t } = useI18n()

const badgeClass = computed(() => {
  switch (props.provenance) {
    case 'derived':
      return 'badge-context-green'
    case 'reported':
      return 'badge-context-amber'
    default:
      return 'badge-context-slate'
  }
})

const label = computed(() => {
  switch (props.provenance) {
    case 'derived':
      return t('views.LifecycleMapView.journey.provenance.derived')
    case 'reported':
      return t('views.LifecycleMapView.journey.provenance.reported')
    default:
      return props.provenance ?? ''
  }
})
</script>
