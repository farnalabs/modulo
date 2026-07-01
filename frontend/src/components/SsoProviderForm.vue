<template>
  <div class="space-y-4">
    <div>
      <label class="mb-1 block text-sm font-medium">Provider Type</label>
      <div class="flex gap-2">
        <button
          class="flex-1 rounded-lg border px-4 py-2 text-sm font-medium transition-colors"
          :class="
            data.provider_type === 'oidc'
              ? 'border-primary bg-primary/10 text-primary'
              : 'border-input bg-background hover:bg-accent'
          "
          @click="
            emitUpdate({
              ...data,
              provider_type: 'oidc',
              client_secret: '',
              metadata_url: '',
              metadata_xml: '',
              entity_id: '',
            })
          "
        >
          OpenID Connect (OIDC)
        </button>
        <button
          class="flex-1 rounded-lg border px-4 py-2 text-sm font-medium transition-colors"
          :class="
            data.provider_type === 'saml'
              ? 'border-primary bg-primary/10 text-primary'
              : 'border-input bg-background hover:bg-accent'
          "
          @click="
            emitUpdate({
              ...data,
              provider_type: 'saml',
              client_id: '',
              client_secret: '',
              discovery_url: '',
              scopes: '',
            })
          "
        >
          SAML 2.0
        </button>
      </div>
    </div>

    <div>
      <label class="mb-1 block text-sm font-medium">Name</label>
      <input
        :value="data.name"
        type="text"
        class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        placeholder="e.g. Google Workspace"
        @input="
          emitUpdate({
            ...data,
            name: ($event.target as HTMLInputElement).value,
          })
        "
      />
    </div>

    <template v-if="data.provider_type === 'oidc'">
      <div>
        <label class="mb-1 block text-sm font-medium">Client ID</label>
        <input
          :value="data.client_id"
          type="text"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          placeholder="e.g. 1234567890-abc123.apps.googleusercontent.com"
          @input="
            emitUpdate({
              ...data,
              client_id: ($event.target as HTMLInputElement).value,
            })
          "
        />
      </div>
      <div>
        <label class="mb-1 block text-sm font-medium">Client Secret</label>
        <input
          :value="data.client_secret"
          type="password"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          placeholder="Leave blank to keep existing"
          @input="
            emitUpdate({
              ...data,
              client_secret: ($event.target as HTMLInputElement).value,
            })
          "
        />
      </div>
      <div>
        <label class="mb-1 block text-sm font-medium">Discovery URL</label>
        <input
          :value="data.discovery_url"
          type="url"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          placeholder="https://accounts.google.com/.well-known/openid-configuration"
          @input="
            emitUpdate({
              ...data,
              discovery_url: ($event.target as HTMLInputElement).value,
            })
          "
        />
      </div>
      <div>
        <label class="mb-1 block text-sm font-medium">Scopes</label>
        <input
          :value="data.scopes"
          type="text"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          placeholder="openid, profile, email"
          @input="
            emitUpdate({
              ...data,
              scopes: ($event.target as HTMLInputElement).value,
            })
          "
        />
        <p class="mt-1 text-xs text-muted-foreground">
          Comma-separated list of scopes
        </p>
      </div>
    </template>

    <template v-if="data.provider_type === 'saml'">
      <div>
        <label class="mb-1 block text-sm font-medium">Metadata URL</label>
        <input
          :value="data.metadata_url"
          type="url"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          placeholder="https://idp.example.com/metadata.xml"
          @input="
            emitUpdate({
              ...data,
              metadata_url: ($event.target as HTMLInputElement).value,
            })
          "
        />
      </div>
      <div>
        <label class="mb-1 block text-sm font-medium">Metadata XML</label>
        <textarea
          :value="data.metadata_xml"
          rows="4"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring font-mono"
          placeholder="Paste IdP metadata XML here..."
          @input="
            emitUpdate({
              ...data,
              metadata_xml: ($event.target as HTMLTextAreaElement).value,
            })
          "
        />
      </div>
      <div>
        <label class="mb-1 block text-sm font-medium">Entity ID</label>
        <input
          :value="data.entity_id"
          type="text"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          placeholder="urn:example:entity"
          @input="
            emitUpdate({
              ...data,
              entity_id: ($event.target as HTMLInputElement).value,
            })
          "
        />
      </div>
    </template>

    <div>
      <label class="mb-1 block text-sm font-medium">Auto-Provision</label>
      <div class="flex items-center gap-2">
        <label
          class="relative inline-flex cursor-pointer items-center"
          @click.prevent="
            emitUpdate({ ...data, auto_provision: !data.auto_provision })
          "
        >
          <div
            class="h-6 w-11 rounded-full transition-colors"
            :class="data.auto_provision ? 'bg-primary' : 'bg-input'"
          >
            <div
              class="h-5 w-5 rounded-full bg-white shadow-sm transition-transform"
              :class="
                data.auto_provision
                  ? 'translate-x-[1.375rem]'
                  : 'translate-x-0.5'
              "
              style="margin-top: 2px"
            />
          </div>
        </label>
        <span class="text-sm text-muted-foreground">
          {{
            data.auto_provision
              ? "Users are auto-provisioned on first login"
              : "Manual user creation required"
          }}
        </span>
      </div>
    </div>

    <div>
      <label class="mb-1 block text-sm font-medium">Default Role</label>
      <select
        :value="data.default_role"
        class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        @change="
          emitUpdate({
            ...data,
            default_role: ($event.target as HTMLSelectElement).value,
          })
        "
      >
        <option value="runner">Runner</option>
        <option value="operator">Operator</option>
      </select>
    </div>

    <div v-if="error" class="text-sm text-destructive">{{ error }}</div>

    <div class="flex items-center gap-2">
      <button
        :disabled="!data.name.trim() || saving"
        class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
        @click="$emit('submit')"
      >
        {{ saving ? savingLabel : submitLabel }}
      </button>
      <button
        class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
        @click="$emit('cancel')"
      >
        Cancel
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
interface SsoFormState {
  provider_type: string;
  name: string;
  client_id: string;
  client_secret: string;
  discovery_url: string;
  metadata_url: string;
  metadata_xml: string;
  entity_id: string;
  scopes: string;
  auto_provision: boolean;
  default_role: string;
}

defineProps<{
  data: SsoFormState;
  saving: boolean;
  submitLabel: string;
  savingLabel: string;
  error: string | null;
}>();

const emit = defineEmits<{
  "update:data": [value: SsoFormState];
  submit: [];
  cancel: [];
}>();

function emitUpdate(updated: SsoFormState) {
  emit("update:data", updated);
}
</script>
