<template>
  <div
    class="overflow-x-auto rounded-lg border bg-card shadow-sm"
    role="status"
    aria-busy="true"
    :aria-label="ariaLabel"
  >
    <table class="w-full text-left text-sm">
      <thead class="bg-muted/50 text-xs font-medium uppercase text-muted-foreground">
        <tr>
          <th
            v-for="col in columns"
            :key="col"
            class="px-4 py-4"
            :class="col === columns ? 'text-right' : 'text-left'"
          >
            <span
              class="inline-block h-3 w-24 animate-pulse rounded bg-muted"
              :class="col === columns ? 'ml-auto' : ''"
            />
          </th>
        </tr>
      </thead>
      <tbody class="divide-y">
        <tr v-for="row in rows" :key="row">
          <td
            v-for="col in columns"
            :key="col"
            class="px-4 py-4"
            :class="col === columns ? 'text-right' : 'text-left'"
          >
            <span
              class="inline-block h-4 w-full animate-pulse rounded bg-muted"
              :class="col === columns ? 'ml-auto' : ''"
            />
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup lang="ts">
// Shared table loading skeleton (ux-conformance STATE-3 + VIS-3). The `columns`
// prop is the single source of truth for the placeholder column count so a
// caller can derive it from the same array that renders its real <th> row and
// the placeholder can never silently desync from the real layout.
withDefaults(
  defineProps<{
    columns: number
    rows?: number
    ariaLabel?: string
  }>(),
  {
    rows: 5,
    ariaLabel: undefined,
  },
)
</script>
