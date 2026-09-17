<template>
  <div class="space-y-4">
    <!-- Provider type: OIDC vs SAML -->
    <div>
      <span class="mb-1 block text-sm font-medium">{{ $t('components.SsoProviderForm.provider_type') }}</span>
      <div class="flex gap-2">
        <button type="button"
          class="flex-1 rounded-lg border px-4 py-2 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          :class="
            data.provider_type === 'oidc'
              ? 'border-primary bg-primary/10 text-primary'
              : 'border-input bg-background hover:bg-accent'
          "
          @click="
            emitUpdate({
              ...data,
              provider_type: 'oidc',
              preset: 'custom',
              client_secret: '',
              metadata_url: '',
              metadata_xml: '',
              entity_id: '',
              tenant_domain: '',
            })
          "
        >
          {{ $t('components.SsoProviderForm.oidc_label') }}
        </button>
        <button type="button"
          class="flex-1 rounded-lg border px-4 py-2 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          :class="
            data.provider_type === 'saml'
              ? 'border-primary bg-primary/10 text-primary'
              : 'border-input bg-background hover:bg-accent'
          "
          @click="
            emitUpdate({
              ...data,
              provider_type: 'saml',
              preset: 'custom',
              client_id: '',
              client_secret: '',
              discovery_url: '',
              scopes: '',
              tenant_domain: '',
            })
          "
        >
          {{ $t('components.SsoProviderForm.saml_label') }}
        </button>
      </div>
    </div>

    <!-- SSO Preset selector (OIDC only) -->
    <div v-if="data.provider_type === 'oidc' && presets.length > 0">
      <label for="sso-preset-select" class="mb-1 block text-sm font-medium">
        {{ $t('components.SsoProviderForm.sso_preset') }}
      </label>
      <div class="grid grid-cols-2 gap-2 sm:grid-cols-3">
        <button
          v-for="p in presets"
          :key="p.id"
          type="button"
          class="flex items-center gap-2 rounded-lg border px-3 py-2 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          :class="
            data.preset === p.id
              ? 'border-primary bg-primary/10 text-primary'
              : 'border-input bg-background hover:bg-accent'
          "
          :data-testid="`sso-preset-${p.id}`"
          :aria-pressed="data.preset === p.id"
          @click="onPresetChange(p.id)"
        >
          <SsoBrandMark :preset="p.id" />
          {{ p.label }}
        </button>
      </div>
    </div>

    <!-- Name -->
    <div>
      <label for="ssoproviderform-field-9" class="mb-1 block text-sm font-medium">{{ $t('components.SsoProviderForm.name') }}</label>
      <input id="ssoproviderform-field-9"
        :value="data.name"
        type="text"
        class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        :placeholder="$t('components.SsoProviderForm.eg_google_workspace')"
        @input="
          emitUpdate({
            ...data,
            name: ($event.target as HTMLInputElement).value,
          })
        "
      />
    </div>

    <!-- OIDC fields -->
    <template v-if="data.provider_type === 'oidc'">
      <!-- Client ID (always shown for OIDC) -->
      <div>
        <label for="ssoproviderform-field-8" class="mb-1 block text-sm font-medium">{{ $t('components.SsoProviderForm.client_id') }}</label>
        <input id="ssoproviderform-field-8"
          :value="data.client_id"
          type="text"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          :placeholder="$t('components.SsoProviderForm.eg_1234567890abc123appsgoogleusercontentcom')"
          @input="
            emitUpdate({
              ...data,
              client_id: ($event.target as HTMLInputElement).value,
            })
          "
        />
      </div>

      <!-- Client Secret -->
      <div>
        <label for="ssoproviderform-field-7" class="mb-1 block text-sm font-medium">{{ $t('components.SsoProviderForm.client_secret') }}</label>
        <input id="ssoproviderform-field-7"
          :value="data.client_secret"
          type="password"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          :placeholder="$t('components.SsoProviderForm.leave_blank_to_keep_existing')"
          @input="
            emitUpdate({
              ...data,
              client_secret: ($event.target as HTMLInputElement).value,
            })
          "
        />
      </div>

      <!-- Tenant Domain (only for presets that require_tenant) -->
      <div v-if="activePreset?.requires_tenant">
        <label for="ssoproviderform-tenant-domain" class="mb-1 block text-sm font-medium">
          {{ activePreset.tenant_label || $t('components.SsoProviderForm.tenant_domain') }}
        </label>
        <input id="ssoproviderform-tenant-domain"
          :value="data.tenant_domain"
          type="text"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          :placeholder="$t(tenantPlaceholderKey)"
          data-testid="sso-tenant-domain"
          @input="
            emitUpdate({
              ...data,
              tenant_domain: ($event.target as HTMLInputElement).value,
            })
          "
        />
      </div>

      <!-- Discovery URL + Scopes: ONLY for Custom preset -->
      <template v-if="data.preset === 'custom'">
        <div>
          <label for="ssoproviderform-field-6" class="mb-1 block text-sm font-medium">{{ $t('components.SsoProviderForm.discovery_url') }}</label>
          <input id="ssoproviderform-field-6"
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
          <label for="ssoproviderform-field-5" class="mb-1 block text-sm font-medium">{{ $t('components.SsoProviderForm.scopes') }}</label>
          <input id="ssoproviderform-field-5"
            :value="data.scopes"
            type="text"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            :placeholder="$t('components.SsoProviderForm.openid_profile_email')"
            @input="
              emitUpdate({
                ...data,
                scopes: ($event.target as HTMLInputElement).value,
              })
            "
          />
          <p class="mt-1 text-xs text-muted-foreground">
            {{ $t('components.SsoProviderForm.scopes_hint') }}
          </p>
        </div>
      </template>

      <!-- Derived info for native presets (read-only) -->
      <div v-if="data.preset !== 'custom' && derivedDiscoveryUrl" class="rounded-lg border border-input bg-muted/50 p-3">
        <p class="text-xs font-medium text-muted-foreground">{{ $t('components.SsoProviderForm.derived_discovery_url') }}</p>
        <p class="mt-0.5 break-all font-mono text-xs">{{ derivedDiscoveryUrl }}</p>
      </div>
      <div v-if="data.preset !== 'custom' && derivedScopes" class="rounded-lg border border-input bg-muted/50 p-3">
        <p class="text-xs font-medium text-muted-foreground">{{ $t('components.SsoProviderForm.derived_scopes') }}</p>
        <p class="mt-0.5 font-mono text-xs">{{ derivedScopes }}</p>
      </div>
    </template>

    <!-- SAML fields -->
    <template v-if="data.provider_type === 'saml'">
      <div>
        <label for="ssoproviderform-field-4" class="mb-1 block text-sm font-medium">{{ $t('components.SsoProviderForm.metadata_url') }}</label>
        <input id="ssoproviderform-field-4"
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
        <label for="ssoproviderform-field-3" class="mb-1 block text-sm font-medium">{{ $t('components.SsoProviderForm.metadata_xml') }}</label>
        <textarea id="ssoproviderform-field-3"
          :value="data.metadata_xml"
          rows="4"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring font-mono"
          :placeholder="$t('components.SsoProviderForm.metadata_xml_placeholder')"
          @input="
            emitUpdate({
              ...data,
              metadata_xml: ($event.target as HTMLTextAreaElement).value,
            })
          "
        />
      </div>
      <div>
        <label for="ssoproviderform-field-2" class="mb-1 block text-sm font-medium">{{ $t('components.SsoProviderForm.entity_id') }}</label>
        <input id="ssoproviderform-field-2"
          :value="data.entity_id"
          type="text"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          :placeholder="$t('components.SsoProviderForm.entity_id_placeholder')"
          @input="
            emitUpdate({
              ...data,
              entity_id: ($event.target as HTMLInputElement).value,
            })
          "
        />
      </div>
    </template>

    <!-- Callback URL (create + edit) -->
    <div v-if="callbackUrl" class="rounded-lg border border-primary/30 bg-primary/5 p-3">
      <p class="text-xs font-medium text-primary">{{ $t('components.SsoProviderForm.callback_url_label') }}</p>
      <p class="mt-1 break-all font-mono text-xs text-muted-foreground">{{ callbackUrl }}</p>
      <button
        type="button"
        class="mt-2 inline-flex items-center gap-1 rounded border border-input bg-background px-2 py-1 text-xs font-medium hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        data-testid="sso-callback-url-copy"
        :aria-label="$t('components.SsoProviderForm.copy_callback_url')"
        @click="copyCallbackUrl"
      >
        <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>
        </svg>
        {{ copySuccess ? $t('components.SsoProviderForm.copied') : $t('components.SsoProviderForm.copy_callback_url') }}
      </button>
      <span v-if="copySuccess" class="ml-2 text-xs text-primary" role="status" aria-live="polite" data-testid="sso-callback-url-copied">
        {{ $t('components.SsoProviderForm.copied') }}
      </span>
    </div>

    <!-- Provisioning mode -->
    <div>
      <span class="mb-1 block text-sm font-medium" id="sso-provisioning-mode-label">{{ $t('components.SsoProviderForm.provisioning_mode') }}</span>
      <div role="radiogroup" aria-labelledby="sso-provisioning-mode-label" data-testid="sso-provisioning-mode">
        <!-- The unrestricted option is plan-gated off while the provider is
             already in unrestricted mode: surface it so the state is not
             silently re-committed on save. -->
        <div
          v-if="unrestrictedOptionHidden"
          class="mb-2 rounded-lg border border-destructive/50 bg-destructive/5 p-3 text-xs text-destructive"
          role="alert"
          data-testid="sso-unrestricted-locked-notice"
        >
          {{ $t('components.SsoProviderForm.mode_unrestricted_locked') }}
        </div>

        <!-- Mode 1: Invitation only -->
        <label
          class="flex cursor-pointer items-start gap-3 rounded-lg border p-3 transition-colors mb-2"
          :class="provisioningMode === 'invitation'
            ? 'border-primary bg-primary/10'
            : 'border-input hover:bg-accent/50'"
        >
          <input
            type="radio"
            name="sso-provisioning-mode"
            value="invitation"
            :checked="provisioningMode === 'invitation'"
            class="mt-0.5"
            data-testid="sso-mode-invitation"
            @change="onModeChange('invitation')"
          />
          <div class="min-w-0">
            <span class="text-sm font-medium">{{ $t('components.SsoProviderForm.mode_invitation') }}</span>
            <p class="mt-0.5 text-xs text-muted-foreground">{{ $t('components.SsoProviderForm.mode_invitation_help') }}</p>
          </div>
        </label>

        <!-- Mode 2: Email domain allowlist -->
        <label
          class="flex cursor-pointer items-start gap-3 rounded-lg border p-3 transition-colors mb-2"
          :class="provisioningMode === 'domains'
            ? 'border-primary bg-primary/10'
            : 'border-input hover:bg-accent/50'"
        >
          <input
            type="radio"
            name="sso-provisioning-mode"
            value="domains"
            :checked="provisioningMode === 'domains'"
            class="mt-0.5"
            data-testid="sso-mode-domains"
            @change="onModeChange('domains')"
          />
          <div class="min-w-0">
            <span class="text-sm font-medium">{{ $t('components.SsoProviderForm.mode_domains') }}</span>
            <p class="mt-0.5 text-xs text-muted-foreground">{{ $t('components.SsoProviderForm.mode_domains_help') }}</p>
          </div>
        </label>

        <!-- Mode 3: Anyone (dangerous — flag-gated) -->
        <label
          v-if="unrestrictedProvisioningAvailable"
          class="flex cursor-pointer items-start gap-3 rounded-lg border p-3 transition-colors mb-2"
          :class="provisioningMode === 'unrestricted'
            ? 'border-destructive bg-destructive/10'
            : 'border-input hover:bg-accent/50'"
        >
          <input
            type="radio"
            name="sso-provisioning-mode"
            value="unrestricted"
            :checked="provisioningMode === 'unrestricted'"
            class="mt-0.5"
            data-testid="sso-mode-unrestricted"
            @change="onModeChange('unrestricted')"
          />
          <div class="min-w-0">
            <span class="text-sm font-medium text-destructive">{{ $t('components.SsoProviderForm.mode_unrestricted') }}</span>
            <p class="mt-0.5 text-xs text-muted-foreground">{{ $t('components.SsoProviderForm.mode_unrestricted_help') }}</p>
            <div
              v-if="provisioningMode === 'unrestricted'"
              class="mt-2 rounded border border-destructive/50 bg-destructive/5 p-2 text-xs text-destructive"
              role="alert"
              data-testid="sso-unrestricted-warning"
            >
              {{ $t('components.SsoProviderForm.mode_unrestricted_warning') }}
            </div>
          </div>
        </label>
      </div>
    </div>

    <!-- Allowed domains (mode 2 only) -->
    <div v-if="provisioningMode === 'domains'">
      <label class="mb-1 block text-sm font-medium" for="sso-domain-input">{{ $t('components.SsoProviderForm.allowed_domains') }}</label>
      <p class="mb-2 text-xs text-muted-foreground">{{ $t('components.SsoProviderForm.allowed_domains_help') }}</p>
      <div
        class="flex flex-wrap items-center gap-1.5 rounded-lg border border-input bg-background px-2 py-1.5 focus-within:ring-2 focus-within:ring-ring"
        data-testid="sso-domain-list"
      >
        <span
          v-for="(domain, idx) in data.allowed_domains"
          :key="domain"
          class="inline-flex items-center gap-1 rounded bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary"
        >
          {{ domain }}
          <button
            type="button"
            class="ml-0.5 rounded-full p-0.5 hover:bg-primary/20 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary"
            :aria-label="$t('components.SsoProviderForm.domain_remove', { domain })"
            :data-testid="`sso-domain-remove-${idx}`"
            @click="removeDomain(idx)"
          >
            <svg class="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M18 6 6 18M6 6l12 12" />
            </svg>
          </button>
        </span>
        <input
          id="sso-domain-input"
          v-model="domainInput"
          type="text"
          class="min-w-[120px] flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
          :placeholder="$t('components.SsoProviderForm.domain_placeholder')"
          data-testid="sso-domain-input"
          :aria-describedby="domainError ? 'sso-domain-error' : undefined"
          @keydown.enter.prevent="addDomain"
          @keydown.tab="addDomain"
        />
      </div>
      <div v-if="domainError" id="sso-domain-error" class="mt-1 text-xs text-destructive" role="alert" aria-live="polite" data-testid="sso-domain-error">
        {{ domainError }}
      </div>
    </div>

    <!-- Default role -->
    <div>
      <label for="ssoproviderform-field-1" class="mb-1 block text-sm font-medium">{{ $t('components.SsoProviderForm.default_role') }}</label>
      <Select
  :aria-label="$t('components.SsoProviderForm.default_role')"
  :model-value="data.default_role"
  @update:model-value="(val) => emitUpdate({...data, default_role: val as string})"
  :placeholder="$t('components.SsoProviderForm.default_role')"
  class="w-full"
  :options="[{ value: 'runner', label: $t('components.SsoProviderForm.role_runner') }, { value: 'operator', label: $t('components.SsoProviderForm.role_operator') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
    </div>

    <div v-if="error" class="text-sm text-destructive">{{ error }}</div>

    <div class="flex items-center gap-2">
      <Button :disabled="!data.name.trim() || saving" @click="$emit('submit')">
        {{ saving ? savingLabel : submitLabel }}
      </Button>
      <button type="button"
        class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        @click="$emit('cancel')"
      >
        {{ $t('components.SsoProviderForm.cancel') }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onBeforeUnmount, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Button from 'primevue/button'
import Select from './shared/AppSelect.vue'
import SsoBrandMark from './SsoBrandMark.vue'

export interface SsoPresetInfo {
  id: string
  label: string
  requires_tenant: boolean
  tenant_label: string
}

export interface SsoFormState {
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
  preset: string;
  tenant_domain: string;
  allowed_domains: string[];
}

type DomainErrorKey =
  | 'components.SsoProviderForm.domain_invalid'
  | 'components.SsoProviderForm.domain_invalid_no_dot'

/** Client-side domain validation mirroring backend rules in validate_allowed_domains.
 *  Returns the locale key of the validation error, or null when the domain is valid. */
function validateDomain(raw: string): DomainErrorKey | null {
  const domain = raw.trim().toLowerCase().replace(/\.$/, '')
  if (!domain) return null
  if (/[ @/*]/.test(domain)) {
    return 'components.SsoProviderForm.domain_invalid'
  }
  if (!domain.includes('.')) {
    return 'components.SsoProviderForm.domain_invalid_no_dot'
  }
  return null
}

const props = defineProps<{
  data: SsoFormState;
  saving: boolean;
  submitLabel: string;
  savingLabel: string;
  error: string | null;
  presets: SsoPresetInfo[];
  callbackUrl?: string | null;
  unrestrictedProvisioningAvailable?: boolean;
}>();

const emit = defineEmits<{
  "update:data": [value: SsoFormState];
  submit: [];
  cancel: [];
}>();

function emitUpdate(updated: SsoFormState) {
  emit("update:data", updated);
}

const { t } = useI18n()

// ── Provisioning mode ──────────────────────────────────────────────
type ProvisioningMode = 'invitation' | 'domains' | 'unrestricted'

function deriveMode(data: SsoFormState): ProvisioningMode {
  if (!data.auto_provision) return 'invitation'
  if (data.allowed_domains.length > 0) return 'domains'
  return 'unrestricted'
}

// The payload is ambiguous between domains-mode-with-an-empty-allowlist and
// unrestricted mode (both are auto_provision=true, allowed_domains=[]), so the
// selection cannot be re-derived from it: choosing "Email domain allowlist"
// before adding the first domain would otherwise snap back to unrestricted and
// the domain input would never appear. Latch the user's explicit choice and
// fall back to the derived mode only until they choose one.
const chosenMode = ref<ProvisioningMode | null>(null)
const provisioningMode = computed<ProvisioningMode>(() => chosenMode.value ?? deriveMode(props.data))

function onModeChange(mode: ProvisioningMode) {
  chosenMode.value = mode
  if (mode === 'invitation') {
    emitUpdate({ ...props.data, auto_provision: false, allowed_domains: [] })
  } else if (mode === 'domains') {
    emitUpdate({ ...props.data, auto_provision: true, allowed_domains: props.data.allowed_domains.length > 0 ? [...props.data.allowed_domains] : [] })
  } else {
    emitUpdate({ ...props.data, auto_provision: true, allowed_domains: [] })
  }
}

// A provider already in unrestricted mode renders no radio when the plan gates
// the option off, so the current (dangerous) state would otherwise be invisible
// and saving would silently re-commit unrestricted provisioning.
const unrestrictedOptionHidden = computed(
  () => provisioningMode.value === 'unrestricted' && !props.unrestrictedProvisioningAvailable
)

// ── Domain tag/list input ──────────────────────────────────────────
const domainInput = ref('')
const domainError = ref<string | null>(null)

function addDomain() {
  const raw = domainInput.value.trim()
  if (!raw) return
  const err = validateDomain(raw)
  if (err) {
    domainError.value = t(err)
    return
  }
  domainError.value = null
  const normalised = raw.trim().toLowerCase().replace(/\.$/, '')
  if (props.data.allowed_domains.includes(normalised)) {
    domainInput.value = ''
    return
  }
  emitUpdate({
    ...props.data,
    allowed_domains: [...props.data.allowed_domains, normalised],
  })
  domainInput.value = ''
}

function removeDomain(idx: number) {
  const updated = [...props.data.allowed_domains]
  updated.splice(idx, 1)
  emitUpdate({ ...props.data, allowed_domains: updated })
}

// Clear domain error when the user starts typing again
watch(domainInput, () => {
  if (domainError.value) domainError.value = null
})

const activePreset = computed(() =>
  props.presets.find(p => p.id === props.data.preset) ?? null
)

// Per-preset tenant placeholder so subdomain-style values (e.g. "acme") are not
// mistaken for full domains on presets that interpolate the whole hostname.
const TENANT_PLACEHOLDER_KEYS: Record<string, string> = {
  auth0: 'components.SsoProviderForm.tenant_placeholder_auth0',
  okta: 'components.SsoProviderForm.tenant_placeholder_okta',
  'azure-ad': 'components.SsoProviderForm.tenant_placeholder_azure_ad',
  onelogin: 'components.SsoProviderForm.tenant_placeholder_onelogin',
}

const tenantPlaceholderKey = computed(
  () =>
    TENANT_PLACEHOLDER_KEYS[props.data.preset] ??
    'components.SsoProviderForm.tenant_domain_placeholder'
)

// Derived values for native presets (read-only display)
const derivedDiscoveryUrl = computed(() => {
  if (props.data.preset === 'custom') return ''
  const tenant = props.data.tenant_domain?.trim()
  if (props.data.preset === 'google') return 'https://accounts.google.com/.well-known/openid-configuration'
  if (props.data.preset === 'auth0') return tenant ? `https://${tenant}/.well-known/openid-configuration` : ''
  if (props.data.preset === 'okta') return tenant ? `https://${tenant}/.well-known/openid-configuration` : ''
  if (props.data.preset === 'azure-ad') return tenant ? `https://login.microsoftonline.com/${tenant}/v2.0/.well-known/openid-configuration` : ''
  if (props.data.preset === 'onelogin') return tenant ? `https://${tenant}.onelogin.com/oidc/2/.well-known/openid-configuration` : ''
  return ''
})

const derivedScopes = computed(() => {
  if (props.data.preset === 'custom') return ''
  return 'openid profile email'
})

// Copy to clipboard
const copySuccess = ref(false)
let copyResetTimer: ReturnType<typeof setTimeout> | undefined

function scheduleCopyReset() {
  if (copyResetTimer) clearTimeout(copyResetTimer)
  copyResetTimer = setTimeout(() => {
    copySuccess.value = false
  }, 2000)
}

onBeforeUnmount(() => {
  if (copyResetTimer) clearTimeout(copyResetTimer)
})

async function copyCallbackUrl() {
  if (!props.callbackUrl) return
  try {
    await navigator.clipboard.writeText(props.callbackUrl)
    copySuccess.value = true
    scheduleCopyReset()
  } catch {
    // Fallback for non-secure contexts
    const textarea = document.createElement('textarea')
    textarea.value = props.callbackUrl
    document.body.appendChild(textarea)
    textarea.select()
    document.execCommand('copy')
    document.body.removeChild(textarea)
    copySuccess.value = true
    scheduleCopyReset()
  }
}

function onPresetChange(presetId: string) {
  // When switching to a native preset, clear fields that the server derives
  if (presetId !== 'custom') {
    emitUpdate({
      ...props.data,
      preset: presetId,
      discovery_url: '',
      scopes: '',
      tenant_domain: '',
    })
  } else {
    emitUpdate({
      ...props.data,
      preset: presetId,
      tenant_domain: '',
    })
  }
}
</script>
