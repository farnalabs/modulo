<template>
  <div data-theme="agent" class="page-wide">
    <PageHeader :title="$t('views.AdminTelemetryView.telemetry')" :subtitle="$t('views.AdminTelemetryView.telemetry_page_subtitle')" />

    <div class="rounded-lg border bg-card p-6">
      <div class="flex items-center justify-between">
        <div>
          <h3 class="text-sm font-semibold">{{ $t('views.AdminTelemetryView.usage_telemetry') }}</h3>
          <p class="mt-1 text-sm text-muted-foreground">
            {{ $t('views.AdminTelemetryView.telemetry_toggle_description') }}
          </p>
        </div>
        <ToggleSwitch
          :checked="telemetryEnabled"
          :label="$t('views.AdminTelemetryView.usage_telemetry')"
          data-testid="admin-telemetry-toggle"
          :disabled="loading || saving"
          @toggle="toggleTelemetry"
        />
      </div>

      <div v-if="loading" class="mt-4 flex items-center gap-2 text-sm text-muted-foreground" aria-live="polite">
        <div class="h-4 w-4 animate-spin rounded-full border-2 border-primary border-t-transparent" />
        {{ $t('views.AdminTelemetryView.loading') }}
      </div>

      <div v-if="error" class="mt-4 rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive" role="alert" aria-live="assertive">
        {{ error }}
      </div>

      <div v-if="successMessage" class="mt-4 rounded-lg border border-success/50 bg-success/10 p-3 text-sm text-success" role="status" aria-live="polite">
        {{ successMessage }}
      </div>

      <div class="mt-6 space-y-4 border-t pt-4">
        <h4 class="text-sm font-medium">{{ $t('views.AdminTelemetryView.what_is_collected') }}</h4>
        <ul class="space-y-2 text-sm text-muted-foreground">
          <li class="flex items-start gap-2">
            <span class="mt-0.5 text-primary" aria-hidden="true">&#8226;</span>
            {{ $t('views.AdminTelemetryView.telemetry_item_pipeline_runs') }}
          </li>
          <li class="flex items-start gap-2">
            <span class="mt-0.5 text-primary" aria-hidden="true">&#8226;</span>
            {{ $t('views.AdminTelemetryView.telemetry_item_error_rates') }}
          </li>
          <li class="flex items-start gap-2">
            <span class="mt-0.5 text-primary" aria-hidden="true">&#8226;</span>
            {{ $t('views.AdminTelemetryView.telemetry_item_feature_usage') }}
          </li>
        </ul>
        <p class="text-xs text-muted-foreground">
          {{ $t('views.AdminTelemetryView.telemetry_no_pii') }}
        </p>
        <p class="text-xs text-muted-foreground">
          {{ $t('views.AdminTelemetryView.telemetry_default_off') }}
        </p>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import PageHeader from '../components/shared/PageHeader.vue'
import ToggleSwitch from '../components/shared/ToggleSwitch.vue'

const { t } = useI18n()

const loading = ref(true)
const saving = ref(false)
const telemetryEnabled = ref(false)
const error = ref<string | null>(null)
const successMessage = ref<string | null>(null)

async function loadStatus() {
  loading.value = true
  error.value = null
  try {
    const { data, error: err } = await api.GET('/api/v1/admin/telemetry')
    if (err) {
      error.value = formatApiError(err)
    } else if (data) {
      telemetryEnabled.value = data.enabled
    }
  } catch (e: unknown) {
    error.value = formatApiError(e)
  } finally {
    loading.value = false
  }
}

async function toggleTelemetry() {
  saving.value = true
  error.value = null
  successMessage.value = null
  try {
    const { data, error: err } = await api.PUT('/api/v1/admin/telemetry', {
      body: { enabled: !telemetryEnabled.value },
    })
    if (err) {
      error.value = formatApiError(err)
    } else if (data) {
      telemetryEnabled.value = data.enabled
      successMessage.value = data.enabled
        ? t('views.AdminTelemetryView.telemetry_enabled_success')
        : t('views.AdminTelemetryView.telemetry_disabled_success')
    }
  } catch (e: unknown) {
    error.value = formatApiError(e)
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  loadStatus()
})
</script>
