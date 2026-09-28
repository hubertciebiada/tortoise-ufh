// The /brag video for Tortoise-UFH: scenes and window.renderFrame(t).
// Every visual property is computed from t alone (seconds), so any frame can be
// rendered on its own.

import { markBody } from "/scripts/brand/logo.mjs";
import "/custom_components/tortoise_ufh/frontend/tortoise-ufh-panel.js";

const NS = "http://www.w3.org/2000/svg";
// ?teaser: flat background, no glow and no sway, so the README loop compresses
// cleanly as an animated WebP.
const TEASER = new URLSearchParams(location.search).has("teaser");
if (TEASER) document.getElementById("stage").style.background = "#0f2f35";
const $ = (id) => document.getElementById(id);

// --- Timing helpers ------------------------------------------------------------

const clamp = (v, a = 0, b = 1) => Math.min(b, Math.max(a, v));
const lerp = (a, b, u) => a + (b - a) * u;
const seg = (t, a, b) => clamp((t - a) / (b - a));
const easeOut = (u) => 1 - (1 - u) ** 3;
const easeInOut = (u) => (u < 0.5 ? 4 * u ** 3 : 1 - (-2 * u + 2) ** 3 / 2);
const easeSine = (u) => -(Math.cos(Math.PI * u) - 1) / 2;
const easeBack = (u) => {
  const c1 = 1.55;
  const c3 = c1 + 1;
  return 1 + c3 * (u - 1) ** 3 + c1 * (u - 1) ** 2;
};
/** 0 -> 1 over [a, a + dIn], back to 0 over [b - dOut, b] (visibility envelope). */
const env = (t, a, dIn, b, dOut) => Math.min(easeOut(seg(t, a, a + dIn)), 1 - easeInOut(seg(t, b - dOut, b)));

function show(el, opacity, tx = 0, ty = 0, scale = 1) {
  el.style.opacity = opacity.toFixed(4);
  el.style.visibility = opacity <= 0.001 ? "hidden" : "visible";
  const identity = Math.abs(tx) < 0.01 && Math.abs(ty) < 0.01 && Math.abs(scale - 1) < 1e-4;
  el.style.transform = identity ? "none" : `translate(${tx.toFixed(2)}px, ${ty.toFixed(2)}px) scale(${scale.toFixed(4)})`;
}

// --- Scene timeline (s) ----------------------------------------------------------

const T = {
  fill: [0.2, 2.72], // hot water runs through the loop
  drop: 3.0, // downbeat: camera pulls out
  s3: 7.0,
  clickLive: 8.22,
  clickYes: 8.6,
  clickTab: 9.15,
  s4: 11.0,
  s5: 15.0,
  s6: 18.0,
  end: 21.0,
};

// --- Fonts -------------------------------------------------------------------------

await Promise.all([
  document.fonts.load('800 84px "Nunito"'),
  document.fonts.load('700 40px "Nunito"'),
  document.fonts.load('400 16px "Roboto"'),
  document.fonts.load('500 16px "Roboto"'),
  document.fonts.load('700 16px "Roboto"'),
]);
await document.fonts.ready;

// --- The mark ----------------------------------------------------------------------

const cam = $("cam");
cam.innerHTML = markBody({ turns: 2, idPrefix: "tu" });
const q = (id) => cam.querySelector(`#tu-${id}`);

// Wrap pop-in parts so they can scale about their attachment point.
function wrap(el, px, py) {
  const g = document.createElementNS(NS, "g");
  el.parentNode.insertBefore(g, el);
  g.appendChild(el);
  return { g, px, py };
}
const legs = ["fl", "fr", "bl", "br"].map((k) => {
  const el = q(`leg-${k}`);
  const m = /translate\(([-\d.]+) ([-\d.]+)\) rotate\(([-\d.]+)\)/.exec(el.getAttribute("transform"));
  const [x, y, a] = [Number(m[1]), Number(m[2]), Number(m[3])];
  return { el, x, y, a, k, pop: wrap(el, x, y) };
});
const head = wrap(q("head"), 256, 150);
const tail = wrap(q("tail"), 256, 404);

// Empty pipes (dim channels) under the water, and the water itself drawn as a
// growing dash along supply -> turn -> return (flow direction).
const loopG = q("loop");
const pipes = ["supply", "turn", "return"].map((id) => q(id));
const lengths = pipes.map((p) => p.getTotalLength());
const total = lengths.reduce((a, b) => a + b, 0);
for (const p of [...pipes].reverse()) {
  const under = p.cloneNode();
  under.removeAttribute("id");
  under.setAttribute("stroke", "#1f5359");
  loopG.insertBefore(under, loopG.firstChild);
}
const front = document.createElementNS(NS, "circle");
front.setAttribute("r", "9");
front.setAttribute("fill", "#ffe9cf");
front.setAttribute("filter", "url(#tu-soft)");
loopG.appendChild(front);
const soft = document.createElementNS(NS, "filter");
soft.id = "tu-soft";
soft.setAttribute("x", "-2");
soft.setAttribute("y", "-2");
soft.setAttribute("width", "5");
soft.setAttribute("height", "5");
soft.innerHTML = '<feGaussianBlur stdDeviation="4.5"/>';
cam.querySelector("defs").appendChild(soft);

// A bright pulse that runs through the loop once on the end card.
const pulse = pipes.map((p) => {
  const c = p.cloneNode();
  c.removeAttribute("id");
  c.setAttribute("stroke", "#fff4e6");
  c.setAttribute("stroke-linecap", "round");
  c.style.mixBlendMode = "screen";
  loopG.insertBefore(c, front);
  return c;
});

function setWater(s) {
  let off = 0;
  pipes.forEach((p, i) => {
    const vis = clamp(s - off, 0, lengths[i]);
    p.setAttribute("stroke-dasharray", `${vis.toFixed(2)} ${(lengths[i] + 20).toFixed(2)}`);
    off += lengths[i];
  });
}
function pointAt(s) {
  let off = 0;
  for (let i = 0; i < pipes.length; i++) {
    if (s <= off + lengths[i] || i === pipes.length - 1) {
      return pipes[i].getPointAtLength(clamp(s - off, 0, lengths[i]));
    }
    off += lengths[i];
  }
  return { x: 0, y: 0 };
}
function setPulse(s, len, opacity) {
  let off = 0;
  pulse.forEach((p, i) => {
    const a = clamp(s - len - off, 0, lengths[i]);
    const b = clamp(s - off, 0, lengths[i]);
    p.setAttribute("stroke-dasharray", `0 ${a.toFixed(2)} ${(b - a).toFixed(2)} ${(lengths[i] * 2).toFixed(2)}`);
    p.setAttribute("opacity", (b > a ? opacity : 0).toFixed(3));
    off += lengths[i];
  });
}

function setCamera(ax, ay, sx, sy, k, extra = "") {
  cam.setAttribute("transform", `translate(${sx} ${sy}) scale(${k}) translate(${-ax} ${-ay}) ${extra}`);
  return (x, y) => ({ x: sx + (x - ax) * k, y: sy + (y - ay) * k });
}
const pop = (w, s) =>
  w.g.setAttribute("transform", `translate(${w.px} ${w.py}) scale(${Math.max(0.0001, s)}) translate(${-w.px} ${-w.py})`);

// --- Warm glow ---------------------------------------------------------------------

const glow = $("glow").getContext("2d");
function drawGlow(x, y, r, alpha) {
  glow.clearRect(0, 0, 1920, 1080);
  if (alpha <= 0.001 || TEASER) return;
  const g = glow.createRadialGradient(x, y, 0, x, y, r);
  g.addColorStop(0, `rgba(255, 128, 64, ${alpha})`);
  g.addColorStop(0.45, `rgba(255, 128, 64, ${alpha * 0.35})`);
  g.addColorStop(1, "rgba(255, 128, 64, 0)");
  glow.fillStyle = g;
  glow.fillRect(0, 0, 1920, 1080);
}

// --- The real panel ----------------------------------------------------------------

const panel = $("panel");
const hass = window.makeHass();
panel.hass = hass;
const settle = () => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
await settle();
await panel._loadConfig();
await panel._poll();
panel._stopTimers();
const noMotion = document.createElement("style");
noMotion.textContent = "*,*::before,*::after{transition:none!important;animation:none!important}";
panel.shadowRoot.appendChild(noMotion);

let panelState = null;
async function setPanel({ live, valve, tab, confirm, age }) {
  const key = `${live}|${valve}|${tab}|${confirm}`;
  window.__now = Date.UTC(2026, 0, 14, 7, 40, 0) + age * 1000;
  panel._tickAge();
  if (key === panelState) return;
  const liveChanged = !panelState || panelState.split("|")[0] !== String(live);
  panelState = key;
  window.mock.bedroomLive = live;
  window.mock.bedroomValve = valve;
  if (liveChanged) await panel._loadConfig();
  await panel._poll();
  panel._setActiveTab(tab);
  const btn = panel._rows.get("Bedroom").querySelector('.seg-state-btn[data-state="live"]');
  if (confirm && !panel._confirmResolve) {
    panel._confirm(btn, "Enable control of “Bedroom”? Tortoise will start writing commands to the valves and the assist unit.");
  } else if (!confirm && panel._confirmResolve) {
    panel._resolveConfirm(false);
  }
  await settle();
}

const SCROLL = 330; // px the manifold view scrolls to reveal the valve actuators
// Cursor targets in stage coordinates (the panel is laid out 1:1).
const center = (el) => {
  const r = el.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
};
show($("panelWin"), 1);
await setPanel({ live: false, valve: 0, tab: "rooms", confirm: false, age: 38 });
const liveBtn = center(panel._rows.get("Bedroom").querySelector('.seg-state-btn[data-state="live"]'));
await setPanel({ live: false, valve: 0, tab: "rooms", confirm: true, age: 38 });
const yesBtn = center(panel._confirmEls.yes);
await setPanel({ live: false, valve: 0, tab: "rooms", confirm: false, age: 38 });
const tabBtn = center(panel._tabs.btns.manifolds);
const cursorPath = [
  { t: 7.62, p: { x: 1580, y: 960 } },
  { t: 8.18, p: liveBtn },
  { t: 8.34, p: liveBtn },
  { t: 8.56, p: yesBtn },
  { t: 8.72, p: yesBtn },
  { t: 9.12, p: tabBtn },
  { t: 9.5, p: tabBtn },
  { t: 9.9, p: { x: tabBtn.x + 260, y: tabBtn.y + 330 } },
];
function cursorAt(t) {
  for (let i = 0; i < cursorPath.length - 1; i++) {
    const a = cursorPath[i];
    const b = cursorPath[i + 1];
    if (t <= b.t) {
      const u = easeInOut(seg(t, a.t, b.t));
      return { x: lerp(a.p.x, b.p.x, u), y: lerp(a.p.y, b.p.y, u) };
    }
  }
  return cursorPath[cursorPath.length - 1].p;
}

// --- The proof chart (real controller on the digital twin) --------------------

const sim = await (await fetch("simdata.json")).json();
const rows = sim.steady_heating.rooms.main;
const C = { x0: 230, x1: 1480, tTop: 21.5, tBot: 19.5, yTop: 262, yBot: 772, vTop: 842, vBot: 952 };
const X = (h) => C.x0 + (h / 48) * (C.x1 - C.x0);
const Y = (tc) => C.yBot - ((tc - C.tBot) / (C.tTop - C.tBot)) * (C.yBot - C.yTop);
const V = (pct) => C.vBot - (pct / 100) * (C.vBot - C.vTop);
const peak = rows.reduce((m, r) => (r.t_air > m.t_air ? r : m), rows[0]);
const overshoot = peak.t_air - 21.0;
const chart = $("chart");
const tempD = rows.map((r, i) => `${i ? "L" : "M"}${X(r.t_h).toFixed(1)} ${Y(r.t_air).toFixed(1)}`).join("");
const valveD = rows.map((r, i) => `${i ? "L" : "M"}${X(r.t_h).toFixed(1)} ${V(r.valve).toFixed(1)}`).join("");
const label = (x, y, text, size, fill, anchor = "start", weight = 700) =>
  `<text x="${x}" y="${y}" font-family="Nunito" font-weight="${weight}" font-size="${size}" fill="${fill}" text-anchor="${anchor}">${text}</text>`;
chart.innerHTML = `
  <defs>
    <clipPath id="reveal"><rect id="revealRect" x="0" y="0" width="0" height="1080"/></clipPath>
    <linearGradient id="tempFill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#ff6b3d" stop-opacity=".30"/><stop offset="1" stop-color="#ff6b3d" stop-opacity="0"/>
    </linearGradient>
    <linearGradient id="valveFill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#7dbaae" stop-opacity=".35"/><stop offset="1" stop-color="#7dbaae" stop-opacity="0"/>
    </linearGradient>
    <filter id="lineGlow" x="-10%" y="-10%" width="120%" height="120%"><feGaussianBlur stdDeviation="6" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
  </defs>
  <g id="axes">
    ${[20, 21].map((v) => `<line x1="${C.x0}" x2="${C.x1}" y1="${Y(v)}" y2="${Y(v)}" stroke="rgba(242,247,246,.10)" stroke-width="2"/>${label(C.x0 - 22, Y(v) + 10, `${v} °C`, 30, "#93bbb4", "end")}`).join("")}
    <line x1="${C.x0}" x2="${C.x1}" y1="${C.vBot}" y2="${C.vBot}" stroke="rgba(242,247,246,.10)" stroke-width="2"/>
    ${label(C.x0 - 22, C.vBot - 18, "valve", 28, "#7dbaae", "end")}
    ${[0, 12, 24, 36, 48].map((h) => label(X(h), 1006, `${h} h`, 30, "#93bbb4", "middle")).join("")}
  </g>
  <g id="setpoint">
    <line x1="${C.x0}" x2="${C.x1}" y1="${Y(21)}" y2="${Y(21)}" stroke="#f2f7f6" stroke-opacity=".75" stroke-width="3" stroke-dasharray="14 12"/>
    ${label(C.x0 + 6, Y(21) - 20, "setpoint", 30, "#f2f7f6", "start", 800)}
  </g>
  <g clip-path="url(#reveal)">
    <path d="${valveD}L${X(48)} ${C.vBot}L${X(0)} ${C.vBot}Z" fill="url(#valveFill)"/>
    <path d="${valveD}" fill="none" stroke="#7dbaae" stroke-width="4" stroke-linejoin="round"/>
    <path d="${tempD}L${X(48)} ${C.yBot}L${X(0)} ${C.yBot}Z" fill="url(#tempFill)"/>
    <path d="${tempD}" fill="none" stroke="#ff6b3d" stroke-width="7" stroke-linejoin="round" stroke-linecap="round" filter="url(#lineGlow)"/>
  </g>
  <circle id="head4" r="11" fill="#ffe9cf" stroke="#ff6b3d" stroke-width="5"/>
  <g id="callout">
    <path d="M${X(48) + 20} ${Y(21)}h16V${Y(peak.t_air)}h-16" fill="none" stroke="#ffbc4e" stroke-width="4" stroke-linejoin="round"/>
    ${label(X(48) + 50, Y(21) - 34, `+${overshoot.toFixed(2)} K`, 64, "#ffbc4e", "start", 800)}
    ${label(X(48) + 52, Y(21) + 16, "peak above", 28, "#cfe5e0")}
    ${label(X(48) + 52, Y(21) + 50, "setpoint", 28, "#cfe5e0")}
  </g>`;
const revealRect = $("revealRect");
const head4 = $("head4");
const tempPath = chart.querySelector('path[stroke="#ff6b3d"]');
const tempLen = tempPath.getTotalLength();

// --- Frame -------------------------------------------------------------------------

window.renderFrame = async (t) => {
  // Scene 1-2 and 6: the mark.
  const fill = easeSine(seg(t, ...T.fill));
  setWater(fill * total);
  const flowing = t > T.fill[0] && t < T.fill[1] + 0.25;
  const fp = pointAt(fill * total);
  front.setAttribute("cx", fp.x.toFixed(2));
  front.setAttribute("cy", fp.y.toFixed(2));
  front.setAttribute("opacity", (flowing ? 0.95 * (1 - seg(t, T.fill[1], T.fill[1] + 0.25)) : 0).toFixed(3));

  let toScreen;
  let markOpacity = 0;
  if (t < T.drop) {
    const k = lerp(2.5, 2.62, easeSine(seg(t, 0, T.drop)));
    toScreen = setCamera(256, 272, 960, 458, k);
    markOpacity = easeOut(seg(t, 0, 0.35));
  } else if (t < T.s3 + 0.1) {
    const u = easeInOut(seg(t, T.drop, T.drop + 0.95));
    const k = lerp(2.62, 1.58, u);
    const ax = lerp(256, 258, u);
    const ay = lerp(272, 258, u);
    const sx = lerp(960, 560, u);
    const sy = lerp(458, 545, u);
    // two slow steps: a gentle sway of the body while the legs paddle
    const walk = seg(t, 3.9, 6.6);
    const sway = TEASER ? 0 : Math.sin(walk * Math.PI * 2) * 1.2 * Math.sin(walk * Math.PI);
    const out = easeInOut(seg(t, 6.72, 6.98));
    toScreen = setCamera(ax, ay, sx - out * 40, sy, k * (1 - 0.04 * out), `rotate(${sway.toFixed(3)} 256 272)`);
    markOpacity = 1 - out;
  } else if (t >= T.s6) {
    const u = easeOut(seg(t, T.s6, T.s6 + 0.45));
    toScreen = setCamera(258, 258, 960, 392, lerp(0.96, 1.02, u));
    markOpacity = u;
  } else {
    toScreen = setCamera(258, 258, 960, 392, 1);
  }
  $("markSvg").style.opacity = markOpacity.toFixed(4);
  $("markSvg").style.visibility = markOpacity <= 0.001 ? "hidden" : "visible";

  // head, legs, tail pop out from under the shell as the camera pulls back
  const popAt = (start) => (t < T.drop ? 0 : t >= T.s6 ? 1 : easeBack(seg(t, start, start + 0.42)));
  pop(head, popAt(3.22));
  legs.forEach((l, i) => pop(l.pop, popAt(3.3 + i * 0.06)));
  pop(tail, popAt(3.56));
  const walking = seg(t, 3.9, 6.6);
  legs.forEach((l) => {
    const phase = l.k === "fl" || l.k === "br" ? 0 : Math.PI;
    const amp = t < T.s6 ? 11 * Math.sin(walking * Math.PI) : 0;
    const da = amp * Math.sin(walking * Math.PI * 4 + phase);
    l.el.setAttribute("transform", `translate(${l.x} ${l.y}) rotate(${(l.a + da).toFixed(3)})`);
  });

  // end card: one bright pulse runs through the loop
  const ps = seg(t, 18.55, 19.75);
  setPulse(easeInOut(ps) * (total + 90), 90, t >= T.s6 ? 0.75 * Math.sin(Math.PI * ps) : 0);

  // warm glow behind the shell
  const c = toScreen(256, 272);
  let glowA = 0;
  if (t < T.s3) glowA = 0.22 * fill * (1 - easeInOut(seg(t, 6.72, 6.98)));
  if (t >= T.s6) glowA = 0.2 * easeOut(seg(t, T.s6, T.s6 + 0.6));
  drawGlow(c.x, c.y, t < T.drop ? 760 : 620, glowA);

  // Scene 1 headline
  const hook = env(t, 0.25, 0.35, 2.96, 0.24);
  show($("hook"), hook, 0, (1 - easeOut(seg(t, 0.25, 0.6))) * 26);

  // Scene 2 wordmark + line
  const bw = env(t, 3.68, 0.5, 6.96, 0.24);
  show($("brand").querySelector(".word"), bw, (1 - easeOut(seg(t, 3.68, 4.18))) * 60 - easeInOut(seg(t, 6.72, 6.96)) * 30);
  const bs = env(t, 3.95, 0.5, 6.96, 0.24);
  show($("brand").querySelector(".sub"), bs, (1 - easeOut(seg(t, 3.95, 4.45))) * 60 - easeInOut(seg(t, 6.72, 6.96)) * 30);

  // Scene 3: the real panel
  const win = env(t, T.s3, 0.45, 10.98, 0.26);
  const winIn = easeOut(seg(t, T.s3, T.s3 + 0.45));
  const winOut = easeInOut(seg(t, 10.72, 10.98));
  show($("panelWin"), win, 0, (1 - winIn) * 70, 1 - 0.03 * winOut);
  if (t >= T.s3 - 0.05 && t < T.s4) {
    const live = t >= T.clickYes + 0.04;
    const valve = live ? Math.round(46 * easeOut(seg(t, 8.7, 9.3))) : 0;
    const confirm = t >= T.clickLive + 0.04 && !live;
    const tab = t >= T.clickTab + 0.05 ? "manifolds" : "rooms";
    await setPanel({ live, valve, tab, confirm, age: 38 + (t - T.s3) });
    $("panelHost").scrollTop = Math.round(SCROLL * easeInOut(seg(t, 9.42, 10.02)));
  }
  show($("cap3a"), env(t, 7.12, 0.3, 9.42, 0.14), 0, (1 - easeOut(seg(t, 7.12, 7.42))) * 18);
  show($("cap3b"), env(t, 9.46, 0.28, 10.9, 0.2), 0, (1 - easeOut(seg(t, 9.46, 9.74))) * 18);

  // cursor + click ripples
  const cur = cursorAt(t);
  const curA = env(t, 7.62, 0.14, 9.95, 0.2);
  const press = [T.clickLive, T.clickYes, T.clickTab].some((c0) => t >= c0 - 0.03 && t < c0 + 0.07);
  $("arrow").setAttribute("transform", `translate(${cur.x.toFixed(1)} ${cur.y.toFixed(1)}) scale(${press ? 0.86 : 1})`);
  $("arrow").setAttribute("opacity", curA.toFixed(3));
  const click = [T.clickLive, T.clickYes, T.clickTab].find((c0) => t >= c0 && t < c0 + 0.4);
  const ripple = $("ripple");
  if (click !== undefined) {
    const u = seg(t, click, click + 0.4);
    const at = cursorAt(click);
    ripple.setAttribute("cx", at.x.toFixed(1));
    ripple.setAttribute("cy", at.y.toFixed(1));
    ripple.setAttribute("r", (10 + 34 * easeOut(u)).toFixed(2));
    ripple.setAttribute("opacity", (0.9 * (1 - u)).toFixed(3));
  } else {
    ripple.setAttribute("opacity", "0");
  }

  // Scene 4: the chart
  const ch = env(t, T.s4, 0.35, 14.98, 0.26);
  chart.style.opacity = ch.toFixed(4);
  chart.style.visibility = ch <= 0.001 ? "hidden" : "visible";
  const draw = easeSine(seg(t, 11.35, 13.4));
  revealRect.setAttribute("width", (C.x0 + draw * (C.x1 - C.x0 + 30)).toFixed(1));
  const hp = tempPath.getPointAtLength(draw * tempLen);
  head4.setAttribute("cx", hp.x.toFixed(1));
  head4.setAttribute("cy", hp.y.toFixed(1));
  head4.setAttribute("opacity", (draw > 0 && draw < 1 ? 1 : draw >= 1 ? 1 - seg(t, 13.4, 13.7) : 0).toFixed(3));
  const co = easeBack(seg(t, 13.38, 13.78));
  const callout = $("callout");
  callout.setAttribute("opacity", seg(t, 13.38, 13.6).toFixed(3));
  callout.setAttribute(
    "transform",
    `translate(${X(48) + 20} ${Y(21)}) scale(${Math.max(0.001, co).toFixed(4)}) translate(${-(X(48) + 20)} ${-Y(21)})`,
  );
  show($("cap4"), env(t, 11.06, 0.3, 14.96, 0.22), 0, (1 - easeOut(seg(t, 11.06, 11.36))) * 18);
  show($("foot4"), env(t, 11.4, 0.3, 14.96, 0.22));

  // Scene 5: the fable
  const f1 = env(t, 15.08, 0.32, 17.94, 0.24);
  const f2 = env(t, 15.55, 0.32, 17.94, 0.24);
  show($("fl1"), f1, 0, (1 - easeOut(seg(t, 15.08, 15.4))) * 34);
  show($("fl2"), f2, 0, (1 - easeOut(seg(t, 15.55, 15.87))) * 34);
  const tags = document.querySelectorAll("#fable .tag");
  tags[0].style.opacity = env(t, 15.3, 0.3, 17.94, 0.24).toFixed(4);
  tags[1].style.opacity = env(t, 15.78, 0.3, 17.94, 0.24).toFixed(4);

  // Scene 6: end card
  show($("end").querySelector(".word"), easeOut(seg(t, 18.14, 18.46)), 0, (1 - easeOut(seg(t, 18.14, 18.46))) * 26);
  show($("end").querySelector(".cta"), easeOut(seg(t, 18.4, 18.72)), 0, (1 - easeOut(seg(t, 18.4, 18.72))) * 20);

  await new Promise((r) => requestAnimationFrame(r));
  return true;
};

// Initial state for the first frame.
for (const el of document.querySelectorAll(".abs")) el.style.visibility = "hidden";
for (const id of ["glow", "markSvg", "cursor"]) $(id).style.visibility = "visible";
for (const id of ["hook", "brand", "fable", "end"]) $(id).style.visibility = "visible";
await window.renderFrame(0);
window.videoReady = true;
