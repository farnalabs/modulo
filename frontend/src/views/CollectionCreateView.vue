<template>
  <div class="page-wide py-8">
    <PageHeader :title="$t('views.LibraryView.collection_create_title')" />
    <div class="max-w-2xl mx-auto mt-6 space-y-4">
      <div
        v-if="error"
        class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive"
        role="alert"
      >
        {{ error }}
      </div>
      <form class="space-y-4" @submit.prevent="submit">
        <div>
          <label for="collection-name" class="block text-sm font-medium mb-1">
            {{ $t('views.LibraryView.collection_name') }}
          </label>
          <input
            id="collection-name"
            v-model="name"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            required
          />
        </div>
        <div>
          <label for="collection-slug" class="block text-sm font-medium mb-1">
            {{ $t('views.LibraryView.collection_slug') }}
          </label>
          <input
            id="collection-slug"
            v-model="slug"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            pattern="[a-z0-9-]+"
            required
          />
        </div>
        <div>
          <label for="collection-description" class="block text-sm font-medium mb-1">
            {{ $t('views.LibraryView.collection_description') }}
          </label>
          <textarea
            id="collection-description"
            v-model="description"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            rows="3"
          />
        </div>
        <div>
          <label for="collection-visibility" class="block text-sm font-medium mb-1">
            {{ $t('views.LibraryView.collection_visibility') }}
          </label>
          <select
            id="collection-visibility"
            v-model="visibility"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
          >
            <option value="org">{{ $t('views.LibraryView.collection_visibility_org') }}</option>
            <option value="team">{{ $t('views.LibraryView.collection_visibility_team') }}</option>
          </select>
        </div>
        <div>
          <div class="flex items-center justify-between mb-1">
            <span class="block text-sm font-medium">
              {{ $t('views.LibraryView.collection_manifest_pins') }}
            </span>
            <Button type="button" class="px-3 py-1 text-sm" @click="addPin">
              {{ $t('views.LibraryView.collection_add_pin') }}
            </Button>
          </div>
          <div v-if="pins.length === 0" class="text-sm text-muted-foreground">
            {{ $t('views.LibraryView.collection_empty_pins') }}
          </div>
          <div v-for="(pin, i) in pins" :key="i" class="flex gap-2 mb-2">
            <label class="sr-only" :for="`pin-slug-${i}`">
              {{ $t('views.LibraryView.collection_pin_slug') }}
            </label>
            <input
              :id="`pin-slug-${i}`"
              v-model="pin.slug"
              :placeholder="$t('views.LibraryView.collection_pin_slug')"
              class="flex-1 rounded-lg border border-input bg-background px-3 py-2 text-sm"
            />
            <label class="sr-only" :for="`pin-version-${i}`">
              {{ $t('views.LibraryView.collection_pin_version') }}
            </label>
            <input
              :id="`pin-version-${i}`"
              v-model="pin.version"
              :placeholder="$t('views.LibraryView.collection_pin_version')"
              class="w-32 rounded-lg border border-input bg-background px-3 py-2 text-sm"
            />
            <Button type="button" class="px-3 py-1 text-sm" @click="removePin(i)">
              {{ $t('views.LibraryView.collection_remove_pin') }}
            </Button>
          </div>
        </div>
        <div class="flex justify-end gap-2">
          <Button as="router-link" to="/library?type=library_collection" class="px-4 py-1.5">
            {{ $t('views.LibraryView.collection_cancel') }}
          </Button>
          <Button
            type="submit"
            :disabled="saving"
            class="px-4 py-1.5"
            data-testid="collection-create-submit"
          >
            {{ $t('views.LibraryView.collection_create_submit') }}
          </Button>
        </div>
      </form>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import Button from 'primevue/button'
import PageHeader from '../components/shared/PageHeader.vue'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'

const router = useRouter()

const name = ref('')
const slug = ref('')
const description = ref('')
const visibility = ref('org')
const pins = ref<{ slug: string; version: string }[]>([])
const saving = ref(false)
const error = ref<string | null>(null)

function addPin() {
  pins.value.push({ slug: '', version: '' })
}

function removePin(i: number) {
  pins.value.splice(i, 1)
}

async function submit() {
  saving.value = true
  error.value = null
  try {
    const { data, error: err } = await api.POST('/api/v1/libraries/collections', {
      body: {
        name: name.value,
        slug: slug.value,
        description: description.value || null,
        visibility: visibility.value,
        manifest_pins: pins.value
          .filter((p) => p.slug && p.version)
          .map((p) => ({ slug: p.slug, version: p.version })),
      },
    })
    if (err) {
      error.value = formatApiError(err)
      return
    }
    const created = data as unknown as { id: string }
    router.push({ name: 'library-collection-detail', params: { id: created.id } })
  } catch (e) {
    error.value = formatApiError(e)
  } finally {
    saving.value = false
  }
}
</script>
