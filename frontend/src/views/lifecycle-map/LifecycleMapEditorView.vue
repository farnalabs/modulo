<template>
  <BackLink to="/lifecycle-maps" label="Back to Lifecycle Maps" />
  <LifecycleMapEditor
    :map-id="mapId"
    @saved="onSaved"
  />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import BackLink from '../../components/BackLink.vue'
import LifecycleMapEditor from '../../components/lifecycle-map/editor/LifecycleMapEditor.vue'

const route = useRoute()
const mapId = computed(() => route.params.id as string)

function onSaved() {
  // Toast or notification handled by parent
}
</script>
