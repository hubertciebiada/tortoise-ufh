# Branding

## The mark

A tortoise, seen from above, carries an underfloor-heating loop on its shell. The loop
is a bifilar spiral (the *ślimak* layout installers use): supply (hot, orange) and return
(cooler, amber) run side by side, the supply winds in, turns round an S-bend in the
middle and winds back out between its own turns, and both ends leave together at one
corner, the way a real loop reaches its manifold. Hot next to cooler is what keeps a
slab evenly warm, and the tortoise is the slab: heavy, slow and steady, and it always
gets there. On the S-bend's spine the supply colour fades into the return colour. Like a
real PEX/PERT pipe, the loop is bent, never kinked: no bend is tighter than a minimum
radius (`minBend` in `logo.mjs`), so the outer turns follow the shell while the small
inner turns come out round.

Palette (from `scripts/brand/logo.mjs`):

| Role | Colour |
| --- | --- |
| shell | `#153E44` |
| shell rim | `#2E6F6C` |
| skin | `#7DBAAE` |
| supply pipe | `#FF6B3D` |
| return pipe | `#FFBC4E` |
| wordmark (light / dark theme) | `#153E44` / `#E4F1EE`, "ufh" `#F2572C` / `#FF6B3D` |

The wordmark is Nunito ExtraBold (SIL Open Font License), converted to outlines, so no
font is needed to display it.

## Where the assets come from

Every asset is generated from code, in `scripts/brand/`:

- `logo.mjs` draws the mark and the lockup as SVG from plain geometry (lines and
  circular arcs). It has no dependencies and also runs in a browser; the pipes, legs,
  head and shell are id-tagged elements (`tu-supply`, `tu-return`, `tu-leg-fl`, ...)
  so the same drawing can be animated.
- `build.mjs` outlines the wordmark (opentype.js + `@fontsource/nunito`), measures the
  mark's visible ink, writes `icon.svg` and rasterises every PNG with headless Chromium
  (Playwright).

```bash
cd scripts/brand
npm ci
node build.mjs                       # regenerate all tracked assets in place
node build.mjs --preview ../../scratchpad/brand-preview   # + a review contact sheet
```

The loop has two levels of detail with the same outline: two turns per spiral for the
logo and the icons, one turn (fewer, bolder pipes) for the 22 px panel header.

Tracked assets:

| File | Size | Purpose |
| --- | --- | --- |
| `custom_components/tortoise_ufh/brand/icon.png` | 256×256 | canonical icon |
| `custom_components/tortoise_ufh/brand/icon@2x.png` | 512×512 | hi-DPI icon |
| `custom_components/tortoise_ufh/brand/icon.svg` | vector | the mark (same drawing as the icons) |
| `custom_components/tortoise_ufh/brand/logo.png` | 800×658 | full lockup (mark + wordmark + tagline), README header (light theme) |
| `custom_components/tortoise_ufh/brand/logo-dark.png` | 800×658 | dark-theme lockup (light wordmark), README `<picture>` source |
| `custom_components/tortoise_ufh/frontend/panel-icon.png` | 256×256 | panel header mark (one-turn loop), served at `/tortoise_ufh_panel/panel-icon.png` (`panel.py`); the panel falls back to the 🐢 glyph if it fails to load |
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
