<template>
  <svg
    v-if="preset === 'google'"
    class="h-5 w-5 shrink-0"
    viewBox="0 0 24 24"
    aria-hidden="true"
    focusable="false"
  >
    <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 0 1-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z" fill="#4285F4"/>
    <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/>
    <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05"/>
    <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/>
  </svg>

  <svg
    v-else-if="preset === 'auth0'"
    class="h-5 w-5 shrink-0"
    viewBox="0 0 24 24"
    aria-hidden="true"
    focusable="false"
  >
    <path d="M21.988 8.024a10.9 10.9 0 0 0-.164-1.672 10.9 10.9 0 0 0-11.486-8.352A10.9 10.9 0 0 0 2.6 8.674l.003.04a10.9 10.9 0 0 0-.023 1.288 10.86 10.86 0 0 0 .05 1.012l-.05-.001h18.533c.04.066.075.134.114.2a10.97 10.97 0 0 1 .761 3.454 10.9 10.9 0 0 1-1.51 5.266 10.85 10.85 0 0 1-4.342 3.672 10.9 10.9 0 0 1-5.856.444 10.84 10.84 0 0 1-4.883-2.677l1.38-1.078a8.34 8.34 0 0 0 3.71 2.087 8.38 8.38 0 0 0 4.515-.35 8.35 8.35 0 0 0 3.36-2.835 8.4 8.4 0 0 0 1.17-4.07 8.38 8.38 0 0 0-.586-2.675H2.777l-.002-.004c.23-3.576 3.16-6.46 6.752-6.8a6.8 6.8 0 0 1 6.642 4.873l-3.081 2.388 5.9 1.317z" fill="#EB5424"/>
  </svg>

  <svg
    v-else-if="preset === 'okta'"
    class="h-5 w-5 shrink-0"
    viewBox="0 0 24 24"
    aria-hidden="true"
    focusable="false"
  >
    <circle cx="12" cy="12" r="10" fill="#007DC1"/>
    <circle cx="12" cy="12" r="4" fill="#fff"/>
  </svg>

  <svg
    v-else-if="preset === 'azure-ad'"
    class="h-5 w-5 shrink-0"
    viewBox="0 0 24 24"
    aria-hidden="true"
    focusable="false"
  >
    <path d="M11.4 2L2 6.5v11l9.4 4.5L21 17.5V6.5L11.4 2z" fill="#0078D4"/>
    <path d="M11.4 2L21 6.5l-9.6 5.5L11.4 2z" fill="#00A4EF"/>
    <path d="M2 6.5l9.4 4.5v11L2 17.5V6.5z" fill="#50E6FF"/>
    <path d="M11.4 11l9.6-4.5v11L11.4 22v-11z" fill="#FFB900"/>
  </svg>

  <svg
    v-else-if="preset === 'onelogin'"
    class="h-5 w-5 shrink-0"
    viewBox="0 0 24 24"
    aria-hidden="true"
    focusable="false"
  >
    <rect width="24" height="24" rx="4" fill="#2D374B"/>
    <path d="M8 8h3v3H8V8zm0 5h3v3H8v-3zm5-5h3v3h-3V8zm0 5h3v3h-3v-3z" fill="#fff"/>
  </svg>

  <Lock
    v-else
    class="h-5 w-5 shrink-0 text-muted-foreground"
    aria-hidden="true"
    focusable="false"
  />
</template>

<script setup lang="ts">
import { Lock } from "@lucide/vue"

defineProps<{
  preset: string
}>()
</script>
