# 00: Design system — TurboChickens GUI (served by the coop container)

**Status:** settled 2026-11-12 with the operator. Governs every GUI ticket: **specs 12–18 must build to this document.** Any deviation needs operator sign-off before the ticket lands.

## Naming

- The **GUI and its brand are "TurboChickens"** — sidebar wordmark, `<title>`, login card.
- **"coop" is the container image and the daemon instance inside it** (e.g. the status pill reads "coop · up 3h 12m"; `docker run … coop`). The GUI never calls itself coop.
- Default port `8737`.

## Kind

Software app shell, site-kind `software`, **light clean**. The design-taste-frontend house rules apply in full: equal left/right content edges, one button pair, one card style, hover/press motion on everything interactive, real icons, no violet/lavender anywhere, nothing under 13px, no sideways scroll at 320, keyboard-complete (skip link, visible focus ring, named controls). Adapted to an app: sidebar + one view at a time instead of marketing sections.

## Colour tokens (contrast-verified 2026-11-12)

| Token | Hex | Contrast | Use |
| --- | --- | --- | --- |
| `--p-bg` | `#FCFAF5` warm paper | — | app ground |
| `--p-surface` | `#F3EFE6` | — | panels, cards, inputs |
| `--p-ink` | `#221D14` warm near-black | 16.1:1 on bg | text, hairlines, secondary buttons |
| `--p-muted` | `#6E6656` | 5.4:1 on bg | labels, dates, meta only — never paragraphs |
| `--p-action` | `#C87F0A` yolk amber | 3.1:1 on bg | the one filled primary button, focus ring, running/quarantine state |
| action label | `#1A1408` on `#C87F0A` | 5.7:1 | primary button text |
| decorative yolk | `#EFA92B` | 1.9:1 — decorative only | tiny marks; never text, never fills that carry meaning |

State (functional, dots + short labels only): succeeded = green, failed = red, quarantined = amber `#C87F0A`, queued = hollow dot, running = pulsing `#C87F0A`. Exact green/red hexes chosen at build time, verified ≥3:1 on bg for the dots. The interface stays ~95% neutral; colour lives in state and the one accent.

## Type

**Geist + Geist Mono** (Google Fonts, `display=swap`). Display weight 500, tight tracking at heading sizes. Sizes per the kit's scale: h1 72/38, h2 48/30, h3 28/24, lead/card titles 20/18, body 16, small 14, labels 13/500 sentence case. **Mono for all data:** run ids, spec hashes, cron expressions, timestamps, logs, JSON results.

## Radius and buttons

`data-radius="soft"` (8/12/20). `data-buttons="solid"`: one filled amber primary (`#C87F0A` fill, `#1A1408` label) + one outlined secondary (ink border), both 44px high, label 16px. Hover steps, press sinks. Exactly one filled button marks the main action per view. Text links are ink with a drawn underline.

## Shell

Fixed **240px sidebar on the same paper ground**, 1px hairline right edge: wordmark "TurboChickens" + egg-dot mark at top; nav list — Dashboard, Runs, Jobs, Schedules, Stats, Audit, Settings — current view marked; daemon up/down pill at the foot. Content pane on `--p-bg`, one `.wrap` (`min(100% - 2*gut, 1280px)`) with equal edges; 12-col grid, 24px gutter. Each view = header row (h2 + one-line intro left, view actions right on the same line) then content. **Login view** on 401: centered card, token field, one primary button, no cookies — the token lives in `sessionStorage` and goes out as `Authorization: Bearer`.

## Views

- **Dashboard** — stat row (daemon uptime, queue depth, in-flight weight vs budget, alerts count, schedules count, last retention sweep), the flock diagram (below), recent runs strip.
- **Runs** — filterable table (id, job, status egg-dot, trigger, started/finished); row click opens a detail pane with tabs **Manifest / Logs / Result-or-Quarantine** (mono JSON, copy button).
- **Jobs** — registered jobs table (name, spec hash, registered-at) + upload/validate form with field-named errors under the fields and a Run action on success.
- **Schedules** — table (job, cron, next run), add form, remove action; errors inline.
- **Stats** — per-job cards: n, mean/p50/p95 tokens in/out, total p95, expected cost per run.
- **Audit** — mono log tail, newest first, event-type filter.
- **Settings** — read-only rows of the effective settings.

## The one dealt moment

**Flock diagram** on the Dashboard: a kit `data-diagram` hub card (tech preset with pulses) — daemon hub in the middle, queue and schedules feeding in, in-flight runs as lanes out, pulses travelling the lines in 2.5–4 s cycles, restarting on hover. Labels: daemon, queue, in flight, results. This is the app's one effect; everything else is flat grounds.

## Data behaviour and motion

Views poll their `/api/*` endpoint every ~2 s; a changed row flashes its background once (never a scroll-triggered fade-in anywhere). Hero/view header enters once on load (`data-enter`). Hover: linked cards lift 2px, buttons step and press, underlines draw. Under reduced motion everything rests. No tilt, carousels, parallax, scroll-jacking.

## Icons and mark

Interface glyphs from the kit's `ui` set (lucide), resolved once by `icons.mjs scan` into local SVG files — no runtime CDN, no drawn look-alikes. File-type icons for job specs (toml/json/txt). The brand mark is designed, not borrowed: an egg dot (yolk fill) beside the wordmark.

## Copy voice

Sentence case everywhere. Buttons are a verb and an object ("Run job", "Add schedule", "Sweep now"). Headings name things ("Runs", "Schedules"). Light wit only in labels: "in flight", "the flock". Quarantine stays literal.

## Never

No violet/lavender in any token or effect; no scroll fade-ins; no second button or card style; no captions under visuals; nothing under 13px; no sideways scroll at 320; no placeholder text left visible; every control named; focus ring always visible.
