<template>
  <div class="page-narrow">
    <PageHeader :title="$t('views.OnboardingWizard.sdlc_onboarding')" :subtitle="$t('views.OnboardingWizard.guided_setup_wizard_mdash_connect_tools_infer_schemas_browse')" />

    <div class="flex items-center justify-center gap-0">
      <template v-for="(_, i) in steps" :key="i">
        <div class="flex items-center">
          <div
            class="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-xs font-medium transition-colors"
            :class="stepCircleClass(i)"
          >
            <Check v-if="i < currentStep" class="h-4 w-4" aria-hidden="true" />
            <span v-else>{{ i + 1 }}</span>
          </div>
          <div v-if="i < steps.length - 1" class="mx-2 h-px w-8 sm:w-16" :class="i < currentStep ? 'bg-primary' : 'bg-border'" />
        </div>
      </template>
    </div>

    <div class="rounded-lg border bg-card p-6 shadow-sm">
      <header class="mb-6">
        <h2 class="text-xl font-semibold">{{ steps[currentStep].title }}</h2>
        <p class="mt-1 text-sm text-muted-foreground">{{ steps[currentStep].subtitle }}</p>
      </header>

      <!-- Step 0: Welcome -->
      <div v-if="currentStep === 0" class="space-y-4 text-center">
        <div class="mx-auto flex h-16 w-16 items-center justify-center rounded-full bg-primary/10">
          <Layers class="h-8 w-8 text-primary" aria-hidden="true" />
        </div>
        <p class="text-muted-foreground">
          {{ $t('views.OnboardingWizard.wizard_guide_before') }} <strong>{{ $t('views.OnboardingWizard.wizard_guide_steps') }}</strong> {{ $t('views.OnboardingWizard.wizard_guide_after') }}
        </p>
        <ul class="mx-auto max-w-md space-y-2 text-left text-sm text-muted-foreground">
          <li v-for="(s, i) in steps.slice(1)" :key="i" class="flex items-start gap-2">
            <span class="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-primary/10 text-xs font-medium text-primary">{{ i + 1 }}</span>
            <span><strong>{{ s.title }}:</strong> {{ s.subtitle }}</span>
          </li>
        </ul>
      </div>

      <!-- Step 1: Connect Tools -->
      <div v-if="currentStep === 1" class="space-y-4">
        <div v-if="loadingConnectors" class="space-y-3 py-4" aria-hidden="true">
          <div class="h-16 animate-pulse rounded-lg bg-muted" />
          <div class="h-16 animate-pulse rounded-lg bg-muted" />
          <div class="h-16 animate-pulse rounded-lg bg-muted" />
        </div>
        <div v-else-if="connectorsError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive">{{ connectorsError }}</div>
        <div v-else-if="connectors.length === 0" class="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
          {{ $t('views.OnboardingWizard.no_connectors_found') }} <a href="/settings/connectors" data-testid="onboarding-wizard-create-connector" class="text-primary underline">{{ $t('views.OnboardingWizard.create_one') }}</a> {{ $t('views.OnboardingWizard.first_then_come_back') }}
        </div>
        <div v-else class="space-y-2">
          <span class="mb-1 block text-sm font-medium">{{ $t('views.OnboardingWizard.select_connector_instance') }}</span>
          <div role="button" tabindex="0" @keydown.enter="($event.currentTarget as HTMLElement).click()" @keydown.space.prevent="($event.currentTarget as HTMLElement).click()"
            v-for="c in connectors"
            :key="c.id"
            data-testid="onboarding-wizard-connector-card"
            class="flex cursor-pointer items-center gap-3 rounded-lg border p-4 transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            :class="wizardState.connectorId === c.id ? 'border-primary bg-primary/5' : 'border-input'"
            @click="wizardState.connectorId = c.id; wizardState.connectorName = c.name"
          >
            <div class="flex h-5 w-5 shrink-0 items-center justify-center rounded-full border-2" :class="wizardState.connectorId === c.id ? 'border-primary' : 'border-input'">
              <div v-if="wizardState.connectorId === c.id" class="h-2.5 w-2.5 rounded-full bg-primary" />
            </div>
            <div>
              <p class="text-sm font-medium">{{ c.name }}</p>
              <p class="text-xs text-muted-foreground">{{ c.connector_type_id }}</p>
            </div>
          </div>
        </div>
      </div>

      <!-- Step 2: Run Inference -->
      <div v-if="currentStep === 2" class="space-y-4">
        <div class="rounded-lg bg-muted p-3 text-sm text-muted-foreground">
          {{ $t('views.OnboardingWizard.connector_label') }} <strong>{{ wizardState.connectorName }}</strong>
        </div>
        <div>
          <label for="onboardingwizard-field-7" class="mb-1 block text-sm font-medium">{{ $t('views.OnboardingWizard.resource_type') }}</label>
          <input id="onboardingwizard-field-7"
            v-model="wizardState.resourceType"
            type="text"
            data-testid="onboarding-wizard-resource-type"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            :placeholder="$t('views.OnboardingWizard.resource_type_placeholder')"
          />
        </div>
        <div>
          <label for="onboardingwizard-field-6" class="mb-1 block text-sm font-medium">{{ $t('views.OnboardingWizard.sample_query') }} <span class="text-muted-foreground">{{ $t('views.OnboardingWizard.optional') }}</span></label>
          <textarea id="onboardingwizard-field-6"
            v-model="wizardState.sampleQuery"
            rows="2"
            data-testid="onboarding-wizard-sample-query"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            :placeholder="$t('views.OnboardingWizard.sample_query_placeholder')"
          />
        </div>
        <div class="flex items-center gap-2">
          <Button :disabled="!wizardState.resourceType.trim() || inferring" data-testid="onboarding-wizard-infer-schema" @click="inferSchema">
            {{ inferring ? $t('views.SchemaInferenceView.inferring') : $t('views.SchemaInferenceView.infer_schema') }}
          </Button>
        </div>
        <div v-if="inferError" class="text-sm text-destructive">{{ inferError }}</div>

        <div v-if="wizardState.draftSchema" class="rounded-lg border bg-card p-4">
          <h3 class="mb-3 text-sm font-semibold">{{ $t('views.OnboardingWizard.draft_label') }}: {{ wizardState.draftSchema.name }}</h3>
          <p v-if="wizardState.draftSchema.description" class="mb-3 text-xs text-muted-foreground">{{ wizardState.draftSchema.description }}</p>
          <table v-if="wizardState.draftSchema.fields.length > 0" class="w-full text-sm">
            <thead>
              <tr class="border-b text-left text-muted-foreground">
                <th class="pb-2 font-medium">{{ $t('views.SchemaInferenceView.field_name') }}</th>
                <th class="pb-2 font-medium">{{ $t('views.SchemaInferenceView.field_type') }}</th>
                <th class="pb-2 font-medium">{{ $t('views.SchemaInferenceView.field_required') }}</th>
                <th class="pb-2 font-medium">{{ $t('views.SchemaInferenceView.field_description') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="field in wizardState.draftSchema.fields" :key="field.name" class="border-b last:border-0">
                <td class="py-2 font-mono text-xs">{{ field.name }}</td>
                <td class="py-2 font-mono text-xs text-muted-foreground">{{ field.type }}</td>
                <td class="py-2">
                  <span class="inline-block rounded px-1.5 py-0.5 text-xs font-medium" :class="field.required ? 'bg-primary/10 text-primary' : 'bg-muted text-muted-foreground'">
                    {{ field.required ? $t('views.SchemaInferenceView.yes') : $t('views.SchemaInferenceView.no') }}
                  </span>
                </td>
                <td class="py-2 text-xs text-muted-foreground">{{ field.description ?? '—' }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="text-sm text-muted-foreground">{{ $t('views.SchemaInferenceView.no_fields_inferred') }}</p>
        </div>
      </div>

      <!-- Step 3: Review Schemas -->
      <div v-if="currentStep === 3" class="space-y-4">
        <div v-if="!wizardState.draftSchema" class="py-8 text-center text-sm text-muted-foreground">
          {{ $t('views.OnboardingWizard.no_schema_inferred') }}
        </div>
        <template v-else>
          <div class="flex items-center gap-4">
            <div>
              <label for="onboardingwizard-field-5" class="mb-1 block text-sm font-medium">{{ $t('views.OnboardingWizard.schema_name') }}</label>
              <input id="onboardingwizard-field-5"
                v-model="editableSchemaName"
                type="text"
                data-testid="onboarding-wizard-schema-name"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </div>
            <div class="flex-1">
              <label for="onboardingwizard-field-4" class="mb-1 block text-sm font-medium">{{ $t('views.OnboardingWizard.description') }}</label>
              <input id="onboardingwizard-field-4"
                v-model="editableSchemaDescription"
                type="text"
                data-testid="onboarding-wizard-schema-description"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </div>
          </div>
          <div>
            <h3 class="mb-2 text-sm font-medium">{{ $t('views.OnboardingWizard.fields') }} <span class="text-muted-foreground">{{ $t('views.OnboardingWizard.fields_hint') }}</span></h3>
            <table class="w-full text-sm">
              <thead>
                <tr class="border-b text-left text-muted-foreground">
                  <th class="pb-2 font-medium">{{ $t('views.OnboardingWizard.name') }}</th>
                  <th class="pb-2 font-medium">{{ $t('views.OnboardingWizard.type') }}</th>
                  <th class="pb-2 font-medium">{{ $t('views.OnboardingWizard.required') }}</th>
                  <th class="pb-2 font-medium">{{ $t('views.OnboardingWizard.description') }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="field in wizardState.draftSchema.fields" :key="field.name" class="border-b last:border-0">
                  <td class="py-2 font-mono text-xs">{{ field.name }}</td>
                  <td class="py-2 font-mono text-xs text-muted-foreground">{{ field.type }}</td>
                  <td class="py-2">
                    <span class="inline-block rounded px-1.5 py-0.5 text-xs font-medium" :class="field.required ? 'bg-primary/10 text-primary' : 'bg-muted text-muted-foreground'">{{ field.required ? $t('views.SchemaInferenceView.yes') : $t('views.SchemaInferenceView.no') }}</span>
                  </td>
                  <td class="py-2 text-xs text-muted-foreground">{{ field.description ?? '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div class="flex items-center gap-2">
          <Button :disabled="savingSchema" data-testid="onboarding-wizard-confirm-save-schema" @click="saveSchema">
            {{ savingSchema ? $t('views.OnboardingWizard.saving') : $t('views.OnboardingWizard.confirm_save_schema') }}
          </Button>
          </div>
          <div v-if="schemaSaveError" class="text-sm text-destructive">{{ schemaSaveError }}</div>
          <div v-if="wizardState.publishedSchemaId" class="rounded-lg bg-success/10 p-3 text-sm text-success">
            {{ $t('views.OnboardingWizard.schema_saved', { name: editableSchemaName }) }}
          </div>
        </template>
      </div>

      <!-- Step 4: Browse Library -->
      <div v-if="currentStep === 4" class="space-y-4">
        <div v-if="loadingLibrary" class="grid grid-cols-1 gap-3 sm:grid-cols-2" aria-hidden="true">
          <div v-for="n in 4" :key="n" class="h-24 animate-pulse rounded-lg bg-muted" />
        </div>
        <div v-else-if="libraryError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive">{{ libraryError }}</div>
        <div v-else-if="libraryItems.length === 0" class="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
          {{ $t('views.OnboardingWizard.no_library_items') }}
        </div>
        <div v-else class="space-y-2">
          <div class="flex items-center gap-3">
            <input id="onboardingwizard-library-search"
              v-model="librarySearch"
              type="text"
              :aria-label="$t('views.OnboardingWizard.search_library')"
              :placeholder="$t('views.OnboardingWizard.filter_items')"
              data-testid="onboarding-wizard-library-search"
              class="flex-1 rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
            <Select
  :aria-label="$t('views.OnboardingWizard.filter_by_type')"
  v-model="libraryTypeFilter"
  :placeholder="$t('views.OnboardingWizard.all_types')"
  data-testid="onboarding-wizard-library-type-filter"
  :options="[{ value: 'pipeline_template', label: $t('views.OnboardingWizard.pipeline_templates') }, { value: 'agent', label: $t('views.OnboardingWizard.agents') }, { value: 'schema', label: $t('views.OnboardingWizard.schemas') }, { value: 'integration', label: $t('views.OnboardingWizard.integrations') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
          </div>
          <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div role="button" tabindex="0" @keydown.enter="($event.currentTarget as HTMLElement).click()" @keydown.space.prevent="($event.currentTarget as HTMLElement).click()"
              v-for="item in filteredLibraryItems"
              :key="item.id"
              data-testid="onboarding-wizard-library-item"
              class="cursor-pointer rounded-lg border p-4 transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              :class="wizardState.selectedLibraryItemId === item.id ? 'border-primary bg-primary/5' : 'border-input'"
              @click="wizardState.selectedLibraryItemId = wizardState.selectedLibraryItemId === item.id ? null : item.id"
            >
              <div class="flex items-start justify-between">
                <div>
                  <span class="inline-block rounded bg-muted px-1.5 py-0.5 text-xs font-medium text-muted-foreground">{{ item.primitive_type }}</span>
                  <h4 class="mt-1 text-sm font-medium">{{ item.name }}</h4>
                  <p v-if="item.description" class="mt-0.5 text-xs text-muted-foreground line-clamp-2">{{ item.description }}</p>
                </div>
                <div v-if="wizardState.selectedLibraryItemId === item.id" class="mt-1">
                  <Check class="h-5 w-5 text-primary" aria-hidden="true" />
                </div>
              </div>
              <div v-if="item.tags && item.tags.length > 0" class="mt-2 flex flex-wrap gap-1">
                <span v-for="tag in item.tags.slice(0, 3)" :key="tag" class="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground">{{ tag }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Step 5: Wire Pipeline -->
      <div v-if="currentStep === 5" class="space-y-4">
        <div>
          <label for="onboardingwizard-field-2" class="mb-1 block text-sm font-medium">{{ $t('views.OnboardingWizard.pipeline_name') }}</label>
          <input id="onboardingwizard-field-2"
            v-model="wizardState.pipelineName"
            type="text"
            data-testid="onboarding-wizard-pipeline-name"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            :placeholder="$t('views.OnboardingWizard.pipeline_name_placeholder')"
          />
        </div>
        <div>
          <label for="onboardingwizard-field-1" class="mb-1 block text-sm font-medium">{{ $t('views.OnboardingWizard.description') }}</label>
          <textarea id="onboardingwizard-field-1"
            v-model="wizardState.pipelineDescription"
            rows="3"
            data-testid="onboarding-wizard-pipeline-description"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            :placeholder="$t('views.OnboardingWizard.pipeline_description_placeholder')"
          />
        </div>
        <div v-if="wizardState.selectedLibraryItemId && selectedLibraryItem" class="rounded-lg bg-muted p-3">
          <p class="text-xs text-muted-foreground">{{ $t('views.OnboardingWizard.selected_library_item') }}</p>
          <p class="text-sm font-medium">{{ selectedLibraryItem.name }}</p>
          <p v-if="selectedLibraryItem.description" class="text-xs text-muted-foreground">{{ selectedLibraryItem.description }}</p>
        </div>
        <div class="flex items-center gap-2">
          <Button :disabled="!wizardState.pipelineName.trim() || creatingPipeline" data-testid="onboarding-wizard-create-pipeline" @click="createPipeline">
            {{ creatingPipeline ? $t('views.OnboardingWizard.creating') : $t('views.OnboardingWizard.create_pipeline') }}
          </Button>
        </div>
        <div v-if="pipelineCreateError" class="text-sm text-destructive">{{ pipelineCreateError }}</div>
        <div v-if="wizardState.createdPipelineId" class="rounded-lg bg-success/10 p-3 text-sm text-success">
          {{ $t('views.OnboardingWizard.pipeline_label') }} "{{ wizardState.pipelineName }}" {{ $t('views.OnboardingWizard.created_suffix') }}.
        </div>
      </div>

      <!-- Step 6: Done -->
      <div v-if="currentStep === 6" class="space-y-6 py-4 text-center">
        <div class="mx-auto flex h-16 w-16 items-center justify-center rounded-full bg-success/10">
          <Check class="h-8 w-8 text-success" aria-hidden="true" />
        </div>
        <h3 class="text-2xl font-bold">{{ $t('views.OnboardingWizard.all_set') }}</h3>
        <p class="text-muted-foreground">
          {{ $t('views.OnboardingWizard.your_pipeline_has_been_created') }} <strong>{{ wizardState.pipelineName }}</strong>
          {{ wizardState.createdPipelineId ? $t('views.OnboardingWizard.and_ready_to_run') : '' }}.
          {{ $t('views.OnboardingWizard.heres_what_was_accomplished') }}
        </p>
        <ul class="mx-auto max-w-sm space-y-2 text-left text-sm">
          <li v-if="wizardState.connectorName" class="flex items-center gap-2 text-muted-foreground">
            <Check class="h-4 w-4 shrink-0 text-success" aria-hidden="true" />
            {{ $t('views.OnboardingWizard.connected') }} <strong>{{ wizardState.connectorName }}</strong>
          </li>
          <li v-if="wizardState.draftSchema" class="flex items-center gap-2 text-muted-foreground">
            <Check class="h-4 w-4 shrink-0 text-success" aria-hidden="true" />
            {{ $t('views.OnboardingWizard.inferred_schema') }} <strong>{{ wizardState.draftSchema.name }}</strong>
          </li>
          <li v-if="wizardState.publishedSchemaId" class="flex items-center gap-2 text-muted-foreground">
            <Check class="h-4 w-4 shrink-0 text-success" aria-hidden="true" />
            {{ $t('views.OnboardingWizard.published_to_schema_registry') }}
          </li>
          <li v-if="wizardState.selectedLibraryItemId" class="flex items-center gap-2 text-muted-foreground">
            <Check class="h-4 w-4 shrink-0 text-success" aria-hidden="true" />
            {{ $t('views.OnboardingWizard.selected_library_item') }}
          </li>
          <li v-if="wizardState.createdPipelineId" class="flex items-center gap-2 text-muted-foreground">
            <Check class="h-4 w-4 shrink-0 text-success" aria-hidden="true" />
            {{ $t('views.OnboardingWizard.pipeline_label') }} <strong>{{ wizardState.pipelineName }}</strong> {{ $t('views.OnboardingWizard.created_suffix') }}
          </li>
        </ul>
        <div class="flex items-center justify-center gap-3 pt-4">
          <Button v-if="wizardState.createdPipelineId" :disabled="runningPipeline" data-testid="onboarding-wizard-run-pipeline-now" @click="runPipeline">
            {{ runningPipeline ? $t('views.OnboardingWizard.starting') : $t('views.OnboardingWizard.run_pipeline_now') }}
          </Button>
          <router-link
            :to="{ name: 'dashboard' }"
            data-testid="onboarding-wizard-go-to-dashboard"
            class="rounded-lg border border-input bg-background px-6 py-2.5 text-sm font-medium hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {{ $t('views.OnboardingWizard.go_to_dashboard') }}
          </router-link>
        </div>
        <div v-if="emptyRunWarning" class="rounded-lg bg-warning/10 border border-warning/30 p-3 text-sm text-warning" data-testid="onboarding-wizard-run-empty-warning">
          {{ emptyRunWarning }}
        </div>
        <div v-if="runResult" class="rounded-lg bg-success/10 p-3 text-sm text-success">
          {{ $t('views.OnboardingWizard.pipeline_started') }} <router-link :to="{ name: 'dashboard' }" class="underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">{{ $t('views.OnboardingWizard.view_runs_on_dashboard') }}</router-link>.
        </div>
        <div v-if="pipelineRunError" class="text-sm text-destructive">{{ pipelineRunError }}</div>
      </div>
    </div>

    <div v-if="currentStep < 6" class="flex items-center justify-between">
      <div>
        <button type="button"
          v-if="currentStep > 0"
          data-testid="onboarding-wizard-previous"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          @click="prevStep"
        >
          {{ $t('views.OnboardingWizard.previous') }}
        </button>
      </div>
      <div class="flex items-center gap-3">
        <button type="button"
          v-if="currentStep > 0 && currentStep < 5"
          data-testid="onboarding-wizard-skip-to-end"
          class="text-sm text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          @click="skipToEnd"
        >
          {{ $t('views.OnboardingWizard.skip_to_end') }}
        </button>
        <Button :disabled="!canProceed" data-testid="onboarding-wizard-next" @click="nextStep">
          {{ currentStep === 5 ? $t('views.OnboardingWizard.finish') : $t('views.OnboardingWizard.next') }}
        </Button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Check, Layers } from '@lucide/vue'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/client'
import PageHeader from '../components/shared/PageHeader.vue'
import { formatApiError } from '../lib/api/formatError'
import Button from 'primevue/button'
import Select from 'primevue/select'

type ConnectorItem = components['schemas']['ConnectorResponse']

interface DraftSchema {
  name: string
  description: string | null
  fields: Array<{name: string; type: string; required: boolean; description: string | null}>
}

const { t } = useI18n()

const steps = computed(() => [
  { title: t('views.OnboardingWizard.welcome'), subtitle: t('views.OnboardingWizard.get_started_with_sdlc_onboarding') },
  { title: t('views.OnboardingWizard.connect_tools'), subtitle: t('views.OnboardingWizard.select_a_connector_instance_github_jira_filesystem') },
  { title: t('views.OnboardingWizard.run_inference'), subtitle: t('views.OnboardingWizard.infer_a_schema_from_your_connected_data_source') },
  { title: t('views.OnboardingWizard.review_schemas'), subtitle: t('views.OnboardingWizard.review_edit_and_confirm_the_inferred_schema') },
  { title: t('views.OnboardingWizard.browse_library'), subtitle: t('views.OnboardingWizard.find_compatible_agents_and_blueprints') },
  { title: t('views.OnboardingWizard.wire_pipeline'), subtitle: t('views.OnboardingWizard.name_describe_and_create_your_pipeline') },
  { title: t('views.OnboardingWizard.done'), subtitle: t('views.OnboardingWizard.your_pipeline_is_ready_to_run') },
])

const currentStep = ref(0)

const wizardState = reactive({
  connectorId: '',
  connectorName: '',
  resourceType: '',
  sampleQuery: '',
  draftSchema: null as DraftSchema | null,
  rawDefinitionJson: null as Record<string, unknown> | null,
  publishedSchemaId: null as string | null,
  selectedLibraryItemId: null as string | null,
  pipelineName: '',
  pipelineDescription: '',
  createdPipelineId: null as string | null,
  createdPipelineName: null as string | null,
})

const connectors = ref<ConnectorItem[]>([])
const loadingConnectors = ref(false)
const connectorsError = ref<string | null>(null)

const inferring = ref(false)
const inferError = ref<string | null>(null)

const savingSchema = ref(false)
const schemaSaveError = ref<string | null>(null)
const editableSchemaName = ref('')
const editableSchemaDescription = ref('')

interface LibraryPrimitive {
  id: string
  primitive_type: string
  name: string
  description: string | null
  tags: string[]
  visibility: string
}
const libraryItems = ref<LibraryPrimitive[]>([])
const loadingLibrary = ref(false)
const libraryError = ref<string | null>(null)
const librarySearch = ref('')
const libraryTypeFilter = ref('')

const creatingPipeline = ref(false)
const pipelineCreateError = ref<string | null>(null)

const runningPipeline = ref(false)
const pipelineRunError = ref<string | null>(null)
const runResult = ref<string | null>(null)
const confirmEmptyRun = ref(false)
const emptyRunWarning = ref<string | null>(null)

const canProceed = computed(() => {
  switch (currentStep.value) {
    case 0: return true
    case 1: return !!wizardState.connectorId
    case 2: return !!wizardState.draftSchema
    case 3: return !!wizardState.publishedSchemaId
    case 4: return true
    case 5: return !!wizardState.createdPipelineId
    default: return false
  }
})

const filteredLibraryItems = computed(() => {
  let items = libraryItems.value
  if (libraryTypeFilter.value) {
    items = items.filter(i => i.primitive_type === libraryTypeFilter.value)
  }
  if (librarySearch.value) {
    const q = librarySearch.value.toLowerCase()
    items = items.filter(i => i.name.toLowerCase().includes(q) || (i.description && i.description.toLowerCase().includes(q)))
  }
  return items
})

const selectedLibraryItem = computed(() => {
  if (!wizardState.selectedLibraryItemId) return null
  return libraryItems.value.find(i => i.id === wizardState.selectedLibraryItemId) ?? null
})

function stepCircleClass(i: number): string {
  if (i < currentStep.value) return 'bg-primary text-primary-foreground'
  if (i === currentStep.value) return 'border-2 border-primary text-primary'
  return 'border-2 border-border text-muted-foreground'
}

async function loadConnectors() {
  loadingConnectors.value = true
  connectorsError.value = null
  try {
    const { data, error: err } = await api.GET('/api/v1/connectors')
    if (err) {
      connectorsError.value = `${t('views.OnboardingWizard.failed_to_load_connectors')}${formatApiError(err)}`
    } else if (data) {
      connectors.value = data.items
    }
  } catch (e: unknown) {
    connectorsError.value = `${t('views.OnboardingWizard.failed_to_load_connectors')}${formatApiError(e)}`
  } finally {
    loadingConnectors.value = false
  }
}

function extractFieldsFromDefinition(def: Record<string, unknown>): DraftSchema['fields'] {
  const properties = (def.properties as Record<string, unknown>) || {}
  const required = (def.required as string[]) || []
  return Object.entries(properties).map(([name, schema]) => {
    const s = schema as Record<string, unknown>
    return {
      name,
      type: (s.type as string) || 'string',
      required: required.includes(name),
      description: (s.description as string) || null,
    }
  })
}

async function inferSchema() {
  if (!wizardState.connectorId || !wizardState.resourceType.trim()) return
  inferring.value = true
  inferError.value = null
  wizardState.draftSchema = null
  wizardState.rawDefinitionJson = null
  try {
    const { data, error: err } = await api.POST('/api/v1/schemas/infer', {
      body: {
        connector_instance_id: wizardState.connectorId,
        sample_query: {
          resource: wizardState.resourceType.trim(),
          filters: {},
          limit: 200,
        },
      },
    })
    if (err) {
      inferError.value = `${t('views.OnboardingWizard.schema_inference_failed')}${formatApiError(err)}`
    } else if (data) {
      wizardState.rawDefinitionJson = data.definition_json
      wizardState.draftSchema = {
        name: data.suggestion_name,
        description: data.suggestion_description ?? null,
        fields: extractFieldsFromDefinition(data.definition_json),
      }
      editableSchemaName.value = data.suggestion_name
      editableSchemaDescription.value = data.suggestion_description ?? ''
    }
  } catch (e: unknown) {
    inferError.value = `${t('views.OnboardingWizard.schema_inference_failed')}${formatApiError(e)}`
  } finally {
    inferring.value = false
  }
}

async function saveSchema() {
  if (!wizardState.draftSchema) return
  savingSchema.value = true
  schemaSaveError.value = null
  try {
    const { data: schemaData, error: schemaErr } = await api.POST('/api/v1/schemas', {
      body: {
        name: editableSchemaName.value,
        description: editableSchemaDescription.value || null,
      },
    })
    if (schemaErr) {
      schemaSaveError.value = `${t('views.OnboardingWizard.save_failed')}${formatApiError(schemaErr)}`
      return
    }
    if (!schemaData) {
      schemaSaveError.value = t('views.OnboardingWizard.save_failed_no_response')
      return
    }

    const { error: versionErr } = await api.POST('/api/v1/schemas/{schema_id}/versions', {
      params: { path: { schema_id: schemaData.id } },
      body: {
        version: 'v1',
        version_number: 1,
        definition_json: wizardState.rawDefinitionJson || { type: 'object', properties: {} },
        published: true,
      },
    })
    if (versionErr) {
      schemaSaveError.value = `${t('views.OnboardingWizard.save_failed')}${formatApiError(versionErr)}`
      return
    }

    wizardState.publishedSchemaId = schemaData.id
  } catch (e: unknown) {
    schemaSaveError.value = `${t('views.OnboardingWizard.save_failed')}${formatApiError(e)}`
  } finally {
    savingSchema.value = false
  }
}

async function loadLibrary() {
  loadingLibrary.value = true
  libraryError.value = null
  try {
    const { data, error: err } = await api.GET('/api/v1/libraries', { params: { query: { page: 1, page_size: 50 } } })
    if (err) throw err
    libraryItems.value = data!.items
  } catch (e) {
    libraryError.value = e instanceof Error ? e.message : t('views.OnboardingWizard.failed_to_load_library')
  } finally {
    loadingLibrary.value = false
  }
}

async function createPipeline() {
  if (!wizardState.pipelineName.trim()) return
  creatingPipeline.value = true
  pipelineCreateError.value = null
  try {
    const { data, error: err } = await api.POST('/api/v1/pipelines', {
      body: {
        name: wizardState.pipelineName.trim(),
        description: wizardState.pipelineDescription.trim() || null,
        visibility: 'org',
        max_concurrent_runs: 10,
        lock_wait_timeout_seconds: 30,
        node_timeout_seconds: 300,
        default_autonomy_level: 'balanced',
        max_duration_seconds: 3600,
        stale_run_timeout_minutes: 30,
      },
    })
    if (err) throw err
    wizardState.createdPipelineId = data!.id
    wizardState.createdPipelineName = data!.name
  } catch (e) {
    pipelineCreateError.value = e instanceof Error ? e.message : t('views.OnboardingWizard.failed_to_create_pipeline')
  } finally {
    creatingPipeline.value = false
  }
}

async function runPipeline() {
  if (!wizardState.createdPipelineId) return
  if (!confirmEmptyRun.value) {
    confirmEmptyRun.value = true
    emptyRunWarning.value = t('views.OnboardingWizard.no_input_empty_run_warning')
    return
  }
  emptyRunWarning.value = null
  confirmEmptyRun.value = false
  runningPipeline.value = true
  pipelineRunError.value = null
  runResult.value = null
  try {
    const { error: err } = await api.POST('/api/v1/runs', {
      body: {
        pipeline_id: wizardState.createdPipelineId,
        input_payload: {},
      },
    })
    if (err) throw err
    runResult.value = t('views.OnboardingWizard.pipeline_started')
  } catch (e) {
    pipelineRunError.value = e instanceof Error ? e.message : t('views.OnboardingWizard.failed_to_start_pipeline')
  } finally {
    runningPipeline.value = false
  }
}

function nextStep() {
  if (currentStep.value < steps.value.length - 1) {
    currentStep.value++
  }
}

function prevStep() {
  if (currentStep.value > 0) {
    currentStep.value--
  }
}

function skipToEnd() {
  currentStep.value = steps.value.length - 1
}

watch(() => wizardState.pipelineDescription, () => {
  if (confirmEmptyRun.value) {
    confirmEmptyRun.value = false
    emptyRunWarning.value = null
  }
})

watch(currentStep, (step) => {
  if (step === 1 && connectors.value.length === 0 && !loadingConnectors.value) {
    loadConnectors()
  }
  if (step === 4 && libraryItems.value.length === 0 && !loadingLibrary.value) {
    loadLibrary()
  }
})

onMounted(() => {
  loadConnectors()
})
</script>
