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
  props: {
    pipelineId?: string | null
    pipelineName?: string | null
    snapshotId?: string | null
    snapshotVersion?: number | null
  },
  slotText?: string,
): Promise<VueWrapper> {
  // i18n and the shared `router-link` stub (which renders the link target as a
  // real <a href>) both come from the shared setup.ts config.
  return mount(RunPipelineVersionLink, {
    props,
    slots: slotText !== undefined ? { default: slotText } : {},
  })
}

/**
 * FAR-1813 defensive coercion: mounts the component with a payload the PROP
 * TYPE would reject (numeric string, boolean, NaN, float, 0) so the tests
 * prove the render path rejects it — never the type system. The single cast
 * is the deliberate contract violation under test.
 */
function mountWithVersion(snapshotVersion: unknown): VueWrapper {
  return mount(RunPipelineVersionLink, {
    props: {
      pipelineId: 'p-1',
      pipelineName: 'Data Sync',
      snapshotId: 'snap-42',
      snapshotVersion: snapshotVersion as number | null,
    },
    slots: { default: 'Data Sync #7' },
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

  it('renders the pipeline name as plain content (no slot) when there is no link target', async () => {
    // The v-else branch's <slot> fallback only runs when NO caller slot is
    // supplied — the link-branch tests always pass explicit slot text, and the
    // no-slot label test only exercises the v-if branch, leaving this fallback
    // uncovered (FAR-1813 changed-lines coverage gate).
    const wrapper = await mountLink({ pipelineId: null, pipelineName: 'Data Sync', snapshotId: 'snap-42' })
    expect(wrapper.find('a').exists()).toBe(false)
    expect(wrapper.find('span').text()).toBe('Data Sync')
    wrapper.unmount()
  })

  // --- FAR-1813: the `v{n}` version chip ---

  it('renders a localised v{n} chip when the run carries a snapshot version', async () => {
    const wrapper = await mountLink(
      { pipelineId: 'p-1', pipelineName: 'Data Sync', snapshotId: 'snap-42', snapshotVersion: 3 },
      'Data Sync #7',
    )
    const chip = wrapper.find('[data-testid="run-pipeline-version-chip"]')
    expect(chip.exists()).toBe(true)
    expect(chip.text()).toBe('v3')
    // Label and tooltip both come from locale keys — no hardcoded string.
    expect(chip.text()).toBe(t('components.RunPipelineVersionLink.version_chip', { version: 3 }))
    expect(chip.attributes('title')).toBe(
      t('components.RunPipelineVersionLink.version_chip_title', { version: 3 }),
    )
    wrapper.unmount()
  })

  it('the chip label and its title are real locale keys (plain {version} interpolation, no JS expression)', async () => {
    // Pins requirement 5 independently of rendering: if the keys are dropped
    // from en-US.js, vue-i18n returns the raw key path and this fails.
    expect(t('components.RunPipelineVersionLink.version_chip', { version: 3 })).toBe('v3')
    expect(t('components.RunPipelineVersionLink.version_chip_title', { version: 3 })).toContain('3')
  })

  it('renders NO chip for a legacy run (snapshotVersion null) — absence, never v0 or a placeholder shell', async () => {
    const wrapper = await mountLink(
      { pipelineId: 'p-1', pipelineName: 'Data Sync', snapshotId: null, snapshotVersion: null },
      'Data Sync #7',
    )
    const chip = wrapper.find('[data-testid="run-pipeline-version-chip"]')
    expect(chip.exists()).toBe(false)
    const text = wrapper.text()
    expect(text).not.toContain('v0')
    expect(text).not.toContain('vnull')
    expect(text).not.toContain('vNaN')
    expect(text).not.toContain('vundefined')
    expect(text).toContain('Data Sync #7')
    wrapper.unmount()
  })

  it('renders no chip when the version arrives as a NUMERIC STRING (defensive coercion)', async () => {
    const wrapper = mountWithVersion('3')
    const chip = wrapper.find('[data-testid="run-pipeline-version-chip"]')
    expect(chip.exists()).toBe(false)
    wrapper.unmount()
  })

  it('renders no chip when the version is NaN', async () => {
    const wrapper = mountWithVersion(Number.NaN)
    const chip = wrapper.find('[data-testid="run-pipeline-version-chip"]')
    expect(chip.exists()).toBe(false)
    wrapper.unmount()
  })

  it('renders no chip when the version is a boolean', async () => {
    const wrapper = mountWithVersion(true)
    const chip = wrapper.find('[data-testid="run-pipeline-version-chip"]')
    expect(chip.exists()).toBe(false)
    wrapper.unmount()
  })

  it('never renders v0 for a zero version', async () => {
    const wrapper = mountWithVersion(0)
    const chip = wrapper.find('[data-testid="run-pipeline-version-chip"]')
    expect(chip.exists()).toBe(false)
    expect(wrapper.text()).not.toContain('v0')
    wrapper.unmount()
  })

  it('renders no chip for a non-integer version (never v3.5)', async () => {
    const wrapper = mountWithVersion(3.5)
    const chip = wrapper.find('[data-testid="run-pipeline-version-chip"]')
    expect(chip.exists()).toBe(false)
    wrapper.unmount()
  })

  it('ONE tab stop, ONE accessible name: the link announces the pipeline name AND the version together', async () => {
    const wrapper = await mountLink(
      { pipelineId: 'p-1', pipelineName: 'Data Sync', snapshotId: 'snap-42', snapshotVersion: 3 },
      'Data Sync',
    )
    const links = wrapper.findAll('a')
    expect(links).toHaveLength(1)
    const link = links[0]
    expect(link.text()).toContain('Data Sync')
    expect(link.text()).toContain('v3')
    // The chip is INSIDE the link — never a sibling focusable element.
    expect(link.find('[data-testid="run-pipeline-version-chip"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('the chip still renders — as plain, NON-interactive content — when there is no link target', async () => {
    // The version describes the RUN, so it stays honest even when the pipeline
    // row is gone: shown, but never as a second focusable element and never
    // inside a dead link.
    const wrapper = await mountLink(
      { pipelineId: 'p-1', pipelineName: null, snapshotId: 'snap-42', snapshotVersion: 3 },
      '(deleted pipeline)',
    )
    expect(wrapper.find('a').exists()).toBe(false)
    const chip = wrapper.find('[data-testid="run-pipeline-version-chip"]')
    expect(chip.exists()).toBe(true)
    expect(chip.text()).toBe('v3')
    expect(chip.element.tagName).toBe('SPAN')
    expect(wrapper.find('a [data-testid="run-pipeline-version-chip"]').exists()).toBe(false)
    wrapper.unmount()
  })
})
