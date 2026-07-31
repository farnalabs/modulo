<template>
  <div class="space-y-1.5">
    <span v-if="label" class="text-sm font-medium leading-none">
      {{ label }}
    </span>
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
          <Globe class="mr-2 h-4 w-4 text-muted-foreground shrink-0" />
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
            {{ $t('components.OwnershipPicker.loading_teams') }}
          </div>
        </template>
        <template v-else-if="fetchError">
          <div class="px-2 py-4 text-center text-sm text-destructive">
            {{ fetchError }}
          </div>
        </template>
        <template v-else-if="teams.length > 0">
          <div class="px-2 py-1.5 text-xs font-medium text-muted-foreground">
            {{ $t('components.OwnershipPicker.teams_header') }}
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
              {{ team.member_count === 1 ? $t('components.OwnershipPicker.member') : $t('components.OwnershipPicker.members') }}</span
            >
          </button>
        </template>
      </PopoverContent>
    </PopoverRoot>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import { PopoverRoot, PopoverTrigger, PopoverContent } from "radix-vue";
import { useI18n } from "vue-i18n";
import { ChevronDown } from "@lucide/vue";
import { Globe } from "@lucide/vue";
import { Users } from "@lucide/vue";
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

const { t } = useI18n();

const open = ref(false);
const teams = ref<TeamItem[]>([]);
const loading = ref(true);
const fetchError = ref<string | null>(null);

const selectedLabel = computed(() => {
  if (!props.modelValue) return null;
  if (props.modelValue.visibility === "org") return t("components.OwnershipPicker.orgwide");
  const team = teams.value.find(
    (t) => t.id === props.modelValue?.owner_team_id,
  );
  return team?.name ?? t("components.OwnershipPicker.unknown_team");
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
  try {
    const { data, error } = await api.GET("/api/v1/admin/teams");
    if (error) {
      fetchError.value = t("components.OwnershipPicker.failed_to_load_teams");
    } else if (data) {
      teams.value = data.items;
    }
  } catch (e: unknown) {
    fetchError.value = e instanceof Error ? e.message : t("components.OwnershipPicker.failed_to_load_teams");
  } finally {
    loading.value = false;
  }
}

onMounted(loadTeams);
</script>
