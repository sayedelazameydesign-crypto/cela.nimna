# Nimna Web — Agent Workspace UI

A **Manus × Claude** inspired frontend for the Nimna agent.
**Current phase: structure only** — layout, tokens, and static components.
No state, no event handlers, no API calls yet (interactions are gated behind design approval).

## Stack

- Next.js 15 (App Router) + TypeScript (strict)
- Tailwind CSS 3.4 + `tailwindcss-animate`
- shadcn-compatible conventions (`components.json` ready) — primitives are
  dependency-free for now; Radix-based shadcn components can be added with
  `npx shadcn add` when interactions are approved
- lucide-react icons

## File tree

```
web/
├── package.json                 # scripts: dev / build / typecheck
├── next.config.mjs
├── postcss.config.mjs
├── tailwind.config.ts           # maps every token → Tailwind utility
├── components.json              # shadcn config (ready for phase 2)
├── tsconfig.json
├── styles/
│   └── tokens.css               # ★ SINGLE design-tokens file (light + dark)
├── app/
│   ├── layout.tsx               # fonts (Inter / Newsreader / JetBrains Mono)
│   ├── globals.css              # base styles, scrollbars, selection
│   ├── page.tsx                 # composes the shell
│   └── icon.svg                 # favicon
├── lib/
│   ├── utils.ts                 # cn() helper
│   └── mock.ts                  # static demo content (no logic)
└── components/
    ├── layout/
    │   ├── app-shell.tsx        # 3-column grid: sidebar | chat | artifact
    │   └── sidebar.tsx          # brand, new task, nav, recent tasks, credits
    ├── chat/
    │   ├── chat-panel.tsx       # header + transcript + composer
    │   ├── message-list.tsx
    │   ├── message-bubble.tsx   # user bubble / assistant row + artifact chip
    │   ├── tool-steps.tsx       # "Used N tools" transparency card
    │   └── composer.tsx         # inert input area (attach, skills, model, send)
    ├── artifacts/
    │   └── artifact-panel.tsx   # tabs (Preview/Code/Console) + document preview
    └── ui/                      # shadcn-style primitives
        ├── button.tsx
        ├── badge.tsx
        ├── avatar.tsx
        ├── separator.tsx
        └── textarea.tsx
```

## Design tokens

Everything design-related lives in **one file**: `styles/tokens.css`.

- Colors are RGB triplets → Tailwind opacity modifiers work
  (`bg-accent/20`, `border-line/70`).
- Semantic names only in components: `canvas`, `surface`, `ink`, `line`,
  `accent`, `success/warning/danger/info`, `user-bubble`, `code-block`.
- Light theme = Claude-flavored warm paper + terracotta accent.
- Dark theme = `.dark` class on `<html>` → Manus-flavored graphite
  (toggle ships with the interaction phase).
- Tokens also cover: type scale, layout widths, radius, elevation,
  motion (durations + easings), z-index.

## Run

```bash
cd web
npm install
npm run dev        # http://localhost:3000
npm run typecheck  # strict TS check
npm run build      # production build
```

## Phase status

| Phase | Scope | Status |
|------:|-------|--------|
| 1 | File tree, design tokens, static shell (sidebar + chat + artifact) | ✅ this commit |
| 2 | Interactions: composer state, tab switching, theme toggle, panel collapse | ⏸ awaiting approval |
| 3 | Data: REST + WebSocket bindings to the Nimna backend, streaming, approvals UI | ⏸ |
| 4 | RTL / Arabic layout pass, i18n | ⏸ |
