<template>
  <div data-theme="agent" class="mx-auto max-w-4xl space-y-6 p-6">
    <header class="flex items-center justify-between">
      <div>
        <h1 class="text-3xl font-bold tracking-tight">My Remy Skills</h1>
        <p class="mt-1 text-muted-foreground">Manage your personal skills for the Remy AI assistant</p>
      </div>
      <button
        class="rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground border border-primary/30 hover:brightness-110 transition-all"
        data-testid="remy-user-skills-add"
        @click="openSkillDialog()"
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
                    @click="openSkillDialog(skill)"
                  >
                    <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <path d="M17 3a2.85 2.85 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" />
                    </svg>
                  </button>
                  <button
                    class="rounded p-1 text-destructive hover:bg-destructive/10"
                    :aria-label="'Delete skill'"
                    title="Delete skill"
                    @click="confirmDeleteSkill(skill)"
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

    <!-- Skill Dialog -->
    <Dialog :open="skillDialogOpen" @update:open="skillDialogOpen = false">
      <DialogContent class="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{{ editingSkillId ? 'Edit Skill' : 'Add Skill' }}</DialogTitle>
          <DialogDescription>
            {{ editingSkillId ? 'Update your personal skill.' : 'Create a new personal skill for Remy.' }}
          </DialogDescription>
        </DialogHeader>

        <form @submit.prevent="saveSkill" class="space-y-4">
          <div>
            <label class="mb-1 block text-sm font-medium">Name <span class="text-destructive">*</span></label>
            <input
              v-model="skillForm.name"
              type="text"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              placeholder="Skill name"
              required
              data-testid="remy-user-skills-form-name"
            />
          </div>
          <div>
            <label class="mb-1 block text-sm font-medium">Description</label>
            <textarea
              v-model="skillForm.description"
              rows="2"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              placeholder="What this skill does"
              data-testid="remy-user-skills-form-description"
            />
          </div>
          <div>
            <label class="mb-1 block text-sm font-medium">Triggers</label>
            <input
              v-model="skillForm.triggersInput"
              type="text"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              placeholder="trigger1, trigger2"
              data-testid="remy-user-skills-form-triggers"
            />
            <p class="mt-1 text-xs text-muted-foreground">Comma-separated trigger keywords</p>
          </div>
          <div>
            <label class="mb-1 block text-sm font-medium">Body (Markdown)</label>
            <textarea
              v-model="skillForm.body"
              rows="6"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
              placeholder="# Skill instructions&#10;Write markdown here..."
              data-testid="remy-user-skills-form-body"
            />
          </div>
          <div class="flex items-center gap-2">
            <label class="flex items-center gap-2 text-sm cursor-pointer">
              <input
                v-model="skillForm.active"
                type="checkbox"
                class="rounded border-input"
                data-testid="remy-user-skills-form-active"
              />
              Active
            </label>
          </div>
          <div v-if="skillFormError" class="text-sm text-destructive">{{ skillFormError }}</div>
          <DialogFooter>
            <button
              type="button"
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
              data-testid="remy-user-skills-form-cancel"
              @click="closeSkillDialog"
            >
              Cancel
            </button>
            <button
              :disabled="skillSaving || !skillForm.name.trim()"
              type="submit"
              class="rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground hover:brightness-110 disabled:opacity-50 transition-all"
              data-testid="remy-user-skills-form-submit"
            >
              {{ skillSaving ? 'Saving...' : (editingSkillId ? 'Update' : 'Create') }}
            </button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>

    <!-- Delete skill confirmation -->
    <Dialog :open="deleteSkillDialogOpen" @update:open="deleteSkillDialogOpen = false">
      <DialogContent class="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>Delete Skill</DialogTitle>
          <DialogDescription>
            Are you sure you want to delete "{{ deleteSkillName }}"? This action cannot be undone.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <button
            type="button"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
            data-testid="remy-user-skills-delete-cancel"
            @click="deleteSkillDialogOpen = false"
          >
            Cancel
          </button>
          <button
            :disabled="skillDeleting"
            type="button"
            class="rounded-lg bg-destructive px-4 py-2 text-sm font-semibold text-destructive-foreground hover:brightness-110 disabled:opacity-50 transition-all"
            data-testid="remy-user-skills-delete-confirm"
            @click="deleteSkill"
          >
            {{ skillDeleting ? 'Deleting...' : 'Delete' }}
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { api } from '../lib/api/client'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '../components/ui/dialog'

interface SkillItem {
  id: string
  name: string
  description?: string
  triggers?: string[]
  body?: string
  active: boolean
}

const skills = ref<SkillItem[]>([])
const loading = ref(true)
const loadError = ref<string | null>(null)

const skillDialogOpen = ref(false)
const editingSkillId = ref<string | null>(null)
const skillForm = reactive({
  name: '',
  description: '',
  triggersInput: '',
  body: '',
  active: true,
})
const skillSaving = ref(false)
const skillFormError = ref<string | null>(null)

const deleteSkillDialogOpen = ref(false)
const deleteSkillId = ref<string | null>(null)
const deleteSkillName = ref('')
const skillDeleting = ref(false)

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

function openSkillDialog(skill?: SkillItem) {
  skillDialogOpen.value = true
  skillFormError.value = null
  if (skill) {
    editingSkillId.value = skill.id
    skillForm.name = skill.name
    skillForm.description = skill.description || ''
    skillForm.triggersInput = (skill.triggers || []).join(', ')
    skillForm.body = skill.body || ''
    skillForm.active = skill.active
  } else {
    editingSkillId.value = null
    skillForm.name = ''
    skillForm.description = ''
    skillForm.triggersInput = ''
    skillForm.body = ''
    skillForm.active = true
  }
}

function closeSkillDialog() {
  skillDialogOpen.value = false
  editingSkillId.value = null
}

async function saveSkill() {
  if (!skillForm.name.trim()) return
  skillSaving.value = true
  skillFormError.value = null
  try {
    const triggers = skillForm.triggersInput.split(/[\s,]+/).map(s => s.trim()).filter(Boolean)
    const body = {
      name: skillForm.name.trim(),
      description: skillForm.description.trim() || null,
      triggers,
      body: skillForm.body,
      active: skillForm.active,
    }

    if (editingSkillId.value) {
      const { error: err } = await (api as any).PUT('/api/v1/me/remy/skills/{skill_id}', {
        params: { path: { skill_id: editingSkillId.value } },
        body,
      })
      if (err) {
        skillFormError.value = `Failed to update skill: ${err}`
        return
      }
    } else {
      const { data, error: err } = await (api as any).POST('/api/v1/me/remy/skills', { body })
      if (err) {
        skillFormError.value = `Failed to create skill: ${err}`
        return
      }
      if (data) skills.value.push(data as SkillItem)
    }

    closeSkillDialog()
    await loadSkills()
  } catch (e: unknown) {
    skillFormError.value = `Failed to save skill: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    skillSaving.value = false
  }
}

function confirmDeleteSkill(skill: SkillItem) {
  deleteSkillId.value = skill.id
  deleteSkillName.value = skill.name
  deleteSkillDialogOpen.value = true
}

async function deleteSkill() {
  if (!deleteSkillId.value) return
  skillDeleting.value = true
  try {
    const { error: err } = await (api as any).DELETE('/api/v1/me/remy/skills/{skill_id}', {
      params: { path: { skill_id: deleteSkillId.value } },
    })
    if (err) {
      skillFormError.value = `Failed to delete skill: ${err}`
      return
    }
    skills.value = skills.value.filter(s => s.id !== deleteSkillId.value)
    deleteSkillDialogOpen.value = false
    deleteSkillId.value = null
  } catch (e: unknown) {
    skillFormError.value = `Failed to delete skill: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    skillDeleting.value = false
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
