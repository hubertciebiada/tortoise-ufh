/**
 * Build every Tortoise-UFH brand asset from `logo.mjs`.
 *
 *   cd scripts/brand && npm ci && node build.mjs [--preview <dir>]
 *
 * Steps: outline the wordmark with the bundled Nunito font (opentype.js, so the
 * SVGs need no font), measure the mark's visible ink in headless Chromium,
 * write the vector files, then rasterise every PNG with the same browser
 * (Playwright). `--preview <dir>` also writes a contact sheet (light and dark
 * backgrounds, several sizes) for review; it is not a tracked asset.
 */

import { mkdirSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import opentype from "opentype.js";
import { chromium } from "playwright";

import { lockupSvg, markSvg } from "./logo.mjs";

const require = createRequire(import.meta.url);
const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..", "..");
const BRAND = join(ROOT, "custom_components", "tortoise_ufh", "brand");
const FRONTEND = join(ROOT, "custom_components", "tortoise_ufh", "frontend");

const NAME = [
  { text: "tortoise-", role: "name" },
  { text: "ufh", role: "accent" },
];
const TAGLINE = "SLOW & STEADY UNDERFLOOR HEATING";

// --- Wordmark outlines --------------------------------------------------------

function loadFont(weight) {
  return opentype.loadSync(
    require.resolve(`@fontsource/nunito/files/nunito-latin-${weight}-normal.woff`),
  );
}

const emptyBox = () => ({ minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity });
function grow(box, b) {
  box.minX = Math.min(box.minX, b.minX);
  box.minY = Math.min(box.minY, b.minY);
  box.maxX = Math.max(box.maxX, b.maxX);
  box.maxY = Math.max(box.maxY, b.maxY);
}

/**
 * Outline `text` glyph by glyph (pair kerning, optional tracking) starting at
 * pen position x on baseline 0. Returns path data, ink box and the pen advance.
 */
function outline(font, text, size, { x = 0, tracking = 0 } = {}) {
  const scale = size / font.unitsPerEm;
  const box = emptyBox();
  const parts = [];
  let pen = x;
  let prev = null;
  for (const ch of text) {
    const glyph = font.charToGlyph(ch);
    if (prev) pen += font.getKerningValue(prev, glyph) * scale;
    const path = glyph.getPath(pen, 0, size);
    const bb = path.getBoundingBox();
    if (bb.x1 < bb.x2) grow(box, { minX: bb.x1, minY: bb.y1, maxX: bb.x2, maxY: bb.y2 });
    parts.push(path.toPathData(2));
    pen += glyph.advanceWidth * scale + tracking;
    prev = glyph;
  }
  return { d: parts.join(""), box, end: pen - tracking };
}

function wordmark() {
  const font = loadFont(800);
  const box = emptyBox();
  const parts = [];
  let x = 0;
  for (const { text, role } of NAME) {
    const o = outline(font, text, 132, { x });
    parts.push({ d: o.d, role });
    grow(box, o.box);
    x = o.end;
  }
  return { parts, box };
}

function tagline() {
  return outline(loadFont(700), TAGLINE, 29, { tracking: 5 });
}

// --- Browser helpers --------------------------------------------------------

const page = (svg, w, h) =>
  `<!doctype html><html><head><style>html,body{margin:0;background:transparent}` +
  `svg{display:block;width:${w}px;height:${h}px}</style></head><body>${svg}</body></html>`;

/**
 * Tight box of the visible ink of an SVG drawn on the 512 artboard, found by
 * rasterising it at 4x and scanning the alpha channel (clipped pipe leads and
 * other hidden geometry do not count).
 */
async function inkBox(browser, svg) {
  const tab = await browser.newPage();
  await tab.setContent("<!doctype html><html><body></body></html>");
  const box = await tab.evaluate(async (source) => {
    const scale = 4;
    const img = new Image();
    img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(source)}`;
    await img.decode();
    const canvas = document.createElement("canvas");
    canvas.width = 512 * scale;
    canvas.height = 512 * scale;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    const { data } = ctx.getImageData(0, 0, canvas.width, canvas.height);
    let x0 = canvas.width;
    let y0 = canvas.height;
    let x1 = -1;
    let y1 = -1;
    for (let y = 0; y < canvas.height; y++) {
      for (let x = 0; x < canvas.width; x++) {
        if (data[(y * canvas.width + x) * 4 + 3] > 8) {
          if (x < x0) x0 = x;
          if (x > x1) x1 = x;
          if (y < y0) y0 = y;
          if (y > y1) y1 = y;
        }
      }
    }
    return [x0 / scale, y0 / scale, (x1 + 1) / scale, (y1 + 1) / scale];
  }, svg);
  await tab.close();
  return { minX: box[0], minY: box[1], maxX: box[2], maxY: box[3] };
}

/** Square viewBox around the ink with `pad` (fraction of the side) of air. */
function squareAround(b, pad) {
  const side = Math.max(b.maxX - b.minX, b.maxY - b.minY) * (1 + 2 * pad);
  const cx = (b.minX + b.maxX) / 2;
  const cy = (b.minY + b.maxY) / 2;
  return [cx - side / 2, cy - side / 2, side, side];
}

async function png(browser, svg, file, w, h) {
  const tab = await browser.newPage({ viewport: { width: w, height: h } });
  await tab.setContent(page(svg, w, h));
  await tab.screenshot({
    path: file,
    omitBackground: true,
    clip: { x: 0, y: 0, width: w, height: h },
  });
  await tab.close();
}

// --- Build ------------------------------------------------------------------

async function main() {
  const previewAt = process.argv.indexOf("--preview");
  const previewDir = previewAt > 0 ? resolve(process.argv[previewAt + 1]) : null;
  const browser = await chromium.launch();
  try {
    const box = {};
    const view = {};
    for (const turns of [1, 2]) {
      box[turns] = await inkBox(browser, markSvg({ turns }));
      view[turns] = squareAround(box[turns], 0.02);
    }
    const icon = (turns, size) => markSvg({ turns, viewBox: view[turns], size });
    const markBox = [
      box[2].minX,
      box[2].minY,
      box[2].maxX - box[2].minX,
      box[2].maxY - box[2].minY,
    ];
    const name = wordmark();
    const tag = tagline();
    const lockups = Object.fromEntries(
      ["light", "dark"].map((theme) => [theme, lockupSvg({ markBox, name, tagline: tag, theme })]),
    );

    // Full detail everywhere except the panel header, which shows the mark at
    // 22 px: there the one-turn loop (fewer, bolder pipes) stays legible.
    for (const dir of [BRAND, FRONTEND]) mkdirSync(dir, { recursive: true });
    writeFileSync(join(BRAND, "icon.svg"), `${icon(2)}\n`);
    await png(browser, icon(2, 256), join(BRAND, "icon.png"), 256, 256);
    await png(browser, icon(2, 512), join(BRAND, "icon@2x.png"), 512, 512);
    await png(browser, icon(1, 256), join(FRONTEND, "panel-icon.png"), 256, 256);
    for (const [theme, file] of [
      ["light", "logo.png"],
      ["dark", "dark_logo.png"],
    ]) {
      const { svg, width, height } = lockups[theme];
      await png(browser, svg, join(BRAND, file), width, height);
    }

    if (previewDir) {
      mkdirSync(previewDir, { recursive: true });
      writeFileSync(join(previewDir, "logo.svg"), lockups.light.svg);
      writeFileSync(join(previewDir, "mark.svg"), icon(2));
      const sizes = [256, 128, 64, 44, 22];
      const strip = (turns) =>
        sizes.map((s) => `<div class="c">${icon(turns, s)}<span>${s}</span></div>`).join("");
      const panel = (bg, fg) =>
        `<div class="p" style="background:${bg};color:${fg}">` +
        `<div class="lk">${(bg === "#ffffff" ? lockups.light : lockups.dark).svg.replace(/width="\d+" height="\d+"/, 'width="400"')}</div>` +
        `<div><div class="row">${strip(2)}</div><div class="lbl">turns = 2 (logo, icons)</div>` +
        `<div class="row">${strip(1)}</div><div class="lbl">turns = 1 (panel header)</div></div></div>`;
      const sheet =
        `<!doctype html><html><head><style>body{margin:0;font:13px sans-serif}` +
        `.p{display:flex;gap:40px;align-items:center;padding:28px 36px}.row{display:flex;gap:22px;align-items:flex-end}` +
        `.c{display:flex;flex-direction:column;align-items:center;gap:6px}.lbl{margin:6px 0 18px;opacity:.7}</style></head>` +
        `<body>${panel("#ffffff", "#333")}${panel("#111418", "#ccc")}</body></html>`;
      const tab = await browser.newPage({ viewport: { width: 1440, height: 900 } });
      await tab.setContent(sheet);
      await tab.screenshot({ path: join(previewDir, "preview.png"), fullPage: true });
      await tab.close();
    }
  } finally {
    await browser.close();
  }
}

await main();
