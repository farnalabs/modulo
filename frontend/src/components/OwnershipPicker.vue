<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import { PopoverRoot, PopoverTrigger, PopoverContent } from "radix-vue";
import type { Component } from "vue";

// Inline SVG icon components (lucide-vue-next bundle is broken in this version)
const ChevronDown: Component = {
  template:
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>',
};
const Earth: Component = {
  template:
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>',
};
const Users: Component = {
  template:
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
};
import { api } from "../lib/api/client";
import type { components } from "../lib/api/client";

type TeamItem = components["schemas"]["AdminTeamItem"];

export interface OwnershipValue {
  owner_team_id: string | null;
  visibility: "org" | "team";
}

const props = defineProps<{
  modelValue?: OwnershipValue | null;
  label?: string;
}>();

const emit = defineEmits<{
  (e: "update:modelValue", value: OwnershipValue): void;
}>();

const open = ref(false);
const teams = ref<TeamItem[]>([]);
const loading = ref(true);
const fetchError = ref<string | null>(null);

const selectedLabel = computed(() => {
  if (!props.modelValue) return null;
  if (props.modelValue.visibility === "org") return "Org-wide";
  const team = teams.value.find(
    (t) => t.id === props.modelValue!.owner_team_id,
  );
  return team?.name ?? "Unknown team";
});

function selectOrg() {
  emit("update:modelValue", { owner_team_id: null, visibility: "org" });
  open.value = false;
}

function selectTeam(team: TeamItem) {
  emit("update:modelValue", { owner_team_id: team.id, visibility: "team" });
  open.value = false;
}

async function loadTeams() {
  loading.value = true;
  fetchError.value = null;
  const { data, error } = await api.GET("/api/v1/admin/teams");
  if (error) {
    fetchError.value = "Failed to load teams";
  } else if (data) {
    teams.value = data.items;
  }
  loading.value = false;
}

onMounted(loadTeams);
</script>

<template>
  <div class="space-y-1.5">
    <label v-if="label" class="text-sm font-medium leading-none">
      {{ label }}
    </label>
    <PopoverRoot v-model:open="open">
      <PopoverTrigger
        as="button"
        type="button"
        class="flex w-full items-center justify-between rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground hover:bg-accent/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 disabled:cursor-not-allowed disabled:opacity-50 [&>svg]:shrink-0 transition-colors"
      >
        <span v-if="selectedLabel">{{ selectedLabel }}</span>
        <span v-else class="text-muted-foreground">{{ $t('components.OwnershipPicker.select_ownership') }}</span>
        <ChevronDown
          class="h-4 w-4 text-muted-foreground transition-transform duration-200"
          :class="open && 'rotate-180'"
        />
      </PopoverTrigger>
      <PopoverContent
        class="z-50 w-[--radix-popover-trigger-width] min-w-[200px] overflow-hidden rounded-lg border bg-popover p-1 text-popover-foreground shadow-md"
        :side-offset="4"
        align="start"
      >
        <button
          type="button"
          class="relative flex w-full cursor-default select-none items-center rounded-sm px-2 py-1.5 text-sm outline-none hover:bg-accent hover:text-accent-foreground focus-visible:bg-accent focus-visible:text-accent-foreground"
          :class="{
            'bg-accent text-accent-foreground':
              modelValue?.visibility === 'org' && !modelValue?.owner_team_id,
          }"
          @click="selectOrg"
        >
          <Earth class="mr-2 h-4 w-4 text-muted-foreground shrink-0" />
          <span class="flex-1">{{ $t('components.OwnershipPicker.orgwide') }}</span>
          <span class="text-xs text-muted-foreground">{{ $t('components.OwnershipPicker.everyone_in_the_org') }}</span>
        </button>
        <div
          v-if="teams.length > 0"
          class="my-1 h-px bg-border"
          role="separator"
        />
        <template v-if="loading">
          <div class="px-2 py-4 text-center text-sm text-muted-foreground">
            Loading teams...
          </div>
        </template>
        <template v-else-if="fetchError">
          <div class="px-2 py-4 text-center text-sm text-destructive">
            {{ fetchError }}
          </div>
        </template>
        <template v-else-if="teams.length > 0">
          <div class="px-2 py-1.5 text-xs font-medium text-muted-foreground">
            Teams
          </div>
          <button
            v-for="team in teams"
            :key="team.id"
            type="button"
            class="relative flex w-full cursor-default select-none items-center rounded-sm px-2 py-1.5 text-sm outline-none hover:bg-accent hover:text-accent-foreground focus-visible:bg-accent focus-visible:text-accent-foreground"
            :class="{
              'bg-accent text-accent-foreground':
                modelValue?.owner_team_id === team.id,
            }"
            @click="selectTeam(team)"
          >
            <Users class="mr-2 h-4 w-4 text-muted-foreground shrink-0" />
            <span class="flex-1">{{ team.name }}</span>
            <span class="text-xs text-muted-foreground"
              >{{ team.member_count }}
              {{ team.member_count === 1 ? "member" : "members" }}</span
            >
          </button>
        </template>
      </PopoverContent>
    </PopoverRoot>
  </div>
</template>
