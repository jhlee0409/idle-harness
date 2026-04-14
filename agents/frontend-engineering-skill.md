# Frontend Engineering Skill

Standards for building production-grade frontends. Read this BEFORE writing any frontend code.

> Pairs with: `backend-engineering-skill.md` — error/response formats referenced here are defined there.

## Rendering Strategy

Choose ONE based on app requirements:

- **CSR (Client-Side Rendering)** — React SPA with Vite. Default choice. Use when: interactive dashboards, real-time updates, complex client state.
- **SSR (Server-Side Rendering)** — Next.js or Remix. Use when: SEO required, content-heavy pages, fast first-paint critical.

**Declare your choice at build start.** Criteria like "loading skeleton" and "page transitions" assume CSR. If using SSR:
- Replace "loading skeleton" with "SSR pages render with data on first paint — no blank flash before hydration"
- Replace "page transition animation" with server-side navigation (no SPA routing)

## Data Fetching: TanStack Query (MANDATORY)

```bash
npm install @tanstack/react-query
```

### Setup (in main.tsx or App.tsx)

```tsx
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,       // 30s before refetch
      retry: 1,                // 1 retry on failure
      refetchOnWindowFocus: false,
    },
  },
})

// Wrap app: <QueryClientProvider client={queryClient}>
```

### Rules

- ALL API data fetching MUST use `useQuery` / `useMutation` — NEVER raw `fetch()` in components
- This automatically provides `isLoading`, `isError`, `error`, `data` states — use them ALL:

```tsx
const { data, isLoading, isError, error, refetch } = useQuery({
  queryKey: ['endpoints'],
  queryFn: () => api.getEndpoints(),
})

if (isLoading) return <Skeleton />           // loading state
if (isError) return <ErrorState onRetry={refetch} />  // error + retry
if (!data?.length) return <EmptyState />     // empty state
return <EndpointList data={data} />          // success state
```

- Mutations use `useMutation` with `onSuccess` invalidation:

```tsx
const createEndpoint = useMutation({
  mutationFn: api.createEndpoint,
  onSuccess: () => {
    queryClient.invalidateQueries({ queryKey: ['endpoints'] })
    toast.success('Endpoint created')
  },
  onError: (err) => toast.error(err.message),
})
```

- `isPending` on mutations for button loading state: `<Button disabled={createEndpoint.isPending}>`

### Why TanStack Query (not raw fetch)

1. **Loading/error/empty states are automatic** — no manual `useState` for loading/error flags
2. **Mount refetch prevents SPA caching issues** — `staleTime` controls when data is re-fetched on navigation
3. **Evaluator compatibility** — fetch interception works because TanStack Query calls `fetch()` internally, and `isLoading` renders skeleton regardless of cache state when `refetchOnMount` fires
4. **Optimistic updates with rollback** — `onMutate` + `onError` for instant UI feedback with automatic revert on failure
5. **No manual cache invalidation** — `invalidateQueries` after mutations keeps UI in sync

### API Client Pattern

Create a typed API client that TanStack Query calls:

```tsx
// lib/api.ts
const BASE = '/api'

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw body.error || { code: 'UNKNOWN', message: res.statusText }
  }
  const json = await res.json()
  return json.data ?? json  // unwrap {data: T} envelope
}

export const api = {
  getEndpoints: () => request<Endpoint[]>('/endpoints'),
  createEndpoint: (data: CreateEndpoint) =>
    request<Endpoint>('/endpoints', { method: 'POST', body: JSON.stringify(data) }),
  // ...
}
```

This client unwraps the backend's `{data, error}` envelope and throws structured errors that TanStack Query's `isError` catches automatically.

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
- TanStack Query handles this automatically via `isError` + `error` — just render the error UI

## Anti-Patterns (NEVER do these)

- Emoji as UI icons (`☰`, `🔔`, `🔍`, etc.)
- Native `<select>` or browser date/color/range pickers
- `window.alert()` / `window.confirm()` / `window.prompt()`
- Uncustomized shadcn default gray theme
- Raw `fetch()` / `useEffect` + `useState` for data fetching — use TanStack Query
- Manual `isLoading` / `setError` state management — TanStack Query provides these
- Missing loading/empty/error states on data-driven components
- `Inter`, `Roboto`, `Arial`, or system default fonts
- Bare solid white/gray backgrounds with no texture or depth
