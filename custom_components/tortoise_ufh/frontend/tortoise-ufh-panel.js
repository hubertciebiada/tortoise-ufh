/**
 * Tortoise-UFH sidebar panel — self-contained vanilla-JS ES module.
 *
 * A dependency-free custom element (no CDN / external imports / build step,
 * CSP-safe) that renders the per-room underfloor-heating control state as a
 * health hero + room-card grid + master/detail room inspector, and lets an
 * admin edit the global home temperature, mode, kill-switch and per-room
 * offset / live-control. It talks to the integration exclusively through the
 * `tortoise_ufh/*` websocket commands (get_config / get_live / set_*).
 *
 * The panel is registered with `embed_iframe: False`, so it lives in Home
 * Assistant's main document: HA theme CSS custom properties inherit across the
 * shadow boundary (all colours use `var(--ha-token, fallback)`), the native
 * entity dialog opens via the `hass-more-info` event, `<ha-icon>` is reachable
 * (feature-detected), and the font is inherited from HA.
 *
 * Units: temperatures in degrees Celsius; offsets in kelvin; valve position in
 * percent (0..100); trend in kelvin per hour.
 *
 * Rendering is incremental: the skeleton is built once, then every 5-second
 * `get_live` poll updates values in place keyed by `data-room` — never
 * rebuilding whole sections, so there is no flicker and no scroll jump. All
 * writable controls are buttons / steppers (no text fields or selects), so a
 * background poll can never clobber a value the admin is mid-typing. Polling is
 * paused while `document.hidden` and resumed (with an immediate refresh) on
 * `visibilitychange`. Every websocket call is wrapped in try/catch: a failure
 * shows a banner and is never allowed to throw out of a handler or the loop.
 * Every interpolated string is inserted via `textContent` / `setAttribute`
 * (never raw innerHTML), so room names, entity ids and states cannot inject.
 */

// --- Timing / limits --------------------------------------------------------

const POLL_INTERVAL_MS = 5000;
const AGE_TICK_MS = 1000;
const STALE_AGE_MS = 7 * 60 * 1000; // last cycle older than this reads as stale.

const HOME_MIN = 5.0;
const HOME_MAX = 30.0;
const HOME_STEP = 0.5;
const OFFSET_MIN = -5.0;
const OFFSET_MAX = 5.0;
const OFFSET_STEP = 0.5;

const MANAGE_ROOMS_PATH = "/config/integrations/integration/tortoise_ufh";

const MODES = ["heating", "transitional", "cooling", "off"];

/** Websocket command type strings (frozen backend contract). */
const WS = {
  getConfig: "tortoise_ufh/get_config",
  getLive: "tortoise_ufh/get_live",
  setHome: "tortoise_ufh/set_home_temperature",
  setOffset: "tortoise_ufh/set_room_offset",
  setEnabled: "tortoise_ufh/set_room_enabled",
  setMode: "tortoise_ufh/set_mode",
  setKill: "tortoise_ufh/set_kill_switch",
  // Core recorder commands (read-only history for charts). These are HA
  // built-ins, not part of the tortoise_ufh contract.
  history: "history/history_during_period",
  statistics: "recorder/statistics_during_period",
};

// --- Charts -----------------------------------------------------------------

/**
 * Time windows for the history charts.
 *
 * Short windows use per-sample recorder history (`history/history_during_period`,
 * timestamps in epoch SECONDS); the long window uses hourly long-term
 * statistics (`recorder/statistics_during_period`, timestamps in epoch
 * MILLISECONDS) so a week of data stays small.
 */
const WINDOWS = {
  "6h": { hours: 6, mode: "history" },
  "24h": { hours: 24, mode: "history" },
  "7d": { hours: 168, mode: "stats", period: "hour", periodMs: 3600 * 1000 },
};
const WINDOW_ORDER = ["6h", "24h", "7d"];
const SPARK_WINDOW = "24h";

/** Cached history is reused for this long before a background refetch. */
const HIST_MAXAGE_MS = 60 * 1000;

/** Detail-chart geometry (pixels; width is measured at draw time). */
const CHART_H = 240;
const CHART_MARGIN = { l: 42, r: 46, t: 12, b: 26 };
const CHART_MIN_W = 300;
const CHART_MAX_W = 900;

/** Sparkline geometry (fixed viewBox; scaled to card width, non-distorting stroke). */
const SPARK_VB_W = 240;
const SPARK_VB_H = 40;
const SPARK_PAD = 3;

// --- i18n -------------------------------------------------------------------

const STR = {
  pl: {
    status_running: "Działa · ostatni cykl {age}",
    status_stale: "Nieaktualne",
    status_error: "Błąd algorytmu",
    status_nodata: "Brak danych",
    age_now: "przed chwilą",
    age_min: "{n} min temu",
    age_hour: "{n} h temu",
    age_unknown: "brak znacznika czasu",
    rooms_live_cap: "Pokoje live",
    flags_cap: "Flagi",
    dew_cap: "Bezp. punkt rosy",
    home_cap: "Temperatura domu",
    mode_cap: "Tryb",
    mode_heating: "Grzanie",
    mode_transitional: "Przejściowy",
    mode_cooling: "Chłodzenie",
    mode_off: "Wył.",
    kill_on: "KILL-SWITCH: tylko obserwacja",
    kill_off: "Kill-switch: wył.",
    kill_hint_on: "Sterowanie wstrzymane — klik, aby wznowić",
    kill_hint_off: "Klik, aby zatrzymać wszystkie komendy",
    manage_rooms: "Zarządzaj pokojami",
    loading: "Ładowanie…",
    no_rooms: "Brak skonfigurowanych pokoi.",
    card_measured: "Pomiar",
    card_setpoint: "Zadana",
    card_offset: "korekta {v}",
    card_valve: "Zawór",
    live: "Live",
    shadow: "Obserwacja",
    fast_on: "wł.",
    fast_off: "źródło wył.",
    fmode_heating: "grzanie",
    fmode_cooling: "chłodzenie",
    fmode_off: "wył.",
    not_participating: "nie uczestniczy",
    detail_close: "Zamknij",
    sec_wiring: "Okablowanie",
    sec_decision: "Decyzja regulatora",
    sec_history: "Historia",
    sec_diagnostics: "Encje diagnostyczne",
    history_soon: "Ładowanie historii…",
    chart_temp: "Temperatura",
    chart_setpoint: "Zadana",
    chart_valve: "Zawór",
    chart_error: "Uchyb",
    chart_loading: "Ładowanie historii…",
    chart_nodata: "Brak danych historycznych",
    chart_unavailable: "Brak encji do wykresu",
    win_6h: "6 godz.",
    win_24h: "24 godz.",
    win_7d: "7 dni",
    role_entity_temp_room: "Temperatura pokoju",
    role_entity_humidity: "Wilgotność",
    role_entity_temp_outdoor: "Temperatura zewnętrzna",
    role_entity_valves: "Zawory",
    role_entity_supply: "Zasilanie",
    role_entity_return: "Powrót",
    role_entity_fast_source: "Źródło szybkie",
    wire_missing: "brak",
    wire_unset: "— nie przypisano —",
    wire_unavailable: "niedostępny",
    dec_error: "Błąd regulacji",
    dec_trend: "Trend temperatury",
    dec_dew: "Punkt rosy w pomieszczeniu",
    dec_terms: "Wkład członów w zawór",
    term_p: "P — proporcjonalny",
    term_i: "I — całkujący",
    term_trend: "Trend — tłumienie",
    term_ff: "FF — pogodowy",
    dec_raw_valve: "Zawór surowy (przed limitami)",
    dec_final_valve: "Zawór końcowy",
    dec_throttle: "Przepływ chłodzenia",
    dec_integrator: "Integrator",
    integ_frozen: "zamrożony",
    integ_active: "aktywny",
    dec_saturated: "Nasycenie zaworu",
    dec_floor: "Minimalne otwarcie (podłoga)",
    dec_fast: "Szybkie źródło",
    dec_explanation: "Wyjaśnienie",
    dec_flags: "Flagi",
    yes: "tak",
    no: "nie",
    raw_show: "Pokaż surowe dane",
    no_live_room: "Brak danych na żywo dla tego pokoju.",
  },
  en: {
    status_running: "Running · last cycle {age}",
    status_stale: "Stale",
    status_error: "Algorithm error",
    status_nodata: "No data",
    age_now: "just now",
    age_min: "{n} min ago",
    age_hour: "{n} h ago",
    age_unknown: "no timestamp",
    rooms_live_cap: "Rooms live",
    flags_cap: "Flags",
    dew_cap: "Safe dew point",
    home_cap: "Home temperature",
    mode_cap: "Mode",
    mode_heating: "Heating",
    mode_transitional: "Transitional",
    mode_cooling: "Cooling",
    mode_off: "Off",
    kill_on: "KILL-SWITCH: observe only",
    kill_off: "Kill-switch: off",
    kill_hint_on: "Control paused — click to resume",
    kill_hint_off: "Click to stop all commands",
    manage_rooms: "Manage rooms",
    loading: "Loading…",
    no_rooms: "No rooms configured.",
    card_measured: "Measured",
    card_setpoint: "Setpoint",
    card_offset: "offset {v}",
    card_valve: "Valve",
    live: "Live",
    shadow: "Shadow",
    fast_on: "on",
    fast_off: "source off",
    fmode_heating: "heat",
    fmode_cooling: "cool",
    fmode_off: "off",
    not_participating: "not participating",
    detail_close: "Close",
    sec_wiring: "Wiring",
    sec_decision: "Controller decision",
    sec_history: "History",
    sec_diagnostics: "Diagnostic entities",
    history_soon: "Loading history…",
    chart_temp: "Temperature",
    chart_setpoint: "Setpoint",
    chart_valve: "Valve",
    chart_error: "Error",
    chart_loading: "Loading history…",
    chart_nodata: "No history data",
    chart_unavailable: "No chartable entities",
    win_6h: "6 h",
    win_24h: "24 h",
    win_7d: "7 d",
    role_entity_temp_room: "Room temperature",
    role_entity_humidity: "Humidity",
    role_entity_temp_outdoor: "Outdoor temperature",
    role_entity_valves: "Valves",
    role_entity_supply: "Supply",
    role_entity_return: "Return",
    role_entity_fast_source: "Fast source",
    wire_missing: "missing",
    wire_unset: "— not set —",
    wire_unavailable: "unavailable",
    dec_error: "Control error",
    dec_trend: "Temperature trend",
    dec_dew: "Room dew point",
    dec_terms: "Term contributions to valve",
    term_p: "P — proportional",
    term_i: "I — integral",
    term_trend: "Trend — damping",
    term_ff: "FF — weather",
    dec_raw_valve: "Raw valve (pre-limits)",
    dec_final_valve: "Final valve",
    dec_throttle: "Cooling flow",
    dec_integrator: "Integrator",
    integ_frozen: "frozen",
    integ_active: "active",
    dec_saturated: "Valve saturation",
    dec_floor: "Valve floor (minimum)",
    dec_fast: "Fast source",
    dec_explanation: "Explanation",
    dec_flags: "Flags",
    yes: "yes",
    no: "no",
    raw_show: "Show raw data",
    no_live_room: "No live data for this room yet.",
  },
};

/**
 * Localised, severity-tagged labels for controller flag codes.
 *
 * Codes mirror the exact strings the pure core emits: the room-controller
 * flags (`sensor_lost`, `s2_condensation`, `fast_source_cannot_cool`,
 * `fast_source_min_runtime`, `cooling_disabled`, `unknown_room`,
 * `controller_error`) plus the safety-rule names merged into the report
 * (`s1_floor_overheat`, `s3_emergency_heat`, `s4_emergency_cool`,
 * `s5_watchdog`). `saturated` / `valve_floor` are synthetic labels for the
 * report booleans surfaced as chips in the decision view. Unknown codes fall
 * back to the raw string at `warn` severity. `sev` drives the room status dot.
 */
const FLAG_LABELS = {
  sensor_lost: { pl: "Utrata czujnika", en: "Sensor lost", sev: "problem" },
  s2_condensation: { pl: "Ryzyko kondensacji", en: "Condensation risk", sev: "problem" },
  s1_floor_overheat: { pl: "Przegrzanie podłogi", en: "Floor overheat", sev: "problem" },
  s3_emergency_heat: { pl: "Awaryjne grzanie", en: "Emergency heat", sev: "problem" },
  s4_emergency_cool: { pl: "Awaryjne chłodzenie", en: "Emergency cooling", sev: "problem" },
  s5_watchdog: { pl: "Watchdog", en: "Watchdog", sev: "problem" },
  unknown_room: { pl: "Brak konfiguracji", en: "Unknown room", sev: "problem" },
  controller_error: { pl: "Błąd regulatora", en: "Controller error", sev: "problem" },
  fast_source_cannot_cool: {
    pl: "Szybkie źródło nie chłodzi",
    en: "Fast source can't cool",
    sev: "warn",
  },
  fast_source_min_runtime: {
    pl: "Min. czas pracy źródła",
    en: "Fast source min runtime",
    sev: "warn",
  },
  cooling_disabled: { pl: "Chłodzenie wyłączone", en: "Cooling disabled", sev: "warn" },
  saturated: { pl: "Nasycenie zaworu", en: "Valve saturated", sev: "warn" },
  valve_floor: { pl: "Minimalne otwarcie", en: "Valve floor", sev: "warn" },
};

const SEV_RANK = { ok: 0, warn: 1, problem: 2 };

/** Ordered wiring roles (config `entities` dict keys → i18n label keys). */
const WIRING_ROLES = [
  "entity_temp_room",
  "entity_humidity",
  "entity_temp_outdoor",
  "entity_valves",
  "entity_supply",
  "entity_return",
  "entity_fast_source",
];

const UNAVAILABLE_STATES = new Set(["unavailable", "unknown", "none", ""]);

// --- Small pure helpers -----------------------------------------------------

/** Coerce a value to a finite number, or return null. */
function num(value) {
  const n = typeof value === "string" ? parseFloat(value) : value;
  return typeof n === "number" && Number.isFinite(n) ? n : null;
}

/** Format a number to `digits` decimals, or an em dash when null. */
function fmt(value, digits, suffix) {
  const n = num(value);
  if (n === null) {
    return "—";
  }
  return n.toFixed(digits) + (suffix || "");
}

/** Format a number with an explicit leading sign, or an em dash when null. */
function signed(value, digits, suffix) {
  const n = num(value);
  if (n === null) {
    return "—";
  }
  return (n > 0 ? "+" : "") + n.toFixed(digits) + (suffix || "");
}

/** Clamp `v` to the inclusive `[lo, hi]` range. */
function clamp(v, lo, hi) {
  return Math.min(hi, Math.max(lo, v));
}

/** Round `v` to the nearest multiple of `step`. */
function roundStep(v, step) {
  return Math.round(v / step) * step;
}

/** Return the first non-null/undefined key present in `obj`, else `fallback`. */
function pick(obj, keys, fallback) {
  if (obj && typeof obj === "object") {
    for (const key of keys) {
      if (obj[key] !== undefined && obj[key] !== null) {
        return obj[key];
      }
    }
  }
  return fallback;
}

/** Substitute `{name}` placeholders in `tpl` from `params`. */
function fmtStr(tpl, params) {
  return String(tpl).replace(/\{(\w+)\}/g, (m, k) =>
    params && k in params ? String(params[k]) : m,
  );
}

/** A directional trend glyph for a K/h rate (dead-band around zero). */
function trendArrow(rate) {
  const n = num(rate);
  if (n === null) {
    return "";
  }
  if (n > 0.05) {
    return "↑";
  }
  if (n < -0.05) {
    return "↓";
  }
  return "→";
}

/**
 * Minimal, safe DOM builder.
 *
 * Text is set via `textContent` and attributes via `setAttribute`, so no
 * caller value is ever parsed as HTML. Supported attr keys: `class`, `text`,
 * `title`, `type`, `tabindex`, arbitrary attributes; `dataset` (object),
 * `style` (string) and `on` (event→handler object).
 */
function h(tag, attrs, children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) {
        continue;
      }
      if (k === "class") {
        el.className = v;
      } else if (k === "text") {
        el.textContent = v;
      } else if (k === "style") {
        el.setAttribute("style", v);
      } else if (k === "dataset") {
        for (const [dk, dv] of Object.entries(v)) {
          el.dataset[dk] = dv;
        }
      } else if (k === "on") {
        for (const [ev, fn] of Object.entries(v)) {
          el.addEventListener(ev, fn);
        }
      } else {
        el.setAttribute(k, v === true ? "" : String(v));
      }
    }
  }
  if (children != null) {
    const kids = Array.isArray(children) ? children : [children];
    for (const c of kids) {
      if (c == null || c === false) {
        continue;
      }
      el.appendChild(typeof c === "object" ? c : document.createTextNode(String(c)));
    }
  }
  return el;
}

// --- SVG / chart helpers (all pure, CSP-safe: no innerHTML, no libraries) ----

const SVGNS = "http://www.w3.org/2000/svg";

/** SVG element builder — mirror of `h`, using createElementNS. */
function s(tag, attrs, children) {
  const el = document.createElementNS(SVGNS, tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) {
        continue;
      }
      if (k === "text") {
        el.textContent = v;
      } else {
        el.setAttribute(k, v === true ? "" : String(v));
      }
    }
  }
  if (children != null) {
    const kids = Array.isArray(children) ? children : [children];
    for (const c of kids) {
      if (c == null || c === false) {
        continue;
      }
      el.appendChild(typeof c === "object" ? c : document.createTextNode(String(c)));
    }
  }
  return el;
}

/** Parse a Home Assistant state string to a finite number, or null (gap). */
function parseState(raw) {
  if (raw == null || UNAVAILABLE_STATES.has(String(raw))) {
    return null;
  }
  const n = parseFloat(raw);
  return Number.isFinite(n) ? n : null;
}

/**
 * Split a time series into runs of consecutive real points.
 *
 * A null / non-finite value is a GAP: it flushes the current run so the line
 * breaks there and is never interpolated across (matches HA `unavailable` /
 * `unknown` handling).
 */
function segments(points) {
  const segs = [];
  let cur = [];
  for (const p of points) {
    if (p.v == null || !Number.isFinite(p.v)) {
      if (cur.length) {
        segs.push(cur);
        cur = [];
      }
    } else {
      cur.push(p);
    }
  }
  if (cur.length) {
    segs.push(cur);
  }
  return segs;
}

/**
 * Linear value of a series at time `t`, matching the drawn polyline exactly.
 *
 * Interpolates only between two adjacent real samples (the same pairs the line
 * connects); returns null before the first sample, after the last, or across a
 * gap — so a tooltip never invents a value the chart does not draw.
 */
function interpAt(points, t) {
  if (!points || points.length === 0) {
    return null;
  }
  let prev = null;
  for (const p of points) {
    const v = p.v == null || !Number.isFinite(p.v) ? null : p.v;
    if (p.t === t && v != null) {
      return v;
    }
    if (prev && prev.t <= t && t <= p.t) {
      if (prev.v == null || v == null) {
        return null; // gap on one side
      }
      const span = p.t - prev.t;
      if (span <= 0) {
        return v;
      }
      return prev.v + ((v - prev.v) * (t - prev.t)) / span;
    }
    prev = { t: p.t, v };
  }
  return null;
}

/** "Nice" round number for axis steps (Heckbert). */
function niceNum(range, round) {
  const exp = Math.floor(Math.log10(range || 1));
  const frac = (range || 1) / Math.pow(10, exp);
  let nf;
  if (round) {
    nf = frac < 1.5 ? 1 : frac < 3 ? 2 : frac < 7 ? 5 : 10;
  } else {
    nf = frac <= 1 ? 1 : frac <= 2 ? 2 : frac <= 5 ? 5 : 10;
  }
  return nf * Math.pow(10, exp);
}

/** Evenly-rounded axis ticks spanning [min, max] (~`count` of them). */
function niceTicks(min, max, count) {
  if (!(max > min)) {
    return [min];
  }
  const range = niceNum(max - min, false);
  const step = niceNum(range / Math.max(1, count - 1), true);
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = lo; v <= hi + step * 0.5; v += step) {
    ticks.push(Number(v.toFixed(6)));
  }
  return ticks;
}

// --- Custom element ---------------------------------------------------------

class TortoiseUfhPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._config = null; // last get_config payload
    this._live = null; // last get_live payload
    this._selectedRoom = null;
    this._error = "";
    this._lang = "en";

    this._built = false;
    this._builtLang = null;
    this._els = null; // stable skeleton element refs
    this._hero = null; // hero part refs
    this._cards = new Map(); // room name -> card element
    this._detail = null; // detail part refs
    this._detailRoom = null; // room name the detail is currently built for

    this._view = []; // normalized room view-models
    this._viewByName = new Map();
    this._lastUpdateMs = null; // parsed last_update_timestamp (epoch ms)

    this._pollTimer = null;
    this._ageTimer = null;
    this._onVisibility = () => this._handleVisibility();
    this._hasHaIcon = false;

    // History-chart data cache, keyed by `${entityId}|${window}`.
    this._histCache = new Map(); // key -> { at: epochMs, data: [{t, v}] }
    this._histInflight = new Map(); // key -> Promise<{at, data}> (dedup in-flight)
    this._chartRO = null; // ResizeObserver redrawing the detail chart on resize
    this._chartWindow = "24h"; // persisted across room switches
    this._chartVisible = { temp: true, setpoint: true, valve: true };

    // If a host set `hass` before this element's definition was registered,
    // the own property shadows the setter; re-apply it through the setter.
    this._upgradeProperty("hass");
  }

  /** Canonical custom-element property-upgrade guard (see MDN). */
  _upgradeProperty(prop) {
    if (Object.prototype.hasOwnProperty.call(this, prop)) {
      const value = this[prop];
      delete this[prop];
      this[prop] = value;
    }
  }

  // -- Lifecycle -----------------------------------------------------------

  /** HA assigns this on every state change; keep the latest handle. */
  set hass(hass) {
    const first = this._hass === null;
    this._hass = hass;
    this._lang = this._resolveLang(hass);
    if (first) {
      this._loadConfig();
      this._poll();
    } else if (this._selectedRoom) {
      // Cheap: keep the wiring section's live entity states current without a
      // full re-render (hass.states changes far more often than we poll).
      this._refreshWiringStates();
    }
  }

  get hass() {
    return this._hass;
  }

  connectedCallback() {
    this._hasHaIcon = !!(window.customElements && customElements.get("ha-icon"));
    if (!this._hasHaIcon && window.customElements && customElements.whenDefined) {
      customElements.whenDefined("ha-icon").then(
        () => {
          this._hasHaIcon = true;
          // Force the detail to rebuild so icon affordances swap in.
          this._detailRoom = null;
          this._render();
        },
        () => {},
      );
    }
    document.addEventListener("visibilitychange", this._onVisibility);
    if (!document.hidden) {
      this._startTimers();
    }
    if (this._hass) {
      this._loadConfig();
      this._poll();
    } else {
      this._render();
    }
  }

  disconnectedCallback() {
    this._stopTimers();
    this._disconnectChartRO();
    document.removeEventListener("visibilitychange", this._onVisibility);
  }

  _disconnectChartRO() {
    if (this._chartRO) {
      this._chartRO.disconnect();
      this._chartRO = null;
    }
  }

  _resolveLang(hass) {
    const raw = (hass && (hass.language || hass.selectedLanguage)) || "en";
    return String(raw).toLowerCase().startsWith("pl") ? "pl" : "en";
  }

  _handleVisibility() {
    if (document.hidden) {
      this._stopTimers();
    } else {
      this._poll();
      this._startTimers();
    }
  }

  _startTimers() {
    if (this._pollTimer === null) {
      this._pollTimer = window.setInterval(() => this._poll(), POLL_INTERVAL_MS);
    }
    if (this._ageTimer === null) {
      this._ageTimer = window.setInterval(() => this._tickAge(), AGE_TICK_MS);
    }
  }

  _stopTimers() {
    if (this._pollTimer !== null) {
      window.clearInterval(this._pollTimer);
      this._pollTimer = null;
    }
    if (this._ageTimer !== null) {
      window.clearInterval(this._ageTimer);
      this._ageTimer = null;
    }
  }

  // -- i18n ----------------------------------------------------------------

  _t(key) {
    const dict = STR[this._lang] || STR.en;
    if (key in dict) {
      return dict[key];
    }
    return key in STR.en ? STR.en[key] : key;
  }

  _flagLabel(code) {
    const meta = FLAG_LABELS[code];
    return meta ? meta[this._lang] || meta.en : code;
  }

  _flagSev(code) {
    const meta = FLAG_LABELS[code];
    return meta ? meta.sev : "warn";
  }

  _modeLabel(mode) {
    const key = "mode_" + mode;
    return key in STR.en ? this._t(key) : mode;
  }

  // -- Websocket -----------------------------------------------------------

  async _callWS(message) {
    if (!this._hass || typeof this._hass.callWS !== "function") {
      return null;
    }
    try {
      const result = await this._hass.callWS(message);
      this._setError("");
      return result;
    } catch (err) {
      const detail = err && err.message ? err.message : String(err);
      this._setError(`${message.type}: ${detail}`);
      return null;
    }
  }

  async _loadConfig() {
    const cfg = await this._callWS({ type: WS.getConfig });
    if (cfg) {
      this._config = cfg;
      this._render();
    }
  }

  async _poll() {
    const live = await this._callWS({ type: WS.getLive });
    if (live) {
      this._live = live;
      const ts = live.last_update_timestamp
        ? Date.parse(live.last_update_timestamp)
        : NaN;
      this._lastUpdateMs = Number.isFinite(ts) ? ts : null;
      this._render();
    }
  }

  // -- History fetch (recorder; cached per entity+window, silent on error) --

  /** Call a websocket command without touching the error banner (charts fail soft). */
  async _callWSSilent(message) {
    if (!this._hass || typeof this._hass.callWS !== "function") {
      return null;
    }
    try {
      return await this._hass.callWS(message);
    } catch (err) {
      return null;
    }
  }

  /**
   * Resolve a time series for one entity + window, cached and de-duplicated.
   *
   * Returns `{ at, data }` where `data` is `[{t: epochMs, v: number|null}]`
   * sorted ascending and `v` is null for unavailable/unknown samples (a gap).
   * Fresh cache hits skip the network entirely, so the 5 s live poll never
   * refetches history; concurrent requests for the same key share one call.
   */
  _series(entityId, windowKey) {
    const key = entityId + "|" + windowKey;
    const now = Date.now();
    const cached = this._histCache.get(key);
    if (cached && now - cached.at < HIST_MAXAGE_MS) {
      return Promise.resolve(cached);
    }
    const inflight = this._histInflight.get(key);
    if (inflight) {
      return inflight;
    }
    const p = this._fetchOne(entityId, windowKey)
      .then((data) => {
        const entry = { at: Date.now(), data };
        this._histCache.set(key, entry);
        this._histInflight.delete(key);
        return entry;
      })
      .catch(() => {
        this._histInflight.delete(key);
        const entry = { at: Date.now(), data: [] };
        this._histCache.set(key, entry);
        return entry;
      });
    this._histInflight.set(key, p);
    return p;
  }

  /** One recorder round-trip → normalized `[{t, v}]` (see `_series`). */
  async _fetchOne(entityId, windowKey) {
    const w = WINDOWS[windowKey] || WINDOWS[SPARK_WINDOW];
    const end = new Date();
    const start = new Date(end.getTime() - w.hours * 3600 * 1000);
    if (w.mode === "stats") {
      const res = await this._callWSSilent({
        type: WS.statistics,
        start_time: start.toISOString(),
        end_time: end.toISOString(),
        statistic_ids: [entityId],
        period: w.period,
        types: ["mean"],
      });
      const arr = (res && res[entityId]) || [];
      // Statistics `start` is epoch MILLISECONDS already — use it directly.
      const rows = arr
        .map((row) => ({
          t: num(row.start),
          v: row.mean == null ? null : num(row.mean),
        }))
        .filter((pt) => pt.t !== null)
        .sort((a, b) => a.t - b.t);
      // The recorder OMITS empty buckets (an outage yields no row at all, not
      // a `mean: null` row), so a multi-hour gap arrives as two adjacent rows.
      // Insert an explicit null between rows spaced more than ~1.5 periods so
      // `segments()` breaks the line instead of interpolating across the gap —
      // mirroring the `unavailable` breaks history mode gets for free.
      const periodMs = w.periodMs || 3600 * 1000;
      const out = [];
      let prevT = null;
      for (const pt of rows) {
        if (prevT !== null && pt.t - prevT > periodMs * 1.5) {
          out.push({ t: prevT + periodMs, v: null });
        }
        out.push(pt);
        prevT = pt.t;
      }
      return out;
    }
    const res = await this._callWSSilent({
      type: WS.history,
      start_time: start.toISOString(),
      end_time: end.toISOString(),
      entity_ids: [entityId],
      minimal_response: true,
      no_attributes: true,
      significant_changes_only: false,
    });
    const arr = (res && res[entityId]) || [];
    // History `lu` (last_updated) is epoch SECONDS → convert to milliseconds.
    return arr
      .map((pt) => ({
        t: num(pt.lu) === null ? null : num(pt.lu) * 1000,
        v: parseState(pt.s),
      }))
      .filter((pt) => pt.t !== null)
      .sort((a, b) => a.t - b.t);
  }

  // -- Write commands (optimistic local patch, then re-sync) ---------------

  _patchConfig(mutator) {
    if (!this._config) {
      return;
    }
    const next = { ...this._config };
    if (Array.isArray(next.rooms)) {
      next.rooms = next.rooms.map((r) => ({ ...r }));
    }
    mutator(next);
    this._config = next;
    this._render();
  }

  async _nudgeHome(delta) {
    const cur = num(this._config && this._config.home_setpoint_c);
    if (cur === null) {
      return;
    }
    const next = clamp(roundStep(cur + delta, HOME_STEP), HOME_MIN, HOME_MAX);
    if (next === cur) {
      return;
    }
    this._patchConfig((c) => {
      c.home_setpoint_c = next;
    });
    await this._callWS({ type: WS.setHome, temperature: next });
    this._loadConfig();
    this._poll();
  }

  async _nudgeOffset(room, delta) {
    const view = this._viewByName.get(room);
    const cur = view ? num(view.offset) : null;
    if (cur === null) {
      return;
    }
    const next = clamp(roundStep(cur + delta, OFFSET_STEP), OFFSET_MIN, OFFSET_MAX);
    if (next === cur) {
      return;
    }
    this._patchConfig((c) => {
      const r = (c.rooms || []).find((x) => x.name === room);
      if (r) {
        r.offset_c = next;
      }
    });
    await this._callWS({ type: WS.setOffset, room, offset: next });
    this._loadConfig();
    this._poll();
  }

  async _setMode(mode) {
    if (!MODES.includes(mode)) {
      return;
    }
    // Optimistically patch both payloads: `live.mode` (polled, authoritative
    // for the active-mode display + dew chip) and `config.mode`.
    if (this._live) {
      this._live = { ...this._live, mode };
    }
    if (this._config) {
      this._config = { ...this._config, mode };
    }
    this._render();
    await this._callWS({ type: WS.setMode, mode });
    this._loadConfig();
    this._poll();
  }

  async _toggleKill() {
    const engaged = !(this._config && this._config.kill_switch);
    this._patchConfig((c) => {
      c.kill_switch = engaged;
    });
    await this._callWS({ type: WS.setKill, engaged });
    this._loadConfig();
    this._poll();
  }

  async _toggleLive(room) {
    const view = this._viewByName.get(room);
    const enabled = !(view && view.live);
    this._patchConfig((c) => {
      const r = (c.rooms || []).find((x) => x.name === room);
      if (r) {
        r.live_control = enabled;
      }
    });
    await this._callWS({ type: WS.setEnabled, room, enabled });
    this._loadConfig();
    this._poll();
  }

  _manageRooms() {
    try {
      window.history.pushState(null, "", MANAGE_ROOMS_PATH);
      window.dispatchEvent(
        new CustomEvent("location-changed", {
          detail: { replace: false },
          bubbles: true,
          composed: true,
        }),
      );
    } catch (err) {
      this._setError(String((err && err.message) || err));
    }
  }

  _moreInfo(entityId) {
    if (!entityId) {
      return;
    }
    this.dispatchEvent(
      new CustomEvent("hass-more-info", {
        detail: { entityId },
        bubbles: true,
        composed: true,
      }),
    );
  }

  // -- Data shaping --------------------------------------------------------

  _severity(flags) {
    let rank = 0;
    for (const f of flags) {
      rank = Math.max(rank, SEV_RANK[this._flagSev(f)] || 0);
    }
    return rank >= 2 ? "problem" : rank === 1 ? "warn" : "ok";
  }

  /** Merge config + live into a sorted list of normalized room view-models. */
  _computeView() {
    const cfg = this._config || {};
    const cfgRooms = Array.isArray(cfg.rooms) ? cfg.rooms : [];
    const live = this._live || {};
    const liveRooms =
      live.rooms && typeof live.rooms === "object" ? live.rooms : {};
    const homeTemp = num(cfg.home_setpoint_c);

    const cfgByName = new Map();
    for (const r of cfgRooms) {
      if (r && r.name) {
        cfgByName.set(r.name, r);
      }
    }
    const names = new Set([...cfgByName.keys(), ...Object.keys(liveRooms)]);

    const rows = [];
    for (const name of names) {
      if (!name) {
        continue;
      }
      const c = cfgByName.get(name) || {};
      const lv = liveRooms[name] || {};
      const report = lv.report && typeof lv.report === "object" ? lv.report : {};
      const fast =
        lv.fast_source && typeof lv.fast_source === "object" ? lv.fast_source : {};

      const offsetRaw = num(pick(c, ["offset_c"], null));
      const offset = offsetRaw === null ? 0 : offsetRaw;
      const setpoint = num(
        pick(
          lv,
          ["setpoint_c"],
          homeTemp !== null ? homeTemp + offset : null,
        ),
      );
      const errorC = num(report.error_c);
      // room_temp = setpoint - error (heating sign convention, all modes).
      const current =
        setpoint !== null && errorC !== null ? setpoint - errorC : null;
      const flags = Array.isArray(report.flags) ? report.flags.slice() : [];

      rows.push({
        name,
        offset,
        setpoint,
        current,
        errorC,
        trend: num(report.trend_c_per_h),
        valve: num(lv.valve_position_pct),
        rawValve: num(report.raw_valve_pct),
        dew: num(report.room_dew_point_c),
        throttle: num(report.dew_throttle_factor),
        pTerm: num(report.p_term),
        iTerm: num(report.i_term),
        trendTerm: num(report.trend_term),
        ffTerm: num(report.feedforward_term),
        saturated: !!report.saturated,
        valveFloor: !!report.valve_floor_applied,
        integratorFrozen: !!report.integrator_frozen,
        fastOn: !!fast.on,
        fastMode: pick(fast, ["mode"], "off"),
        fastTarget: num(fast.target_temperature_c),
        live: !!pick(lv, ["live_control_enabled"], pick(c, ["live_control"], false)),
        cooling: !!pick(c, ["cooling_enabled"], false),
        participates: pick(c, ["participates"], true) !== false,
        flags,
        explanation: pick(report, ["explanation"], "") || "",
        severity: this._severity(flags),
        entities: c.entities && typeof c.entities === "object" ? c.entities : {},
        diagnosticEntities:
          c.diagnostic_entities && typeof c.diagnostic_entities === "object"
            ? c.diagnostic_entities
            : null,
        report,
        hasLive: name in liveRooms,
      });
    }
    rows.sort((a, b) => a.name.localeCompare(b.name));

    this._view = rows;
    this._viewByName = new Map(rows.map((r) => [r.name, r]));
    return rows;
  }

  // -- Rendering: skeleton -------------------------------------------------

  _setError(msg) {
    if (msg === this._error) {
      return;
    }
    this._error = msg;
    if (this._els && this._els.banner) {
      this._els.banner.textContent = msg;
      this._els.banner.style.display = msg ? "" : "none";
    }
  }

  _icon(name, fallback) {
    if (this._hasHaIcon) {
      return h("ha-icon", { class: "hicon", icon: name });
    }
    return h("span", { class: "hicon-fallback", text: fallback || "›" });
  }

  _ensureBuilt() {
    if (this._built && this._builtLang === this._lang) {
      return;
    }
    if (!this._built) {
      const style = h("style");
      style.textContent = STYLE;
      this.shadowRoot.appendChild(style);
    } else if (this._els && this._els.wrap) {
      // Language changed at runtime: drop content, keep the <style>.
      this._els.wrap.remove();
      this._cards = new Map();
      this._detail = null;
      this._detailRoom = null;
    }
    this._hasHaIcon = !!(window.customElements && customElements.get("ha-icon"));
    this._builtLang = this._lang;
    this._built = true;
    this._buildSkeleton();
  }

  _buildSkeleton() {
    const banner = h("div", { class: "banner", style: "display:none" });
    const hero = this._buildHero();
    const grid = h("div", { class: "grid" });
    const empty = h("div", { class: "empty", text: this._t("loading") });
    const main = h("div", { class: "col-main" }, [empty, grid]);
    const detail = h("section", { class: "detail", style: "display:none" });
    const layout = h("div", { class: "layout" }, [main, detail]);
    const wrap = h("div", { class: "wrap" }, [banner, hero, layout]);

    this._els = { wrap, banner, grid, empty, detail, layout };
    this.shadowRoot.appendChild(wrap);
  }

  _buildHero() {
    const H = {};

    // Brand + manage-rooms deep link.
    const brand = h("div", { class: "brand" }, [
      this._icon("mdi:tortoise", "🐢"),
      h("span", { text: "Tortoise-UFH" }),
    ]);
    H.manageBtn = h(
      "button",
      { class: "ghost-btn", type: "button", on: { click: () => this._manageRooms() } },
      [this._icon("mdi:cog-outline", "⚙"), h("span", { text: this._t("manage_rooms") })],
    );
    const brandRow = h("div", { class: "hero-row brand-row" }, [
      brand,
      h("span", { class: "spacer" }),
      H.manageBtn,
    ]);

    // Status pill + metrics.
    H.pillDot = h("span", { class: "pill-dot" });
    H.pillText = h("span", { class: "pill-text" });
    H.pill = h("div", { class: "pill" }, [H.pillDot, H.pillText]);

    H.liveVal = h("span", { class: "metric-val" });
    const liveMetric = h("div", { class: "metric" }, [
      H.liveVal,
      h("span", { class: "metric-cap", text: this._t("rooms_live_cap") }),
    ]);

    H.flagsVal = h("span", { class: "metric-val" });
    H.flagsMetric = h("div", { class: "metric" }, [
      H.flagsVal,
      h("span", { class: "metric-cap", text: this._t("flags_cap") }),
    ]);

    H.dewVal = h("span", { class: "metric-val" });
    H.dewChip = h("div", { class: "chip chip-dew", style: "display:none" }, [
      h("span", { class: "chip-cap", text: this._t("dew_cap") }),
      H.dewVal,
    ]);

    const statusRow = h("div", { class: "hero-row status-row" }, [
      H.pill,
      liveMetric,
      H.flagsMetric,
      H.dewChip,
    ]);

    // Controls: home stepper, mode segmented, kill switch.
    H.homeMinus = h("button", {
      class: "step-btn",
      type: "button",
      text: "−",
      on: { click: () => this._nudgeHome(-HOME_STEP) },
    });
    H.homeVal = h("span", { class: "step-val" });
    H.homePlus = h("button", {
      class: "step-btn",
      type: "button",
      text: "+",
      on: { click: () => this._nudgeHome(HOME_STEP) },
    });
    const homeCtl = h("div", { class: "ctl" }, [
      h("span", { class: "ctl-cap", text: this._t("home_cap") }),
      h("div", { class: "stepper" }, [H.homeMinus, H.homeVal, H.homePlus]),
    ]);

    H.modeBtns = {};
    const seg = h(
      "div",
      { class: "seg", role: "group" },
      MODES.map((m) => {
        const b = h("button", {
          class: "seg-btn",
          type: "button",
          text: this._modeLabel(m),
          dataset: { mode: m },
          on: { click: () => this._setMode(m) },
        });
        H.modeBtns[m] = b;
        return b;
      }),
    );
    const modeCtl = h("div", { class: "ctl" }, [
      h("span", { class: "ctl-cap", text: this._t("mode_cap") }),
      seg,
    ]);

    H.killBtn = h("button", {
      class: "kill-btn",
      type: "button",
      on: { click: () => this._toggleKill() },
    });
    const killCtl = h("div", { class: "ctl ctl-kill" }, [
      h("span", { class: "ctl-cap", text: " " }),
      H.killBtn,
    ]);

    const ctlRow = h("div", { class: "hero-row ctl-row" }, [homeCtl, modeCtl, killCtl]);

    this._hero = H;
    return h("header", { class: "hero" }, [brandRow, statusRow, ctlRow]);
  }

  // -- Rendering: orchestration -------------------------------------------

  _render() {
    this._ensureBuilt();
    this._computeView();

    if (this._els.banner) {
      this._els.banner.textContent = this._error;
      this._els.banner.style.display = this._error ? "" : "none";
    }

    this._updateHero();
    this._reconcileCards();
    this._syncDetail();
  }

  // -- Rendering: hero -----------------------------------------------------

  _ageMs() {
    if (this._lastUpdateMs === null) {
      return null;
    }
    return Math.max(0, Date.now() - this._lastUpdateMs);
  }

  _ageText(ms) {
    const minutes = Math.floor(ms / 60000);
    if (minutes < 1) {
      return this._t("age_now");
    }
    if (minutes < 60) {
      return fmtStr(this._t("age_min"), { n: minutes });
    }
    return fmtStr(this._t("age_hour"), { n: Math.floor(minutes / 60) });
  }

  _pillState() {
    const live = this._live;
    if (!live) {
      return { sev: "problem", text: this._t("status_nodata") };
    }
    if (live.algorithm_status === "error") {
      return { sev: "problem", text: this._t("status_error") };
    }
    const ageMs = this._ageMs();
    const stale =
      live.algorithm_status === "stale" ||
      live.watchdog_state === "stale" ||
      (ageMs !== null && ageMs > STALE_AGE_MS);
    if (stale) {
      const suffix = ageMs !== null ? " · " + this._ageText(ageMs) : "";
      return { sev: "warn", text: this._t("status_stale") + suffix };
    }
    const age = ageMs !== null ? this._ageText(ageMs) : this._t("age_unknown");
    return { sev: "ok", text: fmtStr(this._t("status_running"), { age }) };
  }

  _applyPill() {
    if (!this._hero) {
      return;
    }
    const { sev, text } = this._pillState();
    this._hero.pill.className = "pill pill-" + sev;
    this._hero.pillText.textContent = text;
  }

  /** 1-second tick: refresh only the age-dependent pill (cheap, no reflow). */
  _tickAge() {
    if (this._built) {
      this._applyPill();
    }
  }

  _updateHero() {
    const H = this._hero;
    const cfg = this._config || {};
    const live = this._live || {};

    this._applyPill();

    const total = this._view.length;
    const liveN = this._view.filter((r) => r.live).length;
    H.liveVal.textContent = total ? `${liveN}/${total}` : "—";

    const flagN = this._view.reduce((s, r) => s + r.flags.length, 0);
    H.flagsVal.textContent = String(flagN);
    H.flagsMetric.classList.toggle("has", flagN > 0);

    // Prefer live.mode: it is refreshed on every poll, whereas config.mode is
    // only re-fetched on writes / initial load (so it can lag an external
    // mode change made through the HA mode entity).
    const mode = pick(live, ["mode"], pick(cfg, ["mode"], "heating"));
    for (const m of MODES) {
      H.modeBtns[m].classList.toggle("active", m === mode);
    }

    // Safe dew point is only meaningful while cooling.
    const dew = num(live.global_safe_dew_point_c);
    if (mode === "cooling") {
      H.dewChip.style.display = "";
      H.dewVal.textContent = fmt(dew, 1, " °C");
    } else {
      H.dewChip.style.display = "none";
    }

    const homeTemp = num(cfg.home_setpoint_c);
    H.homeVal.textContent = fmt(homeTemp, 1, " °C");
    H.homeMinus.disabled = homeTemp === null || homeTemp <= HOME_MIN;
    H.homePlus.disabled = homeTemp === null || homeTemp >= HOME_MAX;

    const kill = !!pick(cfg, ["kill_switch"], pick(live, ["kill_switch"], false));
    H.killBtn.textContent = kill ? this._t("kill_on") : this._t("kill_off");
    H.killBtn.title = kill ? this._t("kill_hint_on") : this._t("kill_hint_off");
    H.killBtn.classList.toggle("engaged", kill);
    this._els.wrap.classList.toggle("killed", kill);
  }

  // -- Rendering: room cards ----------------------------------------------

  _reconcileCards() {
    const grid = this._els.grid;
    const rows = this._view;

    if (rows.length === 0) {
      for (const [, el] of this._cards) {
        el.remove();
      }
      this._cards.clear();
      const loading = !this._config && !this._live;
      this._els.empty.textContent = loading ? this._t("loading") : this._t("no_rooms");
      this._els.empty.style.display = "";
      return;
    }
    this._els.empty.style.display = "none";

    const desired = new Set(rows.map((r) => r.name));
    for (const [name, el] of [...this._cards]) {
      if (!desired.has(name)) {
        el.remove();
        this._cards.delete(name);
      }
    }

    rows.forEach((r, i) => {
      let card = this._cards.get(r.name);
      if (!card) {
        card = this._buildCard(r.name);
        this._cards.set(r.name, card);
      }
      const at = grid.children[i];
      if (at !== card) {
        grid.insertBefore(card, at || null);
      }
      this._updateCard(card, r);
    });
  }

  _buildCard(name) {
    const p = {};
    p.dot = h("span", { class: "dot" });
    p.name = h("span", { class: "card-name", text: name });
    p.live = h("button", {
      class: "badge live-badge",
      type: "button",
      on: {
        click: (e) => {
          e.stopPropagation();
          this._toggleLive(name);
        },
      },
    });
    const head = h("div", { class: "card-head" }, [
      p.dot,
      p.name,
      h("span", { class: "spacer" }),
      p.live,
    ]);

    p.temp = h("span", { class: "big-val" });
    p.trend = h("span", { class: "trend" });
    const tempCell = h("div", { class: "cell" }, [
      h("span", { class: "cell-cap", text: this._t("card_measured") }),
      h("div", { class: "big-row" }, [p.temp, p.trend]),
    ]);

    p.spMinus = h("button", {
      class: "step-btn sm",
      type: "button",
      text: "−",
      on: {
        click: (e) => {
          e.stopPropagation();
          this._nudgeOffset(name, -OFFSET_STEP);
        },
      },
    });
    p.spVal = h("span", { class: "step-val" });
    p.spPlus = h("button", {
      class: "step-btn sm",
      type: "button",
      text: "+",
      on: {
        click: (e) => {
          e.stopPropagation();
          this._nudgeOffset(name, OFFSET_STEP);
        },
      },
    });
    p.spOffset = h("span", { class: "cell-sub" });
    const spCell = h("div", { class: "cell" }, [
      h("span", { class: "cell-cap", text: this._t("card_setpoint") }),
      h("div", { class: "stepper" }, [p.spMinus, p.spVal, p.spPlus]),
      p.spOffset,
    ]);

    const grid2 = h("div", { class: "card-grid" }, [tempCell, spCell]);

    p.valveFill = h("span", { class: "valve-fill" });
    p.valveVal = h("span", { class: "valve-val" });
    const valveBlock = h("div", { class: "valve-block" }, [
      h("div", { class: "valve-line" }, [
        h("span", { class: "cell-cap", text: this._t("card_valve") }),
        p.valveVal,
      ]),
      h("div", { class: "valve-track" }, [p.valveFill]),
    ]);

    p.spark = h("div", { class: "spark" });

    p.fast = h("div", { class: "fast-badge" });
    p.flags = h("div", { class: "chips card-chips" });

    const card = h(
      "article",
      {
        class: "card",
        tabindex: "0",
        dataset: { room: name },
        on: {
          click: (e) => {
            if (e.target.closest("button")) {
              return;
            }
            this._select(name);
          },
          keydown: (e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              this._select(name);
            }
          },
        },
      },
      [head, grid2, valveBlock, p.spark, p.fast, p.flags],
    );
    card._parts = p;
    card._flagKey = null;
    return card;
  }

  _updateCard(card, r) {
    const p = card._parts;

    card.classList.toggle("selected", r.name === this._selectedRoom);
    card.classList.toggle("dim", !r.participates);
    p.dot.className = "dot sev-" + r.severity;

    p.temp.textContent = fmt(r.current, 1, "°");
    p.trend.textContent = trendArrow(r.trend) + " " + fmt(r.trend, 1, " K/h");

    p.spVal.textContent = fmt(r.setpoint, 1);
    p.spOffset.textContent = fmtStr(this._t("card_offset"), {
      v: signed(r.offset, 1),
    });
    p.spMinus.disabled = r.offset <= OFFSET_MIN;
    p.spPlus.disabled = r.offset >= OFFSET_MAX;

    const valve = r.valve === null ? 0 : clamp(r.valve, 0, 100);
    p.valveFill.style.width = valve + "%";
    p.valveVal.textContent = fmt(r.valve, 0, "%");

    if (r.fastOn) {
      p.fast.className = "fast-badge on";
      p.fast.textContent =
        this._t("fast_on") +
        " · " +
        this._t("fmode_" + r.fastMode) +
        (r.fastTarget !== null ? " · " + fmt(r.fastTarget, 1, "°") : "");
    } else {
      p.fast.className = "fast-badge off";
      p.fast.textContent = this._t("fast_off");
    }

    p.live.textContent = r.live ? this._t("live") : this._t("shadow");
    p.live.className = "badge live-badge " + (r.live ? "on" : "off");

    const flagKey = r.flags.join("|");
    if (card._flagKey !== flagKey) {
      card._flagKey = flagKey;
      p.flags.textContent = "";
      for (const f of r.flags) {
        p.flags.appendChild(this._chip(f));
      }
      p.flags.style.display = r.flags.length ? "" : "none";
    }

    this._renderSpark(p.spark, r);
  }

  // -- Sparkline (24 h room temperature; one per card) ---------------------

  /**
   * Render / refresh a card's 24 h temperature sparkline.
   *
   * Fetches (cached) the room-temp source entity's history and draws a single
   * gap-aware trace. Redraw is gated by a data signature so a 5 s poll only
   * repaints when new samples actually arrived; with no temp entity the block
   * stays empty (charts degrade silently).
   */
  _renderSpark(container, r) {
    const tempId = r.entities ? r.entities.entity_temp_room : null;
    if (!tempId) {
      container._sig = "none";
      container.textContent = "";
      container.style.display = "none";
      return;
    }
    container.style.display = "";
    this._series(tempId, SPARK_WINDOW).then((entry) => {
      // Guard: the card may have been recycled to another room mid-flight.
      if (!container.isConnected) {
        return;
      }
      const cur = this._viewByName.get(r.name);
      if (cur && cur.entities && cur.entities.entity_temp_room !== tempId) {
        return;
      }
      const sig = tempId + "|" + entry.at + "|" + Math.round((cur && cur.setpoint) || 0);
      if (container._sig === sig) {
        return;
      }
      container._sig = sig;
      this._drawSpark(container, entry.data, cur ? cur.setpoint : null);
    });
  }

  _drawSpark(container, data, setpoint) {
    container.textContent = "";
    const segs = segments(data || []);
    const pts = segs.flat();
    if (pts.length < 2) {
      return; // nothing meaningful to draw
    }
    let vmin = Infinity;
    let vmax = -Infinity;
    for (const p of pts) {
      if (p.v < vmin) vmin = p.v;
      if (p.v > vmax) vmax = p.v;
    }
    if (setpoint != null && Number.isFinite(setpoint)) {
      vmin = Math.min(vmin, setpoint);
      vmax = Math.max(vmax, setpoint);
    }
    if (!(vmax > vmin)) {
      vmax = vmin + 1;
    }
    const t0 = pts[0].t;
    const t1 = pts[pts.length - 1].t;
    const tSpan = t1 - t0 || 1;
    const xOf = (t) =>
      SPARK_PAD + ((t - t0) / tSpan) * (SPARK_VB_W - 2 * SPARK_PAD);
    const yOf = (v) =>
      SPARK_PAD + (1 - (v - vmin) / (vmax - vmin)) * (SPARK_VB_H - 2 * SPARK_PAD);

    const svg = s("svg", {
      class: "spark-svg",
      viewBox: `0 0 ${SPARK_VB_W} ${SPARK_VB_H}`,
      preserveAspectRatio: "none",
      "aria-hidden": "true",
    });

    if (setpoint != null && Number.isFinite(setpoint)) {
      const y = yOf(setpoint).toFixed(1);
      svg.appendChild(
        s("line", {
          class: "spark-setpoint",
          x1: SPARK_PAD,
          x2: SPARK_VB_W - SPARK_PAD,
          y1: y,
          y2: y,
        }),
      );
    }

    // Area under the first→last segment span (visual weight), then the line.
    const areaParts = [];
    const lineParts = [];
    for (const seg of segs) {
      if (seg.length < 2) {
        continue;
      }
      let ad = "M" + xOf(seg[0].t).toFixed(1) + " " + (SPARK_VB_H - SPARK_PAD).toFixed(1);
      let ld = "";
      seg.forEach((p, i) => {
        const x = xOf(p.t).toFixed(1);
        const y = yOf(p.v).toFixed(1);
        ad += " L" + x + " " + y;
        ld += (i === 0 ? "M" : "L") + x + " " + y + " ";
      });
      ad +=
        " L" + xOf(seg[seg.length - 1].t).toFixed(1) + " " + (SPARK_VB_H - SPARK_PAD).toFixed(1) + " Z";
      areaParts.push(ad);
      lineParts.push(ld.trim());
    }
    if (areaParts.length) {
      svg.appendChild(s("path", { class: "spark-area", d: areaParts.join(" ") }));
    }
    if (lineParts.length) {
      svg.appendChild(s("path", { class: "spark-line", d: lineParts.join(" ") }));
    }
    // Last-value dot.
    const last = pts[pts.length - 1];
    svg.appendChild(
      s("circle", {
        class: "spark-dot",
        cx: xOf(last.t).toFixed(1),
        cy: yOf(last.v).toFixed(1),
        r: "2.5",
      }),
    );
    container.appendChild(svg);
  }

  _chip(code) {
    return h("span", {
      class: "chip chip-" + this._flagSev(code),
      text: this._flagLabel(code),
    });
  }

  // -- Rendering: room detail ---------------------------------------------

  _select(name) {
    this._selectedRoom = name;
    this._render();
    if (this._els.detail && this._els.detail.scrollIntoView) {
      this._els.detail.scrollIntoView({ block: "nearest" });
    }
  }

  _deselect() {
    this._selectedRoom = null;
    this._render();
  }

  _syncDetail() {
    const detail = this._els.detail;
    if (!this._selectedRoom) {
      detail.style.display = "none";
      this._els.layout.classList.remove("has-detail");
      this._detailRoom = null;
      this._disconnectChartRO();
      return;
    }
    detail.style.display = "";
    this._els.layout.classList.add("has-detail");

    if (this._detailRoom !== this._selectedRoom) {
      this._buildDetail(this._selectedRoom);
      this._detailRoom = this._selectedRoom;
    }
    const r = this._viewByName.get(this._selectedRoom);
    this._updateDetail(r);
    this._refreshWiringStates();
    this._refreshHistory();
    this._updateLegendValues();
  }

  _buildDetail(name) {
    const D = { rows: [] };
    // Publish the new refs before building sections: `_buildHistory` kicks off
    // an async fetch that resolves against `this._detail.history`.
    this._detail = D;
    const detail = this._els.detail;
    detail.textContent = "";

    // Header with close.
    D.title = h("span", { class: "detail-title", text: name });
    const close = h(
      "button",
      {
        class: "ghost-btn",
        type: "button",
        title: this._t("detail_close"),
        on: { click: () => this._deselect() },
      },
      [this._icon("mdi:close", "✕")],
    );
    const header = h("div", { class: "detail-head" }, [
      D.title,
      h("span", { class: "spacer" }),
      close,
    ]);

    // Section 1: wiring.
    D.wiringBody = h("div", { class: "wire-list" });
    const wiring = this._section(this._t("sec_wiring"), D.wiringBody);
    this._buildWiring(D, name);

    // Section 2: controller decision.
    const dec = this._buildDecision(D);

    // Section 3: history charts, mounted in `.history-mount[data-room]`.
    const historyMount = h("div", {
      class: "history-mount",
      dataset: { room: name },
    });
    this._buildHistory(D, name, historyMount);
    const history = this._section(this._t("sec_history"), historyMount);

    detail.appendChild(header);
    detail.appendChild(wiring);
    detail.appendChild(dec);
    detail.appendChild(history);
  }

  _section(title, body) {
    return h("section", { class: "sub" }, [
      h("div", { class: "sub-title", text: title }),
      body,
    ]);
  }

  _buildWiring(D, name) {
    const room = this._viewByName.get(name) || {};
    const entities = room.entities || {};
    for (const role of WIRING_ROLES) {
      const value = entities[role];
      const ids = Array.isArray(value) ? value.filter(Boolean) : value ? [value] : [];
      const label = this._t("role_" + role);
      if (ids.length === 0) {
        D.wiringBody.appendChild(
          h("div", { class: "wire-row muted" }, [
            h("span", { class: "wire-role", text: label }),
            h("span", { class: "wire-id", text: this._t("wire_unset") }),
          ]),
        );
        continue;
      }
      for (const id of ids) {
        this._addWireRow(D, label, id);
      }
    }

    // Optional future field: per-room diagnostic entities (role -> id/list).
    const diag = room.diagnosticEntities;
    if (diag && Object.keys(diag).length) {
      D.wiringBody.appendChild(
        h("div", { class: "wire-group-cap", text: this._t("sec_diagnostics") }),
      );
      for (const [key, value] of Object.entries(diag)) {
        const ids = Array.isArray(value) ? value.filter(Boolean) : value ? [value] : [];
        const roleKey = "role_" + key;
        const label = roleKey in STR.en ? this._t(roleKey) : key;
        for (const id of ids) {
          this._addWireRow(D, label, id);
        }
      }
    }
  }

  _addWireRow(D, label, id) {
    const stateEl = h("span", { class: "wire-state" });
    const badgeEl = h("span", {
      class: "wire-badge",
      text: this._t("wire_unavailable"),
      style: "display:none",
    });
    const rowEl = h(
      "div",
      {
        class: "wire-row link",
        tabindex: "0",
        title: id,
        on: {
          click: () => this._moreInfo(id),
          keydown: (e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              this._moreInfo(id);
            }
          },
        },
      },
      [
        h("span", { class: "wire-role", text: label }),
        h("span", { class: "wire-id", text: id }),
        stateEl,
        badgeEl,
        this._icon("mdi:open-in-new", "›"),
      ],
    );
    D.rows.push({ entityId: id, stateEl, badgeEl, rowEl });
    D.wiringBody.appendChild(rowEl);
  }

  _refreshWiringStates() {
    const D = this._detail;
    if (!D || !this._selectedRoom) {
      return;
    }
    const states = (this._hass && this._hass.states) || {};
    for (const row of D.rows) {
      if (!row.entityId) {
        continue;
      }
      const st = states[row.entityId];
      const val = st ? st.state : null;
      const bad = !st || val === null || UNAVAILABLE_STATES.has(String(val));
      const unit =
        st && st.attributes ? st.attributes.unit_of_measurement || "" : "";
      if (!st) {
        row.stateEl.textContent = this._t("wire_missing");
      } else {
        row.stateEl.textContent = String(val) + (unit && !bad ? " " + unit : "");
      }
      row.badgeEl.style.display = bad ? "" : "none";
      row.rowEl.classList.toggle("wire-bad", bad);
    }
  }

  _buildDecision(D) {
    const body = h("div", { class: "dec" });

    D.errorEl = this._decRow(body, this._t("dec_error"));
    D.trendEl = this._decRow(body, this._t("dec_trend"));
    D.dewEl = this._decRow(body, this._t("dec_dew"));

    // Signed contribution bars for the four valve terms.
    D.terms = {};
    const termsWrap = h("div", { class: "terms" });
    for (const [key, cap] of [
      ["pTerm", this._t("term_p")],
      ["iTerm", this._t("term_i")],
      ["trendTerm", this._t("term_trend")],
      ["ffTerm", this._t("term_ff")],
    ]) {
      const fill = h("span", { class: "term-fill" });
      const valEl = h("span", { class: "term-val" });
      const row = h("div", { class: "term-row" }, [
        h("span", { class: "term-cap", text: cap }),
        h("div", { class: "term-track" }, [h("span", { class: "term-mid" }), fill]),
        valEl,
      ]);
      D.terms[key] = { fill, valEl };
      termsWrap.appendChild(row);
    }
    body.appendChild(
      h("div", { class: "dec-block" }, [
        h("div", { class: "dec-block-cap", text: this._t("dec_terms") }),
        termsWrap,
      ]),
    );

    D.rawValveEl = this._decRow(body, this._t("dec_raw_valve"));
    D.finalValveEl = this._decRow(body, this._t("dec_final_valve"));
    D.throttleEl = this._decRow(body, this._t("dec_throttle"));
    D.integratorEl = this._decRow(body, this._t("dec_integrator"));
    D.saturatedEl = this._decRow(body, this._t("dec_saturated"));
    D.floorEl = this._decRow(body, this._t("dec_floor"));
    D.fastEl = this._decRow(body, this._t("dec_fast"));

    D.explanationEl = h("div", { class: "explanation" });
    body.appendChild(
      h("div", { class: "dec-block" }, [
        h("div", { class: "dec-block-cap", text: this._t("dec_explanation") }),
        D.explanationEl,
      ]),
    );

    D.flagsEl = h("div", { class: "chips" });
    D.flagsBlock = h("div", { class: "dec-block", style: "display:none" }, [
      h("div", { class: "dec-block-cap", text: this._t("dec_flags") }),
      D.flagsEl,
    ]);
    body.appendChild(D.flagsBlock);

    // Raw report JSON in a collapsed <details> (open state survives polls).
    D.rawPre = h("pre", { class: "raw-pre" });
    const details = h("details", { class: "raw" }, [
      h("summary", { text: this._t("raw_show") }),
      D.rawPre,
    ]);
    body.appendChild(details);

    return this._section(this._t("sec_decision"), body);
  }

  _decRow(parent, label) {
    const valEl = h("span", { class: "dval" });
    parent.appendChild(
      h("div", { class: "drow" }, [h("span", { class: "dlabel", text: label }), valEl]),
    );
    return valEl;
  }

  _updateDetail(r) {
    const D = this._detail;
    if (!D) {
      return;
    }
    if (!r) {
      D.errorEl.textContent = this._t("no_live_room");
      return;
    }
    D.title.textContent = r.name;

    D.errorEl.textContent = fmt(r.errorC, 2, " K");
    D.trendEl.textContent = trendArrow(r.trend) + " " + fmt(r.trend, 2, " K/h");
    D.dewEl.textContent = fmt(r.dew, 1, " °C");

    const terms = {
      pTerm: r.pTerm,
      iTerm: r.iTerm,
      trendTerm: r.trendTerm,
      ffTerm: r.ffTerm,
    };
    const maxAbs = Math.max(
      0.001,
      ...Object.values(terms).map((v) => Math.abs(v === null ? 0 : v)),
    );
    for (const [key, cell] of Object.entries(D.terms)) {
      const v = terms[key] === null ? 0 : terms[key];
      const ratio = clamp(Math.abs(v) / maxAbs, 0, 1);
      const half = ratio * 50;
      if (v >= 0) {
        cell.fill.style.left = "50%";
        cell.fill.style.width = half + "%";
        cell.fill.classList.remove("neg");
      } else {
        cell.fill.style.left = 50 - half + "%";
        cell.fill.style.width = half + "%";
        cell.fill.classList.add("neg");
      }
      cell.valEl.textContent = signed(terms[key], 1, "%");
    }

    D.rawValveEl.textContent = fmt(r.rawValve, 0, "%");
    D.finalValveEl.textContent = fmt(r.valve, 0, "%");
    D.throttleEl.textContent =
      r.throttle === null ? "—" : fmt(r.throttle * 100, 0, "%");
    D.integratorEl.textContent = r.integratorFrozen
      ? this._t("integ_frozen")
      : this._t("integ_active");
    D.saturatedEl.textContent = r.saturated ? this._t("yes") : this._t("no");
    D.floorEl.textContent = r.valveFloor ? this._t("yes") : this._t("no");
    D.fastEl.textContent = r.fastOn
      ? this._t("fast_on") +
        " · " +
        this._t("fmode_" + r.fastMode) +
        (r.fastTarget !== null ? " · " + fmt(r.fastTarget, 1, "°") : "")
      : this._t("fast_off");

    D.explanationEl.textContent = r.explanation || "—";

    D.flagsEl.textContent = "";
    for (const f of r.flags) {
      D.flagsEl.appendChild(this._chip(f));
    }
    D.flagsBlock.style.display = r.flags.length ? "" : "none";

    let raw;
    try {
      raw = JSON.stringify(r.report, null, 2);
    } catch (err) {
      raw = String(r.report);
    }
    D.rawPre.textContent = raw || "{}";
  }

  // -- Rendering: detail history chart ------------------------------------

  /**
   * Build the history section UI (window switch, legend, scroll+SVG holder,
   * tooltip) into the `.history-mount` and kick off the first fetch.
   *
   * Window and series-visibility choices persist on the instance across room
   * switches. The SVG itself is rebuilt on every draw inside a stable holder,
   * so pointer / resize wiring attaches once and survives redraws.
   */
  _buildHistory(D, name, mount) {
    const HI = {
      mount,
      room: name,
      window: WINDOWS[this._chartWindow] ? this._chartWindow : "24h",
      visible: { ...this._chartVisible },
      data: null,
      sig: null,
      ctx: null,
      token: 0,
    };

    HI.winBtns = {};
    const wins = h(
      "div",
      { class: "chart-wins", role: "group" },
      WINDOW_ORDER.map((wk) => {
        const b = h("button", {
          class: "chart-win",
          type: "button",
          text: this._t("win_" + wk),
          dataset: { win: wk },
          on: { click: () => this._setChartWindow(wk) },
        });
        HI.winBtns[wk] = b;
        return b;
      }),
    );
    const head = h("div", { class: "chart-head" }, [
      h("span", { class: "spacer" }),
      wins,
    ]);

    HI.note = h("div", { class: "chart-note muted", text: this._t("chart_loading") });
    HI.holder = h("div", { class: "chart-holder" });
    HI.tip = h("div", { class: "chart-tip", style: "display:none" });
    HI.scroll = h("div", { class: "chart-scroll" }, [HI.holder, HI.tip]);

    HI.legItems = {};
    HI.legend = h("div", { class: "chart-legend" });
    for (const key of ["temp", "setpoint", "valve"]) {
      const val = h("span", { class: "leg-val" });
      const item = h(
        "button",
        {
          class: "leg-item",
          type: "button",
          dataset: { series: key },
          on: { click: () => this._toggleSeries(key) },
        },
        [
          h("span", { class: "leg-sw leg-" + key }),
          h("span", { class: "leg-lab", text: this._t("chart_" + key) }),
          val,
        ],
      );
      HI.legItems[key] = { item, val };
      HI.legend.appendChild(item);
    }

    mount.textContent = "";
    mount.appendChild(head);
    mount.appendChild(HI.note);
    mount.appendChild(HI.scroll);
    mount.appendChild(HI.legend);

    HI.holder.addEventListener("pointermove", (e) => this._chartPointer(e));
    HI.holder.addEventListener("pointerleave", () => this._chartHideTip());

    D.history = HI;
    this._applyWinButtons();

    // Redraw on width change (debounced to one rAF).
    this._disconnectChartRO();
    if (typeof ResizeObserver !== "undefined") {
      let raf = 0;
      this._chartRO = new ResizeObserver(() => {
        if (raf) {
          return;
        }
        raf = requestAnimationFrame(() => {
          raf = 0;
          const hi = this._detail && this._detail.history;
          if (hi && hi.data && this._selectedRoom) {
            this._drawChart(hi);
          }
        });
      });
      this._chartRO.observe(HI.scroll);
    }

    this._refreshHistory(HI);
  }

  _applyWinButtons() {
    const HI = this._detail && this._detail.history;
    if (!HI) {
      return;
    }
    for (const wk of WINDOW_ORDER) {
      HI.winBtns[wk].classList.toggle("active", wk === HI.window);
    }
  }

  _setChartWindow(wk) {
    if (!WINDOWS[wk]) {
      return;
    }
    this._chartWindow = wk;
    const HI = this._detail && this._detail.history;
    if (!HI) {
      return;
    }
    HI.window = wk;
    HI.sig = null;
    HI.data = null;
    this._applyWinButtons();
    HI.note.textContent = this._t("chart_loading");
    HI.note.style.display = "";
    this._chartHideTip();
    this._refreshHistory(HI, true);
  }

  _toggleSeries(key) {
    const HI = this._detail && this._detail.history;
    if (!HI || !(key in HI.visible)) {
      return;
    }
    HI.visible[key] = !HI.visible[key];
    this._chartVisible[key] = HI.visible[key];
    this._chartHideTip();
    if (HI.data) {
      this._drawChart(HI);
    }
  }

  /**
   * Fetch the room's chartable series for the active window and redraw when the
   * data actually changed. Cheap on a 5 s poll (cache-gated); silent when the
   * room has no chartable entities.
   */
  _refreshHistory(hi, force) {
    const HI = hi || (this._detail && this._detail.history);
    if (!HI || !this._selectedRoom || HI.room !== this._selectedRoom) {
      return;
    }
    const r = this._viewByName.get(HI.room);
    if (!r) {
      return;
    }
    const wk = HI.window;
    const tempId = r.entities ? r.entities.entity_temp_room : null;
    const diag = r.diagnosticEntities || {};
    const errorId = diag.error_c || null;
    const valveId = diag.recommended_valve || null;

    if (!tempId && !errorId && !valveId) {
      HI.data = null;
      HI.ctx = null;
      HI.holder.textContent = "";
      HI.note.textContent = this._t("chart_unavailable");
      HI.note.style.display = "";
      return;
    }

    const empty = Promise.resolve({ at: 0, data: [] });
    const jobs = [
      tempId ? this._series(tempId, wk) : empty,
      errorId ? this._series(errorId, wk) : empty,
      valveId ? this._series(valveId, wk) : empty,
    ];
    const token = ++HI.token;
    Promise.all(jobs).then(([te, ee, ve]) => {
      // Stale guard: the detail was rebuilt or the window changed mid-flight.
      if (!this._detail || this._detail.history !== HI || HI.token !== token) {
        return;
      }
      const sig = [wk, te.at, ee.at, ve.at, tempId, errorId, valveId].join("|");
      if (!force && HI.sig === sig && HI.data) {
        return;
      }
      HI.sig = sig;

      const end = Date.now();
      const t0 = end - WINDOWS[wk].hours * 3600 * 1000;
      const temp = te.data || [];
      const error = ee.data || [];
      const valve = ve.data || [];

      // Setpoint = temp + error (as-of join on the temp timeline); fall back to
      // a flat line at the current live setpoint when no error history exists.
      let setpoint = [];
      if (temp.length && error.length) {
        setpoint = temp.map((p) => {
          if (p.v == null) {
            return { t: p.t, v: null };
          }
          const e = interpAt(error, p.t);
          return { t: p.t, v: e == null ? null : p.v + e };
        });
      } else if (temp.length && r.setpoint != null && Number.isFinite(r.setpoint)) {
        setpoint = [
          { t: temp[0].t, v: r.setpoint },
          { t: temp[temp.length - 1].t, v: r.setpoint },
        ];
      }

      // Optional comfort deadband — read defensively; the core does not expose
      // it today, so the band simply does not draw (honest "if available").
      const deadband = num(pick(r.report, ["deadband_c", "deadband"], null));

      HI.data = { t0, t1: end, temp, setpoint, valve, error, deadband };
      this._drawChart(HI);
    });
  }

  _hasReal(arr) {
    return Array.isArray(arr) && arr.some((p) => p.v != null && Number.isFinite(p.v));
  }

  _drawChart(hi) {
    const HI = hi || (this._detail && this._detail.history);
    if (!HI || !HI.data) {
      return;
    }
    const d = HI.data;
    const W = clamp(
      Math.round(HI.scroll.clientWidth || CHART_MIN_W),
      CHART_MIN_W,
      CHART_MAX_W,
    );
    const H = CHART_H;
    const m = CHART_MARGIN;
    const plotX = m.l;
    const plotY = m.t;
    const plotW = Math.max(10, W - m.l - m.r);
    const plotH = Math.max(10, H - m.t - m.b);

    const hasTemp = this._hasReal(d.temp);
    const hasSp = this._hasReal(d.setpoint);
    const hasValve = this._hasReal(d.valve);

    if (!hasTemp && !hasSp && !hasValve) {
      HI.ctx = null;
      HI.holder.textContent = "";
      HI.note.textContent = this._t("chart_nodata");
      HI.note.style.display = "";
      this._updateLegend(HI, hasTemp, hasSp, hasValve);
      return;
    }
    HI.note.style.display = "none";

    // Left (°C) domain from all available temperature + setpoint samples.
    let vmin = Infinity;
    let vmax = -Infinity;
    const consider = (arr) => {
      for (const p of arr) {
        if (p.v != null && Number.isFinite(p.v)) {
          if (p.v < vmin) vmin = p.v;
          if (p.v > vmax) vmax = p.v;
        }
      }
    };
    if (hasTemp) consider(d.temp);
    if (hasSp) consider(d.setpoint);
    if (hasSp && d.deadband && d.deadband > 0) {
      vmin -= d.deadband;
      vmax += d.deadband;
    }
    const hasLeft = vmin !== Infinity;
    let leftTicks = [];
    let domMin = 0;
    let domMax = 100;
    let tickDigits = 0;
    if (hasLeft) {
      leftTicks = niceTicks(vmin, vmax, 5);
      domMin = leftTicks[0];
      domMax = leftTicks[leftTicks.length - 1];
      if (!(domMax > domMin)) {
        domMax = domMin + 1;
      }
      const step = leftTicks.length > 1 ? leftTicks[1] - leftTicks[0] : 1;
      tickDigits = step < 1 ? 1 : 0;
    }

    const span = d.t1 - d.t0 || 1;
    const xOf = (t) => plotX + ((t - d.t0) / span) * plotW;
    const yL = (v) => plotY + plotH - ((v - domMin) / (domMax - domMin || 1)) * plotH;
    const yR = (v) => plotY + plotH - (clamp(v, 0, 100) / 100) * plotH;

    const svg = s("svg", {
      class: "chart-svg",
      width: W,
      height: H,
      viewBox: `0 0 ${W} ${H}`,
      role: "img",
    });

    // Gridlines + left axis (°C).
    if (hasLeft) {
      for (const tick of leftTicks) {
        const y = yL(tick);
        svg.appendChild(
          s("line", {
            class: "grid-line",
            x1: plotX,
            x2: plotX + plotW,
            y1: y.toFixed(1),
            y2: y.toFixed(1),
          }),
        );
        svg.appendChild(
          s("text", {
            class: "axis-label axis-left",
            x: plotX - 6,
            y: (y + 3).toFixed(1),
            "text-anchor": "end",
            text: tick.toFixed(tickDigits),
          }),
        );
      }
    }

    // Right axis (valve %).
    if (hasValve) {
      for (const pct of [0, 25, 50, 75, 100]) {
        const y = yR(pct);
        if (!hasLeft) {
          svg.appendChild(
            s("line", {
              class: "grid-line",
              x1: plotX,
              x2: plotX + plotW,
              y1: y.toFixed(1),
              y2: y.toFixed(1),
            }),
          );
        }
        svg.appendChild(
          s("text", {
            class: "axis-label axis-right",
            x: plotX + plotW + 6,
            y: (y + 3).toFixed(1),
            "text-anchor": "start",
            text: pct + "%",
          }),
        );
      }
    }

    // Time axis.
    const fmtTime = this._timeFmter(HI.window);
    const nTicks = 5;
    for (let i = 0; i < nTicks; i++) {
      const frac = i / (nTicks - 1);
      const t = d.t0 + frac * (d.t1 - d.t0);
      const x = plotX + frac * plotW;
      svg.appendChild(
        s("text", {
          class: "axis-label axis-time",
          x: x.toFixed(1),
          y: (plotY + plotH + 16).toFixed(1),
          "text-anchor": i === 0 ? "start" : i === nTicks - 1 ? "end" : "middle",
          text: fmtTime(new Date(t)),
        }),
      );
    }

    // Deadband band around the setpoint (only when a value is available).
    if (hasSp && HI.visible.setpoint && d.deadband && d.deadband > 0) {
      for (const seg of segments(d.setpoint)) {
        if (seg.length < 2) {
          continue;
        }
        let path = "";
        seg.forEach((p, i) => {
          path +=
            (i === 0 ? "M" : "L") + xOf(p.t).toFixed(1) + " " + yL(p.v + d.deadband).toFixed(1) + " ";
        });
        for (let i = seg.length - 1; i >= 0; i--) {
          const p = seg[i];
          path += "L" + xOf(p.t).toFixed(1) + " " + yL(p.v - d.deadband).toFixed(1) + " ";
        }
        path += "Z";
        svg.appendChild(s("path", { class: "deadband", d: path }));
      }
    }

    // Valve area + line (right axis), drawn behind the temperature lines.
    if (hasValve && HI.visible.valve) {
      const areaParts = [];
      const lineParts = [];
      for (const seg of segments(d.valve)) {
        if (!seg.length) {
          continue;
        }
        let ad = "M" + xOf(seg[0].t).toFixed(1) + " " + (plotY + plotH).toFixed(1);
        let ld = "";
        seg.forEach((p, i) => {
          const x = xOf(p.t).toFixed(1);
          const y = yR(p.v).toFixed(1);
          ad += " L" + x + " " + y;
          ld += (i === 0 ? "M" : "L") + x + " " + y + " ";
        });
        ad += " L" + xOf(seg[seg.length - 1].t).toFixed(1) + " " + (plotY + plotH).toFixed(1) + " Z";
        areaParts.push(ad);
        if (seg.length > 1) {
          lineParts.push(ld.trim());
        }
      }
      if (areaParts.length) {
        svg.appendChild(s("path", { class: "s-valve-area", d: areaParts.join(" ") }));
      }
      if (lineParts.length) {
        svg.appendChild(s("path", { class: "s-valve", d: lineParts.join(" ") }));
      }
    }

    // Setpoint line (dashed).
    if (hasSp && HI.visible.setpoint) {
      const parts = this._lineParts(segments(d.setpoint), xOf, yL);
      if (parts) {
        svg.appendChild(s("path", { class: "s-setpoint", d: parts }));
      }
    }

    // Temperature line (solid, foreground).
    if (hasTemp && HI.visible.temp) {
      const parts = this._lineParts(segments(d.temp), xOf, yL);
      if (parts) {
        svg.appendChild(s("path", { class: "s-temp", d: parts }));
      }
    }

    // Hover guide + per-series dots (hidden until pointer move).
    const guide = s("line", {
      class: "chart-guide",
      x1: 0,
      x2: 0,
      y1: plotY,
      y2: plotY + plotH,
      style: "display:none",
    });
    svg.appendChild(guide);
    const dots = {};
    for (const key of ["valve", "setpoint", "temp"]) {
      const c = s("circle", {
        class: "chart-dot dot-" + key,
        r: "3.5",
        style: "display:none",
      });
      svg.appendChild(c);
      dots[key] = c;
    }

    HI.holder.textContent = "";
    HI.holder.appendChild(svg);

    // Merged anchor times (visible series only) for nearest-point snapping.
    const anchorSet = new Set();
    const collect = (arr) => {
      for (const p of arr) {
        if (p.v != null && Number.isFinite(p.v)) {
          anchorSet.add(p.t);
        }
      }
    };
    if (hasTemp && HI.visible.temp) collect(d.temp);
    if (hasSp && HI.visible.setpoint) collect(d.setpoint);
    if (hasValve && HI.visible.valve) collect(d.valve);
    const anchors = [...anchorSet].sort((a, b) => a - b);

    HI.ctx = {
      svg,
      W,
      plotX,
      plotY,
      plotW,
      plotH,
      t0: d.t0,
      t1: d.t1,
      xOf,
      yL,
      yR,
      guide,
      dots,
      anchors,
      series: {
        temp: hasTemp && HI.visible.temp ? d.temp : null,
        setpoint: hasSp && HI.visible.setpoint ? d.setpoint : null,
        valve: hasValve && HI.visible.valve ? d.valve : null,
      },
      error: d.error && d.error.length ? d.error : null,
    };

    this._updateLegend(HI, hasTemp, hasSp, hasValve);
  }

  /** Build a multi-segment polyline `d` string, or null when nothing to draw. */
  _lineParts(segs, xOf, yOf) {
    const parts = [];
    for (const seg of segs) {
      if (seg.length < 2) {
        continue;
      }
      let ld = "";
      seg.forEach((p, i) => {
        ld += (i === 0 ? "M" : "L") + xOf(p.t).toFixed(1) + " " + yOf(p.v).toFixed(1) + " ";
      });
      parts.push(ld.trim());
    }
    return parts.length ? parts.join(" ") : null;
  }

  _updateLegend(HI, hasTemp, hasSp, hasValve) {
    const r = this._viewByName.get(HI.room) || {};
    const set = (key, val, digits, suffix, has) => {
      const it = HI.legItems[key];
      if (!it) {
        return;
      }
      it.val.textContent = has ? fmt(val, digits, suffix) : "—";
      it.item.classList.toggle("off", !HI.visible[key]);
      it.item.classList.toggle("nodata", !has);
    };
    set("temp", r.current, 1, "°", hasTemp);
    set("setpoint", r.setpoint, 1, "°", hasSp);
    set("valve", r.valve, 0, "%", hasValve);
  }

  /**
   * Refresh only the legend's current-value readouts from the live view.
   *
   * Cheap enough to run every 5 s poll (the chart itself redraws far less
   * often); leaves a series showing "—" while it has no chart data.
   */
  _updateLegendValues() {
    const HI = this._detail && this._detail.history;
    if (!HI || !HI.legItems) {
      return;
    }
    const r = this._viewByName.get(HI.room);
    if (!r) {
      return;
    }
    const set = (key, val, digits, suffix) => {
      const it = HI.legItems[key];
      if (it && !it.item.classList.contains("nodata")) {
        it.val.textContent = fmt(val, digits, suffix);
      }
    };
    set("temp", r.current, 1, "°");
    set("setpoint", r.setpoint, 1, "°");
    set("valve", r.valve, 0, "%");
  }

  /** Localised axis-tick time formatter for a window. */
  _timeFmter(windowKey) {
    const opts =
      windowKey === "7d"
        ? { day: "numeric", month: "short" }
        : { hour: "2-digit", minute: "2-digit" };
    let f = null;
    try {
      f = new Intl.DateTimeFormat(this._lang, opts);
    } catch (err) {
      f = null;
    }
    return (date) => (f ? f.format(date) : date.toLocaleString());
  }

  /** Localised tooltip timestamp (fuller than axis ticks). */
  _tipTime(windowKey, t) {
    const opts =
      windowKey === "7d"
        ? { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }
        : { hour: "2-digit", minute: "2-digit" };
    try {
      return new Intl.DateTimeFormat(this._lang, opts).format(new Date(t));
    } catch (err) {
      return new Date(t).toLocaleString();
    }
  }

  _chartPointer(ev) {
    const HI = this._detail && this._detail.history;
    if (!HI || !HI.ctx) {
      return;
    }
    const ctx = HI.ctx;
    const rect = ctx.svg.getBoundingClientRect();
    if (!rect.width) {
      return;
    }
    const scale = ctx.W / rect.width;
    const px = clamp((ev.clientX - rect.left) * scale, ctx.plotX, ctx.plotX + ctx.plotW);
    const t = ctx.t0 + ((px - ctx.plotX) / (ctx.plotW || 1)) * (ctx.t1 - ctx.t0);

    // Snap to the nearest real sample time across the visible series.
    let at = t;
    if (ctx.anchors.length) {
      let best = ctx.anchors[0];
      let bd = Math.abs(best - t);
      for (const a of ctx.anchors) {
        const dd = Math.abs(a - t);
        if (dd < bd) {
          bd = dd;
          best = a;
        }
      }
      at = best;
    }
    const gx = ctx.xOf(at);
    ctx.guide.setAttribute("x1", gx.toFixed(1));
    ctx.guide.setAttribute("x2", gx.toFixed(1));
    ctx.guide.style.display = "";

    const rows = [];
    const place = (key, axis, digits, suffix) => {
      const series = ctx.series[key];
      const dot = ctx.dots[key];
      if (!series) {
        dot.style.display = "none";
        return null;
      }
      const v = interpAt(series, at);
      if (v == null) {
        dot.style.display = "none";
        return null;
      }
      const y = axis === "R" ? ctx.yR(v) : ctx.yL(v);
      dot.setAttribute("cx", gx.toFixed(1));
      dot.setAttribute("cy", y.toFixed(1));
      dot.style.display = "";
      rows.push({ key, label: this._t("chart_" + key), text: fmt(v, digits, suffix) });
      return v;
    };
    const tempV = place("temp", "L", 1, "°");
    const spV = place("setpoint", "L", 1, "°");
    place("valve", "R", 0, "%");

    // Error row: measured error if available, else setpoint − temp.
    let errV = ctx.error ? interpAt(ctx.error, at) : null;
    if (errV == null && spV != null && tempV != null) {
      errV = spV - tempV;
    }

    if (!rows.length && errV == null) {
      this._chartHideTip();
      return;
    }

    HI.tip.textContent = "";
    HI.tip.appendChild(h("div", { class: "tip-time", text: this._tipTime(HI.window, at) }));
    for (const row of rows) {
      HI.tip.appendChild(
        h("div", { class: "tip-row" }, [
          h("span", { class: "tip-sw leg-" + row.key }),
          h("span", { class: "tip-lab", text: row.label }),
          h("span", { class: "tip-val", text: row.text }),
        ]),
      );
    }
    if (errV != null) {
      HI.tip.appendChild(
        h("div", { class: "tip-row" }, [
          h("span", { class: "tip-sw tip-sw-blank" }),
          h("span", { class: "tip-lab", text: this._t("chart_error") }),
          h("span", { class: "tip-val", text: signed(errV, 2, " K") }),
        ]),
      );
    }
    HI.tip.style.display = "";

    // Position within the scroll viewport, flipping near the right edge.
    const visW = HI.scroll.clientWidth;
    const localX = gx - HI.scroll.scrollLeft;
    const tipW = HI.tip.offsetWidth || 130;
    let left = localX + 14;
    if (left + tipW > visW - 4) {
      left = localX - tipW - 14;
    }
    left = clamp(left, 4, Math.max(4, visW - tipW - 4));
    HI.tip.style.left = left.toFixed(0) + "px";
    HI.tip.style.top = "6px";
  }

  _chartHideTip() {
    const HI = this._detail && this._detail.history;
    if (!HI) {
      return;
    }
    if (HI.tip) {
      HI.tip.style.display = "none";
    }
    if (HI.ctx) {
      HI.ctx.guide.style.display = "none";
      for (const key of Object.keys(HI.ctx.dots)) {
        HI.ctx.dots[key].style.display = "none";
      }
    }
  }
}

// --- Styles (all colours via HA theme tokens with safe fallbacks) -----------

const STYLE = `
:host {
  --t-fg: var(--primary-text-color, #212121);
  --t-muted: var(--secondary-text-color, #727272);
  --t-disabled: var(--disabled-text-color, #bdbdbd);
  --t-bg: var(--primary-background-color, #fafafa);
  --t-card: var(--ha-card-background, var(--card-background-color, #fff));
  --t-line: var(--divider-color, rgba(0,0,0,.12));
  --t-primary: var(--primary-color, #03a9f4);
  --t-on-primary: var(--text-primary-color, #fff);
  --t-accent: var(--accent-color, #ff9800);
  --t-error: var(--error-color, #db4437);
  --t-warn: var(--warning-color, #ffa600);
  --t-ok: var(--success-color, #43a047);
  --t-info: var(--info-color, #039be5);
  --t-chip: var(--secondary-background-color, #e5e5e5);
  --t-icon: var(--state-icon-color, #44739e);
  --t-radius: var(--ha-card-border-radius, 12px);
  display: block;
  color: var(--t-fg);
  background: var(--t-bg);
  font-family: inherit;
  min-height: 100%;
  box-sizing: border-box;
}
:host *, :host *::before, :host *::after { box-sizing: border-box; }
.wrap { max-width: 1400px; margin: 0 auto; padding: 16px; }
.wrap.killed {
  outline: 3px solid var(--t-error);
  outline-offset: -3px;
  border-radius: var(--t-radius);
}
.hicon { --mdc-icon-size: 18px; color: var(--t-icon); }
.hicon-fallback { color: var(--t-icon); font-size: 14px; }
.spacer { flex: 1 1 auto; }
.muted { color: var(--t-muted); }

.banner {
  background: var(--t-error); color: var(--t-on-primary);
  padding: 8px 12px; border-radius: 8px; margin-bottom: 12px;
  font-size: 13px; word-break: break-word;
}

/* Buttons */
button { font-family: inherit; cursor: pointer; }
.ghost-btn {
  display: inline-flex; align-items: center; gap: 6px;
  background: transparent; color: var(--t-primary);
  border: 1px solid var(--t-line); border-radius: 8px;
  padding: 6px 12px; font-size: 13px;
}
.ghost-btn:hover { border-color: var(--t-primary); }
.step-btn {
  width: 30px; height: 30px; border-radius: 8px;
  border: 1px solid var(--t-line); background: var(--t-card); color: var(--t-fg);
  font-size: 18px; line-height: 1; display: inline-flex;
  align-items: center; justify-content: center;
}
.step-btn.sm { width: 26px; height: 26px; font-size: 16px; }
.step-btn:hover:not(:disabled) { border-color: var(--t-primary); color: var(--t-primary); }
.step-btn:disabled { opacity: .4; cursor: default; }

/* Hero */
.hero {
  background: var(--t-card); border: 1px solid var(--t-line);
  border-radius: var(--t-radius); padding: 14px 16px; margin-bottom: 16px;
  display: flex; flex-direction: column; gap: 14px;
}
.hero-row { display: flex; flex-wrap: wrap; align-items: center; gap: 14px; }
.brand { display: flex; align-items: center; gap: 8px; font-weight: 600; font-size: 15px; }
.brand .hicon { --mdc-icon-size: 22px; color: var(--t-primary); }
.pill {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 6px 12px; border-radius: 999px;
  background: var(--t-chip); font-size: 13px; font-weight: 500;
}
.pill-dot { width: 10px; height: 10px; border-radius: 50%; background: var(--t-muted); }
.pill-ok .pill-dot { background: var(--t-ok); }
.pill-warn .pill-dot { background: var(--t-warn); }
.pill-problem .pill-dot { background: var(--t-error); }
.pill-ok { color: var(--t-ok); }
.pill-warn { color: var(--t-warn); }
.pill-problem { color: var(--t-error); }
.metric { display: flex; flex-direction: column; line-height: 1.15; }
.metric-val { font-size: 18px; font-weight: 600; }
.metric-cap { font-size: 11px; color: var(--t-muted); text-transform: uppercase; letter-spacing: .04em; }
.metric.has .metric-val { color: var(--t-warn); }
.chip-dew {
  display: inline-flex; flex-direction: column; line-height: 1.15;
  padding: 4px 12px; border-radius: 8px;
  background: color-mix(in srgb, var(--t-info) 14%, transparent);
  border: 1px solid var(--t-info);
}
.chip-dew .chip-cap { font-size: 11px; color: var(--t-muted); }
.chip-dew .metric-val { font-size: 15px; color: var(--t-info); }

.ctl { display: flex; flex-direction: column; gap: 4px; }
.ctl-cap { font-size: 11px; color: var(--t-muted); text-transform: uppercase; letter-spacing: .04em; }
.stepper {
  display: inline-flex; align-items: center; gap: 8px;
  border: 1px solid var(--t-line); border-radius: 8px; padding: 2px 6px; background: var(--t-card);
}
.step-val { min-width: 62px; text-align: center; font-size: 15px; font-weight: 600; }
.seg { display: inline-flex; border: 1px solid var(--t-line); border-radius: 8px; overflow: hidden; }
.seg-btn {
  border: 0; background: var(--t-card); color: var(--t-fg);
  padding: 6px 12px; font-size: 13px; border-right: 1px solid var(--t-line);
}
.seg-btn:last-child { border-right: 0; }
.seg-btn:hover { background: var(--t-chip); }
.seg-btn.active { background: var(--t-primary); color: var(--t-on-primary); }
.ctl-kill { margin-left: auto; }
.kill-btn {
  border: 1px solid var(--t-line); background: var(--t-card); color: var(--t-fg);
  border-radius: 8px; padding: 8px 14px; font-size: 13px; font-weight: 600;
}
.kill-btn.engaged {
  background: var(--t-error); color: var(--t-on-primary); border-color: var(--t-error);
}

/* Layout */
.layout { display: grid; gap: 16px; grid-template-columns: 1fr; align-items: start; }
.col-main { min-width: 0; }
.grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); }
.empty { padding: 40px 16px; text-align: center; color: var(--t-muted); }
@media (min-width: 1100px) {
  .layout.has-detail { grid-template-columns: minmax(0, 1fr) minmax(380px, 460px); }
  .layout.has-detail .detail {
    position: sticky; top: 16px;
    max-height: calc(100vh - 32px); overflow: auto;
  }
}

/* Room card */
.card {
  background: var(--t-card); border: 1px solid var(--t-line);
  border-radius: var(--t-radius); padding: 12px 14px;
  display: flex; flex-direction: column; gap: 10px; cursor: pointer;
  transition: border-color .12s ease;
}
.card:hover { border-color: var(--t-primary); }
.card:focus-visible { outline: 2px solid var(--t-primary); outline-offset: 1px; }
.card.selected { border-color: var(--t-primary); box-shadow: 0 0 0 1px var(--t-primary) inset; }
.card.dim { opacity: .6; }
.card-head { display: flex; align-items: center; gap: 8px; }
.dot { width: 11px; height: 11px; border-radius: 50%; flex: none; background: var(--t-muted); }
.dot.sev-ok { background: var(--t-ok); }
.dot.sev-warn { background: var(--t-warn); }
.dot.sev-problem { background: var(--t-error); }
.card-name { font-weight: 600; font-size: 15px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.badge {
  font-size: 11px; padding: 3px 9px; border-radius: 999px;
  border: 1px solid var(--t-line); background: var(--t-chip); color: var(--t-fg);
}
.live-badge.on { background: var(--t-primary); color: var(--t-on-primary); border-color: var(--t-primary); }
.live-badge.off { background: transparent; color: var(--t-muted); }
.card-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.cell { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.cell-cap { font-size: 11px; color: var(--t-muted); text-transform: uppercase; letter-spacing: .04em; }
.cell-sub { font-size: 11px; color: var(--t-muted); }
.big-row { display: flex; align-items: baseline; gap: 8px; }
.big-val { font-size: 24px; font-weight: 600; }
.trend { font-size: 12px; color: var(--t-muted); white-space: nowrap; }
.valve-block { display: flex; flex-direction: column; gap: 4px; }
.valve-line { display: flex; justify-content: space-between; align-items: baseline; }
.valve-val { font-size: 13px; font-weight: 600; }
.valve-track { height: 8px; border-radius: 999px; background: var(--t-chip); overflow: hidden; }
.valve-fill { display: block; height: 100%; width: 0%; background: var(--t-primary); border-radius: 999px; transition: width .3s ease; }
.fast-badge { font-size: 12px; padding: 4px 10px; border-radius: 8px; align-self: flex-start; }
.fast-badge.on { background: color-mix(in srgb, var(--t-accent) 16%, transparent); color: var(--t-accent); border: 1px solid var(--t-accent); }
.fast-badge.off { color: var(--t-muted); border: 1px solid var(--t-line); }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.card-chips { margin-top: 2px; }
.chip { font-size: 11px; padding: 3px 9px; border-radius: 999px; background: var(--t-chip); color: var(--t-fg); }
.chip-warn { background: color-mix(in srgb, var(--t-warn) 20%, transparent); color: var(--t-warn); }
.chip-problem { background: color-mix(in srgb, var(--t-error) 18%, transparent); color: var(--t-error); }

/* Detail */
.detail {
  background: var(--t-card); border: 1px solid var(--t-line);
  border-radius: var(--t-radius); padding: 14px 16px;
  display: flex; flex-direction: column; gap: 16px;
}
.detail-head { display: flex; align-items: center; gap: 8px; }
.detail-title { font-size: 17px; font-weight: 700; }
.sub { display: flex; flex-direction: column; gap: 8px; }
.sub-title { font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: .05em; color: var(--t-muted); border-bottom: 1px solid var(--t-line); padding-bottom: 4px; }

/* Wiring */
.wire-list { display: flex; flex-direction: column; gap: 2px; }
.wire-group-cap { font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: var(--t-muted); margin: 8px 0 2px; }
.wire-row { display: flex; align-items: center; gap: 8px; padding: 6px 8px; border-radius: 8px; }
.wire-row.link { cursor: pointer; }
.wire-row.link:hover { background: var(--t-chip); }
.wire-row.link:focus-visible { outline: 2px solid var(--t-primary); outline-offset: -2px; }
.wire-role { font-size: 12px; color: var(--t-muted); min-width: 92px; }
.wire-id { font-family: ui-monospace, "Roboto Mono", monospace; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1 1 auto; }
.wire-state { font-size: 12px; font-weight: 600; white-space: nowrap; }
.wire-badge { font-size: 10px; padding: 2px 7px; border-radius: 999px; background: var(--t-error); color: var(--t-on-primary); }
.wire-row.wire-bad .wire-state { color: var(--t-error); }
.wire-row .hicon, .wire-row .hicon-fallback { margin-left: auto; }
.wire-row.link .hicon, .wire-row.link .hicon-fallback { opacity: .6; }

/* Decision */
.dec { display: flex; flex-direction: column; gap: 6px; }
.drow { display: flex; justify-content: space-between; gap: 12px; padding: 4px 0; border-bottom: 1px dashed var(--t-line); }
.dlabel { font-size: 13px; color: var(--t-muted); }
.dval { font-size: 13px; font-weight: 600; text-align: right; white-space: nowrap; }
.dec-block { margin: 8px 0; }
.dec-block-cap { font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: var(--t-muted); margin-bottom: 6px; }
.terms { display: flex; flex-direction: column; gap: 6px; }
.term-row { display: grid; grid-template-columns: 120px 1fr 60px; align-items: center; gap: 8px; }
.term-cap { font-size: 12px; color: var(--t-muted); }
.term-track { position: relative; height: 12px; background: var(--t-chip); border-radius: 4px; }
.term-mid { position: absolute; left: 50%; top: 0; bottom: 0; width: 1px; background: var(--t-line); }
.term-fill { position: absolute; top: 1px; bottom: 1px; width: 0%; background: var(--t-primary); border-radius: 3px; }
.term-fill.neg { background: var(--t-accent); }
.term-val { font-size: 12px; font-weight: 600; text-align: right; }
.explanation { font-size: 13px; line-height: 1.5; background: var(--t-chip); border-radius: 8px; padding: 8px 10px; }
.raw { margin-top: 4px; }
.raw summary { cursor: pointer; font-size: 12px; color: var(--t-primary); }
.raw-pre { margin: 8px 0 0; padding: 10px; background: var(--t-chip); border-radius: 8px; font-family: ui-monospace, "Roboto Mono", monospace; font-size: 11px; line-height: 1.5; overflow-x: auto; white-space: pre; }
.history-mount { display: flex; flex-direction: column; gap: 8px; }
.history-soon { font-size: 13px; }

/* Sparkline (room card) */
.spark { height: 40px; margin-top: 2px; }
.spark:empty { display: none; }
.spark-svg { width: 100%; height: 40px; display: block; }
.spark-line { fill: none; stroke: var(--t-primary); stroke-width: 1.5; stroke-linejoin: round; stroke-linecap: round; vector-effect: non-scaling-stroke; }
.spark-area { fill: var(--t-primary); opacity: .1; stroke: none; }
.spark-setpoint { stroke: var(--t-muted); stroke-width: 1; stroke-dasharray: 3 3; opacity: .7; vector-effect: non-scaling-stroke; }
.spark-dot { fill: var(--t-primary); }

/* Detail history chart */
.chart-head { display: flex; align-items: center; gap: 8px; }
.chart-wins { display: inline-flex; border: 1px solid var(--t-line); border-radius: 8px; overflow: hidden; }
.chart-win { border: 0; background: var(--t-card); color: var(--t-fg); padding: 4px 10px; font-size: 12px; border-right: 1px solid var(--t-line); }
.chart-win:last-child { border-right: 0; }
.chart-win:hover { background: var(--t-chip); }
.chart-win.active { background: var(--t-primary); color: var(--t-on-primary); }
.chart-note { text-align: center; padding: 22px 8px; font-size: 13px; }
.chart-scroll { position: relative; overflow-x: auto; overflow-y: hidden; }
.chart-holder { min-width: min-content; }
.chart-svg { display: block; }
.grid-line { stroke: var(--t-line); stroke-width: 1; shape-rendering: crispEdges; }
.axis-label { fill: var(--t-muted); font-size: 10px; font-family: inherit; }
.deadband { fill: var(--t-ok); opacity: .12; stroke: none; }
.s-temp { fill: none; stroke: var(--t-primary); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.s-setpoint { fill: none; stroke: var(--t-muted); stroke-width: 1.5; stroke-dasharray: 5 3; stroke-linejoin: round; }
.s-valve { fill: none; stroke: var(--t-accent); stroke-width: 1.5; stroke-linejoin: round; }
.s-valve-area { fill: var(--t-accent); opacity: .13; stroke: none; }
.chart-guide { stroke: var(--t-muted); stroke-width: 1; stroke-dasharray: 3 3; pointer-events: none; }
.chart-dot { stroke: var(--t-card); stroke-width: 1.5; pointer-events: none; }
.dot-temp { fill: var(--t-primary); }
.dot-setpoint { fill: var(--t-muted); }
.dot-valve { fill: var(--t-accent); }

/* Chart legend (clickable series toggles) */
.chart-legend { display: flex; flex-wrap: wrap; gap: 6px 14px; }
.leg-item { display: inline-flex; align-items: center; gap: 6px; background: transparent; border: 0; padding: 2px 4px; border-radius: 6px; font-family: inherit; }
.leg-item:hover { background: var(--t-chip); }
.leg-item.off { opacity: .45; }
.leg-item.nodata { opacity: .35; cursor: default; }
.leg-sw { width: 14px; height: 4px; border-radius: 2px; flex: none; }
.leg-temp { background: var(--t-primary); }
.leg-setpoint { background: var(--t-muted); }
.leg-valve { background: var(--t-accent); }
.leg-lab { font-size: 12px; color: var(--t-muted); }
.leg-val { font-size: 12px; font-weight: 600; }

/* Chart tooltip */
.chart-tip {
  position: absolute; pointer-events: none; z-index: 3;
  background: var(--t-card); border: 1px solid var(--t-line);
  border-radius: 8px; padding: 6px 8px; min-width: 118px;
  box-shadow: 0 2px 10px rgba(0,0,0,.18); font-size: 12px;
}
.tip-time { font-size: 11px; color: var(--t-muted); margin-bottom: 3px; }
.tip-row { display: flex; align-items: center; gap: 6px; }
.tip-sw { width: 10px; height: 10px; border-radius: 2px; flex: none; }
.tip-sw-blank { background: transparent; }
.tip-lab { color: var(--t-muted); }
.tip-val { margin-left: auto; font-weight: 600; }
`;

if (!customElements.get("tortoise-ufh-panel")) {
  customElements.define("tortoise-ufh-panel", TortoiseUfhPanel);
}
