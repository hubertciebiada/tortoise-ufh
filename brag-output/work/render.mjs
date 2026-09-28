// Render the /brag video: drive window.renderFrame(t) frame by frame in
// headless Chromium and pipe PNG screenshots straight into ffmpeg.
//   node render.mjs [--stills 0.5,3.2,...] [--from s --to s] [--out file.mp4]
//   node render.mjs --teaser   # README loop frames -> teaser-frames/ (15 fps)
import { spawn } from "node:child_process";
import { chromium } from "../../scripts/brand/node_modules/playwright/index.mjs";

const arg = (name, dflt) => {
  const i = process.argv.indexOf(name);
  return i > 0 ? process.argv[i + 1] : dflt;
};
const FPS = 30;
const DURATION = 21.0;
const FFMPEG = process.env.FFMPEG;
const TEASER = process.argv.includes("--teaser");
const url = `http://127.0.0.1:8765/brag-output/work/video.html${TEASER ? "?teaser" : ""}`;

const browser = await chromium.launch({ args: ["--disable-lcd-text", "--font-render-hinting=none"] });
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
page.on("pageerror", (e) => console.error("pageerror:", e.message));
page.on("console", (m) => m.type() === "error" && console.error("console:", m.text()));
await page.goto(url);
await page.waitForFunction(() => window.videoReady === true, null, { timeout: 60000 });

if (TEASER) {
  const { mkdirSync } = await import("node:fs");
  mkdirSync("teaser-frames", { recursive: true });
  let n = 0;
  for (let t = 1 / 30; t < 6.99; t += 1 / 15) {
    await page.evaluate((tt) => window.renderFrame(tt), t);
    await page.screenshot({ path: `teaser-frames/f${String(n++).padStart(4, "0")}.png` });
  }
  await browser.close();
  process.exit(0);
}

const stills = arg("--stills");
if (stills) {
  for (const s of stills.split(",").map(Number)) {
    await page.evaluate((t) => window.renderFrame(t), s);
    await page.screenshot({ path: `stills/t${s.toFixed(2).padStart(5, "0")}.png` });
  }
  await browser.close();
  process.exit(0);
}

const from = Number(arg("--from", 0));
const to = Number(arg("--to", DURATION));
const out = arg("--out", "video-only.mp4");
const ff = spawn(FFMPEG, [
  "-y", "-loglevel", "error",
  "-f", "image2pipe", "-framerate", String(FPS), "-i", "-",
  "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p",
  "-profile:v", "high", "-movflags", "+faststart", out,
], { stdio: ["pipe", "inherit", "inherit"] });
const n0 = Math.round(from * FPS);
const n1 = Math.round(to * FPS);
const started = Date.now();
for (let n = n0; n < n1; n++) {
  const t = n / FPS;
  await page.evaluate((tt) => window.renderFrame(tt), t);
  const buf = await page.screenshot({ type: "png" });
  if (!ff.stdin.write(buf)) await new Promise((r) => ff.stdin.once("drain", r));
  if (n % 60 === 0) console.log(`frame ${n}/${n1} (${((Date.now() - started) / 1000).toFixed(0)} s)`);
}
ff.stdin.end();
await new Promise((r) => ff.on("close", r));
await browser.close();
console.log("done", out);
