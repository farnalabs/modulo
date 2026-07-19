<template>
  <div class="space-y-3">
    <label for="environmentprofilepicker-field-1" class="mb-1 block text-sm font-medium">Environment Profile</label>

    <Select v-model="selectedId" @update:model-value="emitChange">
      <SelectTrigger class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm" aria-label="Environment profile" data-testid="envprofile-picker-select">
        <SelectValue placeholder="None (no sandbox environment)" />
      </SelectTrigger>
      <SelectContent>
        <SelectItem
          v-for="profile in activeProfiles"
          :key="profile.id"
          :value="profile.id"
        >
          {{ profile.name }} ({{ profile.provider_type }})
        </SelectItem>
      </SelectContent>
    </Select>

    <div v-if="selectedProfile" class="rounded-lg border bg-muted/30 p-3 space-y-2 text-sm">
      <div class="flex items-center gap-2">
        <span class="font-medium">Provider:</span>
        <span class="rounded-full bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary">
          {{ selectedProfile.provider_type }}
        </span>
      </div>
      <div v-if="selectedProfile.image_ref">
        <span class="font-medium">Image:</span>
        <code class="ml-1 rounded bg-muted px-1.5 py-0.5 text-xs font-mono">{{ selectedProfile.image_ref }}</code>
      </div>
      <div v-if="selectedProfile.capabilities.length > 0">
        <span class="font-medium">Capabilities:</span>
        <div class="flex flex-wrap gap-1 mt-1">
          <span
            v-for="cap in selectedProfile.capabilities"
            :key="cap"
            class="rounded-full bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary"
          >
            {{ cap }}
          </span>
        </div>
      </div>
      <p v-if="selectedProfile.description" class="text-xs text-muted-foreground">
        {{ selectedProfile.description }}
      </p>
    </div>

    <div v-if="store.isLoading" class="text-xs text-muted-foreground">Loading profiles...</div>
    <div v-else-if="activeProfiles.length === 0 && !selectedId" class="text-xs text-muted-foreground">
      No active environment profiles. Create one in
      <router-link to="/environment-profiles" class="text-primary hover:underline">Settings</router-link>.
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { Select, SelectTrigger, SelectContent, SelectItem, SelectValue } from '@/components/ui/select'
import { useEnvironmentProfilesStore, type EnvironmentProfileSummary } from '../../stores/environmentProfiles'

const props = defineProps<{
  modelValue?: string
}>()

const emit = defineEmits<{
  'update:modelValue': [value: string]
}>()

const store = useEnvironmentProfilesStore()

const selectedId = ref(props.modelValue ?? '')

const activeProfiles = computed(() =>
  store.profiles.filter((p) => p.status === 'active')
)

const selectedProfile = computed<EnvironmentProfileSummary | null>(() => {
  if (!selectedId.value) return null
  return store.profiles.find((p) => p.id === selectedId.value) ?? null
})

function emitChange() {
  emit('update:modelValue', selectedId.value)
}

watch(
  () => props.modelValue,
  (val) => {
    selectedId.value = val ?? ''
  }
)

onMounted(() => {
  if (store.profiles.length === 0) {
    store.fetchProfiles().catch(() => {})
  }
})
</script>
