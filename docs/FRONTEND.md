# Frontend Guide

## Tech Stack

- **Flask + Jinja2** — server-side rendered HTML
- **HTMX 2.0** — dynamic partial updates without JS frameworks
- **PicoCSS** — base dark theme (used by Scanner/Tailor pages)
- **Custom CSS** — Atriveo-inspired design for LinkedIn feed
- **Sora + IBM Plex Mono** — Google Fonts (headings + monospace data)
- **Chart.js** — imported but not actively used

## Page Templates

```
templates/
├── base.html                  # Global layout: nav, toast, HTMX progress bar
├── linkedin.html              # LinkedIn feed (Atriveo-style design)
├── boards.html                # Job Boards placeholder
├── scan.html                  # Scanner page
├── tailor.html                # Resume tailor page
└── _partials/
    ├── linkedin_row.html      # Single LinkedIn job row
    ├── linkedin_tbody.html    # Table body wrapper (loops over rows)
    ├── scan_status.html       # Scan progress/results
    └── tailor_status.html     # Tailor progress/PDF viewer
```

## LinkedIn Feed Design

The LinkedIn page (`linkedin.html`) uses a completely custom CSS design inspired by Atriveo Radar, scoped to `.li-page`.

### Layout

```
┌─────────────────────────────────────────────────┐
│ LinkedIn Feed          LIVE FEED  Updated  Refresh│
├─────────────────────────────────────────────────┤
│ [This Hour] [Today] [Yesterday] [All Time]  [Score][Recent] │
├─────────────────────────────────────────────────┤
│ ◁  [11 PM 330] [12 AM 49]                    ▷ │  ← hourly cards
├──────────┬──────────────────────────────────────┤
│ SIDEBAR  │  [Search...] [All] [Rec] [Entry] [Mid]  │
│          │  379 jobs · 108 recommended              │
│ Match    │  ┌───────────────────────────────────┐  │
│ Score    │  │ # │ ROLE      │ MATCH│SCORE│LEVEL │  │
│ ────     │  │ 1 │ ML Eng    │  35% │ +45 │Entry │  │
│ Level    │  │ 2 │ SWE Intern│  32% │ +42 │NewGr │  │
│ ────     │  │ ...                                │  │
│ Top Co.  │  └───────────────────────────────────┘  │
│ ────     │                                          │
│ Exp Req  │                                          │
└──────────┴──────────────────────────────────────┘
```

### Key CSS Classes

| Class | Purpose |
|-------|---------|
| `.li-page` | Scoped container for LinkedIn styles |
| `.li-header` | Top bar with title + live indicator |
| `.time-bar` | Time tabs + sort buttons |
| `.time-tab` | Individual time tab (This Hour/Today/etc.) |
| `.hourly-strip` | Scrollable hourly card container |
| `.hour-card` | Individual hour card |
| `.li-layout` | Two-column grid (sidebar + main) |
| `.li-sidebar` | Left sidebar with stat panels |
| `.li-panel` | Stat card (Match Score, Level, etc.) |
| `.li-filters` | Search + filter chips row |
| `.li-chip` | Filter chip button |
| `.li-table` | Main job table |
| `.role-cell` | Company avatar + title + meta |
| `.co-avatar` | Colored circle with company initial |
| `.match-val` | Match percentage number |
| `.score-val` | Raw score number |
| `.lvl-badge` | Level badge (New Grad/Entry/Mid) |
| `.apply-btn` | Teal "Apply" button |
| `.li-status-sel` | Minimal status dropdown |

### Color Palette

```
--bg:         #08101a (deep dark)
--surface:    rgba(15, 27, 42, 0.7)
--accent:     #34d3c4 (teal)
--good:       #39d98a / #52d98a (green)
--warn:       #f5c842 / #ffd066 (yellow)
--bad:        #f5806a / #e06060 (red)
--purple:     #a78bfa (scores)
--muted:      #8fa4be / #6b7f96 (secondary text)
--text:       #e9f1fb (primary text)
```

### Badge Color Reference

| Badge | Color | Background |
|-------|-------|------------|
| `.badge-linkedin` | #34d3c4 teal | rgba(52,211,196,0.12) |
| `.badge-h1b` | #39d98a green | rgba(57,217,138,0.12) |
| `.lvl-newgrad` | #52d98a green | rgba(57,217,138,0.12) |
| `.lvl-entry` | #a78bfa purple | rgba(167,139,250,0.12) |
| `.lvl-mid` | #f5b85a orange | rgba(255,171,94,0.14) |
| `.match-high` (>=60%) | #5ee8a8 | — |
| `.match-med` (30-59%) | #ffd066 | — |
| `.match-low` (<30%) | #8fa4be | — |
| `.sv-high` (>=70) | #a78bfa purple | — |
| `.sv-med` (40-69) | #5b9bd5 blue | — |
| `.chip-rec` (Recommended) | #f5c842 gold | — |

### JavaScript Functions

| Function | Purpose |
|----------|---------|
| `setFilter(name, value)` | Update filter state + active chip styling |
| `setTime(val)` | Set time range filter (hour/today/yesterday/all) |
| `setHour(h)` | Filter to specific hourly card |
| `quickSort(col)` | Quick sort toggle (Score/Recent) |
| `toggleSort(col)` | Toggle sort on table header click |
| `debounceFilter()` | 250ms debounced search input |
| `triggerFilter()` | Execute filter request via fetch(), update counts |
| `formatLocalTime(utcStr)` | Convert UTC to local display |
| `renderUtcTimes()` | Update all `.utc-time` elements |
| `renderHourlyCards()` | Build hourly cards with local timezone |
| `getUserTzOffset()` | Returns `new Date().getTimezoneOffset()` |

### HTMX Patterns

- **Table updates**: `fetch()` → manual innerHTML swap → `htmx.process()` to re-enable HTMX on new content
- **Status change**: `hx-patch` on `<select>` → returns new `<tr>` → `outerHTML` swap → triggers `triggerFilter()` after 400ms delay
- **Progress bar**: `htmx:beforeRequest` / `htmx:afterRequest` events on body
- **Toast**: `showToast(message, type)` — auto-dismiss after 2.5s

### Responsive Breakpoints

| Width | Hidden columns |
|-------|---------------|
| < 1000px | Sidebar hidden |
| (table itself) | Horizontal scroll enabled |
