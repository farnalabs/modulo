<template>
  <!--
    Wrap the PrimeVue Select in a <label> so the rendered <input> is
    statically associated with a label.  PrimeVue's Select renders an
    <input> internally; without an explicit label association SonarCloud's
    Web:InputWithoutLabelCheck flags it as an unlabelled field (this is the
    reliability-rating gate failure on PR #596).  The wrapper itself never
    renders visible label text, so consumer layout is unchanged — it only
    satisfies the accessibility/static-analysis requirement.  See
    AgentRunnerBindings.vue for the same pattern.
  -->
  <label>
    <Select
      v-bind="$attrs"
      :append-to="appendTo"
      :aria-label="resolvedAriaLabel"
    >
      <template v-for="(_, name) in $slots" #[name]="slotData">
        <slot :name="name" v-bind="slotData ?? {}" />
      </template>
    </Select>
  </label>
</template>

<script setup lang="ts">
/**
 * AppSelect — thin wrapper around PrimeVue Select that defaults
 * `appendTo` to `'self'` so the overlay is rendered inside the
 * Select's own `position: relative` wrapper instead of being
 * portalled to `<body>`.  This prevents the overlay from detaching
 * to the viewport origin when the trigger lives inside a layout
 * whose offset parent differs from `<body>` (the root cause of
 * FAR-851: filter dropdowns rendering at the left edge of the
 * window).
 *
 * All Select instances across the app should use this wrapper
 * instead of importing `primevue/select` directly.
 */
import { useAttrs } from 'vue'
import Select from 'primevue/select'

// Keep $attrs (e.g. data-testid) on the inner PrimeVue <Select> only.
// With default inheritance the wrapper <label> would also receive them,
// producing duplicate attributes (e.g. two elements sharing the same
// data-testid) and breaking strict-mode selectors.
defineOptions({ inheritAttrs: false })

const props = withDefaults(defineProps<{
  appendTo?: string
  /**
   * Accessible label for the underlying PrimeVue `Select` (which
   * renders an `<input>` internally).  Falls back to an `aria-label`
   * passed through `$attrs`, then to a generic default.  An explicit
   * association is required so the field is never rendered without a
   * label (SonarCloud `Web:InputWithoutLabelCheck`).
   */
  label?: string
}>(), {
  appendTo: 'self',
  label: '',
})

const attrs = useAttrs()

// Consumers usually pass `aria-label` through `$attrs`; surface it
// explicitly so the label association is statically visible and the
// meaningful per-instance label (e.g. "Level") is preserved.
const resolvedAriaLabel = props.label
  || (attrs['aria-label'] as string | undefined)
  || 'Select'
</script>
