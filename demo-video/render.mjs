// Siriously demo video: two deterministic passes per frame, then ffmpeg.
//   npm run demo:video                 1920x1080 @ 60 fps, screen captured at 2x (crisp after the tilt)
//   npm run demo:video -- --preview    30 fps, 1x capture: fast iteration
//   flags: --keep-frames  --only <shot>[,<shot>]  (renders just those shots; no encode)
// Pass 1 (flat): scene/index.html at window.render(shot, ms) -> frames/flat/<shot>/00001.jpg
// Pass 2 (composite): composite/index.html tilts/blurs/vignettes it -> frames/final/00001.png
// Then out/demo.mp4 (H.264 CRF 18, yuv420p), out/demo.webm (VP9), out/poster.png, out/contact-sheet.png.
import { chromium } from "playwright";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import * as C from "./config.mjs";

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const PREVIEW = args.includes("--preview");
const KEEP = args.includes("--keep-frames");
const onlyArg = args.includes("--only") ? args[args.indexOf("--only") + 1].split(",") : null;
const FPS = PREVIEW ? 30 : C.FPS;
const DSF = PREVIEW ? 1 : 2;
const FADE = Math.max(2, Math.round((C.FADE_FRAMES * FPS) / 60));
const FRAMES = path.join(ROOT, "frames"), OUT = path.join(ROOT, "out");
const FINAL = path.join(FRAMES, "final");

const easeInOut = (x) => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2);
const ffmpeg = (() => { try { execFileSync("ffmpeg", ["-version"], { stdio: "ignore" }); return "ffmpeg"; } catch { return null; } })();

const t0 = Date.now();
fs.rmSync(FRAMES, { recursive: true, force: true });
fs.mkdirSync(FINAL, { recursive: true });
fs.mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();
const scene = await (await browser.newContext({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: DSF, colorScheme: "dark" })).newPage();
await scene.goto(pathToFileURL(path.join(ROOT, "scene/index.html")).href);
await scene.evaluate(() => document.fonts.ready);
const comp = await (await browser.newContext({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 })).newPage();
await comp.goto(pathToFileURL(path.join(ROOT, "composite/index.html")).href);

const shots = C.SHOTS.filter((s) => !onlyArg || onlyArg.includes(s.name));
let g = 0;                                   // global frame number (1-based in file names)
const index = [];                            // [{shot, f, n, global}]
for (const [si, shot] of shots.entries()) {
  const n = Math.round((shot.ms / 1000) * FPS);
  const flatDir = path.join(FRAMES, "flat", shot.name);
  fs.mkdirSync(flatDir, { recursive: true });
  const ts = Date.now();
  for (let f = 0; f < n; f++) {
    const ms = (f * 1000) / FPS;
    const name = String(f + 1).padStart(5, "0");
    // pass 1: the flat screen at this instant
    await scene.evaluate(([s, t]) => window.render(s, t), [shot.name, ms]);
    const flat = path.join(flatDir, `${name}.jpg`);
    await scene.screenshot({ path: flat, type: "jpeg", quality: 95 });
    // pass 2: composite (camera drift eased over the whole shot)
    const p = easeInOut(n > 1 ? f / (n - 1) : 0) - 0.5;
    const half = FADE / 2;
    let fade = 0;
    if (f < half) fade = 1 - (f + 1) / (half + 1);                                     // fade in
    if (si < shots.length - 1 && f >= n - half) fade = (f - (n - half) + 1) / (half + 1); // fade out
    await comp.evaluate((q) => window.compose(q), {
      src: pathToFileURL(flat).href, frame: g + 1,
      focusX: shot.focus[0], focusY: shot.focus[1], driftX: shot.drift[0] * p, driftY: shot.drift[1] * p,
      scale: shot.scale * (1 + shot.zoomDrift * p), rotX: shot.rotX, rotZ: shot.rotZ, liftY: shot.liftY || 0,
      ...C.LOOK, ...(shot.look || {}), fade,
    });
    g += 1;
    await comp.screenshot({ path: path.join(FINAL, `${String(g).padStart(5, "0")}.png`), type: "png" });
    index.push({ shot: shot.name, f, n, ms, global: g });
  }
  console.log(`${shot.name.padEnd(8)} ${n} frames in ${((Date.now() - ts) / 1000).toFixed(1)} s`);
  if (!KEEP) fs.rmSync(flatDir, { recursive: true, force: true });
}
await browser.close();

if (onlyArg) {
  console.log(`only ${onlyArg.join(",")}: frames in ${FINAL} (no encode)`);
  process.exit(0);
}
if (!ffmpeg) { console.error("ffmpeg not found (brew install ffmpeg)"); process.exit(1); }
const ff = (a) => execFileSync(ffmpeg, ["-y", "-loglevel", "error", ...a], { stdio: "inherit" });
const pattern = path.join(FINAL, "%05d.png");
ff(["-framerate", String(FPS), "-i", pattern, "-c:v", "libx264", "-preset", "slow", "-crf", "18",
    "-pix_fmt", "yuv420p", "-movflags", "+faststart", path.join(OUT, "demo.mp4")]);
ff(["-framerate", String(FPS), "-i", pattern, "-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0", "-row-mt", "1",
    "-deadline", "good", "-cpu-used", "4", "-pix_fmt", "yuv420p", path.join(OUT, "demo.webm")]);

// poster: the configured frame
const pf = index.find((x) => x.shot === C.POSTER.shot && x.ms >= C.POSTER.ms) || index[Math.floor(index.length / 2)];
fs.copyFileSync(path.join(FINAL, `${String(pf.global).padStart(5, "0")}.png`), path.join(OUT, "poster.png"));

// contact sheet: 12 evenly spaced frames, 4 x 3
const sheet = path.join(FRAMES, "sheet");
fs.mkdirSync(sheet, { recursive: true });
for (let i = 0; i < 12; i++) {
  const k = 1 + Math.round((i * (g - 1)) / 11);
  fs.copyFileSync(path.join(FINAL, `${String(k).padStart(5, "0")}.png`), path.join(sheet, `${String(i + 1).padStart(2, "0")}.png`));
}
ff(["-i", path.join(sheet, "%02d.png"), "-vf", "scale=640:-1,tile=4x3:padding=6:margin=6:color=black",
    "-frames:v", "1", path.join(OUT, "contact-sheet.png")]);

if (!KEEP) fs.rmSync(FRAMES, { recursive: true, force: true });
console.log(`\n${g} frames @ ${FPS} fps = ${(g / FPS).toFixed(2)} s, rendered in ${((Date.now() - t0) / 1000).toFixed(0)} s`);
for (const f of ["demo.mp4", "demo.webm", "poster.png", "contact-sheet.png"]) {
  console.log(`  ${path.join(OUT, f)}  ${(fs.statSync(path.join(OUT, f)).size / 1e6).toFixed(1)} MB`);
}
