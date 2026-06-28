<template>
  <div class="p-6 max-w-4xl mx-auto space-y-6">
    <div>
      <h1 class="text-2xl font-bold tracking-tight">Users</h1>
      <p class="text-muted-foreground mt-1">Manage user accounts and permissions.</p>
    </div>

    <div class="card overflow-hidden">
      <table class="w-full text-sm">
        <thead>
          <tr class="border-b bg-muted/30">
            <th class="text-left px-4 py-3 font-medium text-muted-foreground">User</th>
            <th class="text-left px-4 py-3 font-medium text-muted-foreground">Role</th>
            <th class="text-left px-4 py-3 font-medium text-muted-foreground">Status</th>
            <th class="text-right px-4 py-3 font-medium text-muted-foreground">Created</th>
          </tr>
        </thead>
        <tbody>
          <tr class="border-b last:border-0 hover:bg-muted/20 transition-colors">
            <td class="px-4 py-3">
              <div class="flex items-center gap-2">
                <div class="avatar-ring">
                  <div class="flex h-7 w-7 items-center justify-center rounded-full bg-gradient-to-br from-primary to-teal-600 text-xs font-bold text-primary-foreground">
                    {{ userInitial }}
                  </div>
                </div>
                <span class="font-medium">{{ userEmail }}</span>
              </div>
            </td>
            <td class="px-4 py-3">
              <span class="inline-flex items-center rounded-md border border-primary/20 bg-primary/5 px-2 py-0.5 text-xs font-medium text-primary">Admin</span>
            </td>
            <td class="px-4 py-3">
              <span class="inline-flex items-center gap-1.5 rounded-full bg-success/10 px-2.5 py-0.5 text-xs font-medium text-success">
                <span class="h-1.5 w-1.5 rounded-full bg-success" />
                Active
              </span>
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
