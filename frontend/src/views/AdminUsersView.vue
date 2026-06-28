<template>
  <div class="p-6 max-w-4xl mx-auto">
    <h1 class="text-2xl font-bold mb-2">Users</h1>
    <p class="text-muted-foreground mb-6">Manage user accounts and permissions.</p>

    <div class="rounded-lg border">
      <table class="w-full text-sm">
        <thead>
          <tr class="border-b bg-muted/50">
            <th class="text-left px-4 py-3 font-medium">User</th>
            <th class="text-left px-4 py-3 font-medium">Role</th>
            <th class="text-left px-4 py-3 font-medium">Status</th>
            <th class="text-right px-4 py-3 font-medium">Created</th>
          </tr>
        </thead>
        <tbody>
          <tr class="border-b last:border-0">
            <td class="px-4 py-3">
              <div class="flex items-center gap-2">
                <div class="flex h-7 w-7 items-center justify-center rounded-full bg-primary text-xs font-bold text-primary-foreground">
                  {{ userInitial }}
                </div>
                <span>{{ userEmail }}</span>
              </div>
            </td>
            <td class="px-4 py-3 text-muted-foreground">Admin</td>
            <td class="px-4 py-3">
              <span class="rounded-full bg-green-500/10 px-2 py-0.5 text-xs font-medium text-green-600 dark:text-green-400">Active</span>
            </td>
            <td class="px-4 py-3 text-right text-muted-foreground">—</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { getAccessToken } from '../lib/api/client'

const userEmail = computed(() => {
  const token = getAccessToken()
  if (!token) return ''
  try {
    const payload = JSON.parse(atob(token.split('.')[1]))
    return payload.sub || ''
  } catch {
    return ''
  }
})

const userInitial = computed(() => {
  const email = userEmail.value
  if (!email) return '?'
  return email.charAt(0).toUpperCase()
})
</script>
