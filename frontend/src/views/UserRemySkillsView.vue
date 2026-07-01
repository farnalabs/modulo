<template>
  <FeatureGate feature-name="remy" required-tier="team">
    <template #locked="{ tooltip }">
      <div data-theme="agent" class="mx-auto max-w-6xl space-y-6 p-6">
        <div class="mb-4 flex items-center gap-2 rounded-lg border border-warning/30 bg-warning/5 p-4 text-sm text-warning">
          <LockIcon :locked="true" :tooltip="tooltip" />
          <span>Remy is not available on your current plan.</span>
        </div>
      </div>
    </template>

  <div data-theme="agent" class="mx-auto max-w-4xl space-y-6 p-6">
    <header class="flex items-center justify-between">
      <div>
        <h1 class="text-3xl font-bold tracking-tight">My Remy Skills</h1>
        <p class="mt-1 text-muted-foreground">Manage your personal skills for the Remy AI assistant</p>
      </div>
      <button
        class="rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground border border-primary/30 hover:brightness-110 transition-all"
        data-testid="remy-user-skills-add"
        @click="skillDialogRef?.openCreate()"
      >
        Add Skill
      </button>
    </header>

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="loadError" :message="loadError" :on-retry="loadSkills" />

    <div v-else-if="skills.length === 0" class="rounded-lg border bg-card p-8 text-center">
      <p class="text-lg font-medium">No personal skills configured</p>
      <p class="mt-1 text-sm text-muted-foreground">
        Create skills to give Remy custom instructions and behaviours.
      </p>
    </div>

    <template v-else>
      <div class="overflow-hidden rounded-lg border bg-card shadow-sm">
        <table class="w-full text-left text-sm">
          <thead class="bg-muted/50 text-xs font-medium uppercase text-muted-foreground">
            <tr>
              <th class="px-4 py-3">Name</th>
              <th class="px-4 py-3">Description</th>
              <th class="px-4 py-3">Triggers</th>
              <th class="px-4 py-3">Active</th>
              <th class="px-4 py-3 text-right">Actions</th>
            </tr>
          </thead>
          <tbody class="divide-y">
            <tr
              v-for="skill in skills"
              :key="skill.id"
              class="transition-colors hover:bg-muted/30"
            >
              <td class="px-4 py-3 font-medium">{{ skill.name }}</td>
              <td class="px-4 py-3 text-muted-foreground max-w-xs truncate">{{ skill.description || '—' }}</td>
              <td class="px-4 py-3">
                <div class="flex flex-wrap gap-1">
                  <span
                    v-for="trigger in (skill.triggers || [])"
                    :key="trigger"
                    class="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground"
                  >
                    {{ trigger }}
                  </span>
                  <span v-if="!skill.triggers?.length" class="text-xs text-muted-foreground">—</span>
                </div>
              </td>
              <td class="px-4 py-3">
                <button
                  class="inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium transition-colors"
                  :class="skill.active ? 'bg-success/10 text-success' : 'bg-muted text-muted-foreground'"
                  @click="toggleSkillActive(skill)"
                >
                  <span
                    class="h-1.5 w-1.5 rounded-full"
                    :class="skill.active ? 'bg-success' : 'bg-muted-foreground'"
                  />
                  {{ skill.active ? 'Active' : 'Inactive' }}
                </button>
              </td>
              <td class="px-4 py-3 text-right">
                <div class="flex items-center justify-end gap-1">
                  <button
                    class="rounded p-1 text-muted-foreground hover:bg-accent"
                    :aria-label="'Edit skill'"
                    title="Edit skill"
                    @click="skillDialogRef?.openEdit(skill)"
                  >
                    <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <path d="M17 3a2.85 2.85 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" />
                    </svg>
                  </button>
                  <button
                    class="rounded p-1 text-destructive hover:bg-destructive/10"
                    :aria-label="'Delete skill'"
                    title="Delete skill"
                    @click="skillDialogRef?.openDelete(skill)"
                  >
                    <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <path d="M3 6h18" /><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6" /><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2" />
                    </svg>
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>

    <RemySkillDialog
      ref="skillDialogRef"
      create-description="Create a new personal skill for Remy."
      edit-description="Update your personal skill."
      create-endpoint="/api/v1/me/remy/skills"
      update-endpoint="/api/v1/me/remy/skills/{skill_id}"
      delete-endpoint="/api/v1/me/remy/skills/{skill_id}"
      @saved="loadSkills"
    />
  </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { api } from '../lib/api/client'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import FeatureGate from '../components/FeatureGate.vue'
import LockIcon from '../components/LockIcon.vue'
import RemySkillDialog from '../components/remy/RemySkillDialog.vue'
import type { SkillItem } from '../types/remy'

const skills = ref<SkillItem[]>([])
const loading = ref(true)
const loadError = ref<string | null>(null)

const skillDialogRef = ref<InstanceType<typeof RemySkillDialog> | null>(null)

async function loadSkills() {
  loading.value = true
  loadError.value = null
  try {
    const { data, error: err } = await (api as any).GET('/api/v1/me/remy/skills')
    if (err) {
      loadError.value = `Failed to load skills: ${err}`
    } else if (data) {
      skills.value = (data as { items: SkillItem[] }).items || (data as SkillItem[])
    }
  } catch (e: unknown) {
    loadError.value = `Failed to load skills: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
}

async function toggleSkillActive(skill: SkillItem) {
  const newActive = !skill.active
  try {
    const { error: err } = await (api as any).PUT('/api/v1/me/remy/skills/{skill_id}', {
      params: { path: { skill_id: skill.id } },
      body: { active: newActive },
    })
    if (err) return
    skill.active = newActive
  } catch {
    // ignore
  }
}

onMounted(() => { loadSkills() })
</script>
