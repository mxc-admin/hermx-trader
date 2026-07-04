# Hermx Dashboard UI

Local Next.js (App Router) dashboard for the Hermx trading execution layer. It
reads the Python backend's `/api` and `/health` endpoints and renders live
strategy cards, execution ledgers, and alert tables.

## Scripts

| Script          | Purpose                                             |
| --------------- | --------------------------------------------------- |
| `npm run dev`   | Dev server on port 3001                             |
| `npm run build` | Production build (`next build`)                     |
| `npm run start` | Serve the production build on port 3001             |
| `npm test`      | Run the Vitest component test suite (`vitest run`)  |
| `npm run dev:full` | UI dev server + Python dashboard together        |

## Testing

Component tests use **Vitest** + **React Testing Library** in a **jsdom**
environment.

- Config: `vitest.config.ts` (jsdom env, `@/*` path alias mirroring `tsconfig.json`).
- Setup: `vitest.setup.ts` registers `@testing-library/jest-dom` matchers and
  runs RTL `cleanup()` after each test.
- Test files live beside the component as `*.test.tsx`.
- Tests assert **behavior** (what `setStrategyMode` is called with, pending/error
  states, aria attributes) — not exact styling — so they survive CSS refactors.

Run `npm test` to execute the suite.

## Styling convention

Styling is split three ways. Pick the mechanism by what the style *is*, not by
where it happens to be:

1. **Shared component chrome → CSS custom properties + semantic classes in
   `app/globals.css`.** Repeated visual primitives (panels, metric tiles,
   labels, badges, section headers) are defined once as classes — `.panel`,
   `.metric-card`, `.metric-label`, `.metric-value`, `.section-header`, etc. —
   built on the design tokens (`--bg-panel`, `--text-muted`, `--positive`, …).
   Reuse these instead of re-declaring the same inline object per component.

2. **One-off page/layout composition → Tailwind utility classes.** Layout and
   spacing that only appears in a single place (page max-width, grid gaps,
   responsive padding) uses Tailwind utilities directly in JSX, e.g.
   `className="max-w-[1200px] mx-auto px-4 md:px-6 py-4"`.

3. **Dynamic / conditional styles → inline `style` in JS.** When a value depends
   on runtime state — a color that follows P&L sign (`uplColor`), a left-border
   that reflects position side, opacity while a request is pending — keep it as
   an inline `style` object. These can't be static classes because they compute
   from props/state.

When a component's inline style object *exactly* duplicates an existing global
class, prefer the class (e.g. `StrategyCard`'s card shell uses `className="panel"`
and keeps only its dynamic left-border accent inline). Do not force a class onto
styles that only partially match — that changes appearance.
