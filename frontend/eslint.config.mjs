import pluginVue from 'eslint-plugin-vue'
import vuejsAccessibility from 'eslint-plugin-vuejs-accessibility'
import { withVueTs, vueTsConfigs } from '@vue/eslint-config-typescript'

export default withVueTs(
  {
    ignores: ['dist/**', 'coverage/**', 'node_modules/**'],
  },
  pluginVue.configs['flat/essential'],
  vueTsConfigs.recommended,
  vuejsAccessibility.configs['flat/recommended'],
  {
    rules: {
      'no-console': ['error', { allow: ['warn', 'error'] }],
      // vue-tsc owns unused-symbol enforcement to avoid duplicate diagnostics.
      '@typescript-eslint/no-unused-vars': 'off',
      // Preserve the legacy config contract; these are tracked by vue-tsc or are
      // intentionally used at API and test boundaries throughout the codebase.
      '@typescript-eslint/no-explicit-any': 'off',
      '@typescript-eslint/ban-ts-comment': 'warn',
      '@typescript-eslint/no-this-alias': 'warn',
      'vue/component-api-style': ['error', ['script-setup']],
      'vue/multi-word-component-names': ['error', {
        ignores: ['Badge', 'Button', 'Card', 'Dialog', 'Input', 'Select', 'Tabs', 'Tooltip', 'LogoMark', 'SidebarLink', 'OwnershipPicker', 'SsoProviderForm'],
      }],
      'vuejs-accessibility/aria-props': 'error',
      'vuejs-accessibility/alt-text': 'error',
      'vuejs-accessibility/no-autofocus': 'warn',
      'vuejs-accessibility/tabindex-no-positive': 'error',
      'vuejs-accessibility/aria-unsupported-elements': 'error',
      'vuejs-accessibility/click-events-have-key-events': 'warn',
      'vuejs-accessibility/form-control-has-label': 'warn',
      'vuejs-accessibility/heading-has-content': 'warn',
      'vuejs-accessibility/label-has-for': ['error', { required: { some: ['id', 'nesting'] } }],
      'vuejs-accessibility/mouse-events-have-key-events': 'warn',
      'vuejs-accessibility/no-distracting-elements': 'error',
      'vuejs-accessibility/no-static-element-interactions': 'warn',
      'vuejs-accessibility/role-has-required-aria-props': 'error',
      'vuejs-accessibility/iframe-has-title': 'error',
      'vuejs-accessibility/interactive-supports-focus': 'warn',
      'vuejs-accessibility/anchor-has-content': 'warn',
    },
  },
)
