/* eslint-env node */
require('@rushstack/eslint-patch/modern-module-resolution')

module.exports = {
  root: true,
  extends: [
    'plugin:vue/vue3-essential',
    'eslint:recommended',
    '@vue/eslint-config-typescript',
    'plugin:vuejs-accessibility/recommended',
  ],
  parserOptions: {
    ecmaVersion: 'latest',
  },
  plugins: [
    // 'no-secrets' disabled — false positives on i18n keys and generated OpenAPI types
  ],
  rules: {
    'no-console': 'warn',
    'vue/component-api-style': ['error', ['script-setup']],
    'vue/multi-word-component-names': ['error', { ignores: ['Badge', 'Button', 'Card', 'Dialog', 'Input', 'Select', 'Tabs', 'Tooltip', 'LogoMark', 'SidebarLink', 'OwnershipPicker', 'SsoProviderForm'] }],
    // Accessibility (eslint-plugin-vuejs-accessibility)
    'vuejs-accessibility/aria-props': 'error',
    'vuejs-accessibility/alt-text': 'error',
    'vuejs-accessibility/no-autofocus': 'warn',
    'vuejs-accessibility/tabindex-no-positive': 'error',
    'vuejs-accessibility/aria-unsupported-elements': 'error',
    'vuejs-accessibility/click-events-have-key-events': 'warn',
    'vuejs-accessibility/form-control-has-label': 'warn',
    'vuejs-accessibility/heading-has-content': 'warn',
    'vuejs-accessibility/label-has-for': 'warn',
    'vuejs-accessibility/mouse-events-have-key-events': 'warn',
    // 'vuejs-accessibility/no-access-state' was removed in v2.x
    'vuejs-accessibility/no-distracting-elements': 'error',
    'vuejs-accessibility/no-static-element-interactions': 'warn',
    'vuejs-accessibility/role-has-required-aria-props': 'error',
    'vuejs-accessibility/iframe-has-title': 'error',
    'vuejs-accessibility/interactive-supports-focus': 'warn',
    'vuejs-accessibility/anchor-has-content': 'warn',
    // 'no-secrets/no-secrets' disabled — false positives on i18n keys and generated types
  },
}
