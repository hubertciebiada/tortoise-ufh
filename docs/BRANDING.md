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
| `custom_components/tortoise_ufh/brand/icon.png` | 256×256 | canonical icon (Home Assistant, HACS) |
| `custom_components/tortoise_ufh/brand/icon@2x.png` | 512×512 | hi-DPI icon |
| `custom_components/tortoise_ufh/brand/icon.svg` | vector | the mark (same drawing as the icons) |
| `custom_components/tortoise_ufh/brand/logo.png` | 800×658 | full lockup (mark + wordmark + tagline) for light backgrounds: README header, Home Assistant logo |
| `custom_components/tortoise_ufh/brand/dark_logo.png` | 800×658 | the same lockup with a light wordmark for dark backgrounds: README `<picture>` source, Home Assistant dark theme |
| `custom_components/tortoise_ufh/frontend/panel-icon.png` | 256×256 | panel header mark (one-turn loop), served at `/tortoise_ufh_panel/panel-icon.png` (`panel.py`); the panel falls back to the 🐢 glyph if it fails to load |

## In Home Assistant and HACS

Since Home Assistant 2026.3 a custom integration ships its own brand images: HA serves
the files in the installed integration's `brand/` folder instead of asking the brands
CDN. It reads `icon.png`, `icon@2x.png`, `logo.png` and `dark_logo.png` from there
(the `dark_` prefix marks a dark-theme variant, hence the name of the dark lockup);
`icon.svg` is not one of them. So Home Assistant and HACS show the images of the
*installed* version: a new icon or logo appears once a release that ships it is
installed and HA restarted (a browser may need a hard refresh).

Nothing is submitted to [home-assistant/brands](https://github.com/home-assistant/brands)
any more. Before 2026.3, Home Assistant shows its generic placeholder icon for the
integration.
