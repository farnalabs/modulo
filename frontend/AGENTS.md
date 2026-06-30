# Frontend — Agent Guidance

## Lessons Learned

- When rewriting or restoring a layout component (e.g., `AppLayout.vue` after a SFC parsing fix), always verify that responsive hiding classes (`hidden md:flex` on desktop sidebar, `md:hidden` on mobile elements) are preserved. These are easily lost during a restore from a pre-mobile baseline.
