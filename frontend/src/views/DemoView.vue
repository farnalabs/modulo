<template>
  <div class="relative mx-auto flex min-h-screen max-w-md items-center justify-center overflow-x-hidden p-6">
    <div class="text-center space-y-2" data-testid="demo-view">
      <h1 class="text-3xl font-bold tracking-tight">{{ $t('views.DemoView.preparing') }}</h1>
      <p class="text-muted-foreground">{{ $t('views.DemoView.signing_in') }}</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { resolveDemoEntry } from '../lib/api/demo'

const router = useRouter()

// Defensive fallback: the /demo route guard performs the hand-off pre-mount and
// redirects away before this component ever renders. If this view is ever
// reached anyway (guard bypassed), apply the SAME rule as the guard so the two
// paths cannot drift: a live session (demo or real) is never torn down — go to
// the dashboard; only a tokenless browser runs the hand-off, once. qa iter 2:
// the rule itself lives in resolveDemoEntry (lib/api/demo.ts). Redirect targets
// use the same route names as the guard (dashboard on success, plain login on
// failure — no error may reveal demo internals).
onMounted(async () => {
  router.replace({ name: await resolveDemoEntry() })
})
</script>
