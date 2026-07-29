<template>
  <component :is="iconComponent" aria-hidden="true" :size="16" />
</template>

<script setup lang="ts">
import { computed } from "vue";
import { CirclePlay } from "@lucide/vue";
import { LayoutDashboard } from "@lucide/vue";
import { BookOpen } from "@lucide/vue";
import { Users } from "@lucide/vue";
import { Shield } from "@lucide/vue";
import { Gauge } from "@lucide/vue";
import { Settings } from "@lucide/vue";
import { Eye } from "@lucide/vue";
import { FileText } from "@lucide/vue";
import { Search } from "@lucide/vue";
import { Flag } from "@lucide/vue";
import { ChartBarIncreasing } from "@lucide/vue";
import { History } from "@lucide/vue";
import { UserCircle } from "@lucide/vue";
import { CheckSquare } from "@lucide/vue";
import { GitFork } from "@lucide/vue";
import { Database } from "@lucide/vue";
import { MessageSquare } from "@lucide/vue";
import { Clipboard } from "@lucide/vue";
import { DollarSign } from "@lucide/vue";
import { Cable } from "@lucide/vue";
import { Plug } from "@lucide/vue";
import { CreditCard } from "@lucide/vue";
import { Copy } from "@lucide/vue";
import { Columns } from "@lucide/vue";
import { KeyRound } from "@lucide/vue";
import { Tag } from "@lucide/vue";
import { Puzzle } from "@lucide/vue";
import { Clock } from "@lucide/vue";
import { Cpu } from "@lucide/vue";
import { Zap } from "@lucide/vue";
import { Building } from "@lucide/vue";
import { ShieldQuestionMark } from "@lucide/vue";
import { Bell } from "@lucide/vue";
import { Container } from "@lucide/vue";
import { LayoutTemplate } from "@lucide/vue";
import { SlidersHorizontal } from "@lucide/vue";
import { Building2 } from "@lucide/vue";
import { Bot } from "@lucide/vue";
import { AlertTriangle } from "@lucide/vue";
import { GitCommit } from "@lucide/vue";
import { FlaskConical } from "@lucide/vue";
import { Map } from "@lucide/vue";
import { Mail } from "@lucide/vue";
import { File } from "@lucide/vue";

const iconMap: Record<string, unknown> = {
  CirclePlay,
  LayoutDashboard,
  BookOpen,
  Users,
  Shield,
  Gauge,
  Settings,
  Eye,
  FileText,
  Search,
  Flag,
  BarChart: ChartBarIncreasing,
  History,
  UserCircle,
  CheckSquare,
  GitFork,
  Database,
  MessageSquare,
  Clipboard,
  DollarSign,
  Cable,
  Plug,
  CreditCard,
  Copy,
  Columns,
  KeyRound,
  Tag,
  Puzzle,
  Clock,
  Cpu,
  Zap,
  Building,
  ShieldQuestion: ShieldQuestionMark,
  Bell,
  Container,
  LayoutTemplate,
  SlidersHorizontal,
  Building2,
  Bot,
  AlertTriangle,
  GitCommit,
  FlaskConical,
  Map,
  Mail,
  File,
};

const props = defineProps<{
  name: string;
}>();

const iconComponent = computed(() => {
  const icon = iconMap[props.name];
  if (!icon) {
    console.warn(`SvgIcon: unknown icon "${props.name}"`);
  }
  return icon;
});
</script>


