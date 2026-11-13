# UI guidelines

These are the rules for adding or changing a screen in `app/`. The goal is that every page looks and behaves like the others, so a new screen doesn't grow its own buttons, dialogs or colours.

The dashboard (`app/src/views/Dashboard.tsx`, `views/dashboard/`) is the visual reference. It is dense and square-cornered, uses only theme tokens, and has slim section headers and monospaced tabular numbers.

## Page anatomy

```
PageHeader        one h1 title, optional description, actions, tabs
FilterBar         search + filters left, actions right (lists only)
Panel / Card      sections; Panel for titled sections, Card for plain blocks
QueryState        loading / error-with-retry / empty around the content
```

Every page starts with `PageHeader`. It renders the page's only `<h1>`, and keeping the title text stable matters because e2e tests find pages by heading name.

## Which component

| Need | Use | Not |
|---|---|---|
| Page title | `PageHeader` (`components/ui`) | a hand-written `<h1>`/`<h2>` |
| Titled section | `Panel` (`components/ui/layout`) | a bordered `div` with its own header |
| Button | `Button` (variant, size) | raw `<button className=…>` |
| Icon-only button | `IconButton` (label required) | an unlabelled icon `<button>` |
| Popup / drawer | `Modal` (`placement="center"` or `"side"`) | a `fixed inset-0` overlay |
| "Are you sure?" | `useConfirm()` (`components/ui/ConfirmDialog`) | `window.confirm` / `alert` |
| Notification | `useSnackbar()` | `alert()` |
| Form field | `Field` + `Input`/`Select`/`Textarea`/`Checkbox` (`components/ui/form`) | per-file input class strings |
| Local tabs | `Tabs` | hand-built tab bars |
| Tabs that are routes | `RouteTabs` (`components/ui/layout`) | NavLink bars styled per page |
| Table | `DataTable` (large or interactive), `Table` (small, static) | raw `<table>` |
| Paging | `Pagination` | hand-built Prev/Next |
| Headline number | `StatTile` (`components/ui/stats`) | a per-page `Stat` |
| Status / filter pill | `Chip`, `Badge`, `SeverityBadge` | coloured `span`s |
| Loading / error / empty | `QueryState`, or `Skeleton` / `ErrorCard` / `EmptyState` | "Loading…" text, `animate-spin` divs |
| Spinner | `Spinner` | hand-built spinners |

## Colour and shape

- Use theme tokens only: `var(--panel)`, `--panel-2`, `--bg-2`, `--text`, `--text-dim`, `--border`, `--accent`.
- For state, use `--ok`, `--warn`, `--danger` and `--critical`. For chart series, use `--series-1..3`.
- Never use Tailwind palette classes (`bg-red-500`, `border-neutral-700`, …) or hex values in TSX. They don't follow the light theme.
- Corners are square. Primitives read `--radius-*` (all `0`). Use `rounded-full` only for dots, avatars and spinners.
- State is never shown by colour alone. Pair it with text or an icon.

## Behaviour rules

- **Destructive actions confirm** through `useConfirm({ danger: true, confirmLabel: 'Delete' })`.
  - Start the confirm label with the verb (Delete, Remove, Block, Revoke, Forget). Tests click it by that name.
  - `useConfirm` is async, so everything that should happen only on "yes" goes after the `await`.
- **Every string goes through `t()`**, with an entry in both `locales/en.ts` and `locales/fr.ts`.
- **Keep test hooks stable.** Existing `title`, `placeholder`, `aria-label`, button text and `data-testid` values are what `tests/e2e/harness/selectors.py` finds. If one must change, update the selector in the same commit.
- **Popups:** Escape closes the topmost one, focus stays inside, and focus returns to the opener. `Modal` does all three; hand-built overlays don't.
  - Pass `closeOnBackdrop={false}` for a form, so a stray click beside it doesn't lose what was typed.

## Enforced by lint

`npm run lint` (and CI) fails on:

- `confirm()`, `alert()` and `prompt()`, including `window.` forms. Use `useConfirm`, `useSnackbar`, or an inline field.
- A `fixed inset-0` class outside the shared `Modal`. Use `Modal`.
- A Tailwind palette class (`bg-red-600`, `text-neutral-500`, …). Use a theme token.

Special layers are exempt in `app/eslint.config.js`: full-screen video and camera views, the mobile nav drawer, stacked pickers and print sheets. Adding a file there needs a reason, the same as an `eslint-disable` comment does.

## Checking a UI change

1. Run `npm run typecheck && npm run lint && npm run build` in `app/`.
2. Run `python tests/e2e/run.py -m ui`, which includes the route and popup smoke tests.
3. Run the before/after sweep:
   - `E2E_SWEEP_LABEL=before python tests/e2e/run.py -m sweep` on the old commit;
   - the same with `after` on the new one;
   - then `python tests/e2e/tools/sweep_diff.py <before> <after>`.

   A presentation-only change must show zero API-contract differences.
4. Work through the relevant parts of `docs/ui-regression-checklist.md`.
