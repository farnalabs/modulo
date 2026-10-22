<template>
  <div class="page-wide">
    <header class="flex items-center gap-3 mb-6">
      <button type="button"
        class="rounded p-1 text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
        data-testid="envprofile-form-back"
        :aria-label="$t('views.EnvironmentProfileForm.back_to_profiles')"
        @click="$router.push('/environment-profiles')"
      >
        <ArrowLeft :size="18" aria-hidden="true" />
      </button>
      <PageHeader
        :title="isEdit ? $t('views.EnvironmentProfileForm.edit_title') : $t('views.EnvironmentProfileForm.create_title')"
        :subtitle="isEdit ? $t('views.EnvironmentProfileForm.edit_subtitle') : $t('views.EnvironmentProfileForm.create_subtitle')"
      />
    </header>

    <form @submit.prevent="handleSubmit" class="card max-w-3xl p-6 space-y-5">
      <div>
        <label for="environmentprofileform-field-7" class="mb-1 block text-sm font-medium">{{ $t('views.EnvironmentProfileForm.name') }} <span class="text-destructive">*</span></label>
        <input id="environmentprofileform-field-7"
          v-model="form.name"
          type="text"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
          :placeholder="$t('views.EnvironmentProfileForm.name_placeholder')"
          data-testid="envprofile-form-name"
          :class="{ 'border-destructive': submitted && !form.name.trim() }"
        />
        <p v-if="submitted && !form.name.trim()" class="mt-1 text-xs text-destructive">{{ $t('views.EnvironmentProfileForm.name_is_required') }}</p>
      </div>

      <div>
        <label for="environmentprofileform-field-6" class="mb-1 block text-sm font-medium">{{ $t('views.EnvironmentProfileForm.description') }}</label>
        <textarea id="environmentprofileform-field-6"
          v-model="form.description"
          rows="3"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
          :placeholder="$t('views.EnvironmentProfileForm.description_placeholder')"
          data-testid="envprofile-form-description"
        />
      </div>

      <div>
        <label for="environmentprofileform-field-5" class="mb-1 block text-sm font-medium">{{ $t('views.EnvironmentProfileForm.provider_type') }} <span class="text-destructive">*</span></label>
        <Select
  :aria-label="$t('views.EnvironmentProfileForm.provider_type')"
  v-model="form.provider_type"
  input-id="environmentprofileform-field-5"
  :placeholder="$t('views.EnvironmentProfileForm.select_provider_type')"
  data-testid="envprofile-form-provider"
  class="w-full"
  :options="providerTypeOptions"
  option-label="label"
  option-value="value"
  :pt="providerSelectPt"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
        <p v-if="submitted && !form.provider_type" id="envprofile-form-provider-error" role="alert" class="mt-1 text-xs text-destructive">{{ $t('views.EnvironmentProfileForm.provider_type_is_required') }}</p>
        <output v-if="formTierLabel" id="envprofile-form-tier-hint" class="mt-1 block text-xs text-muted-foreground">
          <span
            class="mr-1 inline-block rounded-full bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary-text"
            data-testid="envprofile-form-tier-badge"
          >{{ formTierLabel }}</span>
        </output>
      </div>

      <div>
        <label for="environmentprofileform-field-4" class="mb-1 block text-sm font-medium">{{ $t('views.EnvironmentProfileForm.image_reference') }}</label>
        <input id="environmentprofileform-field-4"
          v-model="form.image_ref"
          type="text"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
          :placeholder="$t('views.EnvironmentProfileForm.image_reference_placeholder')"
          data-testid="envprofile-form-image"
          :aria-describedby="imageDescribedBy"
        />
        <!-- FAR-1779 / ADR 059: make the BYO agent-image path discoverable —
             the profile's image_ref is where an operator supplies their own
             image, and the process the agent runs is set per sandbox_agent
             node in the pipeline editor. -->
        <p id="envprofile-form-image-help" class="mt-1 text-xs text-muted-foreground">
          {{ $t('views.EnvironmentProfileForm.image_reference_help') }}
        </p>
        <!-- Digest pinning is advice, never enforcement (ADR 059 §4): a pinned
             ref is visibly acknowledged, a mutable tag warns without blocking.
             The digest state is a single mutually-exclusive region. The release
             placeholder digest is syntactically a valid digest but no registry
             image exists behind it, so it must NEVER render the green pinned
             assurance (`placeholder`). A non-empty ref that is not a valid
             Docker reference (uppercase repository/hex, embedded whitespace, a
             malformed digest) is NOT a mutable tag — telling the operator to
             "pin it by digest" would tell them to do what they already tried —
             so it gets its own `invalid` copy. Only a valid, non-digest ref
             reaches the mutable-tag warning. -->
        <output
          v-if="imageRefState === 'pinned'"
          id="envprofile-form-image-state"
          class="mt-1 flex items-center gap-1 text-xs text-success-text"
          data-testid="envprofile-form-image-pinned"
        >
          <ShieldCheck :size="14" aria-hidden="true" />
          {{ $t('views.EnvironmentProfileForm.image_digest_pinned') }}
        </output>
        <output
          v-else-if="imageRefState === 'placeholder'"
          id="envprofile-form-image-state"
          class="mt-1 flex items-center gap-1 text-xs text-warning-text"
          data-testid="envprofile-form-image-placeholder"
        >
          <AlertTriangle :size="14" aria-hidden="true" />
          {{ $t('views.EnvironmentProfileForm.image_placeholder_unresolvable') }}
        </output>
        <output
          v-else-if="imageRefState === 'invalid'"
          id="envprofile-form-image-state"
          class="mt-1 flex items-center gap-1 text-xs text-destructive"
          data-testid="envprofile-form-image-invalid"
        >
          <AlertTriangle :size="14" aria-hidden="true" />
          {{ $t('views.EnvironmentProfileForm.image_invalid_reference') }}
        </output>
        <output
          v-else-if="imageRefState === 'mutable'"
          id="envprofile-form-image-state"
          class="mt-1 flex items-center gap-1 text-xs text-warning-text"
          data-testid="envprofile-form-image-tag-warning"
        >
          <AlertTriangle :size="14" aria-hidden="true" />
          {{ $t('views.EnvironmentProfileForm.image_mutable_tag_warning') }}
        </output>
      </div>

      <div>
        <span class="mb-1 block text-sm font-medium">{{ $t('views.EnvironmentProfileForm.capabilities') }}</span>
        <div class="flex flex-wrap gap-2">
          <label
            v-for="cap in availableCapabilities"
            :key="cap"
            class="inline-flex items-center gap-1.5 rounded-lg border border-input px-3 py-1.5 text-sm cursor-pointer transition-colors"
            :class="form.capabilities.includes(cap) ? 'bg-primary/10 border-primary/30 text-primary-text' : 'hover:bg-accent'"
          >
            <input
              type="checkbox"
              :value="cap"
              :checked="form.capabilities.includes(cap)"
              class="sr-only"
              @change="toggleCapability(cap)"
            />
            {{ cap }}
          </label>
        </div>
      </div>

      <div>
        <label for="environmentprofileform-field-3" class="mb-1 block text-sm font-medium">{{ $t('views.EnvironmentProfileForm.network_policy') }}</label>
        <Select
  :aria-label="$t('views.EnvironmentProfileForm.network_policy')"
  v-model="form.network_policy"
  :placeholder="$t('views.EnvironmentProfileForm.select_network_policy')"
  data-testid="envprofile-form-network"
  class="w-full"
  :options="[{ value: 'outbound', label: $t('views.EnvironmentProfileForm.outbound_full_egress') }, { value: 'none', label: $t('views.EnvironmentProfileForm.none_isolated') }, { value: 'selected', label: $t('views.EnvironmentProfileForm.selected_domains_only') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
      </div>

      <div>
        <label for="environmentprofileform-field-2" class="mb-1 block text-sm font-medium">{{ $t('views.EnvironmentProfileForm.initialisation_strategy') }}</label>
        <Select
  :aria-label="$t('views.EnvironmentProfileForm.initialisation_strategy')"
  v-model="form.initialisation_strategy"
  :placeholder="$t('views.EnvironmentProfileForm.select_initialisation_strategy')"
  data-testid="envprofile-form-init"
  class="w-full"
  :options="[{ value: 'git_clone', label: $t('views.EnvironmentProfileForm.git_clone') }, { value: 'blank', label: $t('views.EnvironmentProfileForm.blank_empty_workspace') }, { value: 'worktree', label: $t('views.EnvironmentProfileForm.git_worktree') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
      </div>

      <div>
        <label for="environmentprofileform-field-1" class="mb-1 block text-sm font-medium">{{ $t('views.EnvironmentProfileForm.persistence_policy') }}</label>
        <Select
  :aria-label="$t('views.EnvironmentProfileForm.persistence_policy')"
  v-model="form.persistence_policy"
  :placeholder="$t('views.EnvironmentProfileForm.select_persistence_policy')"
  data-testid="envprofile-form-persistence"
  class="w-full"
  :options="[{ value: 'ephemeral', label: $t('views.EnvironmentProfileForm.ephemeral_destroyed_after_run') }, { value: 'retained', label: $t('views.EnvironmentProfileForm.retained_available_for_inspection') }, { value: 'cache', label: $t('views.EnvironmentProfileForm.cache_reusable_between_runs') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
      </div>

      <div v-if="store.error" class="text-sm text-destructive">{{ store.error }}</div>
      <div v-if="formError" class="text-sm text-destructive">{{ formError }}</div>

      <div class="flex items-center gap-2 pt-2">
        <Button :disabled="store.isSaving" type="submit" data-testid="envprofile-form-submit">
          {{ store.isSaving
            ? $t('views.EnvironmentProfileForm.saving')
            : (isEdit ? $t('views.EnvironmentProfileForm.save_changes') : $t('views.EnvironmentProfileForm.create_profile')) }}
        </Button>
        <button
          type="button"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent transition-colors"
          data-testid="envprofile-form-cancel"
          @click="$router.push('/environment-profiles')"
        >
          {{ $t('views.EnvironmentProfileForm.cancel') }}
        </button>
      </div>
    </form>
  </div>
</template>

<script setup lang="ts">
import PageHeader from '../../components/shared/PageHeader.vue'
import { ref, reactive, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useEnvironmentProfilesStore } from '../../stores/environmentProfiles'
import type { ProfileCreatePayload } from '../../stores/environmentProfiles'
import { runnerTierForProvider, runnerTierLabelKey } from '../../lib/runnerTiers'
import { ArrowLeft, ShieldCheck, AlertTriangle } from '@lucide/vue'
import Button from 'primevue/button'
import Select from '../../components/shared/AppSelect.vue'

const props = defineProps<{
  profileId?: string
}>()

const route = useRoute()
const router = useRouter()
const store = useEnvironmentProfilesStore()

const isEdit = computed(() => {
  const id = props.profileId || (route.params.id as string)
  return !!(id && id !== 'new')
})

const { t } = useI18n()

const availableCapabilities = [
  'git',
  'python>=3.12',
  'node>=18',
  'shell',
  'network:github.com',
  'network:pypi.org',
]

const form = reactive({
  name: '',
  description: '',
  provider_type: '',
  image_ref: '',
  capabilities: [] as string[],
  network_policy: 'outbound',
  initialisation_strategy: 'git_clone',
  persistence_policy: 'ephemeral',
})

const submitted = ref(false)
const formError = ref<string | null>(null)
// The form does not expose visibility (it is an org/team policy set elsewhere),
// but the update payload must echo the loaded profile's value: the backend
// applies `exclude_unset` updates, so hardcoding 'org' would silently flip a
// team-scoped profile to org while its owner_team_id survived.
const loadedVisibility = ref('org')

const formTierLabel = computed(() => {
  const tier = runnerTierForProvider(form.provider_type)
  return tier ? t(runnerTierLabelKey(tier)) : null
})

// Provider options offered by the form. The backend provider_type vocabulary
// (FAR-1051) is local_docker / e2b / kubernetes; `local` and `runner_docker`
// are system-seeded and not offered for manual creation.
const providerTypeOptions = computed(() => [
  { value: 'local_docker', label: t('views.EnvironmentProfileForm.local_docker') },
  { value: 'e2b', label: t('views.EnvironmentProfileForm.e2b_sandboxed_cloud') },
  { value: 'kubernetes', label: t('views.EnvironmentProfileForm.kubernetes_external') },
])

// FAR-1779 / ADR 059 §4: digest pinning is docs-advised, never enforced. A
// `name[@tag]@<algorithm>:<lowercase hex>` ref is immutable and visibly
// acknowledged — the algorithm is one go-digest registers (sha256, sha384 or
// sha512) with its fixed hex length; a valid ref that is not digest-pinned (a
// mutable tag, or no tag at all — an implicit `:latest`) warns without blocking
// the save; and a
// ref that is NOT a valid Docker reference is reported as `invalid` with its
// own accurate copy — never as a mutable tag, which would tell the operator to
// "pin it" when their ref cannot even be pulled.
//
// The reference grammar is Docker's real rule (distribution/reference), and it
// has TWO case rules: the registry host is a mixed-case domain
// (`domainComponent = [a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?`) or an IPv6
// address in brackets, with an optional port, while the repository path is
// lowercase — each component is `[a-z0-9]+` joined by a separator that is a
// single `.` or `_`, a double `__`, or a run of one-or-more `-` (so `my--repo`,
// `my---repo` and `my__repo` are valid, while `my..repo`, `my._repo` and
// `my-_repo` are not), plus an optional mixed-case tag and an optional
// `@<algorithm>:<lowercase hex>` digest, where the algorithm is one go-digest
// registers (sha256 with 64 hex, sha384 with 96, sha512 with 128) — any other
// algorithm (`md5`, `blake3`, …) is rejected by the daemon as an unsupported
// digest algorithm. No whitespace anywhere. Docker accepts a
// mixed-case REGISTRY host (`GHCR.io/library/alpine`,
// `MyRegistry.local/team/agent:tag`, `[::1]:5000/repo`) — only the repository
// path is case-sensitive — so the two parts are validated with different
// grammars. The whole reference is validated, not just its digest suffix:
// Docker / containerd reject an uppercase repository path, uppercase digest hex
// and an embedded space, so a ref that merely ENDS in `@<algorithm>:<hex>` must
// never be announced as pinned. The repository path is also length-capped at
// 255 characters (the daemon rejects a longer name outright), so an over-length
// ref is likewise `invalid` rather than pinned. A Hub name with NO namespace is
// normalised to `library/<name>` BEFORE that cap is applied, so such a name may
// only be 247 characters (255 minus the 8-character `library/` prefix) — a
// 248-character no-namespace name normalises to 256 and is rejected, even
// though a 255-character namespaced/host path is accepted.
//
// The classification applies only where image_ref is dispatched as a container
// image — the Runner (runner_docker) and Kubernetes providers. E2B is excluded
// (its image_ref is not dispatched) and so is local_docker, which dispatch
// refuses outright (`runner_dispatch.py` raises SandboxDispatchUnboundError),
// so claiming it runs a container image would be false. Those providers get the
// unclassified `null` state.
const IMAGE_SEPARATOR = '(?:[._]|__|[-]+)'
const IMAGE_COMPONENT = `[a-z0-9]+(?:${IMAGE_SEPARATOR}[a-z0-9]+)*`
const IMAGE_TAG = '[A-Za-z0-9_][A-Za-z0-9._-]{0,127}'
// distribution/reference: domainNameComponent, domainName and ipv6address.
const DOMAIN_COMPONENT = '[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?'
const DOMAIN_NAME = String.raw`${DOMAIN_COMPONENT}(?:\.${DOMAIN_COMPONENT})*`
const IPV6_ADDRESS = String.raw`\[[a-fA-F0-9:]+\]`
const REGISTRY_HOST_RE = new RegExp(`^(?:${DOMAIN_NAME}|${IPV6_ADDRESS})(?::[0-9]+)?$`)
const IMAGE_PATH_RE = new RegExp(`^${IMAGE_COMPONENT}(?:/${IMAGE_COMPONENT})*$`)
const IMAGE_TAG_RE = new RegExp(`^${IMAGE_TAG}$`)
// go-digest (opencontainers/go-digest) registers exactly these three hash
// algorithms, and the Docker daemon accepts all of them — `docker image inspect
// 'ghcr.io/acme/agent@sha512:<128 hex>'` parses (`No such image`). The hex is
// lowercase and the length is fixed per algorithm; any other algorithm (`md5`,
// `blake3`, …) is rejected by the daemon as an unsupported digest algorithm, so
// it must classify as `invalid` rather than pinned.
const DIGEST_HEX_LENGTHS: Readonly<Record<string, number>> = {
  sha256: 64,
  sha384: 96,
  sha512: 128,
}
const IMAGE_DIGEST_RE = /^(sha256|sha384|sha512):([0-9a-f]+)$/

function isValidImageDigest(digest: string): boolean {
  const match = IMAGE_DIGEST_RE.exec(digest)
  if (match === null) return false
  return match[2].length === DIGEST_HEX_LENGTHS[match[1]]
}
const DIGEST_PINNABLE_PROVIDERS: ReadonlySet<string> = new Set(['runner_docker', 'kubernetes'])

// Docker's own rule (distribution/reference `splitDockerDomain`): with a `/`
// present, the first component is a registry host when it contains a `.` or
// `:`, is `localhost`, OR is NOT all-lowercase — "Uppercase namespaces are not
// allowed, so if the first element is not lowercase, we assume it to be a
// domain-name". Only otherwise is it a Docker Hub namespace subject to the
// lowercase path grammar. So `GHCR/acme/agent` is a valid reference (host
// `GHCR`, lowercase path `acme/agent`) while `ghcr.io/ACME/agent` is not (its
// repository path has an uppercase component). Measured against the daemon:
// the former parses (`No such image`), the latter is `invalid reference
// format`. A ref is `invalid` only when Docker would reject it.
function isDockerRegistryHost(component: string): boolean {
  return component.includes('.') || component.includes(':') ||
    component.toLowerCase() === 'localhost' ||
    component !== component.toLowerCase()
}

// distribution/reference caps the REPOSITORY PATH — the name once the registry
// host is split off — at 255 characters; the daemon reports "repository name
// must not be more than 255 characters". Measured against the daemon: a
// 255-character path parses (`No such image`), a 256-character path is
// rejected. An over-length path cannot be pulled, so it must never render the
// green pinned assurance.
const REPOSITORY_PATH_MAX = 255
// Docker normalises a Hub name with NO namespace to `library/<name>` before
// applying the 255-character repository-path cap, so the raw name may only be
// 247 characters. Measured against the daemon: `'a'.repeat(247)` parses
// (`No such image`) while `'a'.repeat(248)` is rejected with "repository name
// must not be more than 255 characters".
const HUB_NO_NAMESPACE_MAX = REPOSITORY_PATH_MAX - 'library/'.length

function isValidDockerImageName(name: string): boolean {
  const slash = name.indexOf('/')
  if (slash === -1) return name.length <= HUB_NO_NAMESPACE_MAX && IMAGE_PATH_RE.test(name)
  const first = name.slice(0, slash)
  const path = name.slice(slash + 1)
  // A non-host first component is a Docker Hub namespace, so Docker measures
  // the WHOLE name against the repository-path cap; only when a registry host
  // is split off is the cap applied to the remaining path alone.
  if (!isDockerRegistryHost(first)) {
    return name.length <= REPOSITORY_PATH_MAX && IMAGE_PATH_RE.test(name)
  }
  return path.length <= REPOSITORY_PATH_MAX &&
    REGISTRY_HOST_RE.test(first) && IMAGE_PATH_RE.test(path)
}

// Split Docker's `name[:tag][@digest]` shape apart before validating each
// part: the tag separator is the last `:` AFTER the last `/`, so a registry
// port (`ghcr.io:5000/repo`) is never mistaken for a tag.
function isValidDockerImageRef(ref: string): boolean {
  let name = ref
  const at = ref.lastIndexOf('@')
  if (at !== -1) {
    if (!isValidImageDigest(ref.slice(at + 1))) return false
    name = ref.slice(0, at)
  }
  const lastSlash = name.lastIndexOf('/')
  const lastColon = name.lastIndexOf(':')
  if (lastColon > lastSlash) {
    if (!IMAGE_TAG_RE.test(name.slice(lastColon + 1))) return false
    name = name.slice(0, lastColon)
  }
  return isValidDockerImageName(name)
}

// The release-advanced placeholder digest: an all-zero sha256 that the seeded
// Bundled Runner carries until the GHCR publish job lands the real pinned
// digest. Mirrors the backend sentinel
// (`modulo.db.bundled_runner_template.BUNDLED_RUNNER_PLACEHOLDER_DIGEST`) —
// duplicated rather than imported because the frontend cannot import backend
// Python. It is deliberately checked BEFORE the digest match: the placeholder
// is syntactically a valid digest, so without this it would render the green
// "pinned / immutable" assurance for an image that cannot be pulled at all.
const PLACEHOLDER_DIGEST = `sha256:${'0'.repeat(64)}`

const imageRefState = computed<'pinned' | 'mutable' | 'invalid' | 'placeholder' | null>(() => {
  if (!DIGEST_PINNABLE_PROVIDERS.has(form.provider_type)) return null
  const ref = form.image_ref.trim()
  if (!ref) return null
  if (ref.endsWith(PLACEHOLDER_DIGEST)) return 'placeholder'
  if (!isValidDockerImageRef(ref)) return 'invalid'
  return ref.includes('@') ? 'pinned' : 'mutable'
})

// The image field always describes itself with the persistent helper; the
// dynamic state region is referenced only while it is rendered, so no
// reference to a missing id is ever emitted.
const imageDescribedBy = computed(() =>
  imageRefState.value
    ? 'envprofile-form-image-help envprofile-form-image-state'
    : 'envprofile-form-image-help',
)

// PrimeVue Select only forwards fallthrough attrs (e.g. aria-describedby) to
// its wrapper <div>, not to the focusable combobox — route the hints through
// pt.label so assistive tech reads them with the control itself. Omit the key
// entirely while no hint exists so no empty/undefined reference is emitted.
const providerSelectPt = computed(() => {
  const hintIds = [
    submitted.value && !form.provider_type ? 'envprofile-form-provider-error' : null,
    formTierLabel.value ? 'envprofile-form-tier-hint' : null,
  ].filter(Boolean)
  return hintIds.length
    ? { label: { 'aria-describedby': hintIds.join(' ') } }
    : { label: {} }
})

function toggleCapability(cap: string) {
  const idx = form.capabilities.indexOf(cap)
  if (idx >= 0) {
    form.capabilities.splice(idx, 1)
  } else {
    form.capabilities.push(cap)
  }
}

async function handleSubmit() {
  submitted.value = true
  formError.value = null

  if (!form.name.trim()) return
  if (!form.provider_type) return

  const profileId = props.profileId || (route.params.id as string)
  const isUpdate = !!(profileId && profileId !== 'new')

  const payload: ProfileCreatePayload = {
    name: form.name.trim(),
    description: form.description.trim() || null,
    provider_type: form.provider_type,
    image_ref: form.image_ref.trim() || null,
    capabilities: [...form.capabilities],
    network_policy: form.network_policy,
    initialisation_strategy: form.initialisation_strategy,
    persistence_policy: form.persistence_policy,
    visibility: isUpdate ? loadedVisibility.value : 'org',
  }

  try {
    if (isUpdate) {
      await store.updateProfile(profileId, payload)
    } else {
      await store.createProfile(payload)
    }
    router.push('/environment-profiles')
  } catch (e: unknown) {
    formError.value = e instanceof Error ? e.message : t('views.EnvironmentProfileForm.save_failed')
  }
}

onMounted(async () => {
  const profileId = props.profileId || (route.params.id as string)
  if (profileId && profileId !== 'new') {
    await store.fetchProfile(profileId)
    if (store.currentProfile) {
      const p = store.currentProfile
      form.name = p.name
      form.description = p.description ?? ''
      form.provider_type = p.provider_type
      form.image_ref = p.image_ref ?? ''
      form.capabilities = [...p.capabilities]
      form.network_policy = p.network_policy
      form.initialisation_strategy = p.initialisation_strategy
      form.persistence_policy = p.persistence_policy
      loadedVisibility.value = p.visibility
    }
  }
})
</script>
