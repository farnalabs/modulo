<template>
  <component :is="iconComponent" aria-hidden="true" :size="16" />
</template>

<script setup lang="ts">
import { computed } from "vue";
import { CirclePlay, LayoutDashboard, BookOpen, Users, Shield, Gauge, Settings, Eye, FileText, Search, Flag, ChartBarIncreasing, History, UserCircle, CheckSquare, GitFork, Database, MessageSquare, Clipboard, DollarSign, Cable, Plug, CreditCard, Copy, Columns, KeyRound, Tag, Puzzle, Clock, Cpu, Zap, Building, ShieldQuestionMark, Bell, Container, LayoutTemplate, SlidersHorizontal, Building2, Bot, AlertTriangle, GitCommit, FlaskConical, Map, Mail, File, Sparkles, ShieldAlert } from "@lucide/vue";

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
  // "Broom" is not exported by @lucide/vue 1.27.0 — housekeeping (cleanup) is
  // represented by Sparkles ("sparkling clean") instead.
  Broom: Sparkles,
  ShieldAlert,
};

const props = defineProps<{
  name: string;
}>();

const iconComponent = computed(() => {
  const icon = iconMap[props.name];
  if (!icon) {
    console.warn(`SvgIcon: unknown icon "${props.name}"`);
  }
  return icon || iconMap.File;
});
</script>
