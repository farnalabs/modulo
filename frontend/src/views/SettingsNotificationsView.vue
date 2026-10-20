<template>
  <div data-theme="agent" class="page-wide">
    <PageHeader
      :title="$t('views.SettingsNotificationsView.title')"
      :subtitle="$t('views.SettingsNotificationsView.subtitle')"
    />

    <LoadingSpinner v-if="loading" data-testid="settings-notifications-loading" />

    <div v-else-if="loadError" data-testid="settings-notifications-error" aria-live="assertive">
      <ErrorAlert
        :message="loadError"
        :on-retry="loadPreferences"
      />
    </div>

    <div v-else class="space-y-6">
      <section class="rounded-lg border bg-card shadow-sm max-w-3xl">
        <div class="p-6 space-y-4">
          <div>
            <h2 class="text-base font-semibold">
              {{ $t('views.SettingsNotificationsView.categories_heading') }}
            </h2>
            <p class="mt-1 text-sm text-muted-foreground">
              {{ $t('views.SettingsNotificationsView.categories_help') }}
            </p>
          </div>

          <EmptyState
            v-if="categories.length === 0"
            data-testid="settings-notifications-empty"
            :title="$t('views.SettingsNotificationsView.no_categories')"
          />

          <ul v-else class="divide-y divide-border" data-testid="settings-notifications-list">
            <li
              v-for="category in categories"
              :key="category"
              class="flex items-center justify-between gap-4 py-3"
            >
              <span class="min-w-0 text-sm font-medium">{{ categoryLabel(category) }}</span>
              <div class="flex items-center gap-2">
                <!-- The in-flight state must not be colour-only: the row says
                     what is happening AND the switch is marked busy. -->
                <span
                  v-if="savingCategory === category"
                  class="text-xs text-muted-foreground"
                >
                  {{ $t('views.SettingsNotificationsView.saving_row') }}
                </span>
                <ToggleSwitch
                  :checked="isEnabled(category)"
                  :label="$t('views.SettingsNotificationsView.toggle_aria_label', { category: categoryLabel(category) })"
                  :disabled="busy"
                  :toggling="savingCategory === category"
                  :aria-busy="savingCategory === category ? 'true' : undefined"
                  :data-testid="`settings-notifications-toggle-${category}`"
                  :data-category="category"
                  @toggle="toggleCategory(category, $event)"
                />
              </div>
            </li>
          </ul>
        </div>
      </section>

      <section class="rounded-lg border bg-card shadow-sm max-w-3xl">
        <div class="p-6 space-y-3">
          <div>
            <h2 class="text-base font-semibold">
              {{ $t('views.SettingsNotificationsView.dashboard_level_heading') }}
            </h2>
            <p class="mt-1 text-sm text-muted-foreground">
              {{ $t('views.SettingsNotificationsView.dashboard_level_summary') }}
            </p>
          </div>
          <label for="settings-notifications-level" class="block text-sm font-medium">
            {{ $t('views.SettingsNotificationsView.dashboard_level_label') }}
          </label>
          <p id="settings-notifications-level-help" class="text-sm text-muted-foreground">
            {{ $t('views.SettingsNotificationsView.dashboard_level_help') }}
          </p>
          <select
            id="settings-notifications-level"
            :value="dashboardLevel"
            :disabled="busy"
            aria-describedby="settings-notifications-level-help"
            data-testid="settings-notifications-level"
            class="w-full max-w-xs rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
            @change="onLevelChange"
          >
            <option v-for="level in dashboardLevels" :key="level" :value="level">
              {{ $t(`views.SettingsNotificationsView.levels.${level}`) }}
            </option>
          </select>
        </div>
      </section>

      <!-- Saving state: a PERSISTENT polite live region (always mounted, its
           text toggles) so screen readers announce the in-flight save without
           stealing focus. A region mounted together with its text is not
           reliably announced. -->
      <p
        class="min-h-5 text-sm text-muted-foreground"
        aria-live="polite"
        data-testid="settings-notifications-status"
      >
        {{ saving ? savingMessage : '' }}
      </p>

      <!-- Save failure: inline, assertive, with the failed change as its retry. -->
      <div v-if="saveError" data-testid="settings-notifications-save-error" aria-live="assertive">
        <ErrorAlert
          :message="saveError"
          :on-retry="retryFailedSave"
        />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import PageHeader from '../components/shared/PageHeader.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import ToggleSwitch from '../components/shared/ToggleSwitch.vue'

const PREFERENCES_PATH = '/api/v1/notifications/in-app/preferences'
const DEFAULT_DASHBOARD_LEVEL = 'warning'
const DASHBOARD_LEVELS = ['debug', 'info', 'warning', 'error'] as const

type DashboardLevel = (typeof DASHBOARD_LEVELS)[number]

type FailedSave =
  | { kind: 'category', category: string, optedOut: boolean }
  | { kind: 'level', level: DashboardLevel }

const { t, te } = useI18n()

const loading = ref(true)
const loadError = ref<string | null>(null)
const saveError = ref<string | null>(null)
const savingCategory = ref<string | null>(null)
const savingLevel = ref(false)
const failedSave = ref<FailedSave | null>(null)
const optOuts = ref<Record<string, boolean>>({})
const dashboardLevel = ref<DashboardLevel>(DEFAULT_DASHBOARD_LEVEL)

// The category set is driven entirely by the GET response (the backend derives
// it from notification_categories()). No hardcoded list or count lives here.
const categories = computed(() => Object.keys(optOuts.value).sort())

const busy = computed(
  () => loading.value || savingCategory.value !== null || savingLevel.value,
)

const saving = computed(() => savingCategory.value !== null || savingLevel.value)

const savingMessage = computed(() => t('views.SettingsNotificationsView.saving'))

function isEnabled(category: string): boolean {
  // notification_opt_outs[category] === true means OPTED OUT; the toggle means
  // "enabled", so it is the inverse.
  return !optOuts.value[category]
}

function categoryLabel(category: string): string {
  const key = `views.SettingsNotificationsView.categories.${category}`
  if (te(key)) return t(key)
  // The backend derives the category set from a fixed event map, and every
  // category it can emit has a curated label above. This fallback therefore
  // never renders a wire key to a user: an unexpected category gets a generic
  // (translated) label rather than internal vocabulary.
  return t('views.SettingsNotificationsView.unknown_category')
}

function isDashboardLevel(value: string): value is DashboardLevel {
  return (DASHBOARD_LEVELS as readonly string[]).includes(value)
}

async function loadPreferences(): Promise<void> {
  loading.value = true
  loadError.value = null
  try {
    const { data, error } = await api.GET(PREFERENCES_PATH)
    if (error) {
      loadError.value = t('views.SettingsNotificationsView.load_failed', {
        message: formatApiError(error),
      })
      return
    }
    optOuts.value = data?.notification_opt_outs ?? {}
    const level = data?.dashboard_level
    dashboardLevel.value = level && isDashboardLevel(level)
      ? level
      : DEFAULT_DASHBOARD_LEVEL
  } catch (error: unknown) {
    loadError.value = t('views.SettingsNotificationsView.load_failed', {
      message: formatApiError(error),
    })
  } finally {
    loading.value = false
  }
}

async function putDelta(
  body: { dashboard_level?: string, notification_opt_outs?: Record<string, boolean> },
): Promise<boolean> {
  saveError.value = null
  try {
    const { error } = await api.PUT(PREFERENCES_PATH, { body })
    if (error) {
      saveError.value = t('views.SettingsNotificationsView.save_failed', {
        message: formatApiError(error),
      })
      return false
    }
    return true
  } catch (error: unknown) {
    saveError.value = t('views.SettingsNotificationsView.save_failed', {
      message: formatApiError(error),
    })
    return false
  }
}

async function applyCategory(category: string, enabled: boolean): Promise<void> {
  // DELTA write: only this one key. Sending the whole set would be a
  // read-modify-write that clobbers a concurrent change from another tab.
  const optedOut = !enabled
  const ok = await putDelta({ notification_opt_outs: { [category]: optedOut } })
  if (ok) {
    optOuts.value = { ...optOuts.value, [category]: optedOut }
    failedSave.value = null
  } else {
    failedSave.value = { kind: 'category', category, optedOut }
  }
}

async function toggleCategory(category: string, enabled: boolean): Promise<void> {
  if (busy.value) return
  savingCategory.value = category
  try {
    await applyCategory(category, enabled)
  } finally {
    savingCategory.value = null
  }
}

async function applyLevel(level: DashboardLevel): Promise<void> {
  const ok = await putDelta({ dashboard_level: level })
  if (ok) {
    dashboardLevel.value = level
    failedSave.value = null
  } else {
    failedSave.value = { kind: 'level', level }
  }
}

async function onLevelChange(event: Event): Promise<void> {
  const value = (event.target as HTMLSelectElement).value
  if (!isDashboardLevel(value)) return
  if (busy.value) return
  savingLevel.value = true
  try {
    await applyLevel(value)
  } finally {
    savingLevel.value = false
  }
}

async function retryFailedSave(): Promise<void> {
  const failed = failedSave.value
  if (!failed || busy.value) return
  if (failed.kind === 'level') {
    savingLevel.value = true
    try {
      await applyLevel(failed.level)
    } finally {
      savingLevel.value = false
    }
    return
  }
  savingCategory.value = failed.category
  try {
    await applyCategory(failed.category, !failed.optedOut)
  } finally {
    savingCategory.value = null
  }
}

const dashboardLevels = DASHBOARD_LEVELS

onMounted(loadPreferences)
</script>
