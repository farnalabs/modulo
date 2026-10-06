<template>
  <!-- @vue-flow/controls@1.1.3 renders its zoom/fit-view buttons as icon-only
       <button>s with no accessible name (axe button-name, critical). Override
       each icon slot with the library's own SVG plus sr-only text so every
       control is named. One wrapper = one fix, shared by every Vue Flow
       surface that renders <Controls>. -->
  <Controls :show-interactive="showInteractive" :position="position">
    <template #icon-zoom-in>
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" aria-hidden="true"><path d="M32 18.133H18.133V32h-4.266V18.133H0v-4.266h13.867V0h4.266v13.867H32z" /></svg>
      <span class="sr-only">{{ t('components.FlowControls.zoom_in') }}</span>
    </template>
    <template #icon-zoom-out>
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 5" aria-hidden="true"><path d="M0 0h32v4.2H0z" /></svg>
      <span class="sr-only">{{ t('components.FlowControls.zoom_out') }}</span>
    </template>
    <template #icon-fit-view>
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 30" aria-hidden="true"><path d="M3.692 4.63c0-.53.4-.938.939-.938h5.215V0H4.708C2.13 0 0 2.054 0 4.63v5.216h3.692V4.631zM27.354 0h-5.2v3.692h5.17c.53 0 .984.4.984.939v5.215H32V4.631A4.624 4.624 0 0 0 27.354 0zm.954 24.83c0 .532-.4.94-.939.94h-5.215v3.768h5.215c2.577 0 4.631-2.13 4.631-4.707v-5.139h-3.692v5.139zm-23.677.94a.919.919 0 0 1-.939-.94v-5.138H0v5.139c0 2.577 2.13 4.707 4.708 4.707h5.138V25.77H4.631z" /></svg>
      <span class="sr-only">{{ t('components.FlowControls.fit_view') }}</span>
    </template>
  </Controls>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { Controls } from '@vue-flow/controls'
import type { PanelPosition, PanelPositionType } from '@vue-flow/core'

const { t } = useI18n()

withDefaults(
  defineProps<{
    /** Show the lock/unlock interaction toggle. Off by default: every surface
     *  in this product renders read-only zoom/fit controls. */
    showInteractive?: boolean
    /** Panel corner. Matches the library default so consumers that omit it
     *  behave exactly as they did before this wrapper existed. */
    position?: PanelPositionType | PanelPosition
  }>(),
  {
    showInteractive: false,
    position: 'bottom-left',
  },
)
</script>
