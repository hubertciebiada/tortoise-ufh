/**
 * Tortoise-UFH sidebar panel — self-contained vanilla-JS ES module.
 *
 * A dependency-free custom element (no CDN / external imports, CSP-safe) that
 * renders the per-room underfloor-heating control state and lets an admin edit
 * the global home temperature, mode, kill-switch, and per-room offset /
 * participation. It talks to the integration exclusively through the
 * `tortoise_ufh/*` websocket commands.
 *
 * Units: temperatures in degrees Celsius, offsets in kelvin, valve in percent
 * (0..100), trend in kelvin per hour.
 *
 * Home Assistant sets `.hass` (and `.narrow` / `.route`) on the element. Every
 * websocket call is wrapped in try/catch: a failure shows a banner and is never
 * allowed to throw out of an event handler or the poll loop.
 */

const POLL_INTERVAL_MS = 5000;

const MODE_OPTIONS = [
  ["heating", "Heating"],
  ["transitional", "Transitional"],
  ["cooling", "Cooling"],
  ["off", "Off"],
];

/** Coerce a value to a finite number, or return null. */
function num(value) {
  const n = typeof value === "string" ? parseFloat(value) : value;
  return typeof n === "number" && Number.isFinite(n) ? n : null;
}

/** Format a number to `digits` decimals, or a dash when null/undefined. */
function fmt(value, digits, suffix) {
  const n = num(value);
  if (n === null) {
    return "—";
  }
  return n.toFixed(digits) + (suffix || "");
}

/** Return the first key present (not undefined) in `obj`, else `fallback`. */
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

class TortoiseUfhPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._config = null; // last get_config payload
    this._live = null; // last get_live payload
    this._selectedRoom = null;
    this._error = "";
    this._timer = null;
    this._built = false;
  }

  /** HA assigns this on every state change; keep the latest handle. */
  set hass(hass) {
    const first = this._hass === null;
    this._hass = hass;
    this._applyTheme();
    if (first) {
      this._loadConfig();
      this._poll();
    }
  }

  get hass() {
    return this._hass;
  }

  connectedCallback() {
    if (!this._built) {
      this._build();
      this._built = true;
    }
    if (this._timer === null) {
      this._timer = window.setInterval(() => this._poll(), POLL_INTERVAL_MS);
    }
    if (this._hass) {
      this._loadConfig();
      this._poll();
    }
  }

  disconnectedCallback() {
    if (this._timer !== null) {
      window.clearInterval(this._timer);
      this._timer = null;
    }
  }

  /** Lovelace-style sizing hint (unused as a panel, provided for parity). */
  getCardSize() {
    const rooms = this._config && Array.isArray(this._config.rooms)
      ? this._config.rooms.length
      : 3;
    return 4 + rooms;
  }

  // -- Websocket helpers ---------------------------------------------------

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
      this._setError(`${message.type} failed: ${detail}`);
      return null;
    }
  }

  async _loadConfig() {
    const cfg = await this._callWS({ type: "tortoise_ufh/get_config" });
    if (cfg) {
      this._config = cfg;
      this._render();
    }
  }

  async _poll() {
    const live = await this._callWS({ type: "tortoise_ufh/get_live" });
    if (live) {
      this._live = live;
      this._render();
    }
  }

  async _setHomeTemperature(value) {
    const t = num(value);
    if (t === null) {
      return;
    }
    await this._callWS({
      type: "tortoise_ufh/set_home_temperature",
      temperature: t,
    });
    this._poll();
  }

  async _setMode(mode) {
    await this._callWS({ type: "tortoise_ufh/set_mode", mode });
    this._loadConfig();
    this._poll();
  }

  async _setKillSwitch(engaged) {
    await this._callWS({
      type: "tortoise_ufh/set_kill_switch",
      engaged: !!engaged,
    });
    this._loadConfig();
    this._poll();
  }

  async _setRoomOffset(room, value) {
    const offset = num(value);
    if (offset === null) {
      return;
    }
    await this._callWS({
      type: "tortoise_ufh/set_room_offset",
      room,
      offset,
    });
    this._loadConfig();
    this._poll();
  }

  async _setRoomEnabled(room, enabled) {
    await this._callWS({
      type: "tortoise_ufh/set_room_enabled",
      room,
      enabled: !!enabled,
    });
    this._loadConfig();
    this._poll();
  }

  // -- Data shaping --------------------------------------------------------

  /** Merge config + live into a normalized list of room view-models. */
  _rooms() {
    const cfgRooms = this._config && Array.isArray(this._config.rooms)
      ? this._config.rooms
      : [];
    const live = this._live || {};
    const liveRooms = pick(live, ["rooms"], {}) || {};
    const building = pick(live, ["building"], {}) || {};
    const buildingRooms = pick(building, ["rooms"], {}) || {};
    const homeTemp = num(
      pick(this._config || {}, ["home_setpoint_c"], null),
    );

    const names = new Set();
    for (const r of cfgRooms) {
      names.add(pick(r, ["name"], ""));
    }
    for (const key of Object.keys(liveRooms)) {
      names.add(key);
    }

    const rows = [];
    for (const name of names) {
      if (!name) {
        continue;
      }
      const cfg = cfgRooms.find((r) => pick(r, ["name"], "") === name) || {};
      const lv = liveRooms[name] || {};
      const bl = buildingRooms[name] || {};
      const report = pick(lv, ["report"], pick(bl, ["report"], {})) || {};
      const fast = pick(lv, ["fast_source"], pick(bl, ["fast_source"], {})) || {};

      const offset = num(pick(cfg, ["offset_c", "offset"], 0));
      const setpoint = num(
        pick(lv, ["setpoint_c"],
          homeTemp !== null && offset !== null ? homeTemp + offset : null),
      );
      const errorC = num(pick(report, ["error_c"], null));
      // room_temp = setpoint - error (heating sign convention).
      const current = setpoint !== null && errorC !== null
        ? setpoint - errorC
        : null;

      rows.push({
        name,
        offset,
        setpoint,
        current,
        errorC,
        valve: num(pick(lv, ["valve_position_pct"],
          pick(bl, ["valve_position_pct"], null))),
        fastOn: !!pick(fast, ["on"], false),
        fastMode: pick(fast, ["mode"], "off"),
        live: !!pick(lv, ["live_control_enabled"],
          pick(cfg, ["live_control", "live_control_enabled"], false)),
        cooling: !!pick(cfg, ["cooling_enabled"], false),
        flags: Array.isArray(report.flags) ? report.flags : [],
        explanation: pick(report, ["explanation"], ""),
        report,
      });
    }
    rows.sort((a, b) => a.name.localeCompare(b.name));
    return rows;
  }

  // -- Rendering -----------------------------------------------------------

  _applyTheme() {
    if (!this._hass || !this.shadowRoot) {
      return;
    }
    const dark = !!(this._hass.themes && this._hass.themes.darkMode);
    this.shadowRoot.host.setAttribute("data-theme", dark ? "dark" : "light");
  }

  _setError(msg) {
    if (msg === this._error) {
      return;
    }
    this._error = msg;
    const banner = this.shadowRoot && this.shadowRoot.getElementById("banner");
    if (banner) {
      banner.textContent = msg;
      banner.style.display = msg ? "block" : "none";
    }
  }

  _build() {
    const style = document.createElement("style");
    style.textContent = STYLE;
    const root = document.createElement("div");
    root.className = "wrap";
    root.innerHTML = `
      <div id="banner" class="banner" style="display:none"></div>
      <div id="header" class="header"></div>
      <table class="rooms">
        <thead><tr>
          <th>Room</th><th>Temp</th><th>Setpoint</th><th>Error</th>
          <th>Valve</th><th>Fast source</th><th>Offset</th>
          <th>Live</th><th>Status</th>
        </tr></thead>
        <tbody id="rows"></tbody>
      </table>
      <div class="report">
        <div class="report-title" id="report-title">Report</div>
        <pre id="report-body" class="report-body">Select a room to inspect its report.</pre>
      </div>`;
    this.shadowRoot.append(style, root);
    this._applyTheme();
    this._render();
  }

  /** Avoid clobbering a field the user is actively editing. */
  _isEditing() {
    const active = this.shadowRoot && this.shadowRoot.activeElement;
    if (!active) {
      return false;
    }
    const tag = active.tagName;
    return tag === "INPUT" || tag === "SELECT";
  }

  _render() {
    if (!this.shadowRoot || !this._built) {
      return;
    }
    const banner = this.shadowRoot.getElementById("banner");
    if (banner) {
      banner.textContent = this._error;
      banner.style.display = this._error ? "block" : "none";
    }
    this._renderHeader();
    if (!this._isEditing()) {
      this._renderRows();
    }
    this._renderReport();
  }

  _renderHeader() {
    const header = this.shadowRoot.getElementById("header");
    if (!header || this._isEditing()) {
      return;
    }
    const live = this._live || {};
    const cfg = this._config || {};
    const homeTemp = num(pick(cfg, ["home_setpoint_c"], null));
    const mode = pick(cfg, ["mode"], pick(live, ["mode"], "heating"));
    const kill = !!pick(cfg, ["kill_switch"], pick(live, ["kill_switch"], false));
    const status = pick(live, ["algorithm_status"], "—");
    const watchdog = pick(live, ["watchdog_state"], "—");
    const dew = num(pick(live, ["global_safe_dew_point_c"], null));

    const options = MODE_OPTIONS.map(
      ([v, label]) =>
        `<option value="${v}"${v === mode ? " selected" : ""}>${label}</option>`,
    ).join("");

    header.innerHTML = `
      <div class="ctl">
        <label>Home temperature</label>
        <input id="home-temp" type="number" min="5" max="30" step="0.5"
          value="${homeTemp === null ? "" : homeTemp}" />
        <span class="unit">°C</span>
      </div>
      <div class="ctl">
        <label>Mode</label>
        <select id="mode">${options}</select>
      </div>
      <div class="ctl">
        <label>Kill switch</label>
        <input id="kill" type="checkbox"${kill ? " checked" : ""} />
        <span class="hint">${kill ? "engaged (no commands)" : "off"}</span>
      </div>
      <div class="ctl status">
        <span class="badge">algo: ${status}</span>
        <span class="badge">watchdog: ${watchdog}</span>
        <span class="badge">safe dew: ${fmt(dew, 1, " °C")}</span>
      </div>`;

    const homeInput = header.querySelector("#home-temp");
    homeInput.addEventListener("change", (e) =>
      this._setHomeTemperature(e.target.value));
    header.querySelector("#mode").addEventListener("change", (e) =>
      this._setMode(e.target.value));
    header.querySelector("#kill").addEventListener("change", (e) =>
      this._setKillSwitch(e.target.checked));
  }

  _renderRows() {
    const tbody = this.shadowRoot.getElementById("rows");
    if (!tbody) {
      return;
    }
    const rows = this._rooms();
    tbody.innerHTML = "";
    if (rows.length === 0) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td colspan="9" class="empty">No rooms configured.</td>`;
      tbody.appendChild(tr);
      return;
    }
    for (const r of rows) {
      const tr = document.createElement("tr");
      tr.className = r.name === this._selectedRoom ? "selected" : "";
      const fast = r.fastOn ? `on / ${r.fastMode}` : "off";
      const status = r.flags.length ? r.flags.join(", ") : "ok";
      tr.innerHTML = `
        <td class="name">${escapeHtml(r.name)}</td>
        <td>${fmt(r.current, 1)}</td>
        <td>${fmt(r.setpoint, 1)}</td>
        <td>${fmt(r.errorC, 2)}</td>
        <td>${fmt(r.valve, 0, "%")}</td>
        <td>${fast}</td>
        <td><input class="offset" type="number" min="-5" max="5" step="0.5"
              value="${r.offset === null ? "" : r.offset}" /></td>
        <td><input class="live" type="checkbox"${r.live ? " checked" : ""} /></td>
        <td class="statuscell" title="${escapeHtml(r.explanation)}">${escapeHtml(status)}</td>`;

      tr.addEventListener("click", (e) => {
        if (e.target.tagName === "INPUT") {
          return;
        }
        this._selectedRoom = r.name;
        this._render();
      });
      const offsetInput = tr.querySelector(".offset");
      offsetInput.addEventListener("click", (e) => e.stopPropagation());
      offsetInput.addEventListener("change", (e) =>
        this._setRoomOffset(r.name, e.target.value));
      const liveInput = tr.querySelector(".live");
      liveInput.addEventListener("click", (e) => e.stopPropagation());
      liveInput.addEventListener("change", (e) =>
        this._setRoomEnabled(r.name, e.target.checked));
      tbody.appendChild(tr);
    }
  }

  _renderReport() {
    const title = this.shadowRoot.getElementById("report-title");
    const body = this.shadowRoot.getElementById("report-body");
    if (!title || !body) {
      return;
    }
    if (!this._selectedRoom) {
      title.textContent = "Report";
      body.textContent = "Select a room to inspect its report.";
      return;
    }
    const room = this._rooms().find((r) => r.name === this._selectedRoom);
    title.textContent = `Report — ${this._selectedRoom}`;
    if (!room) {
      body.textContent = "No live data for this room yet.";
      return;
    }
    let text;
    try {
      text = JSON.stringify(room.report, null, 2);
    } catch (err) {
      text = String(room.report);
    }
    body.textContent = text || "(empty report)";
  }
}

/** Minimal HTML escaping for interpolated text nodes/attributes. */
function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

const STYLE = `
:host {
  --bg: #fafafa; --fg: #212121; --muted: #666; --line: #e0e0e0;
  --card: #ffffff; --accent: #0277bd; --sel: #e1f5fe; --err: #b00020;
  --badge: #eeeeee;
  display: block; color: var(--fg); background: var(--bg);
  font-family: Roboto, system-ui, sans-serif; min-height: 100%;
}
:host([data-theme="dark"]) {
  --bg: #121212; --fg: #e0e0e0; --muted: #9e9e9e; --line: #333;
  --card: #1e1e1e; --accent: #4fc3f7; --sel: #0d3446; --err: #cf6679;
  --badge: #2a2a2a;
}
.wrap { max-width: 1100px; margin: 0 auto; padding: 16px; }
.banner {
  background: var(--err); color: #fff; padding: 8px 12px; border-radius: 6px;
  margin-bottom: 12px; font-size: 13px; word-break: break-word;
}
.header {
  display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-end;
  background: var(--card); border: 1px solid var(--line); border-radius: 8px;
  padding: 12px 16px; margin-bottom: 16px;
}
.ctl { display: flex; flex-direction: column; gap: 4px; }
.ctl.status { flex-direction: row; align-items: center; flex-wrap: wrap; gap: 6px; }
.ctl label { font-size: 12px; color: var(--muted); }
.ctl .unit, .ctl .hint { font-size: 12px; color: var(--muted); }
.badge {
  background: var(--badge); border-radius: 12px; padding: 3px 10px; font-size: 12px;
}
input[type="number"] {
  width: 80px; padding: 4px 6px; border: 1px solid var(--line);
  border-radius: 4px; background: var(--card); color: var(--fg);
}
select {
  padding: 4px 6px; border: 1px solid var(--line); border-radius: 4px;
  background: var(--card); color: var(--fg);
}
table.rooms {
  width: 100%; border-collapse: collapse; background: var(--card);
  border: 1px solid var(--line); border-radius: 8px; overflow: hidden;
  font-size: 13px; display: table;
}
.rooms th, .rooms td {
  padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--line);
  white-space: nowrap;
}
.rooms th { color: var(--muted); font-weight: 600; font-size: 12px; }
.rooms tbody tr { cursor: pointer; }
.rooms tbody tr:hover { background: var(--sel); }
.rooms tbody tr.selected { background: var(--sel); }
.rooms .name { font-weight: 600; }
.rooms .offset { width: 64px; }
.rooms .empty, .rooms .statuscell { color: var(--muted); }
.report {
  margin-top: 16px; background: var(--card); border: 1px solid var(--line);
  border-radius: 8px; overflow: hidden;
}
.report-title {
  padding: 10px 14px; font-weight: 600; border-bottom: 1px solid var(--line);
}
.report-body {
  margin: 0; padding: 14px; font-family: "Roboto Mono", ui-monospace, monospace;
  font-size: 12px; line-height: 1.5; overflow-x: auto; white-space: pre;
}
`;

if (!customElements.get("tortoise-ufh-panel")) {
  customElements.define("tortoise-ufh-panel", TortoiseUfhPanel);
}
