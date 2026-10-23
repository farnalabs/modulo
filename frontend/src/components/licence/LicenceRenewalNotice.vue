<template>
  <!--
    Renewal state surface for the licence page (FAR-2075).

    Every state comes from the backend (``licence_state``) and every date is
    already computed there (``expires_at`` / ``grace_ends_at``). This component
    renders copy and actions only — it never compares dates or adds the
    14-day grace window itself, so the boundary has exactly one implementation.
  -->
  <div
    v-if="state === 'renewal_due'"
    class="rounded-lg border bg-card p-4 shadow-sm"
    :class="renewalBannerClass"
    :data-renewal-mode="renewalMode"
    :data-licence-state="state"
    data-testid="licence-renewal-banner"
    aria-live="polite"
  >
    <div class="flex items-start gap-3">
      <svg
        class="h-5 w-5 shrink-0"
        :class="isAuto ? 'text-muted-foreground' : 'text-warning-text'"
        xmlns="http://www.w3.org/2000/svg"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <circle cx="12" cy="12" r="9" />
        <path d="M12 7v5l3 2" />
      </svg>

      <div class="min-w-0 flex-1 space-y-1">
        <p class="text-sm font-semibold">
          {{ isAuto ? autoTitle : manualTitle }}
        </p>
        <p class="text-sm text-muted-foreground">
          {{ isAuto ? autoBody : manualBody }}
        </p>
        <p v-if="daysUntilExpiry !== null" class="text-xs font-medium text-muted-foreground">
          {{ $t('components.LicenceRenewalNotice.days_until_expiry', { count: daysUntilExpiry }) }}
        </p>

        <!--
          MANUAL only. An ``auto`` licence is already being paid for: offering a
          purchase path to someone mid-subscription is how customers get charged
          twice, so the auto variant renders no action at all.
        -->
        <div v-if="isAuto === false" class="flex flex-wrap items-center gap-x-4 gap-y-2 pt-1">
          <Button
            as="a"
            :href="pricingUrl"
            target="_blank"
            rel="noopener noreferrer"
            data-testid="licence-renewal-renew-link"
          >
            {{ $t('components.LicenceRenewalNotice.renew_action') }}
          </Button>
          <button
            type="button"
            class="text-sm font-medium text-primary-text underline underline-offset-2"
            data-testid="licence-renewal-install-action"
            @click="emit('install-key')"
          >
            {{ $t('components.LicenceRenewalNotice.install_action') }}
          </button>
        </div>
      </div>
    </div>
  </div>

  <div
    v-else-if="state === 'lapsed'"
    class="rounded-lg border border-destructive/40 bg-card p-4 shadow-sm"
    data-testid="licence-lapsed-banner"
    aria-live="polite"
  >
    <div class="flex items-start gap-3">
      <svg
        class="h-5 w-5 shrink-0 text-destructive"
        xmlns="http://www.w3.org/2000/svg"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <circle cx="12" cy="12" r="9" />
        <path d="M12 8v5M12 16h.01" />
      </svg>

      <div class="min-w-0 flex-1 space-y-1">
        <p class="text-sm font-semibold">{{ $t('components.LicenceRenewalNotice.lapsed_title') }}</p>
        <p class="text-sm text-muted-foreground">
          {{ expiredAt ? $t('components.LicenceRenewalNotice.lapsed_body', { date: formatDate(expiredAt) }) : $t('components.LicenceRenewalNotice.lapsed_body_no_date') }}
        </p>
        <button
          type="button"
          class="pt-1 text-sm font-medium text-primary-text underline underline-offset-2"
          data-testid="licence-lapsed-install-action"
          @click="emit('install-key')"
        >
          {{ $t('components.LicenceRenewalNotice.lapsed_install_action') }}
        </button>
      </div>
    </div>
  </div>

  <Dialog
    v-if="state === 'grace'"
    v-model:visible="graceDialogOpen"
    modal
    :style="{ width: '32rem' }"
    :aria-label="$t('components.LicenceRenewalNotice.grace_title')"
  >
    <div class="space-y-3" data-testid="licence-grace-dialog">
      <h2 class="text-lg font-semibold">{{ $t('components.LicenceRenewalNotice.grace_title') }}</h2>
      <p class="text-sm text-muted-foreground">
        {{ $t('components.LicenceRenewalNotice.grace_body', { date: formattedExpiry, graceEndsAt: formattedGraceEnd }) }}
      </p>
      <!--
        Do not presume a replacement key exists. The common real failure is a
        failed charge (expired card, decline, SCA challenge, a billing contact
        who has left), in which case no key was ever sent.
      -->
      <p class="text-sm text-muted-foreground">
        {{ $t('components.LicenceRenewalNotice.grace_no_key_body') }}
      </p>
    </div>
    <template #footer>
      <div class="flex flex-col gap-2 sm:flex-row">
        <!-- Equal prominence: both are valid next steps and the admin may hold
             neither the key nor the card. -->
        <Button
          class="flex-1 justify-center"
          data-testid="licence-grace-install-action"
          @click="installKey"
        >
          {{ $t('components.LicenceRenewalNotice.grace_install_action') }}
        </Button>
        <Button
          as="a"
          :href="pricingUrl"
          target="_blank"
          rel="noopener noreferrer"
          class="flex-1 justify-center"
          data-testid="licence-grace-buy-action"
        >
          {{ $t('components.LicenceRenewalNotice.grace_buy_action') }}
        </Button>
      </div>
    </template>
  </Dialog>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import { format, parseISO } from 'date-fns'
import type { components } from '../../lib/api/schema'

type LicenseStatus = components['schemas']['LicenseStatusResponse']

const props = defineProps<{
  status: LicenseStatus
}>()

const emit = defineEmits<{
  'install-key': []
}>()

const { t } = useI18n()

const pricingUrl = 'https://modulo.run/pricing'
const graceDialogOpen = ref(false)

const state = computed(() => props.status.licence_state)
const renewalMode = computed(() => props.status.renewal_mode)
const isAuto = computed(() => renewalMode.value === 'auto')
const daysUntilExpiry = computed(() => props.status.days_until_expiry ?? null)
const expiredAt = computed(() => props.status.expires_at ?? null)

function formatDate(iso: string | null): string {
  if (!iso) return '—'
  const date = parseISO(iso)
  if (Number.isNaN(date.getTime())) return iso
  return format(date, 'MMMM d, yyyy')
}

const formattedExpiry = computed(() => formatDate(props.status.expires_at ?? null))
const formattedGraceEnd = computed(() => formatDate(props.status.grace_ends_at ?? null))

const autoTitle = computed(() =>
  t('components.LicenceRenewalNotice.renewal_due_auto_title', { date: formattedExpiry.value }),
)
const autoBody = computed(() =>
  t('components.LicenceRenewalNotice.renewal_due_auto_body', { date: formattedExpiry.value }),
)
const manualTitle = computed(() =>
  t('components.LicenceRenewalNotice.renewal_due_manual_title', { date: formattedExpiry.value }),
)
const manualBody = computed(() =>
  t('components.LicenceRenewalNotice.renewal_due_manual_body', { date: formattedExpiry.value }),
)

const renewalBannerClass = computed(() => (isAuto.value ? 'border-border' : 'border-warning/40'))

// The modal is persistent in the sense that it is shown on every visit while
// the licence is in its delivery grace — not a dismiss-once toast. It stays
// dismissible so the admin can always reach the key input below.
watch(
  state,
  (value) => {
    if (value === 'grace') graceDialogOpen.value = true
  },
  { immediate: true },
)

function installKey() {
  graceDialogOpen.value = false
  emit('install-key')
}
</script>
