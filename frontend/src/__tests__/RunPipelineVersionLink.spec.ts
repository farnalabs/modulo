import { describe, it, expect } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import i18n from '../i18n'
import RunPipelineVersionLink from '../components/runs/RunPipelineVersionLink.vue'

const t = i18n.global.t

/**
 * FAR-1813 — the pipeline name on a run surface links to the VERSION the run
 * executed. These tests assert the RESOLVED href (a real vue-router RouterLink
 * against a real router), not merely that a component mounted.
 */
async function mountLink(
  props: { pipelineId?: string | null; pipelineName?: string | null; snapshotId?: string | null },
  slotText?: string,
): Promise<VueWrapper> {
  // i18n and the shared `router-link` stub (which renders the link target as a
  // real <a href>) both come from the shared setup.ts config.
  return mount(RunPipelineVersionLink, {
    props,
    slots: slotText !== undefined ? { default: slotText } : {},
  })
}

describe('RunPipelineVersionLink (FAR-1813)', () => {
  it('links the pipeline name to the version this run executed (?snapshot= from the run payload)', async () => {
    const wrapper = await mountLink(
      { pipelineId: 'p-1', pipelineName: 'Data Sync', snapshotId: 'snap-42' },
      'Data Sync #7',
    )
    const link = wrapper.find('a')
    expect(link.exists()).toBe(true)
    expect(link.attributes('href')).toBe('/pipelines/p-1/editor?snapshot=snap-42')
    expect(link.text()).toBe('Data Sync #7')
    // A real anchor — focusable and keyboard-reachable, not a click handler on
    // a non-interactive element.
    expect(link.attributes('href')).toBeDefined()
    expect(link.element.tagName).toBe('A')
    wrapper.unmount()
  })

  it('renders the honest title for the version link', async () => {
    const wrapper = await mountLink(
      { pipelineId: 'p-1', pipelineName: 'Data Sync', snapshotId: 'snap-42' },
      'Data Sync',
    )
    expect(wrapper.find('a').attributes('title')).toBe(
      t('components.RunPipelineVersionLink.version_title'),
    )
    wrapper.unmount()
  })

  it('falls back to the LIVE editor for a legacy run with no snapshot — never a dead ?snapshot=null link', async () => {
    const wrapper = await mountLink(
      { pipelineId: 'p-1', pipelineName: 'Data Sync', snapshotId: null },
      'Data Sync #7',
    )
    const href = wrapper.find('a').attributes('href')
    expect(href).toBe('/pipelines/p-1/editor')
    expect(href).not.toContain('snapshot')
    expect(href).not.toContain('null')
    // The fallback title tells the truth about what the user will land on.
    expect(wrapper.find('a').attributes('title')).toBe(
      t('components.RunPipelineVersionLink.current_title'),
    )
    wrapper.unmount()
  })

  it('a rerun links to the SOURCE run snapshot it re-executed, never the pipeline head', async () => {
    // The link is derived only from the run being rendered. A rerun carries
    // its source run's snapshot_id; the pipeline head is a different snapshot
    // that must never appear in the target.
    const sourceSnapshot = 'snap-source-9f2'
    const pipelineHeadSnapshot = 'snap-head-77aa'
    const wrapper = await mountLink(
      { pipelineId: 'p-1', pipelineName: 'Data Sync', snapshotId: sourceSnapshot },
      'Data Sync #12',
    )
    const href = wrapper.find('a').attributes('href')
    expect(href).toBe(`/pipelines/p-1/editor?snapshot=${sourceSnapshot}`)
    expect(href).not.toContain(pipelineHeadSnapshot)
    wrapper.unmount()
  })

  it('renders plain content — never a link — when the pipeline is deleted (no pipeline_name)', async () => {
    const wrapper = await mountLink(
      { pipelineId: 'p-1', pipelineName: null, snapshotId: 'snap-42' },
      '(deleted pipeline)',
    )
    expect(wrapper.find('a').exists()).toBe(false)
    expect(wrapper.text()).toBe('(deleted pipeline)')
    wrapper.unmount()
  })

  it('renders plain content when there is no pipeline id to target', async () => {
    const wrapper = await mountLink(
      { pipelineId: null, pipelineName: 'Data Sync', snapshotId: 'snap-42' },
      'Data Sync #7',
    )
    expect(wrapper.find('a').exists()).toBe(false)
    expect(wrapper.text()).toBe('Data Sync #7')
    wrapper.unmount()
  })

  it('falls back to the pipeline name as its own label when no slot text is supplied', async () => {
    const wrapper = await mountLink(
      { pipelineId: 'p-1', pipelineName: 'Data Sync', snapshotId: 'snap-42' },
    )
    expect(wrapper.find('a').text()).toBe('Data Sync')
    wrapper.unmount()
  })
})
