<template>
  <div class="mx-auto max-w-4xl space-y-8 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">SDLC Onboarding</h1>
      <p class="mt-1 text-muted-foreground">Guided setup wizard &mdash; connect tools, infer schemas, browse the library, and wire your first pipeline</p>
    </header>

    <div class="flex items-center justify-center gap-0">
      <template v-for="(_, i) in steps" :key="i">
        <div class="flex items-center">
          <div
            class="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-xs font-medium transition-colors"
            :class="stepCircleClass(i)"
          >
            <svg v-if="i < currentStep" class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="m5 12 5 5 9-9"/></svg>
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
          <svg class="h-8 w-8 text-primary" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>
        </div>
        <p class="text-muted-foreground">
          This wizard will guide you through <strong>6 quick steps</strong> to get your first pipeline running:
          connect your tools, infer a data schema, browse the library for compatible agents and blueprints,
          and wire everything together.
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
        <div v-if="loadingConnectors" class="flex items-center justify-center py-8">
          <div class="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
        </div>
        <div v-else-if="connectorsError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive">{{ connectorsError }}</div>
        <div v-else-if="connectors.length === 0" class="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
          No connectors found. <a href="/settings/connectors" data-testid="onboarding-wizard-create-connector" class="text-primary underline">Create one</a> first, then come back.
        </div>
        <div v-else class="space-y-2">
          <label class="mb-1 block text-sm font-medium">Select a connector instance</label>
          <div
            v-for="c in connectors"
            :key="c.id"
            data-testid="onboarding-wizard-connector-card"
            class="flex cursor-pointer items-center gap-3 rounded-lg border p-4 transition-colors hover:bg-accent"
            :class="wizardState.connectorId === c.id ? 'border-primary bg-primary/5' : 'border-input'"
            @click="wizardState.connectorId = c.id; wizardState.connectorName = c.name"
          >
            <div class="flex h-5 w-5 shrink-0 items-center justify-center rounded-full border-2" :class="wizardState.connectorId === c.id ? 'border-primary' : 'border-input'">
              <div v-if="wizardState.connectorId === c.id" class="h-2.5 w-2.5 rounded-full bg-primary" />
            </div>
            <div>
              <p class="text-sm font-medium">{{ c.name }}</p>
              <p class="text-xs text-muted-foreground">{{ c.connector_type }}{{ c.description ? ' — ' + c.description : '' }}</p>
            </div>
          </div>
        </div>
      </div>

      <!-- Step 2: Run Inference -->
      <div v-if="currentStep === 2" class="space-y-4">
        <div class="rounded-lg bg-muted p-3 text-sm text-muted-foreground">
          Connector: <strong>{{ wizardState.connectorName }}</strong>
        </div>
        <div>
          <label class="mb-1 block text-sm font-medium">Resource type</label>
          <input
            v-model="wizardState.resourceType"
            type="text"
            data-testid="onboarding-wizard-resource-type"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            placeholder="e.g. issues, repositories, pull_requests"
          />
        </div>
        <div>
          <label class="mb-1 block text-sm font-medium">Sample query <span class="text-muted-foreground">(optional)</span></label>
          <textarea
            v-model="wizardState.sampleQuery"
            rows="2"
            data-testid="onboarding-wizard-sample-query"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            placeholder="e.g. state=open&sort=updated"
          />
        </div>
        <div class="flex items-center gap-2">
          <button
            :disabled="!wizardState.resourceType.trim() || inferring"
            data-testid="onboarding-wizard-infer-schema"
            class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            @click="inferSchema"
          >
            {{ inferring ? 'Inferring...' : 'Infer Schema' }}
          </button>
        </div>
        <div v-if="inferError" class="text-sm text-destructive">{{ inferError }}</div>

        <div v-if="wizardState.draftSchema" class="rounded-lg border bg-card p-4">
          <h3 class="mb-3 text-sm font-semibold">Draft: {{ wizardState.draftSchema.name }}</h3>
          <p v-if="wizardState.draftSchema.description" class="mb-3 text-xs text-muted-foreground">{{ wizardState.draftSchema.description }}</p>
          <table v-if="wizardState.draftSchema.fields.length > 0" class="w-full text-sm">
            <thead>
              <tr class="border-b text-left text-muted-foreground">
                <th class="pb-2 font-medium">Name</th>
                <th class="pb-2 font-medium">Type</th>
                <th class="pb-2 font-medium">Required</th>
                <th class="pb-2 font-medium">Description</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="field in wizardState.draftSchema.fields" :key="field.name" class="border-b last:border-0">
                <td class="py-2 font-mono text-xs">{{ field.name }}</td>
                <td class="py-2 font-mono text-xs text-muted-foreground">{{ field.type }}</td>
                <td class="py-2">
                  <span class="inline-block rounded px-1.5 py-0.5 text-xs font-medium" :class="field.required ? 'bg-primary/10 text-primary' : 'bg-muted text-muted-foreground'">
                    {{ field.required ? 'yes' : 'no' }}
                  </span>
                </td>
                <td class="py-2 text-xs text-muted-foreground">{{ field.description ?? '—' }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="text-sm text-muted-foreground">No fields inferred.</p>
        </div>
      </div>

      <!-- Step 3: Review Schemas -->
      <div v-if="currentStep === 3" class="space-y-4">
        <div v-if="!wizardState.draftSchema" class="py-8 text-center text-sm text-muted-foreground">
          No schema inferred yet. Go back to step 3 and run inference first.
        </div>
        <template v-else>
          <div class="flex items-center gap-4">
            <div>
              <label class="mb-1 block text-sm font-medium">Schema name</label>
              <input
                v-model="editableSchemaName"
                type="text"
                data-testid="onboarding-wizard-schema-name"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </div>
            <div class="flex-1">
              <label class="mb-1 block text-sm font-medium">Description</label>
              <input
                v-model="editableSchemaDescription"
                type="text"
                data-testid="onboarding-wizard-schema-description"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </div>
          </div>
          <div>
            <label class="mb-2 block text-sm font-medium">Fields <span class="text-muted-foreground">(read-only — re-infer to change)</span></label>
            <table class="w-full text-sm">
              <thead>
                <tr class="border-b text-left text-muted-foreground">
                  <th class="pb-2 font-medium">Name</th>
                  <th class="pb-2 font-medium">Type</th>
                  <th class="pb-2 font-medium">Required</th>
                  <th class="pb-2 font-medium">Description</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="field in wizardState.draftSchema.fields" :key="field.name" class="border-b last:border-0">
                  <td class="py-2 font-mono text-xs">{{ field.name }}</td>
                  <td class="py-2 font-mono text-xs text-muted-foreground">{{ field.type }}</td>
                  <td class="py-2">
                    <span class="inline-block rounded px-1.5 py-0.5 text-xs font-medium" :class="field.required ? 'bg-primary/10 text-primary' : 'bg-muted text-muted-foreground'">{{ field.required ? 'yes' : 'no' }}</span>
                  </td>
                  <td class="py-2 text-xs text-muted-foreground">{{ field.description ?? '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div class="flex items-center gap-2">
            <button
              :disabled="savingSchema"
              data-testid="onboarding-wizard-confirm-save-schema"
              class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              @click="saveSchema"
            >
              {{ savingSchema ? 'Saving...' : 'Confirm & Save Schema' }}
            </button>
          </div>
          <div v-if="schemaSaveError" class="text-sm text-destructive">{{ schemaSaveError }}</div>
          <div v-if="wizardState.publishedSchemaId" class="rounded-lg bg-success/10 p-3 text-sm text-success">
            Schema "{{ editableSchemaName }}" saved.
          </div>
        </template>
      </div>

      <!-- Step 4: Browse Library -->
      <div v-if="currentStep === 4" class="space-y-4">
        <div v-if="loadingLibrary" class="flex items-center justify-center py-8">
          <div class="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
        </div>
        <div v-else-if="libraryError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive">{{ libraryError }}</div>
        <div v-else-if="libraryItems.length === 0" class="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
          No library items available. You can skip this step and proceed to create a pipeline manually.
        </div>
        <div v-else class="space-y-2">
          <div class="flex items-center gap-3">
            <input
              v-model="librarySearch"
              type="text"
              placeholder="Filter items..."
              data-testid="onboarding-wizard-library-search"
              class="flex-1 rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
            <select
              v-model="libraryTypeFilter"
              data-testid="onboarding-wizard-library-type-filter"
              class="rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <option value="">All types</option>
              <option value="pipeline_template">Pipeline Templates</option>
              <option value="agent">Agents</option>
              <option value="schema">Schemas</option>
              <option value="integration">Integrations</option>
            </select>
          </div>
          <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div
              v-for="item in filteredLibraryItems"
              :key="item.id"
              data-testid="onboarding-wizard-library-item"
              class="cursor-pointer rounded-lg border p-4 transition-colors hover:bg-accent"
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
                  <svg class="h-5 w-5 text-primary" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m5 12 5 5 9-9"/></svg>
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
          <label class="mb-1 block text-sm font-medium">Pipeline name</label>
          <input
            v-model="wizardState.pipelineName"
            type="text"
            data-testid="onboarding-wizard-pipeline-name"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            placeholder="My Onboarding Pipeline"
          />
        </div>
        <div>
          <label class="mb-1 block text-sm font-medium">Description</label>
          <textarea
            v-model="wizardState.pipelineDescription"
            rows="3"
            data-testid="onboarding-wizard-pipeline-description"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            placeholder="What does this pipeline do?"
          />
        </div>
        <div v-if="wizardState.selectedLibraryItemId && selectedLibraryItem" class="rounded-lg bg-muted p-3">
          <p class="text-xs text-muted-foreground">Selected library item</p>
          <p class="text-sm font-medium">{{ selectedLibraryItem.name }}</p>
          <p v-if="selectedLibraryItem.description" class="text-xs text-muted-foreground">{{ selectedLibraryItem.description }}</p>
        </div>
        <div class="flex items-center gap-2">
          <button
            :disabled="!wizardState.pipelineName.trim() || creatingPipeline"
            data-testid="onboarding-wizard-create-pipeline"
            class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            @click="createPipeline"
          >
            {{ creatingPipeline ? 'Creating...' : 'Create Pipeline' }}
          </button>
        </div>
        <div v-if="pipelineCreateError" class="text-sm text-destructive">{{ pipelineCreateError }}</div>
        <div v-if="wizardState.createdPipelineId" class="rounded-lg bg-success/10 p-3 text-sm text-success">
          Pipeline "{{ wizardState.pipelineName }}" created.
        </div>
      </div>

      <!-- Step 6: Done -->
      <div v-if="currentStep === 6" class="space-y-6 py-4 text-center">
        <div class="mx-auto flex h-16 w-16 items-center justify-center rounded-full bg-success/10">
          <svg class="h-8 w-8 text-success" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m5 12 5 5 9-9"/></svg>
        </div>
        <h3 class="text-2xl font-bold">You're all set!</h3>
        <p class="text-muted-foreground">
          Your pipeline <strong>{{ wizardState.pipelineName }}</strong> has been created
          {{ wizardState.createdPipelineId ? 'and is ready to run' : '' }}.
          Here's what was accomplished:
        </p>
        <ul class="mx-auto max-w-sm space-y-2 text-left text-sm">
          <li v-if="wizardState.connectorName" class="flex items-center gap-2 text-muted-foreground">
            <svg class="h-4 w-4 shrink-0 text-success" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m5 12 5 5 9-9"/></svg>
            Connected <strong>{{ wizardState.connectorName }}</strong>
          </li>
          <li v-if="wizardState.draftSchema" class="flex items-center gap-2 text-muted-foreground">
            <svg class="h-4 w-4 shrink-0 text-success" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m5 12 5 5 9-9"/></svg>
            Inferred schema <strong>{{ wizardState.draftSchema.name }}</strong>
          </li>
          <li v-if="wizardState.publishedSchemaId" class="flex items-center gap-2 text-muted-foreground">
            <svg class="h-4 w-4 shrink-0 text-success" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m5 12 5 5 9-9"/></svg>
            Published to schema registry
          </li>
          <li v-if="wizardState.selectedLibraryItemId" class="flex items-center gap-2 text-muted-foreground">
            <svg class="h-4 w-4 shrink-0 text-success" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m5 12 5 5 9-9"/></svg>
            Selected library item
          </li>
          <li v-if="wizardState.createdPipelineId" class="flex items-center gap-2 text-muted-foreground">
            <svg class="h-4 w-4 shrink-0 text-success" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m5 12 5 5 9-9"/></svg>
            Pipeline <strong>{{ wizardState.pipelineName }}</strong> created
          </li>
        </ul>
        <div class="flex items-center justify-center gap-3 pt-4">
          <button
            v-if="wizardState.createdPipelineId"
            :disabled="runningPipeline"
            data-testid="onboarding-wizard-run-pipeline-now"
            class="rounded-lg bg-primary px-6 py-2.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            @click="runPipeline"
          >
            {{ runningPipeline ? 'Starting...' : 'Run Pipeline Now' }}
          </button>
          <router-link
            :to="{ name: 'dashboard' }"
            data-testid="onboarding-wizard-go-to-dashboard"
            class="rounded-lg border border-input bg-background px-6 py-2.5 text-sm font-medium hover:bg-accent"
          >
            Go to Dashboard
          </router-link>
        </div>
        <div v-if="runResult" class="rounded-lg bg-success/10 p-3 text-sm text-success">
          Pipeline started! <router-link :to="{ name: 'dashboard' }" class="underline">View runs on dashboard</router-link>.
        </div>
        <div v-if="pipelineRunError" class="text-sm text-destructive">{{ pipelineRunError }}</div>
      </div>
    </div>

    <div v-if="currentStep < 6" class="flex items-center justify-between">
      <div>
        <button
          v-if="currentStep > 0"
          data-testid="onboarding-wizard-previous"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
          @click="prevStep"
        >
          Previous
        </button>
      </div>
      <div class="flex items-center gap-3">
        <button
          v-if="currentStep > 0 && currentStep < 5"
          data-testid="onboarding-wizard-skip-to-end"
          class="text-sm text-muted-foreground hover:text-foreground"
          @click="skipToEnd"
        >
          Skip to end
        </button>
        <button
          :disabled="!canProceed"
          data-testid="onboarding-wizard-next"
          class="rounded-lg bg-primary px-6 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          @click="nextStep"
        >
          {{ currentStep === 5 ? 'Finish' : 'Next' }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { api } from '../lib/api/client'
import { useApi } from '../composables/useApi'
import type { components } from '../lib/api/client'

type ConnectorItem = components['schemas']['ConnectorItem']
type SchemaInferResponse = components['schemas']['SchemaInferResponse']
const { get, post } = useApi()

const steps = [
  { title: 'Welcome', subtitle: 'Get started with SDLC onboarding' },
  { title: 'Connect Tools', subtitle: 'Select a connector instance (GitHub, Jira, filesystem)' },
  { title: 'Run Inference', subtitle: 'Infer a schema from your connected data source' },
  { title: 'Review Schemas', subtitle: 'Review, edit, and confirm the inferred schema' },
  { title: 'Browse Library', subtitle: 'Find compatible agents and blueprints' },
  { title: 'Wire Pipeline', subtitle: 'Name, describe, and create your pipeline' },
  { title: 'Done', subtitle: 'Your pipeline is ready to run' },
]

const currentStep = ref(0)

const wizardState = reactive({
  connectorId: '',
  connectorName: '',
  resourceType: '',
  sampleQuery: '',
  draftSchema: null as SchemaInferResponse | null,
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
      connectorsError.value = `Failed to load connectors: ${err}`
    } else if (data) {
      connectors.value = data.items
    }
  } catch (e: unknown) {
    connectorsError.value = `Failed to load connectors: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loadingConnectors.value = false
  }
}

async function inferSchema() {
  if (!wizardState.connectorId || !wizardState.resourceType.trim()) return
  inferring.value = true
  inferError.value = null
  wizardState.draftSchema = null
  try {
    const { data, error: err } = await api.POST('/api/v1/schemas/infer', {
      body: {
        connector_instance_id: wizardState.connectorId,
        resource_type: wizardState.resourceType.trim(),
        sample_query: wizardState.sampleQuery.trim() || null,
      },
    })
    if (err) {
      inferError.value = `Schema inference failed: ${err}`
    } else if (data) {
      wizardState.draftSchema = data
      editableSchemaName.value = data.name
      editableSchemaDescription.value = data.description ?? ''
    }
  } catch (e: unknown) {
    inferError.value = `Schema inference failed: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    inferring.value = false
  }
}

async function saveSchema() {
  if (!wizardState.draftSchema) return
  savingSchema.value = true
  schemaSaveError.value = null
  try {
    const { data, error: err } = await api.POST('/api/v1/schemas', {
      body: {
        name: editableSchemaName.value,
        description: editableSchemaDescription.value || null,
        fields: wizardState.draftSchema.fields,
      },
    })
    if (err) {
      schemaSaveError.value = `Save failed: ${err}`
    } else if (data) {
      wizardState.publishedSchemaId = data.id
    }
  } catch (e: unknown) {
    schemaSaveError.value = `Save failed: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    savingSchema.value = false
  }
}

async function loadLibrary() {
  loadingLibrary.value = true
  libraryError.value = null
  try {
    const params = new URLSearchParams({ page: '1', page_size: '50' })
    const data = await get<{ items: LibraryPrimitive[]; total: number }>(`/api/v1/libraries?${params}`)
    libraryItems.value = data.items
  } catch (e) {
    libraryError.value = e instanceof Error ? e.message : 'Failed to load library'
  } finally {
    loadingLibrary.value = false
  }
}

async function createPipeline() {
  if (!wizardState.pipelineName.trim()) return
  creatingPipeline.value = true
  pipelineCreateError.value = null
  try {
    const data = await post<{ id: string; name: string }>('/api/v1/pipelines', {
      name: wizardState.pipelineName.trim(),
      description: wizardState.pipelineDescription.trim() || null,
    })
    wizardState.createdPipelineId = data.id
    wizardState.createdPipelineName = data.name
  } catch (e) {
    pipelineCreateError.value = e instanceof Error ? e.message : 'Failed to create pipeline'
  } finally {
    creatingPipeline.value = false
  }
}

async function runPipeline() {
  if (!wizardState.createdPipelineId) return
  runningPipeline.value = true
  pipelineRunError.value = null
  runResult.value = null
  try {
    await post(`/api/v1/pipelines/${wizardState.createdPipelineId}/run`)
    runResult.value = 'Pipeline started successfully.'
  } catch (e) {
    pipelineRunError.value = e instanceof Error ? e.message : 'Failed to start pipeline'
  } finally {
    runningPipeline.value = false
  }
}

function nextStep() {
  if (currentStep.value < steps.length - 1) {
    currentStep.value++
  }
}

function prevStep() {
  if (currentStep.value > 0) {
    currentStep.value--
  }
}

function skipToEnd() {
  currentStep.value = steps.length - 1
}

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
