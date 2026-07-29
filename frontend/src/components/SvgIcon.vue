<template>
  <component :is="iconComponent" aria-hidden="true" :size="16" />
</template>

<script setup lang="ts">
import { computed } from "vue";
import CirclePlay from "@lucide/vue/icons/circle-play";
import LayoutDashboard from "@lucide/vue/icons/layout-dashboard";
import BookOpen from "@lucide/vue/icons/book-open";
import Users from "@lucide/vue/icons/users";
import Shield from "@lucide/vue/icons/shield";
import Gauge from "@lucide/vue/icons/gauge";
import Settings from "@lucide/vue/icons/settings";
import Eye from "@lucide/vue/icons/eye";
import FileText from "@lucide/vue/icons/file-text";
import Search from "@lucide/vue/icons/search";
import Flag from "@lucide/vue/icons/flag";
import ChartBarIncreasing from "@lucide/vue/icons/chart-bar-increasing";
import History from "@lucide/vue/icons/history";
import UserCircle from "@lucide/vue/icons/circle-user";
import CheckSquare from "@lucide/vue/icons/square-check-big";
import GitFork from "@lucide/vue/icons/git-fork";
import Database from "@lucide/vue/icons/database";
import MessageSquare from "@lucide/vue/icons/message-square";
import Clipboard from "@lucide/vue/icons/clipboard";
import DollarSign from "@lucide/vue/icons/dollar-sign";
import Cable from "@lucide/vue/icons/cable";
import Plug from "@lucide/vue/icons/plug";
import CreditCard from "@lucide/vue/icons/credit-card";
import Copy from "@lucide/vue/icons/copy";
import Columns from "@lucide/vue/icons/columns-2";
import KeyRound from "@lucide/vue/icons/key-round";
import Tag from "@lucide/vue/icons/tag";
import Puzzle from "@lucide/vue/icons/puzzle";
import Clock from "@lucide/vue/icons/clock";
import Cpu from "@lucide/vue/icons/cpu";
import Zap from "@lucide/vue/icons/zap";
import Building from "@lucide/vue/icons/building";
import ShieldQuestionMark from "@lucide/vue/icons/shield-question-mark";
import Bell from "@lucide/vue/icons/bell";
import Container from "@lucide/vue/icons/container";
import LayoutTemplate from "@lucide/vue/icons/layout-template";
import SlidersHorizontal from "@lucide/vue/icons/sliders-horizontal";
import Building2 from "@lucide/vue/icons/building-2";
import Bot from "@lucide/vue/icons/bot";
import AlertTriangle from "@lucide/vue/icons/triangle-alert";
import GitCommit from "@lucide/vue/icons/git-commit-horizontal";
import FlaskConical from "@lucide/vue/icons/flask-conical";
import Map from "@lucide/vue/icons/map";
import Mail from "@lucide/vue/icons/mail";
import File from "@lucide/vue/icons/file";

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
