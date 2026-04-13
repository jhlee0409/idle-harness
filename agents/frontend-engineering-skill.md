# Frontend Engineering Skill

Standards for building production-grade frontends. Read this BEFORE writing any frontend code.

> Pairs with: `backend-engineering-skill.md` — error/response formats referenced here are defined there.

## Component Library: shadcn/ui (MANDATORY)

### Setup (run FIRST, before writing any component code)

```bash
npx shadcn@latest init
npx shadcn@latest add button input label select dialog alert-dialog toast sonner form dropdown-menu popover
```

### Rules

- ALL form controls MUST use shadcn components — `<Input>`, `<Select>`, `<Dialog>`, etc.
- NEVER use native `<select>`, `<input type="date">`, `<input type="color">`, `<input type="range">`
- NEVER use `window.alert()`, `window.confirm()`, `window.prompt()` — use `<AlertDialog>` and `<Toast>`
- Use shadcn `<Form>` with `zod` for form validation — provides inline error messages automatically
- For date picking, use a shadcn-compatible date picker (e.g., react-day-picker + Popover)
- For color picking, build a custom palette component with Popover, not `<input type="color">`

## Icons: Lucide React (MANDATORY)

```bash
npm install lucide-react
```

### Rules

- NEVER use emoji as UI icons — no `☰` `🔔` `🔍` `➕` `✏️` `🗑️` `❌` `✅` `⭐` `🏠`
- Import named icons from `lucide-react`:

| Purpose | Icon |
|---|---|
| Menu / hamburger | `Menu` |
| Close | `X` |
| Notifications | `Bell` |
| Search | `Search` |
| Add / create | `Plus` |
| Delete | `Trash2` |
| Edit | `Pencil` |
| Settings | `Settings` |
| User / profile | `User` |
| Home | `Home` |
| Back | `ArrowLeft` |
| Filter | `Filter` |
| More actions | `MoreHorizontal` |
| Check / success | `Check` |
| Warning | `AlertTriangle` |
| Error | `AlertCircle` |
| Info | `Info` |
| Download | `Download` |
| Upload | `Upload` |
| Calendar | `Calendar` |
| Clock | `Clock` |
| Chevron | `ChevronDown`, `ChevronRight` |

## Theme Customization (MANDATORY)

shadcn default theme = FAIL. The Evaluator treats uncustomized shadcn as "generic AI default."

1. Map spec colors → CSS variables in `globals.css`:
   ```css
   :root {
     --background: /* spec background hex */;
     --foreground: /* spec text hex */;
     --primary: /* spec primary hex */;
     --accent: /* spec accent hex */;
     --muted: /* spec muted hex */;
     --border: /* spec border hex */;
     --radius: /* spec border-radius */;
   }
   ```
2. Map spec typography → `tailwind.config.ts` (`fontFamily`) + Google Fonts `<link>` in `index.html`
3. Override shadcn component defaults to match spec's component style (shadow, border, radius)

## Tailwind CSS v4

- Use `@import "tailwindcss"` (NOT `@tailwind base; @tailwind components; @tailwind utilities;`)
- NEVER add `* { padding: 0; margin: 0; }` — this breaks ALL Tailwind utility classes in v4
- Tailwind v4 includes Preflight reset — no manual reset needed
- Custom base styles go inside `@layer base { ... }` only

## API Error Handling

Backend returns: `{error: {code, message, details?: [{field, message}]}}`

- `details` exists → show `details[i].message` as inline error under `input[name=details[i].field]`
- `details` absent → show `error.message` in Toast notification
- Network error (fetch failed) → Toast: "Connection failed" + retry button
- Loading state → Skeleton or spinner — NEVER blank page
- Empty state → Helpful message + CTA — NEVER blank area

## Anti-Patterns (NEVER do these)

- Emoji as UI icons (`☰`, `🔔`, `🔍`, etc.)
- Native `<select>` or browser date/color/range pickers
- `window.alert()` / `window.confirm()` / `window.prompt()`
- Uncustomized shadcn default gray theme
- Missing loading/empty/error states on data-driven components
- `Inter`, `Roboto`, `Arial`, or system default fonts
- Bare solid white/gray backgrounds with no texture or depth
