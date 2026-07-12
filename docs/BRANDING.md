# Branding

## Where the assets come from

The turtle icon PNGs are generated from a private source image owned by the project
author (an AI-generated logo lockup; the file lives in the repo root as
`logo-source.png` and is **gitignored on purpose** — only the derived PNGs are
tracked). The generation crops the turtle motif to a square, makes the background
transparent, and resizes with Lanczos to the required sizes; the full lockup
(turtle + wordmark) is additionally exported as the README logo.

Tracked assets:

| File | Size | Purpose |
| --- | --- | --- |
| `custom_components/tortoise_ufh/brand/icon.png` | 256×256 | canonical icon |
| `custom_components/tortoise_ufh/brand/logo.png` | 800×658 | full lockup (turtle + wordmark), README header (light theme) |
| `custom_components/tortoise_ufh/brand/logo-dark.png` | 800×658 | dark-theme lockup (charcoal remapped to light grey), README `<picture>` source |
| `custom_components/tortoise_ufh/brand/icon@2x.png` | 512×512 | hi-DPI icon |
| `custom_components/tortoise_ufh/brand/icon.svg` | vector | hand-written placeholder (kept as-is, not a raster trace) |
| `custom_components/tortoise_ufh/frontend/panel-icon.png` | 256×256 | panel header mark, served at `/tortoise_ufh_panel/panel-icon.png` (`panel.py`); the panel falls back to the 🐢 glyph if it fails to load |
| `brand-submission/tortoise_ufh/icon.png` + `icon@2x.png` | 256 / 512 | ready-made [home-assistant/brands](https://github.com/home-assistant/brands) submission |

## Submitting to home-assistant/brands

Until the brand is merged upstream, HA shows a default puzzle-piece icon for the
integration. To fix that, the **project owner** opens a PR against
[home-assistant/brands](https://github.com/home-assistant/brands):

1. Fork `home-assistant/brands`.
2. Copy `brand-submission/tortoise_ufh/` into the fork as
   `custom_integrations/tortoise_ufh/` (the directory name must equal the
   integration domain).
3. Open the PR; the repo's CI validates sizes and names.

Brands requirements covered by the prepared directory:

- `icon.png` — exactly 256×256 px, PNG, transparent background, motif trimmed
  and centred.
- `icon@2x.png` — exactly 512×512 px, same artwork.
- `logo.png` / `logo@2x.png` (wide wordmark) are optional; icon-only submissions
  are accepted, and this icon is square, so none is included.
