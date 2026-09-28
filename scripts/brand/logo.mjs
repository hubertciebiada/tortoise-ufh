/**
 * Tortoise-UFH logo, drawn from code.
 *
 * A tortoise, seen from above, carries an underfloor-heating loop on its
 * shell. The loop is a bifilar spiral (the "ślimak" layout real UFH installers
 * use): supply and return run side by side, the supply winds in, turns round
 * an S-bend in the middle and winds back out between its own turns, and both
 * ends leave together at one corner, the way a loop reaches its manifold. Hot
 * supply next to cooler return is exactly what keeps a slab evenly warm, and
 * the tortoise is the slab: heavy, slow, and it always gets there.
 *
 * Everything is plain geometry (straight lines and circular arcs), so the SVG
 * stays small and exact, and the same numbers can drive an animation (the
 * supply and return paths are separate, id-tagged elements).
 *
 * The module is dependency-free and runs in Node or a browser. Text is not
 * drawn here: `build.mjs` turns the wordmark into outlines and passes them to
 * `lockupSvg`.
 *
 * Units: SVG user units. The mark is designed on a 512 x 512 artboard.
 */

const PI = Math.PI;

// --- Palette ----------------------------------------------------------------

/** Brand colours. `supply` is the hot pipe, `ret` the (cooler) return. */
export const PALETTE = {
  shell: "#153E44",
  rim: "#2E6F6C",
  skin: "#7DBAAE",
  claw: "#5C9990",
  eye: "#0F2226",
  supply: "#FF6B3D",
  ret: "#FFBC4E",
};

/** Wordmark colours per theme (the mark itself works on light and dark). */
export const INK = {
  light: { name: "#153E44", accent: "#F2572C", tagline: "#4E7F7B" },
  dark: { name: "#E4F1EE", accent: "#FF6B3D", tagline: "#9CC3BC" },
};

// --- Geometry kit -----------------------------------------------------------

const pt = (x, y) => ({ x, y });
const line = (p0, p1) => ({ kind: "line", p0, p1 });
/** Circular arc; angles in radians, SVG orientation (y down), a0 -> a1. */
const arc = (c, r, a0, a1) => ({ kind: "arc", c, r, a0, a1 });
const onArc = (c, r, a) => pt(c.x + r * Math.cos(a), c.y + r * Math.sin(a));
const segStart = (s) => (s.kind === "line" ? s.p0 : onArc(s.c, s.r, s.a0));
const segEnd = (s) => (s.kind === "line" ? s.p1 : onArc(s.c, s.r, s.a1));
const segLength = (s) =>
  s.kind === "line" ? Math.hypot(s.p1.x - s.p0.x, s.p1.y - s.p0.y) : Math.abs(s.a1 - s.a0) * s.r;
const reverseSeg = (s) => (s.kind === "line" ? line(s.p1, s.p0) : arc(s.c, s.r, s.a1, s.a0));
const reversePath = (segs) => segs.map(reverseSeg).reverse();
/** Number formatted for SVG output (2 decimals unless asked for more). */
const num = (v, digits = 2) => String(Math.round(v * 10 ** digits) / 10 ** digits);

/** SVG path data for a list of line/arc segments. */
export function toPathData(segs) {
  let d = "";
  let cur = null;
  for (const s of segs) {
    const a = segStart(s);
    if (!cur || Math.hypot(cur.x - a.x, cur.y - a.y) > 0.01) {
      d += `M${num(a.x)} ${num(a.y)}`;
    }
    const b = segEnd(s);
    if (s.kind === "line") {
      d += `L${num(b.x)} ${num(b.y)}`;
    } else {
      const sweep = s.a1 > s.a0 ? 1 : 0;
      const large = Math.abs(s.a1 - s.a0) > PI + 1e-9 ? 1 : 0;
      d += `A${num(s.r)} ${num(s.r)} 0 ${large} ${sweep} ${num(b.x)} ${num(b.y)}`;
    }
    cur = b;
  }
  return d;
}

/**
 * Round every interior corner of a polyline with a tangent arc.
 *
 * `radii[i]` is the radius wanted at vertex i; it is reduced where the
 * neighbouring legs are too short to hold it (a shared leg is split evenly).
 */
function fillet(points, radii) {
  const segs = [];
  let from = points[0];
  const last = points.length - 1;
  for (let i = 1; i < last; i++) {
    const A = points[i - 1];
    const B = points[i];
    const C = points[i + 1];
    const lenIn = Math.hypot(B.x - A.x, B.y - A.y);
    const lenOut = Math.hypot(C.x - B.x, C.y - B.y);
    const ui = pt((B.x - A.x) / lenIn, (B.y - A.y) / lenIn);
    const uo = pt((C.x - B.x) / lenOut, (C.y - B.y) / lenOut);
    const cross = ui.x * uo.y - ui.y * uo.x;
    const turn = Math.atan2(Math.abs(cross), ui.x * uo.x + ui.y * uo.y);
    if (radii[i] <= 0 || turn < 1e-6) {
      segs.push(line(from, B));
      from = B;
      continue;
    }
    const room = Math.min(lenIn * (i === 1 ? 1 : 0.5), lenOut * (i === last - 1 ? 1 : 0.5));
    const tan = Math.min(radii[i] * Math.tan(turn / 2), room);
    const r = tan / Math.tan(turn / 2);
    const p1 = pt(B.x - ui.x * tan, B.y - ui.y * tan);
    const side = cross > 0 ? 1 : -1;
    const c = pt(p1.x - ui.y * side * r, p1.y + ui.x * side * r);
    const a0 = Math.atan2(p1.y - c.y, p1.x - c.x);
    segs.push(line(from, p1), arc(c, r, a0, a0 + side * turn));
    from = pt(B.x + uo.x * tan, B.y + uo.y * tan);
  }
  segs.push(line(from, points[last]));
  return segs.filter((s) => segLength(s) > 1e-6);
}

/** Rounded-rectangle outline as SVG path data. */
function roundRect(x, y, w, h, r) {
  const a = `A${num(r)} ${num(r)} 0 0 1`;
  return (
    `M${num(x + r)} ${num(y)}H${num(x + w - r)}${a} ${num(x + w)} ${num(y + r)}` +
    `V${num(y + h - r)}${a} ${num(x + w - r)} ${num(y + h)}` +
    `H${num(x + r)}${a} ${num(x)} ${num(y + h - r)}` +
    `V${num(y + r)}${a} ${num(x + r)} ${num(y)}Z`
  );
}

// --- The loop ---------------------------------------------------------------

/**
 * Bifilar ("ślimak") UFH loop inside a rounded rectangle.
 *
 * Spiral A (supply) is a clockwise rectangular spiral entering at the
 * bottom-left: its side i is a left/top/right/bottom side (i % 4) inset by
 * floor(i / 2) * p. Spiral B (return) is A turned 180 degrees about the centre,
 * which slots it exactly between A's turns at pipe pitch p. B skips its two
 * outermost sides and drops straight out beside A's entry, so both ends leave
 * at the same corner. In the middle A and B meet in an S-bend whose straight
 * spine (length 2e) lets the pattern be taller than it is wide.
 *
 * Args:
 *   cx, cy: centre of the laid pattern.
 *   turns: full turns of each spiral before the S-bend (k).
 *   p: pipe pitch (centre-to-centre distance of neighbouring pipes).
 *   e: half-length of the S-bend's straight spine.
 *   cornerR: bend radius of the outermost turn; inner turns stay concentric.
 *   lead: how far both ends run on past the pattern (they vanish under the rim).
 *
 * Returns:
 *   { supply, middle, ret } as segment lists in flow direction: supply from
 *   its inlet to the S-bend's spine, the spine itself (where supply becomes
 *   return), and the return from there to its outlet.
 */
export function bifilarLoop({ cx, cy, turns, p, e, cornerR, lead }) {
  const k = turns;
  const x0 = -((4 * k + 2) * p) / 2;
  const x1 = -x0;
  const y0 = -((4 * k + 1) * p) / 2 - e;
  const y1 = -y0;
  const side = (i) => {
    const d = Math.floor(i / 2) * p;
    return [
      { axis: "x", v: x0 + d },
      { axis: "y", v: y0 + d },
      { axis: "x", v: x1 - d },
      { axis: "y", v: y1 - d },
    ][i % 4];
  };
  const corner = (i) => {
    const a = side(i);
    const b = side(i + 1);
    return a.axis === "x" ? pt(a.v, b.v) : pt(b.v, a.v);
  };
  const n = 4 * k + 1; // A's last side is a left side heading up
  const A = [pt(x0, y1 + lead)];
  for (let i = 0; i < n - 1; i++) A.push(corner(i));
  const spine = side(n - 1).v; // x of A's last side (= -p)
  A.push(pt(spine, -e));
  const turned = (q) => pt(-q.x, -q.y);
  const Bgeo = A.map(turned);
  const B = [pt(Bgeo[2].x, y1 + lead), ...Bgeo.slice(3)];
  // concentric bends: radius shrinks with the distance from the laid edge
  const bx1 = x1 - p;
  const by1 = y1 - p;
  const radii = (pts) =>
    pts.map((q, i) => {
      if (i === 0 || i === pts.length - 1) return 0;
      const inset = Math.min(q.x - x0, bx1 - q.x, q.y - y0, by1 - q.y);
      return Math.max(0.55 * p, cornerR - inset);
    });
  const rho = -spine / 2;
  // S-bend: over the top (now heading down), the straight spine where supply
  // turns into return, then under the bottom (heading up again).
  const supply = [...fillet(A, radii(A)), arc(pt(spine + rho, -e), rho, PI, 2 * PI)];
  const middle = [line(pt(0, -e), pt(0, e))];
  const ret = [arc(pt(-spine - rho, e), rho, PI, 0), ...reversePath(fillet(B, radii(B)))];
  // the laid pattern spans [x0, x1 - p] x [y0, y1 - p]; centre it on (cx, cy)
  const dx = cx + p / 2;
  const dy = cy + p / 2;
  const move = (s) =>
    s.kind === "line"
      ? line(pt(s.p0.x + dx, s.p0.y + dy), pt(s.p1.x + dx, s.p1.y + dy))
      : arc(pt(s.c.x + dx, s.c.y + dy), s.r, s.a0, s.a1);
  return { supply: supply.map(move), middle: middle.map(move), ret: ret.map(move) };
}

// --- The tortoise -----------------------------------------------------------

/**
 * Shell and loop proportions. `turns` picks the level of detail: 2 for the
 * logo and large sizes, 1 for small icons (fewer, bolder pipes, same outline).
 */
export const SHELL = {
  cx: 256,
  cy: 272,
  halfW: 118,
  halfH: 132,
  radius: 88,
  rim: 15,
  margin: 14,
  pipeRatio: 0.58, // pipe stroke width as a fraction of the pitch
  headHalfW: 44,
  headLen: 80,
};

/** Loop layout that fills the shell for a given number of turns. */
export function loopLayout(turns, shell = SHELL) {
  const { halfW, halfH, margin } = shell;
  const p = (2 * halfW - 2 * margin) / (4 * turns + 2);
  const e = (2 * halfH - 2 * margin - (4 * turns + 1) * p) / 2;
  return { turns, p, e, width: shell.pipeRatio * p };
}

/** A stubby leg with three claws, pointing along -y before `angle` (degrees). */
function leg(id, x, y, angle, len, C) {
  const h = 25; // half width
  const toes = [-13, 0, 13]
    .map(
      (dx, i) =>
        `<ellipse cx="${dx}" cy="${num(-len + 6 - (i === 1 ? 3 : 0))}" rx="4.6" ry="5.4" fill="${C.claw}"/>`,
    )
    .join("");
  return (
    `<g id="${id}" transform="translate(${num(x)} ${num(y)}) rotate(${angle})">` +
    `<path d="M${-h + 4} 14L${-h} ${num(-len + 30)}Q${-h - 2} ${num(-len - 2)} 0 ${num(-len - 2)}` +
    `Q${h + 2} ${num(-len - 2)} ${h} ${num(-len + 30)}L${h - 4} 14Z" fill="${C.skin}"/>${toes}</g>`
  );
}

/**
 * The mark as SVG markup (no <svg> wrapper), laid out on the 512 artboard.
 *
 * Args:
 *   turns: 2 (full detail) or 1 (compact, for small icons).
 *   idPrefix: prefix for element ids, so several marks can share a page.
 *   palette: colour overrides (keys of PALETTE).
 *   shell: proportion overrides (keys of SHELL).
 */
export function markBody({ turns = 2, idPrefix = "tu", palette = {}, shell = {} } = {}) {
  const C = { ...PALETTE, ...palette };
  const S = { ...SHELL, ...shell };
  const L = S.cx - S.halfW;
  const R = S.cx + S.halfW;
  const T = S.cy - S.halfH;
  const B = S.cy + S.halfH;
  const lay = loopLayout(turns, S);
  const loop = bifilarLoop({
    cx: S.cx,
    cy: S.cy,
    turns,
    p: lay.p,
    e: lay.e,
    cornerR: S.radius - S.margin - lay.p / 2,
    lead: S.margin + S.rim + lay.p,
  });
  const id = (s) => `${idPrefix}-${s}`;
  const rimPath = roundRect(
    L - S.rim / 2,
    T - S.rim / 2,
    2 * S.halfW + S.rim,
    2 * S.halfH + S.rim,
    S.radius + S.rim / 2,
  );
  // Butt caps: the loop's two open ends hide under the rim, and at the spine
  // the pieces meet edge to edge (the spine overlaps them by a hair).
  const pipe = (name, segs, colour) =>
    `<path id="${id(name)}" d="${toPathData(segs)}" fill="none" stroke="${colour}" ` +
    `stroke-width="${num(lay.width)}" stroke-linejoin="round"/>`;
  const top = segStart(loop.middle[0]);
  const bottom = segEnd(loop.middle[0]);
  const spine = [line(pt(top.x, top.y - 0.6), pt(bottom.x, bottom.y + 0.6))];
  const cooling =
    `<linearGradient id="${id("cooling")}" gradientUnits="userSpaceOnUse" ` +
    `x1="${num(top.x)}" y1="${num(top.y)}" x2="${num(bottom.x)}" y2="${num(bottom.y)}">` +
    `<stop offset="0" stop-color="${C.supply}"/><stop offset="1" stop-color="${C.ret}"/></linearGradient>`;
  const headTop = T - S.headLen;
  const hw = S.headHalfW;
  const eyes = [-1, 1]
    .map(
      (s) =>
        `<circle cx="${num(S.cx + s * (hw / 2))}" cy="${num(headTop + 34)}" r="8.5" fill="${C.eye}"/>` +
        `<circle cx="${num(S.cx + s * (hw / 2) + 2.6)}" cy="${num(headTop + 31)}" r="2.8" fill="#fff"/>`,
    )
    .join("");
  return [
    `<defs><clipPath id="${id("rim-clip")}"><path d="${rimPath}"/></clipPath>${cooling}</defs>`,
    `<g id="${id("legs")}">`,
    leg(id("leg-fl"), L + 18, T + 66, -44, 66, C),
    leg(id("leg-fr"), R - 18, T + 66, 66, 66, C),
    leg(id("leg-bl"), L + 20, B - 48, -134, 58, C),
    leg(id("leg-br"), R - 20, B - 48, 114, 58, C),
    `</g>`,
    `<path id="${id("tail")}" d="M${num(S.cx - 15)} ${num(B + 2)}Q${num(S.cx - 4)} ${num(B + 26)} ${num(S.cx + 4)} ${num(B + 42)}` +
      `Q${num(S.cx + 9)} ${num(B + 22)} ${num(S.cx + 15)} ${num(B + 2)}Z" fill="${C.skin}"/>`,
    `<g id="${id("head")}"><path d="M${num(S.cx - hw + 6)} ${num(T + 14)}C${num(S.cx - hw - 4)} ${num(T - 30)} ${num(S.cx - hw)} ${num(headTop)} ${num(S.cx)} ${num(headTop)}` +
      `C${num(S.cx + hw)} ${num(headTop)} ${num(S.cx + hw + 4)} ${num(T - 30)} ${num(S.cx + hw - 6)} ${num(T + 14)}Z" fill="${C.skin}"/>${eyes}</g>`,
    `<path id="${id("shell")}" d="${roundRect(L, T, 2 * S.halfW, 2 * S.halfH, S.radius)}" fill="${C.shell}"/>`,
    `<g id="${id("loop")}" clip-path="url(#${id("rim-clip")})">`,
    pipe("return", loop.ret, C.ret),
    pipe("supply", loop.supply, C.supply),
    pipe("turn", spine, `url(#${id("cooling")})`),
    `</g>`,
    `<path id="${id("rim")}" d="${rimPath}" fill="none" stroke="${C.rim}" stroke-width="${S.rim}"/>`,
  ].join("");
}

/**
 * The mark as a standalone SVG document.
 *
 * Args:
 *   viewBox: [x, y, w, h] crop of the 512 artboard (build.mjs measures a tight
 *     square one); defaults to the whole artboard.
 *   size: optional width/height attributes.
 */
export function markSvg({ viewBox = [0, 0, 512, 512], size, ...opts } = {}) {
  const dims = size ? ` width="${size}" height="${size}"` : "";
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${viewBox.map(num).join(" ")}"${dims}>` +
    `<title>Tortoise-UFH</title>${markBody(opts)}</svg>`
  );
}

// --- Lockup -----------------------------------------------------------------

/**
 * Mark above the wordmark and tagline, as one SVG document.
 *
 * Args:
 *   markBox: tight [x, y, w, h] box of the mark on its artboard.
 *   name: { parts: [{ d, role }], box } outlined wordmark; role is "name" or
 *     "accent" (the "ufh"), box its ink bounds.
 *   tagline: { d, box } outlined tagline, or null.
 *   theme: "light" or "dark" (wordmark colours).
 *   width: output width in user units.
 *   turns: detail level of the mark (see markBody).
 *
 * Returns:
 *   { svg, width, height }.
 */
export function lockupSvg({ markBox, name, tagline, theme = "light", width = 800, turns = 2 }) {
  const ink = INK[theme];
  const pad = 24;
  const markH = 430;
  const markScale = markH / markBox[3];
  const markW = markBox[2] * markScale;
  const nameW = name.box.maxX - name.box.minX;
  const nameScale = Math.min((width - 2 * pad) / nameW, 1.2);
  const nameH = (name.box.maxY - name.box.minY) * nameScale;
  const gap1 = 34;
  const gap2 = 26;
  let y = pad;
  const markX = (width - markW) / 2;
  const out = [];
  out.push(
    `<g transform="translate(${num(markX)} ${num(y)}) scale(${num(markScale, 4)}) translate(${num(-markBox[0])} ${num(-markBox[1])})">` +
      `${markBody({ turns, idPrefix: `tu-${theme}` })}</g>`,
  );
  y += markH + gap1;
  const nameX = (width - nameW * nameScale) / 2 - name.box.minX * nameScale;
  const nameY = y - name.box.minY * nameScale;
  out.push(
    `<g transform="translate(${num(nameX)} ${num(nameY)}) scale(${num(nameScale, 4)})">` +
      name.parts
        .map(
          (part) =>
            `<path d="${part.d}" fill="${part.role === "accent" ? ink.accent : ink.name}"/>`,
        )
        .join("") +
      "</g>",
  );
  y += nameH;
  if (tagline) {
    y += gap2;
    const tagW = tagline.box.maxX - tagline.box.minX;
    const tagX = (width - tagW) / 2 - tagline.box.minX;
    out.push(
      `<path transform="translate(${num(tagX)} ${num(y - tagline.box.minY)})" d="${tagline.d}" fill="${ink.tagline}"/>`,
    );
    y += tagline.box.maxY - tagline.box.minY;
  }
  const height = Math.ceil(y + pad);
  return {
    width,
    height,
    svg:
      `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}">` +
      `<title>Tortoise-UFH</title>${out.join("")}</svg>`,
  };
}
