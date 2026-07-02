# JobFlow Design System — "The Ledger"

A private ledger for the job hunt: deep green ink, champagne brass, ivory text.
Quiet, dense, precise. Register: **product** — the table is the product; chrome
earns its pixels or goes.

All tokens live in `jobflow/web/static/style.css` under `:root` /
`[data-theme="dark"]` and `[data-theme="light"]`. Theme is set on
`<html data-theme>` and persisted in `localStorage("jf-theme")`; dark is the
default (late-night triage), light is the "porcelain" variant.

## Color

### Surfaces (dark / light)

| Token | Dark | Light | Role |
|---|---|---|---|
| `--ink` | `#0c1410` | `#eef0ec` | page background (green-black / porcelain) |
| `--panel` | `#121c16` | `#f9faf8` | table shell, controls |
| `--panel-2` | `#18241d` | `#f1f3ef` | raised surfaces, hovers |
| `--well` | `#080f0b` | `#e4e8e2` | inputs, sunken areas |
| `--hairline` | `#24332a` | `#d5dad3` | default borders/dividers |
| `--hairline-strong` | `#36483d` | `#bcc4bb` | emphasized borders |

### Ink

`--text` (ivory `#eceae2` / near-black `#1a201c`), `--text-2` secondary,
`--text-3` muted. All pairs hold ≥4.5:1 on their surfaces.

### Brass — the one touch of richness

`--brass` (`#cfa96b` dark / `#8a6a26` light) with `--brass-soft` (tint bg),
`--brass-line` (borders), `--brass-ink` (text on brass fills). Used **only**
for: primary actions, active/selected state, the recommended signal, focus
rings, and the brand dot. Never decoration.

### Semantics

- `--grow` green — success, New Grad, high match, live dot
- `--slate` blue — info, Entry level
- `--clay` terracotta — Mid level, Claude badge
- `--rose` red — danger, delete
- `--match-high/med/low` — match-percentage ramp

Each has a `-soft` tint for backgrounds. Full-saturation color never appears
on inactive states.

## Typography

| Token | Family | Use |
|---|---|---|
| `--font-display` | Newsreader (opsz, italic) | identity moments ONLY: brand wordmark (italic), page titles, empty-state line |
| `--font-ui` | Hanken Grotesk | everything else: labels, buttons, body, chips, table headers |
| `--font-data` | IBM Plex Mono | numbers and data: counts, match %, timestamps, code, log output |

Rules: fixed rem scale (html 15px, 14px ≤768px); no display font in buttons,
labels, or data; `font-variant-numeric: tabular-nums` on all numeric UI;
page titles ~2rem Newsreader 500; the brand is italic Newsreader with a brass
`.brand-dot` period.

## Layout & components

- Shell: `max-width: 1180px`, sticky blurred topbar.
- **Ledger line** (`.stat-strip`): the stat row is a bordered strip (top+bottom
  hairline) of borderless cells separated by 1px dividers; active cell gets a
  2px brass underline (animated `scaleX`). Not cards. Never add card grids.
- Table: hairline rows, sticky `thead` (tiny uppercase Hanken labels), hover =
  `--panel-2` tint, selected = `--brass-soft`. Row → flex card below 768px.
- Level = colored 6px dot + quiet text (`.lvl`), not a pill.
- Chips: hairline pills; active chip inverts to solid `--text` on `--ink`;
  the Recommended chip is the brass variant.
- Buttons: 8px radius, Hanken 600; `.btn-primary` = brass fill; `.btn-danger`
  = borderless rose that gains a border on hover; per-row Apply is a quiet
  brass text-button that gains `--brass-soft` on hover.
- Toasts: `--panel-2` + hairline + leading semantic dot (`::before`). Never
  colored left-border stripes.
- Modal: 520px, `--panel`, 14px radius, blurred backdrop.
- z-scale tokens: `--z-sticky` (30) < `--z-topbar` (40) < `--z-modal` (100)
  < `--z-toast` (110). No arbitrary z-index values.

## Motion

State only, 120–260ms, ease-out (`cubic-bezier(0.22,1,0.36,1)`). Inventory:
row entrance stagger (first screenful only), dismiss slide-out, toast in/out,
stat count-up tween (JS), brass underline scaleX, live-dot pulse, spinner,
progress-bar fills, shimmer on the scoring button. **No page-load
choreography.** Everything is disabled under `prefers-reduced-motion`.

## Accessibility

- ≥4.5:1 body text in both themes (placeholders use `--text-3`, which passes).
- 2px brass `:focus-visible` rings on all interactive elements.
- `/` focuses search; Escape closes the modal.
