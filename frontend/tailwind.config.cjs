const defaultTheme = require('tailwindcss/defaultTheme')

/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{vue,js,ts,jsx,tsx}'],
  theme: {
    extend: {
      // Bundled via @fontsource-variable/inter and
      // @fontsource-variable/jetbrains-mono (FAR-1253) — keeps Tailwind's
      // preflight (html, code/kbd/samp/pre) and `font-sans`/`font-mono`
      // stacks in line with the body/agent-theme stacks in style.css.
      fontFamily: {
        sans: ['"Inter Variable"', 'Inter', ...defaultTheme.fontFamily.sans],
        mono: ['"JetBrains Mono Variable"', '"JetBrains Mono"', ...defaultTheme.fontFamily.mono],
        // Brand monospace stack (matches json-viewer.css and the agent theme).
        // Used for canvas node kind labels (FAR-1249).
        'brand-mono': ['"JetBrains Mono Variable"', '"JetBrains Mono"', '"SF Mono"', '"Cascadia Code"', 'Consolas', 'ui-monospace', 'monospace'],
      },
      colors: {
        background: 'hsl(var(--background))',
        foreground: 'hsl(var(--foreground))',
        card: 'hsl(var(--card))',
        'card-foreground': 'hsl(var(--card-foreground))',
        popover: 'hsl(var(--popover))',
        'popover-foreground': 'hsl(var(--popover-foreground))',
        primary: 'hsl(var(--primary))',
        'accent-bright': 'hsl(var(--accent-bright))',
        'primary-foreground': 'hsl(var(--primary-foreground))',
        secondary: 'hsl(var(--secondary))',
        'secondary-foreground': 'hsl(var(--secondary-foreground))',
        muted: 'hsl(var(--muted))',
        'muted-foreground': 'hsl(var(--muted-foreground))',
        accent: 'hsl(var(--accent))',
        'accent-foreground': 'hsl(var(--accent-foreground))',
        destructive: 'hsl(var(--destructive))',
        'destructive-foreground': 'hsl(var(--destructive-foreground))',
        success: 'hsl(var(--success))',
        warning: 'hsl(var(--warning))',
        // FAR-1530/FAR-1587: text-safe counterpart of `warning`. `--warning` is
        // tuned as a FILL/accent hue (50% lightness), so amber text on white or
        // on a bg-warning/* tint is only ~1.8-2.1:1 in light mode (WCAG AA needs
        // 4.5:1). `--warning-text` keeps the hue, darkens light mode to 30%
        // lightness, and is identical to `--warning` in the dark and agent
        // themes. Use `text-warning-text` - never `text-warning` - for warning
        // COLOURED TEXT (see style.css for the measured ratios).
        'warning-text': 'hsl(var(--warning-text))',
        pending: 'hsl(var(--pending))',
        preview: 'hsl(var(--preview))',
        border: 'hsl(var(--border))',
        input: 'hsl(var(--input))',
        ring: 'hsl(var(--ring))',
        ink: {
          50: '#E8EDF2',
          100: '#C5CDD8',
          200: '#9CA6B5',
          300: '#7B8794',
          400: '#5A6673',
          500: '#3D4852',
          600: '#262E38',
          700: '#1E2630',
          800: '#12171F',
          900: '#0B0E14',
          950: '#06080C',
        },
        teal: {
          50: '#E6FFFA',
          100: '#B3FFE8',
          200: '#80FFD9',
          300: '#4DFFCB',
          400: '#1AFFBD',
          500: '#00FFD1',
          600: '#00CCA8',
          700: '#009980',
          800: '#006658',
          900: '#003330',
        },
      },
      borderRadius: {
        sm: 'var(--radius-sm)',
        DEFAULT: 'var(--radius)',
        md: 'var(--radius-md)',
        lg: 'var(--radius-lg)',
      },
      transitionTimingFunction: {
        'out': 'var(--ease-out)',
        'in-out': 'var(--ease-in-out)',
      },
      transitionDuration: {
        'micro': 'var(--duration-micro)',
        'fast': 'var(--duration-fast)',
        'normal': 'var(--duration-normal)',
        'slow': 'var(--duration-slow)',
      },
      keyframes: {
        'fade-in-up': {
          '0%': { opacity: '0', transform: 'translateY(8px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
      },
      animation: {
        'fade-in-up': 'fade-in-up 300ms var(--ease-out) forwards',
      },
    },
  },
  plugins: [],
}
