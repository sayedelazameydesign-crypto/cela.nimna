import type { Config } from "tailwindcss";

/**
 * Tailwind is wired to the single Design Tokens file: styles/tokens.css
 * Every color is an RGB triplet CSS variable so opacity modifiers work:
 *   bg-accent/20, border-line/70, ...
 */
const config: Config = {
  darkMode: ["class"],
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        canvas: {
          DEFAULT: "rgb(var(--color-canvas) / <alpha-value>)",
          subtle: "rgb(var(--color-canvas-subtle) / <alpha-value>)",
        },
        surface: {
          DEFAULT: "rgb(var(--color-surface) / <alpha-value>)",
          raised: "rgb(var(--color-surface-raised) / <alpha-value>)",
          sunken: "rgb(var(--color-surface-sunken) / <alpha-value>)",
        },
        overlay: "rgb(var(--color-overlay) / <alpha-value>)",
        ink: {
          DEFAULT: "rgb(var(--color-ink) / <alpha-value>)",
          secondary: "rgb(var(--color-ink-secondary) / <alpha-value>)",
          muted: "rgb(var(--color-ink-muted) / <alpha-value>)",
          inverse: "rgb(var(--color-ink-inverse) / <alpha-value>)",
        },
        line: {
          DEFAULT: "rgb(var(--color-line) / <alpha-value>)",
          strong: "rgb(var(--color-line-strong) / <alpha-value>)",
          focus: "rgb(var(--color-line-focus) / <alpha-value>)",
        },
        accent: {
          DEFAULT: "rgb(var(--color-accent) / <alpha-value>)",
          hover: "rgb(var(--color-accent-hover) / <alpha-value>)",
          pressed: "rgb(var(--color-accent-pressed) / <alpha-value>)",
          soft: "rgb(var(--color-accent-soft) / <alpha-value>)",
          ink: "rgb(var(--color-accent-ink) / <alpha-value>)",
        },
        success: {
          DEFAULT: "rgb(var(--color-success) / <alpha-value>)",
          soft: "rgb(var(--color-success-soft) / <alpha-value>)",
        },
        warning: {
          DEFAULT: "rgb(var(--color-warning) / <alpha-value>)",
          soft: "rgb(var(--color-warning-soft) / <alpha-value>)",
        },
        danger: {
          DEFAULT: "rgb(var(--color-danger) / <alpha-value>)",
          soft: "rgb(var(--color-danger-soft) / <alpha-value>)",
        },
        info: {
          DEFAULT: "rgb(var(--color-info) / <alpha-value>)",
          soft: "rgb(var(--color-info-soft) / <alpha-value>)",
        },
        "user-bubble": "rgb(var(--color-user-bubble) / <alpha-value>)",
        "code-block": "rgb(var(--color-code-block) / <alpha-value>)",
        "code-ink": "rgb(var(--color-code-ink) / <alpha-value>)",
      },
      fontFamily: {
        sans: ["var(--font-ui)"],
        display: ["var(--font-display)"],
        mono: ["var(--font-code)"],
      },
      fontSize: {
        "2xs": ["var(--text-micro)", { lineHeight: "1.45" }],
        caption: ["var(--text-caption)", { lineHeight: "1.5" }],
        body: ["var(--text-body)", { lineHeight: "1.6" }],
        title: [
          "var(--text-title)",
          { lineHeight: "1.4", letterSpacing: "-0.01em" },
        ],
        display: [
          "var(--text-display)",
          { lineHeight: "1.3", letterSpacing: "-0.015em" },
        ],
        "display-lg": [
          "var(--text-display-lg)",
          { lineHeight: "1.22", letterSpacing: "-0.02em" },
        ],
      },
      borderRadius: {
        xs: "var(--radius-xs)",
        sm: "var(--radius-sm)",
        md: "var(--radius-md)",
        lg: "var(--radius-lg)",
        xl: "var(--radius-xl)",
        "2xl": "var(--radius-2xl)",
      },
      boxShadow: {
        hairline: "var(--shadow-hairline)",
        card: "var(--shadow-card)",
        pop: "var(--shadow-pop)",
        ring: "var(--shadow-focus-ring)",
      },
      maxWidth: {
        chat: "var(--layout-chat-max-width)",
      },
      width: {
        sidebar: "var(--layout-sidebar-width)",
      },
      height: {
        header: "var(--layout-header-height)",
      },
      transitionDuration: {
        instant: "var(--duration-instant)",
        fast: "var(--duration-fast)",
        base: "var(--duration-base)",
        slow: "var(--duration-slow)",
      },
      transitionTimingFunction: {
        standard: "var(--ease-standard)",
        enter: "var(--ease-enter)",
        exit: "var(--ease-exit)",
      },
      keyframes: {
        "fade-in": {
          from: { opacity: "0" },
          to: { opacity: "1" },
        },
        "rise-in": {
          from: { opacity: "0", transform: "translateY(6px)" },
          to: { opacity: "1", transform: "none" },
        },
        "pulse-dot": {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.35" },
        },
      },
      animation: {
        "fade-in": "fade-in var(--duration-base) var(--ease-standard) both",
        "rise-in": "rise-in var(--duration-slow) var(--ease-enter) both",
        "pulse-dot": "pulse-dot 1.6s ease-in-out infinite",
      },
    },
  },
  plugins: [require("tailwindcss-animate")],
};

export default config;
