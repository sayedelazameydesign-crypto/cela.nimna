# Nimna Web — Agent Workspace UI

A **Manus × Claude** inspired frontend for the Nimna agent.
**RTL-first, Arabic, light theme by default** (dark available via toggle).

## Stack

- Next.js 15 (App Router) + TypeScript (strict)
- Tailwind CSS 3.4 + `tailwindcss-animate`
- shadcn conventions (`components.json`); Radix primitives for Tabs +
  DropdownMenu included — only what the interactions actually use
- lucide-react icons · self-hosted variable fonts (@fontsource, no CDN)

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
│   └── mock.ts                  # بيانات عرض عربية ثابتة
└── components/
    ├── workspace.tsx            # ★ حامل حالة المرحلة 2 (رسائل/طيّ اللوحة/الثيم)
    ├── layout/
    │   ├── app-shell.tsx        # شبكة 3 أعمدة، تنعكس تلقائيًا مع RTL
    │   └── sidebar.tsx          # العلامة، مهمة جديدة، تنقل، مهام أخيرة، رصيد
    ├── chat/
    │   ├── chat-panel.tsx       # ترويسة + سجل + حقل إدخال
    │   ├── message-list.tsx
    │   ├── message-bubble.tsx   # فقاعة المستخدم / صف المساعد + شريحة المخرجات
    │   ├── tool-steps.tsx       # بطاقة شفافية "استُخدمت N أدوات"
    │   └── composer.tsx         # ★ حالة: نص/Enter/توسيط + منتقيات مهارات ونموذج
    ├── artifacts/
    │   └── artifact-panel.tsx   # تبويبات Radix: معاينة / الكود / الطرفية
    └── ui/                      # بدائيات بأسلوب shadcn
        ├── button.tsx
        ├── badge.tsx
        ├── avatar.tsx
        ├── separator.tsx
        ├── textarea.tsx
        ├── tabs.tsx             # Radix Tabs (المرحلة 2)
        └── dropdown-menu.tsx    # Radix DropdownMenu (المرحلة 2)
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

## الاتجاه واللغة

الواجهة **عربية، `dir="rtl"` على `<html>`، مبنية مرة واحدة** — لا توجد نسخة
LTR موازية. كل المكونات تستعمل الأدوات المنطقية (`ms/me/ps/pe/start/end`،
`border-s/e`، `rounded-ee`) فتنعكس تلقائيًا، والأكواد والطرفية تبقى
`dir="ltr"` داخل التخطيط. الخطوط: Inter للاتيني، Cairo للعربي،
Newsreader للعرض اللاتيني.

## حالة المراحل

| المرحلة | النطاق | الحالة |
|------:|-------|--------|
| 1 | شجرة الملفات، توكنز التصميم، صدفة ساكنة (سايدبار + محادثة + مخرجات) | ✅ |
| 2 | التفاعلات: حالة Composer (إرسال/Enter/توسيط/منتقيات) · تبديل التبويبات (Radix) · طيّ اللوحة · تبديل الثيم | ✅ |
| 3 | البيانات: ربط REST + WebSocket بخلفية نِمنا، البث، واجهة الموافقات | ⏸ |
