/**
 * Tortoise-UFH sidebar panel — self-contained vanilla-JS ES module.
 *
 * A dependency-free custom element (no CDN / external imports / build step,
 * CSP-safe) that renders the per-room underfloor-heating control state as a
 * health hero + a tabbed workspace (the Overview dashboard — the default tab —
 * then the Rooms table, Flags, Tuning, Manifolds, Assist and Heat pump) + a
 * master/detail room inspector (a sticky side column on wide
 * screens, a right-side overlay drawer on narrow ones), and lets an admin
 * edit the global home temperature, mode, per-room offset and each room's
 * two-state control (off / live). Tuning knobs and the harder decision
 * fields carry an "i" info button feeding ONE shared, CSP-safe tooltip
 * element (hover + keyboard focus + tap). It talks to the integration
 * exclusively through the `tortoise_ufh/*` websocket commands (get_config /
 * get_live / set_*).
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

// Brand icon served by panel.py next to this module; the header falls back to
// the mdi/glyph mark when the image fails to load (e.g. dev preview).
const BRAND_ICON_URL = "/tortoise_ufh_panel/panel-icon.png";

const MODES = ["heating", "transitional", "cooling", "off"];

/** Canonical per-room control state (mirrors the adapter's `ROOM_STATES`). */
const STATE_OFF = "off";
const STATE_LIVE = "live";
const ROOM_STATES = [STATE_OFF, STATE_LIVE];

/**
 * Two-state segment control metadata (column 1 of the room table).
 *
 * One button per state, rendered as an icon with a tooltip; the active state's
 * button is highlighted. Icons resolve through `_icon` (native `<ha-icon>` when
 * available, else a plain-glyph fallback — CSP-safe either way). (The former
 * third state `shadow` was removed 2026-07-12, v0.7.0; migration maps it to
 * `off`.)
 */
const STATE_META = [
  { state: STATE_OFF, icon: "mdi:power", glyph: "⏻", key: "state_off" },
  { state: STATE_LIVE, icon: "mdi:play", glyph: "▶", key: "state_live" },
];

/** Top-level tabs. Order is authoritative for keyboard navigation. */
const TABS = [
  { key: "overview", label: "tab_overview" },
  { key: "rooms", label: "tab_rooms" },
  { key: "flags", label: "tab_flags" },
  { key: "tuning", label: "tab_tuning" },
  { key: "manifolds", label: "tab_manifolds" },
  { key: "assist", label: "tab_assist" },
  { key: "hp", label: "tab_hp" },
];
const TAB_ORDER = TABS.map((t) => t.key);
const DEFAULT_TAB = "overview";
const ACTIVE_TAB_STORAGE_KEY = "tortoise-ufh.activeTab";

/** Width below which the table drops its Error and Assist columns. */
const NARROW_MAX_PX = 560;

/** Width at or below which the room detail becomes a right-side overlay drawer. */
const OVERLAY_MAX_PX = 1099;

/** Websocket command type strings (frozen backend contract). */
const WS = {
  getConfig: "tortoise_ufh/get_config",
  getLive: "tortoise_ufh/get_live",
  setHome: "tortoise_ufh/set_home_temperature",
  setOffset: "tortoise_ufh/set_room_offset",
  setRoomState: "tortoise_ufh/set_room_state",
  setMode: "tortoise_ufh/set_mode",
  getTuning: "tortoise_ufh/get_tuning",
  setTuning: "tortoise_ufh/set_tuning",
  setHpDhw: "tortoise_ufh/set_hp_dhw",
  previewHeatingCurve: "tortoise_ufh/preview_heating_curve",
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
 * MILLISECONDS) so a week of data stays small. `bucketMs` down-samples the
 * drawn temperature series to bucket means (~180 points per chart,
 * presentation only — the recorder keeps every sample); the 7d window is
 * already hourly means and needs none.
 */
const WINDOWS = {
  "6h": { hours: 6, mode: "history", bucketMs: 2 * 60 * 1000 },
  "24h": { hours: 24, mode: "history", bucketMs: 8 * 60 * 1000 },
  "7d": { hours: 168, mode: "stats", period: "hour", periodMs: 3600 * 1000 },
};
const WINDOW_ORDER = ["6h", "24h", "7d"];
/** Default window used as a fallback when an unknown window key is requested. */
const SPARK_WINDOW = "24h";

/** Cached history is reused for this long before a background refetch. */
const HIST_MAXAGE_MS = 60 * 1000;

/** Detail-chart geometry (pixels; width is measured at draw time). The
 * bottom margin holds the split-mode band (8 px) above the time labels. */
const CHART_H = 250;
const CHART_MARGIN = { l: 42, r: 46, t: 12, b: 36 };
const CHART_MIN_W = 300;
const CHART_MAX_W = 900;

// --- Overview (dashboard) ---------------------------------------------------

/**
 * Comfort-band half-width [K] used until the tuning payload arrives — the
 * `ControllerConfig.deadband_c` library default. Afterwards every room uses
 * its effective `deadband_c` (global value or per-room override).
 */
const OV_BAND_FALLBACK_K = 0.3;

/**
 * House charts: 24 h of 5-minute recorder statistics for every room's
 * `error_c` and `recommended_valve` diagnostic sensors (plus the outdoor
 * sensor) in ONE websocket call, refetched at most once per control cycle
 * while the Overview is visible.
 */
const OV_HIST_HOURS = 24;
const OV_HIST_PERIOD = "5minute";
const OV_HIST_PERIOD_MS = 5 * 60 * 1000;
const OV_HIST_MAXAGE_MS = 5 * 60 * 1000;

/** Shared deviation scale [K] of the comfort strip and the deviation chart. */
const OV_SCALE_MIN_K = 1.0;
const OV_SCALE_MAX_K = 3.0;

/**
 * Comfort-strip dots closer than one dot width (`OV_SWARM_DOT_PX` of axis)
 * stack upwards; `OV_SWARM_GAP` is the axis fraction used before the strip
 * is laid out. A crowd stacks at most `OV_SWARM_MAX_LEVELS` steps high —
 * beyond that the steps compress, so the hero never grows.
 */
const OV_SWARM_DOT_PX = 15;
const OV_SWARM_GAP = 0.045;
/** Vertical step [px] between stacked comfort-strip dots. */
const OV_SWARM_STEP_PX = 13;
const OV_SWARM_MAX_LEVELS = 3;

/** Attention entries the Overview hero lists; the rest sit behind "+N more". */
const OV_ATT_MAX = 3;

// --- i18n -------------------------------------------------------------------

const STR = {
  pl: {
    status_running: "Działa · cykl {age}",
    status_stale: "Nieaktualne",
    status_error: "Błąd algorytmu",
    status_nodata: "Brak danych",
    age_sec: "{s} s temu",
    age_min_sec: "{m} min {s} s temu",
    age_min: "{m} min temu",
    age_hour_min: "{h} h {m} min temu",
    age_unknown: "brak znacznika czasu",
    rooms_live_cap: "Steruje",
    flags_cap: "Flagi",
    flag_legend_title: "Flagi — stan zabezpieczeń",
    flag_legend_info:
      "Wszystkie flagi diagnostyczne. Wyszarzone są nieaktywne; zapalone (w kolorze) " +
      "zadziałały w co najmniej jednym pokoju. Kliknij ikonę „i” przy fladze po pełne " +
      "objaśnienie. Pełny słownik jest też w instrukcji (docs/manual/pl.md §12).",
    flag_grp_safety: "Bezpieczeństwo (S1–S5)",
    flag_grp_assist: "Wspomaganie (split)",
    flag_grp_config: "Konfiguracja / stan",
    flag_grp_other: "Pozostałe",
    flag_active_in: "Aktywna w: ",
    flag_active: "aktywna",
    flag_none_active: "Brak aktywnych flag",
    flag_desc_unknown: "Nowa lub nieopisana flaga — patrz instrukcja (docs/manual/pl.md §12).",
    dew_cap: "Bezpieczny punkt rosy",
    home_cap: "Temperatura domu",
    mode_cap: "Tryb",
    mode_heating: "Grzanie",
    mode_transitional: "Przejściowy",
    mode_cooling: "Chłodzenie",
    mode_off: "Wył.",
    tab_rooms: "Pokoje",
    tab_flags: "Flagi",
    tab_tuning: "Strojenie",
    tab_assist: "Wspomaganie",
    tab_hp: "Pompa ciepła",
    tab_manifolds: "Rozdzielacze",
    tab_overview: "Przegląd",
    // Przegląd (dashboard)
    ov_comfort_cap: "Komfort",
    ov_comfort_sub: "sterowanych pokoi w paśmie komfortu",
    ov_none_live: "Żaden pokój nie jest w stanie „Steruje”.",
    ov_cold: "za zimno",
    ov_warm: "za ciepło",
    ov_nodata: "bez odczytu",
    ov_off: "wyłączone",
    ov_no_cool: "bez chłodzenia",
    ov_house_off: "Tryb domu: {mode} — nic nie jest sterowane.",
    ov_band_cap: "pasmo komfortu ±{v} K",
    tip_ov_comfort:
      "Pokój jest w paśmie komfortu, gdy pomiar odbiega od zadanej najwyżej o " +
      "strefę nieczułości regulatora (parametr „Strefa nieczułości wokół " +
      "zadanej”, globalny lub per pokój). Liczą się tylko pokoje sterowane: w " +
      "stanie „Steruje”, w chłodzeniu tylko uczestniczące w chłodzeniu, a w " +
      "trybie domu „Wył.” żaden. Na osi każda kropka to jeden pokój: w lewo " +
      "zimniej, w prawo cieplej niż zadana; zielony pas to pasmo globalne (pokój " +
      "z własnym pasmem ocenia się według swojego). Kliknij kropkę, żeby " +
      "otworzyć szczegóły pokoju.",
    ov_dev_below: "{v} K poniżej zadanej",
    ov_dev_above: "{v} K powyżej zadanej",
    ov_dev_ok: "w paśmie komfortu",
    ov_att_cap: "Wymaga uwagi",
    ov_att_ok: "Wszystko w porządku",
    ov_att_ok_sub: "Algorytm działa, żadna flaga nie wymaga reakcji.",
    ov_att_info: "Informacyjnie:",
    ov_att_more: "+{n} więcej w zakładce Flagi",
    ov_dev_cap: "Odchyłka od zadanej",
    ov_inband_sub: "czasu w paśmie komfortu (24 godz.)",
    ov_fan_tip: "mediana {mid} · rozrzut {lo} … {hi}",
    tip_ov_dev:
      "Ostatnie 24 godziny pokoi w stanie „Steruje” (średnie 5-minutowe z " +
      "recordera; w górę = cieplej niż zadana). Linia to mediana odchyłki, szare " +
      "pole — rozrzut od najzimniejszego do najcieplejszego pokoju, zielony pas — " +
      "pasmo komfortu; czerwone i niebieskie pola to rozrzut ponad i pod pasmem. " +
      "Liczba: jaką część czasu pokoje spędziły w swoim paśmie komfortu. " +
      "Szczegóły pokoi są na zakładce Pokoje.",
    ov_minmax_24h: "min. {min} · maks. {max} (24 godz.)",
    ov_floor_cap: "Podłogówka",
    ov_floor_sub: "średnie otwarcie zaworów",
    ov_loops_open: "otwarte pętle: {n} z {m}",
    tip_ov_floor:
      "Średnie otwarcie zaworów pokoi w stanie „Steruje”, ważone liczbą pętli " +
      "pokoju (pokój z trzema pętlami waży trzy razy więcej). Pętla liczy się " +
      "jako otwarta od 5 %. Wykres: ta sama średnia z ostatnich 24 godzin " +
      "(średnie 5-minutowe z recordera).",
    ov_assist_note: "pracuje teraz",
    ov_assist_idle: "wszystkie w spoczynku",
    ov_assist_manual: "sterowanie ręczne: {n}",
    ov_dhw: "CWU",
    ov_dew_cap: "Ochrona przed rosą",
    ov_dew_worst: "najwyższy punkt rosy w pokojach: {v}",
    ov_dew_throttled: "dławione pokoje: {n}",
    ov_plant_cap: "Instalacja",
    manage_rooms: "Zarządzaj pokojami",
    menu_btn: "Menu boczne",
    loading: "Ładowanie…",
    no_rooms: "Brak skonfigurowanych pokoi.",
    th_room: "Pokój",
    th_measured: "Pomiar",
    th_setpoint: "Zadana",
    th_error: "Uchył",
    th_valve: "Zawór",
    th_supply: "Zasilanie",
    th_return: "Powrót",
    th_assist_mode: "Tryb",
    th_assist_temp: "Temp.",
    th_assist_group: "Wspomaganie",
    th_state: "Sterowanie",
    card_setpoint: "Zadana",
    card_offset: "korekta {v}",
    state_off: "Wyłączony",
    state_live: "Steruje",
    confirm_state_live:
      "Włączyć sterowanie pokojem „{room}”? Tortoise zacznie zapisywać " +
      "komendy do zaworów i wspomagania.",
    confirm_state_off:
      "Wyłączyć sterowanie pokojem „{room}”? Siłowniki zostaną bezpiecznie " +
      "zaparkowane i nic więcej nie będzie zapisywane.",
    confirm_mode: "Zmienić tryb domu na „{mode}”? Dotyczy wszystkich pokoi.",
    confirm_yes: "Tak, zmień",
    confirm_cancel: "Anuluj",
    assist_split: "Split",
    assist_heater: "Grzałka",
    assist_none: "—",
    assist_no_source: "brak wspomagania",
    assist_state_off: "wył.",
    assist_state_heating: "grzeje",
    assist_state_cooling: "chłodzi",
    assist_state_dry: "osusza",
    kind_split: "Klimatyzator (split)",
    kind_heater: "Grzałka elektryczna",
    detail_close: "Zamknij",
    sec_wiring: "Czujniki i sygnały",
    wire_source_tip: "Pokaż źródło (encję)",
    sec_decision: "Decyzja regulatora",
    sec_history: "Historia",
    sec_diagnostics: "Encje diagnostyczne",
    chart_temp: "Temperatura",
    chart_setpoint: "Zadana",
    chart_valve: "Zawór",
    chart_hum: "Wilgotność",
    chart_error: "Uchyb",
    chart_split: "Wspomaganie",
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
    yes: "tak",
    no: "nie",
    raw_show: "Pokaż surowe dane",
    no_live_room: "Brak danych na żywo dla tego pokoju.",
    tune_intro:
      "Globalne nastawy regulatora oraz rzadkie nadpisania per pokój. " +
      "Zapisywane są tylko wartości różne od domyślnych — nastawa równa " +
      "domyślnej podąża za przyszłymi zmianami domyślnych. " +
      "Zapis przeładowuje regulator (integrator PID zostaje wyzerowany).",
    tune_scope_global: "Globalne",
    tune_save: "Zapisz",
    tune_saving: "Zapisywanie…",
    tune_saved: "Zapisano",
    tune_overridden: "nadpisane",
    tune_revert: "Wróć do globalnej",
    tune_custom: "własne",
    tune_reset_default: "Przywróć domyślną",
    tune_old_default: "stara domyślna",
    tune_old_default_tip:
      "Ta wartość to dawna wartość domyślna, zapisana na stałe przez " +
      "starszą wersję integracji — nie Twój wybór. Przywróć domyślną, " +
      "chyba że ustawiłeś ją celowo.",
    tune_on: "wł.",
    tune_off: "wył.",
    tune_grp_pid: "Regulator PI i trend",
    tune_grp_band: "Pasmo komfortu i zawór",
    tune_grp_weather: "Człon pogodowy (otwarcie zaworu)",
    tune_grp_fast: "Szybkie źródło (split/grzałka)",
    tune_grp_dew: "Ochrona przed kondensacją (chłodzenie)",
    tune_grp_cooling_water: "Woda chłodzenia — pompa ciepła (globalne)",
    tune_grp_heating_water: "Woda grzania — pompa ciepła (globalne)",
    tune_grp_other: "Pozostałe",
    tune_kp: "Wzmocnienie części proporcjonalnej (P)",
    tune_ki: "Wzmocnienie części całkującej (I)",
    tune_kt: "Tłumienie trendu temperatury",
    tune_deadband_c: "Strefa nieczułości wokół zadanej",
    tune_valve_floor_pct: "Minimalne otwarcie zaworu przy grzaniu",
    tune_boost_offset_c: "Próg włączenia wspomagania",
    tune_fast_min_on_minutes: "Minimalny czas pracy wspomagania",
    tune_fast_min_off_minutes: "Minimalny czas postoju wspomagania",
    tune_fast_target_offset_k: "Podbicie nastawy wspomagania",
    tune_dry_enabled: "Osuszanie wspomagające",
    tune_dry_dew_max_c: "Próg rosy osuszania",
    tune_fast_manual_hold_minutes: "Czas ręcznego sterowania wspomaganiem",
    tune_dew_margin_k: "Margines bezpieczeństwa nad punktem rosy",
    tune_dew_ramp_k: "Szerokość rampy dławienia chłodzenia",
    tune_outdoor_ff_enabled: "Człon pogodowy włączony",
    tune_ff_neutral_c: "Temperatura neutralna członu pogodowego",
    tune_ff_gain_pct_per_k: "Wzmocnienie członu pogodowego",
    tune_ff_max_pct: "Górny limit członu pogodowego",
    tune_cooling_supply_base_c: "Bazowa temperatura wody chłodzenia",
    tune_heating_supply_base_c: "Bazowa temperatura wody grzania",
    tune_heating_supply_slope: "Nachylenie krzywej wody grzania",
    tune_heating_curve_neutral_c: "Temperatura neutralna krzywej wody grzania",
    tune_heating_supply_min_c: "Minimalna temperatura wody grzania",
    tune_heating_supply_max_c: "Maksymalna temperatura wody grzania",
    tune_heating_gate_enabled: "Czekaj na bezpieczny stan sprężarki",
    tune_heating_curve_hysteresis_k: "Histereza krzywej wody grzania",
    tune_heating_restart_band_k: "Pasmo ponownego startu pompy",
    tune_heating_stop_offset_k: "Próg zatrzymania pompy",
    test_running_min: "~{m} min",
    ast_th_kind: "Rodzaj",
    ast_th_group: "Grupa",
    ast_th_command: "Komenda",
    ast_th_actual: "Stan rzeczywisty",
    ast_th_timer: "Timer",
    ast_th_hours: "Dozwolone godziny",
    ast_hours_always: "zawsze",
    assist_window_sub: "dozwolone {start}–{end}",
    ast_th_flags: "Flagi",
    ast_th_entity: "Encja",
    ast_none_line: "Bez wspomagania: {rooms}",
    ast_timer_unlock: "odblokuje się za ~{n} min",
    ast_timer_locked: "blokada min. czasu",
    ast_timer_conflict: "konflikt grupy",
    ast_tune_link: "dostrój",
    ast_actual_unknown: "—",
    ast_empty: "Żaden pokój nie ma szybkiego źródła.",
    dew_reason_no_humidity: "brak czujnika wilgotności",
    dew_reason_cooling_disabled: "chłodzenie wyłączone",
    dew_reason_not_cooling_mode: "pokój nie chłodzi",
    dew_reason_no_temperature: "brak pomiaru temperatury",
    dew_none_collective: "{n} pokoi bez danych do punktu rosy",
    dec_dew_reason: "Powód braku punktu rosy",
    err_ws: "Błąd połączenia z integracją: {detail}",
    tooltip_show: "Pokaż objaśnienie",
    tooltip_hide: "Ukryj objaśnienie",
    // Tooltipy knobów strojenia (tip_knob_*) — treść wspólna z
    // docs/manual/pl.md §8: zmieniaj OBA miejsca razem.
    tip_knob_kp:
      "Wzmocnienie proporcjonalne: ile procent otwarcia zaworu dodaje każdy " +
      "1 K uchybu (zadana − zmierzona). Wyższe = szybsza reakcja, ale większe " +
      "ryzyko przeregulowania bezwładnej podłogi. Domyślnie 14; typowo 8–20. " +
      "Ruszaj małymi krokami i obserwuj dobę.",
    tip_knob_ki:
      "Wzmocnienie całkujące: jak szybko regulator dokłada otwarcie, gdy mały " +
      "uchyb utrzymuje się godzinami — usuwa trwały niedogrzew. Wartości są " +
      "celowo bardzo małe (sekundy w mianowniku). Domyślnie 0,0015. Za duże " +
      "ki = powolne bujanie temperatury wokół zadanej.",
    tip_knob_kt:
      "Człon trendu (tłumienie): patrzy na tempo zmian temperatury. Gdy pokój " +
      "szybko zmierza do zadanej, przymyka zawór zawczasu i ogranicza " +
      "przestrzelenie bezwładnej podłogi. Domyślnie 12. Zwykle nie wymaga zmian.",
    tip_knob_deadband_c:
      "Strefa nieczułości wokół zadanej (±): wewnątrz niej regulator nie goni " +
      "drobnych odchyłek, co zmniejsza ruchy zaworów i taktowanie. Domyślnie " +
      "0,3 K; typowo 0,2–0,5 K.",
    tip_knob_valve_floor_pct:
      "Minimalne otwarcie zaworu, gdy pokój wzywa ciepło: nie schodzimy " +
      "poniżej tej wartości, żeby w pętli był sensowny przepływ. Domyślnie " +
      "15 %. 0 wyłącza minimum.",
    tip_knob_boost_offset_c:
      "Próg dogrzewu: gdy uchyb przekroczy tę wartość, włącza się szybkie " +
      "źródło (split/grzałka). Split puszcza z powrotem wewnątrz pasma " +
      "komfortu; podłoga zawsze zostaje źródłem bazowym. Musi być większy niż " +
      "strefa nieczułości. Domyślnie 1,5 K.",
    tip_knob_fast_min_on_minutes:
      "Minimalny czas pracy szybkiego źródła po włączeniu. Chroni sprężarkę " +
      "splita przed taktowaniem i uspokaja zmiany kierunku w grupie " +
      "multisplit. Domyślnie 30 min; minimum 3.",
    tip_knob_fast_min_off_minutes:
      "Minimalny czas postoju szybkiego źródła po wyłączeniu, zanim wolno je " +
      "włączyć ponownie. Domyślnie 31 min; minimum 3.",
    tip_knob_fast_target_offset_k:
      "O ile kelwinów nastawa wysyłana do splita jest podbijana poza zadaną " +
      "pokoju podczas dogrzewu/dochładzania (grzanie: zadana + wartość, " +
      "chłodzenie: zadana − wartość). Czujnik splita pod sufitem czyta " +
      "cieplej niż czujnik pokojowy — bez podbicia split dławi się przed " +
      "dostarczeniem dogrzewu. O wyłączeniu i tak decyduje czujnik pokojowy. " +
      "0 = bez podbicia (split dostaje dokładnie zadaną). Domyślnie 0 — " +
      "podbij (np. 1 K), jeśli split dławi się przed dostarczeniem dogrzewu.",
    tip_knob_dry_enabled:
      "W chłodzeniu split (tylko klimatyzator) jest załączany w trybie " +
      "osuszania, gdy punkt rosy pokoju przekroczy próg — nawet gdy " +
      "temperatura niczego nie woła. Podłoga chłodzi tylko jawnie i nie umie " +
      "osuszać, a rosnąca rosa podnosi wodę chłodzącą i zabiera jej moc; " +
      "osuszenie obniża rosę i uwalnia podłogę. Dogrzew/dochłodzenie " +
      "temperaturowe ma zawsze pierwszeństwo. Domyślnie wyłączone.",
    tip_knob_dry_dew_max_c:
      "Punkt rosy pokoju, powyżej którego startuje osuszanie [°C]. Start " +
      "tylko, gdy pokój jest na nastawie lub cieplej (split w dry też " +
      "chłodzi). Zwolnienie 1 K poniżej progu (histereza) albo gdy pokój " +
      "przechłodzi się poza martwą strefę. Domyślnie 16 °C — osuszanie " +
      "rusza, zanim powietrze zrobi się lepkie (od ok. 17–18 °C rosy).",
    tip_knob_fast_manual_hold_minutes:
      "Gdy split fizycznie robi co innego niż ostatnia komenda (włączony " +
      "pilotem, wyłączony, przestawiony kierunek), regulator uznaje to za " +
      "Twoją decyzję: przyjmuje stan urządzenia i przez tyle minut od " +
      "ostatniego dotknięcia tylko go odzwierciedla — nic nie wysyła, nie " +
      "nadpisuje ponownym wymuszaniem i nie przerzuca chłodzącego splita " +
      "wprost na grzanie. Po upływie czasu wraca normalna logika (zmiana " +
      "kierunku dalej przez OFF i minimalny postój). Zabezpieczenia S3/S4 i " +
      "utrata czujnika przerywają utrzymanie. 0 = wyłączone (jak dawniej: " +
      "flaga rozjazdu i ponowne wymuszenie po ~45 min). Domyślnie 60 min.",
    tip_knob_dew_margin_k:
      "Chłodzenie: gdy zasilanie jest co najmniej o tyle K powyżej punktu " +
      "rosy, zawór działa bez ograniczeń; bliżej rosy zaczyna się dławienie " +
      "przepływu (ochrona przed kondensacją na podłodze). Domyślnie 1,5 K " +
      "(pompa trzyma wodę 2 K nad rosą, więc zwykłe opóźnienie wody nie " +
      "dławi); " +
      "zwiększ, jeśli widzisz wilgoć.",
    tip_knob_dew_ramp_k:
      "Szerokość rampy dławienia poniżej marginesu rosy: na tym odcinku " +
      "przepływ maleje liniowo od 100 % do 0. Mniejsza wartość = ostrzejsze " +
      "odcinanie. Domyślnie 2 K.",
    tip_knob_outdoor_ff_enabled:
      "Sprzężenie pogodowe: dodaje bazowe otwarcie zaworu zależne od " +
      "temperatury zewnętrznej, zanim pokój zdąży się wychłodzić. Przydatne " +
      "przy dużych przeszkleniach i mrozach. Domyślnie wyłączone.",
    tip_knob_ff_neutral_c:
      "Temperatura zewnętrzna, przy której człon pogodowy wynosi zero; " +
      "poniżej niej każdy 1 K chłodniej dodaje otwarcie według wzmocnienia " +
      "FF. Nie przesuwa krzywej wody grzania (ma ona własną temperaturę " +
      "neutralną). Domyślnie 15 °C.",
    tip_knob_ff_gain_pct_per_k:
      "Ile procent otwarcia dodaje człon pogodowy za każdy 1 K poniżej " +
      "temperatury neutralnej. Domyślnie 1 %/K.",
    tip_knob_ff_max_pct:
      "Górny limit członu pogodowego — sprzężenie nigdy nie doda więcej niż " +
      "tyle procent otwarcia. Domyślnie 20 %.",
    tip_knob_cooling_supply_base_c:
      "Nastawa wody chłodzenia pisana do pompy, gdy dom chłodzi: zapisywana " +
      "wartość to maksimum z tej bazy i globalnego bezpiecznego punktu rosy " +
      "— nigdy poniżej punktu rosy. Działa tylko przy skonfigurowanej encji " +
      "nastawy chłodzenia (opcje → Pompa ciepła). Parametr globalny — nie da " +
      "się go nadpisać per pokój. Domyślnie 15 °C.",
    tip_knob_heating_supply_base_c:
      "Zasilanie wody grzewczej przy temperaturze neutralnej krzywej " +
      "(parametr „Temperatura neutralna krzywej wody grzania”, domyślnie " +
      "15 °C). Poniżej niej nastawa rośnie według nachylenia krzywej; całość " +
      "obcinana do minimum i maksimum wody grzania (domyślnie 20–40 °C). " +
      "Pisana do pompy tylko przy skonfigurowanej encji nastawy grzania — " +
      "domyślnie pompa jedzie na własnej krzywej. Parametr globalny. " +
      "Domyślnie 26 °C.",
    tip_knob_heating_supply_slope:
      "O ile kelwinów rośnie nastawa wody grzania na każdy 1 K temperatury " +
      "zewnętrznej poniżej temperatury neutralnej krzywej. Krok 0,01, żeby " +
      "odwzorować krzywą pompy: 25 °C przy +15 °C i 32 °C przy −15 °C to " +
      "7/30 ≈ 0,23 K/K. Domyślnie 0,5 K/K; typowo 0,2–0,8 dla dobrze " +
      "ocieplonego domu z podłogówką.",
    tip_knob_heating_curve_neutral_c:
      "Temperatura zewnętrzna, przy której krzywa wody grzania daje " +
      "dokładnie bazę; poniżej niej nastawa rośnie według nachylenia. " +
      "Własny parametr krzywej — zmiana temperatury neutralnej członu " +
      "pogodowego (zawory) już jej nie przesuwa. Parametr globalny. " +
      "Domyślnie 15 °C.",
    tip_knob_heating_supply_min_c:
      "Dolne ograniczenie krzywej wody grzania: nastawa nigdy nie spadnie " +
      "poniżej. Musi być niższe od maksimum, a baza musi leżeć między nimi. " +
      "Parametr globalny. Domyślnie 20 °C.",
    tip_knob_heating_supply_max_c:
      "Górne ograniczenie krzywej wody grzania: nastawa nigdy nie przekroczy " +
      "tej wartości. Dla samej podłogówki często warto zejść poniżej 40 °C " +
      "(wylewka). Parametr globalny. Domyślnie 40 °C.",
    tip_knob_heating_gate_enabled:
      "Panasonic: zmiana nastawy wody grzania, gdy sprężarka stoi, potrafi " +
      "zablokować jej ponowny start. Z podłączonymi encjami częstotliwości " +
      "sprężarki i temperatury wylotu Tortoise pisze nową nastawę tylko " +
      "wtedy, gdy to bezpieczne: przy pracującej sprężarce podwyżkę od " +
      "razu, a obniżkę dopiero, gdy wylot jest niżej niż nowa nastawa + " +
      "próg zatrzymania; przy postoju podwyżkę tylko, gdy wylot jest niżej " +
      "niż nowa nastawa − pasmo startu (sprężarka rusza od razu), a obniżkę " +
      "tylko, gdy wylot jest co najmniej na nowej nastawie. W pozostałych " +
      "przypadkach pompa zostaje na poprzedniej nastawie, a decyzja jest " +
      "sprawdzana co cykl. Niezmieniona nastawa nie jest ponawiana. " +
      "Wyłączone = każda nastawa pisana od razu (jak przed issue #33). " +
      "Parametr globalny. Domyślnie włączone.",
    tip_knob_heating_curve_hysteresis_k:
      "Luz na surowej wartości krzywej, zanim zostanie zaokrąglona do kroku " +
      "encji pompy [K]. Nastawa zmienia się dopiero, gdy krzywa przesunie " +
      "się o tyle za granicę zaokrąglenia — wahanie temperatury zewnętrznej " +
      "o 0,1 K nie przełącza nastawy co cykl. 0 wyłącza. Działa także przy " +
      "wyłączonej bramce. Parametr globalny. Domyślnie 0,5 K.",
    tip_knob_heating_restart_band_k:
      "Własne pasmo pompy „ΔT dla załączenia grzania” [K]: sprężarka rusza, " +
      "gdy wylot spadnie o tyle poniżej nastawy. Podwyżka przy postoju jest " +
      "pisana tylko, gdy wylot jest niżej niż nowa nastawa minus to pasmo. " +
      "Jeśli w opcjach (Pompa ciepła) wskazano encję pasma pompy i podaje " +
      "ona 1–10 K, ma ona pierwszeństwo. Parametr globalny. Domyślnie 3 K.",
    tip_knob_heating_stop_offset_k:
      "Własny próg wyłączenia pompy [K]: pracująca sprężarka staje, gdy " +
      "wylot przekroczy nastawę o tyle. Obniżka przy pracy jest pisana " +
      "dopiero, gdy wylot jest niżej niż nowa nastawa plus ten próg — " +
      "inaczej zapis od razu zakończyłby pracę. Parametr globalny. " +
      "Domyślnie 2 K.",
    // Tooltipy szczegółów pokoju (tip_dec_*) i nagłówków tabel (tip_ast_* /
    // tip_th_*) — treść wspólna z docs/manual/pl.md: zmieniaj razem.
    tip_th_assist:
      "Komenda dla szybkiego źródła pokoju (split/grzałka): kierunek pracy " +
      "i temperatura zadana dla urządzenia. „—” = pokój nie ma wspomagania; " +
      "„wył.” = wspomaganie stoi (pokój trzyma podłoga).",
    tip_assist_target:
      "Nastawa splita może różnić się od zadanej pokoju o „podbicie " +
      "nastawy wspomagania” (domyślnie 0 — split dostaje zadaną; " +
      "grzanie: +, chłodzenie: −). Czujnik splita wisi pod sufitem i może " +
      "czytać cieplej niż nasz czujnik pokojowy — wtedy nastawa równa " +
      "zadanej gasi split, zanim dogrzanie/dochłodzenie faktycznie dotrze " +
      "do pokoju, i warto podbicie zwiększyć (np. 1 K). O wyłączeniu " +
      "i tak decyduje NASZ czujnik pokojowy z histerezą, więc pokój nie " +
      "„ucieknie”. Wartość zmienisz w Strojeniu (0 = bez podbicia). " +
      "W trybie przejściowym nastawa = zadana.",
    tip_dec_error:
      "Uchyb = zadana − zmierzona. Dodatni znaczy: za zimno (w grzaniu " +
      "otwiera zawór); w chłodzeniu znak działa odwrotnie.",
    tip_dec_trend:
      "Filtrowane tempo zmian temperatury pokoju w K/h. To wejście członu " +
      "trendu — szybki wzrost ku zadanej przymyka zawór zawczasu. Po przerwie " +
      "w danych filtr potrzebuje ~30–45 min rozgrzewki.",
    tip_dec_terms:
      "Z czego składa się surowe otwarcie: P reaguje na bieżący uchyb, I na " +
      "uchyb utrzymujący się w czasie, Trend tłumi rozpędzoną temperaturę, FF " +
      "dodaje bazę pogodową. Suma = zawór surowy.",
    tip_dec_raw_valve:
      "Otwarcie policzone przez regulator PRZED limitami: minimalnym " +
      "otwarciem, obcięciem 0–100 % i regułami bezpieczeństwa.",
    tip_dec_final_valve:
      "Otwarcie faktycznie komenderowane do siłowników (po limitach i " +
      "regułach bezpieczeństwa). Jedna wartość dla wszystkich pętli pokoju.",
    tip_dec_throttle:
      "Współczynnik dławienia przy punkcie rosy (tylko chłodzenie): 100 % = " +
      "bez ograniczeń, 0 % = zawór zamknięty, bo zasilanie zeszło do punktu " +
      "rosy (flaga s2_throttle). 0 % pojawia się też zachowawczo, gdy " +
      "brakuje danych o wilgotności lub temperaturze zasilania.",
    tip_dec_integrator:
      "Pamięć członu całkującego. „Zamrożony” znaczy: celowo wstrzymana — " +
      "np. pompa ciepła robi CWU/odszranianie, czujnik zgubiony albo pokój " +
      "wyłączony — żeby całka nie narosła fałszywie.",
    tip_dec_saturated:
      "Zawór uderzył w ogranicznik 0 lub 100 %. Regulator chce więcej, niż " +
      "fizycznie się da; anty-nasycenie pilnuje, by całka nie rosła w ścianę.",
    tip_dec_floor:
      "Czy zadziałało minimalne otwarcie przy grzaniu (parametr „Minimalne " +
      "otwarcie zaworu”).",
    tip_dec_dew:
      "Punkt rosy policzony z temperatury i wilgotności pokoju. Poniżej tej " +
      "temperatury na podłodze skrapla się woda — stąd obie ochrony przy " +
      "chłodzeniu. Obok: powód, gdy pokój nie wnosi wkładu do globalnego " +
      "bezpiecznego punktu rosy.",
    tip_dec_fast:
      "Komenda dla splita/grzałki: włącz + kierunek + temperatura zadana. " +
      "Split reguluje się sam; podłogi nigdy nie wyręcza, tylko " +
      "dogrzewa/dochładza powyżej progu dogrzewu.",
    tip_dec_flags:
      "Flagi diagnostyczne tego cyklu — każda mówi, które zabezpieczenie lub " +
      "ograniczenie zadziałało. Pełny słownik flag znajdziesz w instrukcji " +
      "(docs/manual/pl.md §12).",
    tip_ast_timer:
      "Blokada minimalnego czasu pracy/postoju: tyle jeszcze musi upłynąć, " +
      "zanim wspomaganie może zmienić stan. Chroni sprężarkę.",
    tip_ast_group:
      "Pokoje na wspólnym agregacie (multisplit) muszą chłodzić albo grzać " +
      "razem. Przegrany arbitrażu jest przymusowo wyłączony do czasu zmiany " +
      "kierunku grupy (wygrywa większa potrzeba, urzędujący kierunek ma bonus " +
      "0,5 K).",
    tip_ast_hours:
      "Okno, w którym wspomaganie tego pokoju może pracować (np. " +
      "07:00–22:00; okno może przechodzić przez północ). Poza oknem split " +
      "się nie załącza; jeśli pracuje, gdy okno się kończy, wyłącza się " +
      "dopiero po minimalnym czasie pracy — ochrona sprężarki jest " +
      "ważniejsza niż punktualność. Awaryjne grzanie/chłodzenie (S3/S4) ma " +
      "prawo złamać ciszę. Puste = zawsze wolno.",
    // Zakładka Pompa ciepła (B2) — treść wspólna z docs/manual/pl.md §11.
    hp_empty:
      "Sprzężenie z pompą ciepła nie jest skonfigurowane. Wskaż encje pompy " +
      "w opcjach integracji: Ustawienia → Urządzenia i usługi → Tortoise-UFH " +
      "→ Konfiguruj → Pompa ciepła. Wszystko jest opcjonalne — bez " +
      "konfiguracji Tortoise pompy nie dotyka.",
    hp_sec_mode: "Tryb pracy",
    hp_tortoise_mode: "Tryb Tortoise",
    hp_current_option: "Tryb pompy",
    hp_desired_option: "Tryb docelowy",
    hp_in_sync: "zgodny",
    hp_diverged: "rozjazd",
    hp_dhw_only_note:
      "Pompa grzeje teraz tylko CWU („DHW only”) — Tortoise nie pisze " +
      "trybu, dopóki automatyka CWU nie odda kierunku.",
    hp_no_force:
      "W trybie Przejściowy i Wył. Tortoise nie wymusza kierunku — pompą " +
      "rządzi jej własna automatyka i CWU.",
    tip_hp_mode:
      "Tortoise synchronizuje wyłącznie KIERUNEK trybu pompy: grzanie → " +
      "wariant „Heat”, chłodzenie → wariant „Cool”, zawsze z zachowaniem " +
      "członu „+DHW” (flaga CWU należy do zewnętrznej automatyki). Zapis " +
      "jest rzadki: przy zmianie trybu Tortoise albo gdy rozjazd utrzymuje " +
      "się przez ≥2 cykle, nie częściej niż co 15 minut. Sprężarką i krzywą " +
      "pompy Tortoise nie steruje.",
    hp_sec_dhw: "Ciepła woda (CWU)",
    hp_dhw_switch: "Flaga CWU (+DHW)",
    hp_dhw_warning:
      "Uwaga: zewnętrzna automatyka CWU jest właścicielem tej flagi i może " +
      "ją w każdej chwili nadpisać — to jej prawo. Ten przełącznik to " +
      "ręczna prośba „dogrzej wodę teraz / przestań”, nie blokada.",
    tip_hp_dhw:
      "Dokłada lub zdejmuje człon „+DHW” w encji trybu pompy (np. „Heat " +
      "only” ↔ „Heat+DHW”). Gdy pompa jest w „DHW only”, zdjęcie flagi jest " +
      "niemożliwe — nie ma kierunku, do którego można wrócić.",
    hp_sec_setpoints: "Nastawy wody",
    hp_cool_target: "Nastawa wody chłodzenia",
    hp_cool_calc: "max(baza {base}, bezpieczny punkt rosy {dew})",
    hp_heat_target: "Nastawa wody grzania",
    hp_heat_calc:
      "krzywa: {base} + {slope} · ({neutral} − T zewn. {tout}), w granicach {min}–{max}",
    hp_curve_cap: "Podgląd krzywej wody grzania",
    hp_curve_tout: "T zewn.",
    hp_curve_target: "Woda",
    hp_curve_unsaved: "uwzględnia niezapisane zmiany",
    hp_curve_invalid: "Niepoprawne wartości: {detail}",
    tip_hp_curve:
      "Nastawa wody grzania, którą Tortoise pisałby przy danej temperaturze " +
      "zewnętrznej (z ograniczeniem do minimum i maksimum). Porównaj ją z " +
      "krzywą własnej pompy, zanim podłączysz encję nastawy grzania.",
    hp_not_written: "nie zapisywana w tym trybie",
    hp_not_configured_entity: "encja nieskonfigurowana",
    tip_hp_cool:
      "W trybie chłodzenia Tortoise pisze do pompy nastawę wody = maksimum " +
      "z bazy (parametr strojenia) i globalnego bezpiecznego punktu rosy " +
      "(najwyższy punkt rosy chłodzonych pokoi + 2 K) — woda nigdy nie " +
      "zejdzie do strefy kondensacji. Zapis przy zmianie ≥ 0,5 K, wymuszany " +
      "ponownie co ~45 min.",
    tip_hp_heat:
      "Opcjonalne: w trybie grzania Tortoise pisze nastawę wody z prostej " +
      "krzywej pogodowej (baza + nachylenie × niedobór względem temperatury " +
      "neutralnej krzywej, obcięte do minimum i maksimum — domyślnie " +
      "20–40 °C). Jeśli encji nie skonfigurujesz, pompa jedzie na własnej krzywej — to domyślne i w pełni wspierane. " +
      "Zapis przechodzi przez bramkę nastawy grzania (karta niżej).",
    hp_active_cap: "Pompa dostępna dla podłogówki",
    hp_active_unknown: "nieznane",
    tip_hp_active:
      "Sygnał „pompa pracuje dla podłogówki”: gdy jest wyłączony " +
      "(CWU/odszranianie), regulatory pokojów zamrażają człon całkujący, " +
      "żeby nie nabijać całki, kiedy woda i tak nie płynie w podłogę.",
    hp_writes_paused:
      "Zapisy wstrzymane — żaden pokój nie steruje (Steruje).",
    // Wymuszanie startu chłodzenia (issue #7, 2026-07-15).
    tune_hp_flicker_enabled: "Wymuszanie startu chłodzenia",
    tune_hp_flicker_band_k: "Docelowa histereza chłodzenia",
    tune_hp_flicker_min_open_pct: "Min. odbiór: suma otwarcia pętli",
    tune_hp_flicker_stuck_minutes: "Zwłoka przed wymuszeniem",
    tune_hp_flicker_min_off_minutes: "Min. przerwa między startami",
    tune_hp_flicker_max_starts_per_h: "Maks. startów na godzinę",
    tip_knob_hp_flicker_enabled:
      "Włącza wymuszanie startu sprężarki chłodzenia (tylko Panasonic Aquarea). " +
      "Gdy pokoje wołają o chłód, a sprężarka stoi, na jeden cykl obniża zapisaną " +
      "nastawę wody, by przekroczyć stałą histerezę 3 K powrotu i wystartować " +
      "sprężarkę, po czym przywraca wartość bezpieczną dla rosy. Wymaga encji " +
      "powrotu i częstotliwości sprężarki. Domyślnie wyłączone. Globalne.",
    tip_knob_hp_flicker_band_k:
      "Docelowa efektywna martwa strefa wody chłodzenia [K]. Musi być poniżej " +
      "stałej histerezy 3 K pompy, by ją zacieśnić — impuls uzbraja się, gdy " +
      "powrót wzrośnie o tyle powyżej zapisanej nastawy. Globalne.",
    tip_knob_hp_flicker_min_open_pct:
      "Bramka popytu: minimalna suma otwarcia zaworów pokoi wołających o chłód, " +
      "ważona liczbą ich pętli (zawór % × pętle), zanim start zostanie wymuszony. " +
      "100 = jedna pętla w pełni otwarta; maksimum = wszystkie pętle × 100. " +
      "Poniżej progu bufor pokrywa odbiór i start nie jest wymuszany — wymuszenie " +
      "zbiłoby tylko temperaturę bufora i skróciło cykl sprężarki. Globalne.",
    tip_knob_hp_flicker_stuck_minutes:
      "Jak długo powrót musi tkwić uzbrojony (postój w martwej strefie z realnym " +
      "zapotrzebowaniem), zanim padnie impuls [min]. Globalne.",
    tip_knob_hp_flicker_min_off_minutes:
      "Ochrona sprężarki: minimalny odstęp między wymuszonymi startami [min]. " +
      "Globalne.",
    tip_knob_hp_flicker_max_starts_per_h:
      "Twardy limit wymuszonych startów sprężarki na godzinę kroczącą. Globalne.",
    hp_sec_flicker: "Wymuszanie startu chłodzenia",
    tip_hp_flicker:
      "Panasonic: gdy pompa stoi w martwej strefie, a pokoje wciąż chłodzą, " +
      "Tortoise na jeden cykl obniża nastawę chłodzenia, by wytrącić sprężarkę z " +
      "jej stałej histerezy 3 K powrotu, po czym przywraca wartość bezpieczną dla " +
      "rosy. Skutek: chłodniejsza średnia woda, powrót nadal bezpieczny dla rosy.",
    hp_flicker_on: "włączony",
    hp_flicker_off: "wyłączony",
    hp_flicker_pulsing_badge: "impuls",
    hp_flicker_state: "Stan",
    hp_flicker_st_idle: "czuwa",
    hp_flicker_st_pulse: "impuls",
    hp_flicker_st_cooldown: "przerwa",
    hp_flicker_trigger: "Próg uzbrojenia",
    hp_flicker_demand: "Odbiór (Σ otwarcia pętli)",
    hp_flicker_stuck: "Utknięcie do impulsu",
    hp_flicker_cooldown: "Przerwa do końca",
    hp_flicker_pulses: "Wymuszone starty (ost. godz.)",
    hp_flicker_pulses_24h: "Wymuszone starty (ost. 24 godz.)",
    hp_flicker_last_pulse: "Ostatni cel impulsu",
    hp_flicker_return: "Powrót / wlot",
    hp_flicker_outlet: "Wylot / zasilanie",
    hp_flicker_freq: "Sprężarka",
    hp_sec_heat_gate: "Bramka nastawy grzania",
    tip_hp_heat_gate:
      "Panasonic: krok nastawy wody grzania przy stojącej sprężarce potrafi " +
      "zablokować jej ponowny start. Bramka porównuje nową nastawę z tą, " +
      "którą ma pompa, i z temperaturą wylotu: bezpieczne kroki pisze od " +
      "razu, pozostałe wstrzymuje i sprawdza co cykl. Bez encji " +
      "częstotliwości sprężarki lub wylotu bramka jest nieaktywna i nastawa " +
      "jest pisana od razu.",
    hp_gate_active: "aktywna",
    hp_gate_inactive: "nieaktywna",
    hp_gate_held_badge: "wstrzymana",
    hp_gate_reason: "Decyzja",
    hp_gate_target: "Nastawa docelowa",
    hp_gate_pump: "Nastawa w pompie",
    hp_gate_curve: "Krzywa (surowa)",
    hp_gate_compressor: "Sprężarka",
    hp_gate_running: "pracuje",
    hp_gate_idle: "stoi",
    hp_gate_outlet: "Wylot",
    hp_gate_band: "Pasmo startu",
    hp_gate_band_entity: "z pompy",
    hp_gate_band_knob: "parametr",
    hp_gate_stop: "Próg zatrzymania",
    hp_gate_held_for: "Wstrzymana od",
    hp_gate_r_no_target: "brak nastawy w tym trybie",
    hp_gate_r_in_sync: "pompa ma już tę nastawę",
    hp_gate_r_ungated_disabled: "bramka wyłączona — pisane od razu",
    hp_gate_r_ungated_no_input:
      "bramka nieaktywna: brak odczytu sprężarki/wylotu — pisane od razu",
    hp_gate_r_write_increase_running: "podwyżka zapisana (sprężarka pracuje)",
    hp_gate_r_write_decrease_running:
      "obniżka zapisana (wylot poniżej nastawy + próg zatrzymania)",
    hp_gate_r_write_increase_idle:
      "podwyżka zapisana (wylot poniżej pasma startu — sprężarka rusza)",
    hp_gate_r_write_decrease_idle: "obniżka zapisana (wylot na lub ponad nową nastawą)",
    hp_gate_r_write_unknown_running:
      "zapisana (nastawa pompy nieznana, sprężarka pracuje)",
    hp_gate_r_write_unknown_idle:
      "zapisana (nastawa pompy nieznana, wylot poniżej pasma startu)",
    hp_gate_r_hold_decrease_running: "obniżka wstrzymana: zakończyłaby pracę sprężarki",
    hp_gate_r_hold_increase_idle:
      "podwyżka wstrzymana: sprężarka stoi, wylot w paśmie startu",
    hp_gate_r_hold_decrease_idle:
      "obniżka wstrzymana: sprężarka stoi, wylot poniżej nowej nastawy",
    hp_gate_r_hold_unknown_running: "wstrzymana: nastawa pompy nieznana, wylot za wysoki",
    hp_gate_r_hold_unknown_idle: "wstrzymana: nastawa pompy nieznana, sprężarka stoi",
    tune_heat_kick_enabled: "Kopnięcie sprężarki grzania",
    tune_kick_idle_minutes: "Postój sprężarki przed kopnięciem",
    tune_kick_deficit_k: "Wylot poniżej nastawy pompy",
    tune_kick_start_timeout_minutes: "Czas na start sprężarki",
    tip_knob_heat_kick_enabled:
      "Panasonic: po cyklu CWU sprężarka grzania potrafi stać godzinami, " +
      "choć wylot jest kilka kelwinów poniżej nastawy (zablokowany " +
      "ponowny start). Gdy sprężarka stoi co najmniej „Postój sprężarki " +
      "przed kopnięciem”, a wylot jest co najmniej „Wylot poniżej nastawy " +
      "pompy” niżej niż nastawa pompy, Tortoise podnosi nastawę grzania o " +
      "1 K (o jeden krok encji, jeśli jest większy) — taki krok " +
      "uruchamiał sprężarkę w tej samej minucie. Po starcie normalna " +
      "nastawa wraca przez bramkę nastawy grzania (obniżka przy pracy). " +
      "Najwyżej jedno kopnięcie na postój; bez startu w „Czas na start " +
      "sprężarki” Tortoise rezygnuje do następnej pracy sprężarki. Wymaga " +
      "bramki nastawy grzania i jej encji. Parametr globalny. Domyślnie " +
      "wyłączone.",
    tip_knob_kick_idle_minutes:
      "Jak długo sprężarka musi stać (częstotliwość 0), zanim nastąpi " +
      "kopnięcie [min]. Parametr globalny. Domyślnie 15 min.",
    tip_knob_kick_deficit_k:
      "O ile wylot musi być niżej niż nastawa grzania pompy, zanim " +
      "nastąpi kopnięcie [K]. Wyklucza też cykl CWU — wylot jest wtedy " +
      "gorący. Parametr globalny. Domyślnie 3,5 K.",
    tip_knob_kick_start_timeout_minutes:
      "Ile czekać na start sprężarki po kopnięciu [min]. Bez startu " +
      "Tortoise zdejmuje kopnięcie i do następnej pracy sprężarki już nie " +
      "próbuje; podniesioną nastawę bramka obniży dopiero wtedy, gdy " +
      "będzie to bezpieczne (zwykle po starcie sprężarki). Parametr " +
      "globalny. Domyślnie 5 min.",
    hp_sec_heat_kick: "Kopnięcie sprężarki grzania",
    tip_hp_heat_kick:
      "Panasonic: zablokowany ponowny start sprężarki grzania (np. po " +
      "CWU) zwalnia krok nastawy, po którym wylot jest ponad 3 K poniżej " +
      "nowej nastawy. Kopnięcie robi taki krok (+1 K) najwyżej raz na " +
      "postój, a gdy sprężarka ruszy, bramka nastawy grzania przywraca " +
      "normalną nastawę. Licznik 24 godz. pochodzi z historii sensora " +
      "stanu kopnięcia.",
    hp_kick_state: "Stan",
    hp_kick_reason: "Decyzja",
    hp_kick_idle: "Postój sprężarki",
    hp_kick_deficit: "Wylot poniżej nastawy pompy",
    hp_kick_target: "Nastawa kopnięcia",
    hp_kick_timeout: "Czas na start",
    hp_kick_24h: "Kopnięcia (ost. 24 godz.)",
    hp_kick_last: "Ostatnie kopnięcie",
    hp_kick_st_off: "nieaktywne",
    hp_kick_st_ready: "czuwa",
    hp_kick_st_waiting: "postój",
    hp_kick_st_kick: "kopnięcie",
    hp_kick_st_done: "wykorzystane",
    hp_kick_r_disabled: "wyłączone",
    hp_kick_r_gate_disabled: "bramka nastawy grzania wyłączona",
    hp_kick_r_inactive: "brak zapisu nastawy grzania w tym cyklu",
    hp_kick_r_no_input: "brak odczytu sprężarki, wylotu lub nastawy pompy",
    hp_kick_r_running: "sprężarka pracuje",
    hp_kick_r_idle_wait: "sprężarka stoi — odliczanie postoju",
    hp_kick_r_no_deficit: "wylot za mało poniżej nastawy (np. CWU)",
    hp_kick_r_gate_increase: "bramka sama zapisuje podwyżkę, która uruchomi sprężarkę",
    hp_kick_r_blocked_limit: "podniesiona nastawa przekroczyłaby maksimum",
    hp_kick_r_blocked_band: "podniesiona nastawa zostawiłaby wylot w paśmie startu",
    hp_kick_r_fired: "kopnięcie zapisane",
    hp_kick_r_kicking: "czekam na start sprężarki",
    hp_kick_r_started: "sprężarka ruszyła — normalna nastawa wraca przez bramkę",
    hp_kick_r_timeout: "brak startu — kopnięcie zdjęte",
    hp_kick_r_cancelled: "kopnięcie przerwane",
    hp_kick_r_done: "kopnięcie w tym postoju już wykorzystane",
    hp_kick_res_started: "start",
    hp_kick_res_timeout: "brak startu",
    hp_kick_res_cancelled: "przerwane",
    // Zakładka Rozdzielacze (v0.21.0) — rysunek KAN-therm InoxFlow UFST.
    mf_empty:
      "Brak skonfigurowanych rozdzielaczy. Dodaj je w opcjach integracji: " +
      "Ustawienia → Urządzenia i usługi → Tortoise-UFH → Konfiguruj → " +
      "Rozdzielacze (liczba obwodów, strona przyłącza głównego, sondy Z0/P0, " +
      "potem pętla pokoju na każdym obwodzie).",
    mf_sub: "{n} {circuits}",
    mf_circuits_one: "obwód",
    mf_circuits_few: "obwody",
    mf_circuits_many: "obwodów",
    mf_supply: "zasilanie",
    mf_return: "powrót",
    mf_dt: "ΔT",
    mf_open: "otwarte",
    mf_flags: "flagi",
    mf_th_loop: "Pętla",
    mf_th_open: "Otwarcie",
    mf_th_supply: "Zasilanie",
    mf_th_return: "Powrót",
    mf_th_dt: "ΔT",
    mf_th_state: "Stan",
    mf_state_ok: "ok",
    mf_free: "wolna",
    mf_link_tip: "Otwórz historię encji {id}",
  },
  en: {
    status_running: "Running · cycle {age}",
    status_stale: "Stale",
    status_error: "Algorithm error",
    status_nodata: "No data",
    age_sec: "{s} s ago",
    age_min_sec: "{m} min {s} s ago",
    age_min: "{m} min ago",
    age_hour_min: "{h} h {m} min ago",
    age_unknown: "no timestamp",
    rooms_live_cap: "Live",
    flags_cap: "Flags",
    flag_legend_title: "Flags — protection status",
    flag_legend_info:
      "Every diagnostic flag. Dimmed ones are inactive; lit (coloured) ones have fired in " +
      "at least one room. Click the info icon next to a flag for the full explanation. The " +
      "full dictionary is also in the manual (docs/manual/pl.md §12).",
    flag_grp_safety: "Safety (S1–S5)",
    flag_grp_assist: "Assist (split)",
    flag_grp_config: "Configuration / state",
    flag_grp_other: "Other",
    flag_active_in: "Active in: ",
    flag_active: "active",
    flag_none_active: "No active flags",
    flag_desc_unknown: "New or undocumented flag — see the manual (docs/manual/pl.md §12).",
    dew_cap: "Safe dew point",
    home_cap: "Home temperature",
    mode_cap: "Mode",
    mode_heating: "Heating",
    mode_transitional: "Transitional",
    mode_cooling: "Cooling",
    mode_off: "Off",
    tab_rooms: "Rooms",
    tab_flags: "Flags",
    tab_tuning: "Tuning",
    tab_assist: "Assist",
    tab_hp: "Heat pump",
    tab_manifolds: "Manifolds",
    tab_overview: "Overview",
    // Overview (dashboard)
    ov_comfort_cap: "Comfort",
    ov_comfort_sub: "controlled rooms within the comfort band",
    ov_none_live: "No room is set to “Live”.",
    ov_cold: "too cold",
    ov_warm: "too warm",
    ov_nodata: "no reading",
    ov_off: "off",
    ov_no_cool: "no cooling",
    ov_house_off: "House mode: {mode} — nothing is controlled.",
    ov_band_cap: "comfort band ±{v} K",
    tip_ov_comfort:
      "A room is in its comfort band when the reading is within the " +
      "controller's deadband of the setpoint (the “Deadband around the " +
      "setpoint” knob, global or per room). Only controlled rooms count: set to " +
      "“Live”, in cooling only those taking part in cooling, and none in house " +
      "mode Off. On the axis each dot is one room: left is colder, right is " +
      "warmer than the setpoint; the green stripe is the global band (a room " +
      "with its own band is judged by it). Click a dot to open the room's " +
      "details.",
    ov_dev_below: "{v} K below the setpoint",
    ov_dev_above: "{v} K above the setpoint",
    ov_dev_ok: "in the comfort band",
    ov_att_cap: "Needs attention",
    ov_att_ok: "All good",
    ov_att_ok_sub: "The algorithm is running and no flag needs action.",
    ov_att_info: "For information:",
    ov_att_more: "+{n} more in the Flags tab",
    ov_dev_cap: "Deviation from setpoint",
    ov_inband_sub: "of the time in the comfort band (24 h)",
    ov_fan_tip: "median {mid} · spread {lo} … {hi}",
    tip_ov_dev:
      "The last 24 hours of the rooms set to “Live” (5-minute recorder means; " +
      "up = warmer than the setpoint). The line is the median deviation, the " +
      "grey area the spread from the coldest to the warmest room, the green " +
      "stripe the comfort band; red and blue mark the spread above and below " +
      "the band. The figure is the share of time the rooms spent inside their " +
      "comfort band. Per-room detail is on the Rooms tab.",
    ov_minmax_24h: "min {min} · max {max} (24 h)",
    ov_floor_cap: "Floor loops",
    ov_floor_sub: "mean valve opening",
    ov_loops_open: "open loops: {n} of {m}",
    tip_ov_floor:
      "Mean valve opening of the rooms set to “Live”, weighted by each room's " +
      "loop count (a room with three loops weighs three times as much). A loop " +
      "counts as open from 5 %. The chart: the same mean over the last 24 hours " +
      "(5-minute recorder means).",
    ov_assist_note: "running now",
    ov_assist_idle: "all idle",
    ov_assist_manual: "manual control: {n}",
    ov_dhw: "DHW",
    ov_dew_cap: "Condensation guard",
    ov_dew_worst: "highest room dew point: {v}",
    ov_dew_throttled: "throttled rooms: {n}",
    ov_plant_cap: "Plant",
    manage_rooms: "Manage rooms",
    menu_btn: "Sidebar menu",
    loading: "Loading…",
    no_rooms: "No rooms configured.",
    th_room: "Room",
    th_measured: "Measured",
    th_setpoint: "Setpoint",
    th_error: "Error",
    th_valve: "Valve",
    th_supply: "Supply",
    th_return: "Return",
    th_assist_mode: "Mode",
    th_assist_temp: "Temp.",
    th_assist_group: "Assist",
    th_state: "Control",
    card_setpoint: "Setpoint",
    card_offset: "offset {v}",
    state_off: "Off",
    state_live: "Live",
    confirm_state_live:
      "Enable control of “{room}”? Tortoise will start writing commands to " +
      "the valves and the assist unit.",
    confirm_state_off:
      "Disable control of “{room}”? The actuators will be parked safely and " +
      "nothing more will be written.",
    confirm_mode: "Change the home mode to “{mode}”? This affects every room.",
    confirm_yes: "Yes, change",
    confirm_cancel: "Cancel",
    assist_split: "Split",
    assist_heater: "Heater",
    assist_none: "—",
    assist_no_source: "no assist",
    assist_state_off: "off",
    assist_state_heating: "heating",
    assist_state_cooling: "cooling",
    assist_state_dry: "drying",
    kind_split: "Air-con (split)",
    kind_heater: "Electric heater",
    detail_close: "Close",
    sec_wiring: "Sensors & signals",
    wire_source_tip: "Show source (entity)",
    sec_decision: "Controller decision",
    sec_history: "History",
    sec_diagnostics: "Diagnostic entities",
    chart_temp: "Temperature",
    chart_setpoint: "Setpoint",
    chart_valve: "Valve",
    chart_hum: "Humidity",
    chart_error: "Error",
    chart_split: "Assist",
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
    yes: "yes",
    no: "no",
    raw_show: "Show raw data",
    no_live_room: "No live data for this room yet.",
    tune_intro:
      "Global controller tuning plus sparse per-room overrides. " +
      "Only values that differ from the default are stored — a value left " +
      "at the default follows future default changes. " +
      "Saving reloads the controller (the PID integrator is reset).",
    tune_scope_global: "Global",
    tune_save: "Save",
    tune_saving: "Saving…",
    tune_saved: "Saved",
    tune_overridden: "overridden",
    tune_revert: "Revert to global",
    tune_custom: "custom",
    tune_reset_default: "Reset to default",
    tune_old_default: "old default",
    tune_old_default_tip:
      "This value is a former default that an older version of the " +
      "integration stored permanently — not your choice. Reset it to the " +
      "default unless you set it on purpose.",
    tune_on: "on",
    tune_off: "off",
    tune_grp_pid: "PI controller & trend",
    tune_grp_band: "Comfort band & valve",
    tune_grp_weather: "Weather feedforward (valve opening)",
    tune_grp_fast: "Fast source (split/heater)",
    tune_grp_dew: "Condensation protection (cooling)",
    tune_grp_cooling_water: "Cooling water — heat pump (global)",
    tune_grp_heating_water: "Heating water — heat pump (global)",
    tune_grp_other: "Other",
    tune_kp: "Proportional gain (P)",
    tune_ki: "Integral gain (I)",
    tune_kt: "Temperature-trend damping",
    tune_deadband_c: "Deadband around the setpoint",
    tune_valve_floor_pct: "Minimum valve opening when heating",
    tune_boost_offset_c: "Assist engage threshold",
    tune_fast_min_on_minutes: "Assist minimum run time",
    tune_fast_min_off_minutes: "Assist minimum rest time",
    tune_fast_target_offset_k: "Assist target overdrive",
    tune_dry_enabled: "Dry assist",
    tune_dry_dew_max_c: "Dry-assist dew-point threshold",
    tune_fast_manual_hold_minutes: "Assist manual-hold time",
    tune_dew_margin_k: "Safety margin above the dew point",
    tune_dew_ramp_k: "Cooling throttle ramp width",
    tune_outdoor_ff_enabled: "Weather feedforward enabled",
    tune_ff_neutral_c: "Feedforward neutral outdoor temperature",
    tune_ff_gain_pct_per_k: "Feedforward gain",
    tune_ff_max_pct: "Feedforward upper limit",
    tune_cooling_supply_base_c: "Cooling water base temperature",
    tune_heating_supply_base_c: "Heating water base temperature",
    tune_heating_supply_slope: "Heating water curve slope",
    tune_heating_curve_neutral_c: "Heating curve neutral outdoor temperature",
    tune_heating_supply_min_c: "Heating water minimum temperature",
    tune_heating_supply_max_c: "Heating water maximum temperature",
    tune_heating_gate_enabled: "Wait for a safe compressor state",
    tune_heating_curve_hysteresis_k: "Heating curve hysteresis",
    tune_heating_restart_band_k: "Heat pump restart band",
    tune_heating_stop_offset_k: "Heat pump stop offset",
    ast_th_kind: "Kind",
    ast_th_group: "Group",
    ast_th_command: "Command",
    ast_th_actual: "Actual state",
    ast_th_timer: "Timer",
    ast_th_hours: "Allowed hours",
    ast_hours_always: "always",
    assist_window_sub: "allowed {start}–{end}",
    ast_th_flags: "Flags",
    ast_th_entity: "Entity",
    ast_none_line: "No assist: {rooms}",
    ast_timer_unlock: "unlocks in ~{n} min",
    ast_timer_locked: "min-runtime lock",
    ast_timer_conflict: "group conflict",
    ast_tune_link: "tune",
    ast_actual_unknown: "—",
    ast_empty: "No room has a fast source.",
    dew_reason_no_humidity: "no humidity sensor",
    dew_reason_cooling_disabled: "cooling disabled",
    dew_reason_not_cooling_mode: "room not cooling",
    dew_reason_no_temperature: "no temperature reading",
    dew_none_collective: "{n} rooms lack dew-point data",
    dec_dew_reason: "Safe dew-point exclusion",
    err_ws: "Integration call failed: {detail}",
    tooltip_show: "Show explanation",
    tooltip_hide: "Hide explanation",
    // Tuning-knob tooltips (tip_knob_*) — content shared with
    // docs/manual/pl.md §8: change BOTH places together.
    tip_knob_kp:
      "Proportional gain: how many percent of valve opening each 1 K of error " +
      "(setpoint − measured) adds. Higher reacts faster but risks overshoot " +
      "on a high-mass floor. Default 14; typical 8–20. Change in small steps " +
      "and watch a full day.",
    tip_knob_ki:
      "Integral gain: how quickly the controller adds opening while a small " +
      "error persists for hours — removes steady-state offset. Values are " +
      "deliberately tiny (seconds in the denominator). Default 0.0015. Too " +
      "high = slow temperature oscillation around the setpoint.",
    tip_knob_kt:
      "Trend damping: watches how fast the temperature is moving. When the " +
      "room is closing on the setpoint quickly it eases the valve early, " +
      "taming overshoot of the high-mass floor. Default 12. Rarely needs " +
      "changing.",
    tip_knob_deadband_c:
      "Deadband around the setpoint (±): inside it the controller does not " +
      "chase tiny deviations, reducing valve travel and cycling. Default " +
      "0.3 K; typical 0.2–0.5 K.",
    tip_knob_valve_floor_pct:
      "Minimum valve opening while the room calls for heat, keeping a " +
      "sensible flow in the loop. Default 15 %. 0 disables the floor.",
    tip_knob_boost_offset_c:
      "Boost threshold: once the error exceeds this, the fast source " +
      "(split/heater) engages. It releases inside the comfort band; the floor " +
      "always stays the base source. Must exceed the deadband. Default 1.5 K.",
    tip_knob_fast_min_on_minutes:
      "Minimum runtime of the fast source once switched on. Protects the " +
      "split compressor from short-cycling and calms direction changes on a " +
      "multisplit group. Default 30 min; minimum 3.",
    tip_knob_fast_min_off_minutes:
      "Minimum off-time of the fast source before it may start again. " +
      "Default 31 min; minimum 3.",
    tip_knob_fast_target_offset_k:
      "How many kelvin the setpoint sent to the split is pushed beyond the " +
      "room setpoint during a boost (heating: setpoint + value, cooling: " +
      "setpoint − value). The split's ceiling-mounted sensor reads warmer " +
      "than the room sensor — without the overdrive the unit throttles " +
      "itself before the boost is delivered. Release is still decided by " +
      "the room sensor. 0 = no overdrive (the split gets the plain " +
      "setpoint). Default 0 — raise it (e.g. 1 K) if the split throttles " +
      "itself before the boost is delivered.",
    tip_knob_dry_enabled:
      "In cooling, the split (air-con only) is engaged in its dry mode when " +
      "the room dew point exceeds the threshold — even with no temperature " +
      "call. The floor cools only sensibly and cannot dehumidify, and a " +
      "rising dew point lifts the cooling water and steals the floor's " +
      "capacity; drying lowers the dew point and frees the floor. A " +
      "temperature boost always takes priority. Off by default.",
    tip_knob_dry_dew_max_c:
      "Room dew point above which the dry assist engages [°C]. Engages only " +
      "while the room is at or above its setpoint (a split cools in dry " +
      "too). Releases 1 K below the threshold (hysteresis) or as soon as " +
      "the room overcools past the deadband. Default 16 °C — drying starts " +
      "before the air turns sticky (from a dew point of ~17–18 °C).",
    tip_knob_fast_manual_hold_minutes:
      "When the split is physically doing something other than the last " +
      "command (switched on by remote, switched off, direction changed), the " +
      "controller treats it as your decision: it adopts the unit's state and " +
      "for this many minutes since the last touch only mirrors it — writes " +
      "nothing, does not overwrite it with the re-assert and never flips a " +
      "cooling split straight to heating. Afterwards the normal logic " +
      "resumes (a direction change still goes through OFF and the minimum " +
      "rest time). Safety rules S3/S4 and a lost sensor end the hold. 0 = " +
      "off (legacy: mismatch flag and re-assert after ~45 min). Default 60 min.",
    tip_knob_dew_margin_k:
      "Cooling: while supply is at least this many K above the dew point the " +
      "valve runs unrestricted; closer to the dew point the flow gets " +
      "throttled (floor condensation protection). Default 1.5 K (the heat " +
      "pump keeps the water 2 K above the dew point, so ordinary water lag " +
      "does not throttle); raise it if " +
      "you ever see moisture.",
    tip_knob_dew_ramp_k:
      "Width of the throttle ramp below the dew margin: over this span the " +
      "flow falls linearly from 100 % to 0. Smaller = sharper cut-off. " +
      "Default 2 K.",
    tip_knob_outdoor_ff_enabled:
      "Weather feedforward: adds a baseline valve opening based on outdoor " +
      "temperature, before the room has time to cool down. Useful with large " +
      "glazing and cold snaps. Off by default.",
    tip_knob_ff_neutral_c:
      "Outdoor temperature at which the weather term is zero; below it every " +
      "1 K colder adds opening per the FF gain. It does not move the heating " +
      "water curve (that has its own neutral temperature). Default 15 °C.",
    tip_knob_ff_gain_pct_per_k:
      "How many percent of opening the weather term adds per 1 K below the " +
      "neutral temperature. Default 1 %/K.",
    tip_knob_ff_max_pct:
      "Upper cap on the weather term — the feedforward never adds more than " +
      "this many percent of opening. Default 20 %.",
    tip_knob_cooling_supply_base_c:
      "The cooling-water setpoint written to the heat pump while the home " +
      "cools: the written value is the maximum of this base and the global " +
      "safe dew point — never below the dew point. Active only with the " +
      "cooling-setpoint entity configured (options → Heat pump). Global " +
      "parameter — cannot be overridden per room. Default 15 °C.",
    tip_knob_heating_supply_base_c:
      "Heating supply water at the curve's neutral outdoor temperature (the " +
      "“Heating curve neutral outdoor temperature” knob, default 15 °C). " +
      "Below it the setpoint rises along the curve slope; the result is " +
      "clamped to the heating water minimum and maximum (default 20–40 °C). " +
      "Written to the pump only with the heating-setpoint entity configured " +
      "— by default the pump follows its own curve. Global parameter. " +
      "Default 26 °C.",
    tip_knob_heating_supply_slope:
      "How many kelvins the heating-water setpoint rises per 1 K of outdoor " +
      "temperature below the curve's neutral point. Steps of 0.01 so a pump " +
      "curve can be matched: 25 °C at +15 °C and 32 °C at −15 °C is " +
      "7/30 ≈ 0.23 K/K. Default 0.5 K/K; typically 0.2–0.8 for a " +
      "well-insulated UFH house.",
    tip_knob_heating_curve_neutral_c:
      "The outdoor temperature at which the heating-water curve gives exactly " +
      "the base; below it the setpoint rises along the slope. The curve's own " +
      "knob — changing the feedforward neutral temperature (valves) no longer " +
      "moves it. Global parameter. Default 15 °C.",
    tip_knob_heating_supply_min_c:
      "Lower clamp of the heating-water curve: the setpoint never goes below " +
      "it. Must be below the maximum, and the base must lie between them. " +
      "Global parameter. Default 20 °C.",
    tip_knob_heating_supply_max_c:
      "Upper clamp of the heating-water curve: the setpoint never exceeds it. " +
      "A floor-only install often wants less than 40 °C (screed). Global " +
      "parameter. Default 40 °C.",
    tip_knob_heating_gate_enabled:
      "Panasonic: changing the heating water setpoint while the compressor " +
      "is idle can block its restart. With the compressor-frequency and " +
      "outlet-temperature entities wired, Tortoise writes a new setpoint " +
      "only when it is safe: while running, an increase at once and a " +
      "decrease only once the outlet is below the new setpoint + the stop " +
      "offset; while idle, an increase only when the outlet is below the " +
      "new setpoint − the restart band (the compressor starts at once), and " +
      "a decrease only when the outlet is at or above the new setpoint. " +
      "Otherwise the pump keeps its previous setpoint and the decision is " +
      "re-checked every cycle. An unchanged setpoint is not re-sent. Off = " +
      "every setpoint is written at once (as before issue #33). Global " +
      "parameter. Default on.",
    tip_knob_heating_curve_hysteresis_k:
      "Play on the raw curve value before it is rounded to the pump " +
      "entity’s step [K]. The setpoint changes only once the curve has " +
      "moved this far past a rounding boundary — a 0.1 K outdoor wobble no " +
      "longer toggles the setpoint every cycle. 0 disables it. Also applies " +
      "with the gate off. Global parameter. Default 0.5 K.",
    tip_knob_heating_restart_band_k:
      "The pump’s own “ΔT for heating ON” [K]: the compressor restarts once " +
      "the outlet falls this far below the setpoint. An increase while idle " +
      "is written only when the outlet is below the new setpoint minus this " +
      "band. If a restart-band entity is set in the options (Heat pump) and " +
      "reads 1–10 K, it takes precedence. Global parameter. Default 3 K.",
    tip_knob_heating_stop_offset_k:
      "The pump’s own stop offset [K]: a running compressor stops once the " +
      "outlet exceeds the setpoint by this much. A decrease while running " +
      "is written only once the outlet is below the new setpoint plus this " +
      "offset — otherwise the write would end the run at once. Global " +
      "parameter. Default 2 K.",
    // Room-detail tooltips (tip_dec_*) and table-header tooltips (tip_ast_* /
    // tip_th_*) — content shared with docs/manual/pl.md: change
    // together.
    tip_th_assist:
      "The command for the room's fast source (split/heater): running " +
      "direction and the unit's target temperature. “—” = the room has no " +
      "assist; “off” = the assist is idle (the floor holds the room).",
    tip_assist_target:
      "The split's setpoint can differ from the room target by the " +
      "“assist target overdrive” knob (default 0 — the split gets the " +
      "target; heating: +, cooling: −). The split's own sensor sits near " +
      "the ceiling and may read warmer than our room sensor — then a " +
      "setpoint equal to the target throttles the unit before the boost " +
      "actually reaches the room, and raising the knob (e.g. 1 K) helps. " +
      "Release is still decided by OUR room sensor with hysteresis, so the " +
      "room cannot run away. Adjust it in Tuning (0 = no overdrive). In " +
      "transitional mode the setpoint equals the target.",
    tip_dec_error:
      "Error = setpoint − measured. Positive means too cold (opens the valve " +
      "in heating); in cooling the sign works the other way.",
    tip_dec_trend:
      "Filtered rate of room-temperature change in K/h. Feeds the trend term " +
      "— a fast rise toward the setpoint eases the valve early. After a data " +
      "gap the filter needs ~30–45 min to warm up.",
    tip_dec_terms:
      "What the raw opening is made of: P reacts to the current error, I to " +
      "error persisting over time, Trend damps a fast-moving temperature, FF " +
      "adds the weather baseline. Their sum = raw valve.",
    tip_dec_raw_valve:
      "Opening computed by the controller BEFORE limits: the valve floor, the " +
      "0–100 % clamp and the safety rules.",
    tip_dec_final_valve:
      "Opening actually commanded to the actuators (after limits and safety " +
      "rules). One value for all of the room's loops.",
    tip_dec_throttle:
      "Dew-point throttle factor (cooling only): 100 % = unrestricted, 0 % = " +
      "valve shut because supply reached the dew point (flag s2_throttle). " +
      "0 % is also applied conservatively when humidity or supply data is " +
      "missing.",
    tip_dec_integrator:
      "The integral term's memory. 'Frozen' means deliberately paused — e.g. " +
      "the heat pump is doing DHW/defrost, a sensor is lost or the room is " +
      "off — so the integral cannot wind up falsely.",
    tip_dec_saturated:
      "The valve hit the 0 or 100 % bound. The controller wants more than " +
      "physics allows; anti-windup keeps the integral from climbing into the " +
      "wall.",
    tip_dec_floor:
      "Whether the heating valve floor (the 'Valve floor' knob) was applied " +
      "this cycle.",
    tip_dec_dew:
      "Dew point computed from room temperature and humidity. Below it water " +
      "condenses on the floor — hence both cooling protections. Next to it: " +
      "the reason when the room contributes nothing to the global safe dew " +
      "point.",
    tip_dec_fast:
      "Command for the split/heater: on + direction + target temperature. " +
      "The split self-regulates; it never replaces the floor, it only assists " +
      "beyond the boost threshold.",
    tip_dec_flags:
      "This cycle's diagnostic flags — each names the protection or limit " +
      "that acted. The full flag dictionary is in the manual " +
      "(docs/manual/pl.md §12).",
    tip_ast_timer:
      "Min on/off dwell lock: this much time must still pass before the " +
      "assist may change state. Protects the compressor.",
    tip_ast_group:
      "Rooms on a shared outdoor unit (multisplit) must all heat or all cool. " +
      "The arbitration loser is forced off until the group direction changes " +
      "(largest need wins; the incumbent direction gets a 0.5 K bonus).",
    tip_ast_hours:
      "The window in which this room's assist may run (e.g. 07:00–22:00; " +
      "the window may cross midnight). Outside it the split does not " +
      "engage; if it is running when the window ends, it stops only after " +
      "its minimum run time — compressor protection outranks punctuality. " +
      "Emergency heat/cool (S3/S4) may break the quiet hours. Empty = " +
      "always allowed.",
    // Heat-pump tab (B2) — content shared with docs/manual/pl.md §11.
    hp_empty:
      "The heat-pump link is not configured. Point the integration at the " +
      "pump's entities in Settings → Devices & services → Tortoise-UFH → " +
      "Configure → Heat pump. Everything is optional — unconfigured, " +
      "Tortoise never touches the pump.",
    hp_sec_mode: "Operating mode",
    hp_tortoise_mode: "Tortoise mode",
    hp_current_option: "Heat-pump mode",
    hp_desired_option: "Target mode",
    hp_in_sync: "in sync",
    hp_diverged: "diverged",
    hp_dhw_only_note:
      "The pump is currently DHW-only — Tortoise does not write the mode " +
      "until the DHW automation hands the direction back.",
    hp_no_force:
      "In Transitional and Off, Tortoise never forces a direction — the " +
      "pump's own automation and DHW stay in charge.",
    tip_hp_mode:
      "Tortoise synchronises only the DIRECTION of the pump mode: heating → " +
      "the “Heat” variant, cooling → the “Cool” variant, always preserving " +
      "the “+DHW” part (the DHW flag belongs to the external automation). " +
      "Writes are rare: on a Tortoise mode change or when a divergence " +
      "persists for ≥2 cycles, at most every 15 minutes. Tortoise never " +
      "controls the compressor or the pump's curve.",
    hp_sec_dhw: "Domestic hot water (DHW)",
    hp_dhw_switch: "DHW flag (+DHW)",
    hp_dhw_warning:
      "Note: the external DHW automation owns this flag and may overwrite " +
      "it at any time — that is its right. This switch is a manual request " +
      "(“heat water now / stop”), not a lock.",
    tip_hp_dhw:
      "Adds or removes the “+DHW” part of the pump-mode entity (e.g. “Heat " +
      "only” ↔ “Heat+DHW”). While the pump is in “DHW only”, removing the " +
      "flag is impossible — there is no direction to fall back to.",
    hp_sec_setpoints: "Water setpoints",
    hp_cool_target: "Cooling water setpoint",
    hp_cool_calc: "max(base {base}, safe dew point {dew})",
    hp_heat_target: "Heating water setpoint",
    hp_heat_calc:
      "curve: {base} + {slope} · ({neutral} − outdoor {tout}), within {min}–{max}",
    hp_curve_cap: "Heating curve preview",
    hp_curve_tout: "Outdoor",
    hp_curve_target: "Water",
    hp_curve_unsaved: "includes unsaved changes",
    hp_curve_invalid: "Invalid values: {detail}",
    tip_hp_curve:
      "The heating-water setpoint Tortoise would write at each outdoor " +
      "temperature (clamped to the minimum and maximum). Compare it with the " +
      "pump's own curve before you wire the heating-setpoint entity.",
    hp_not_written: "not written in this mode",
    hp_not_configured_entity: "entity not configured",
    tip_hp_cool:
      "While cooling, Tortoise writes a water setpoint = the maximum of the " +
      "base (a tuning knob) and the global safe dew point (highest dew " +
      "point of the cooled rooms + 2 K) — the water never enters the " +
      "condensation zone. Written on a ≥ 0.5 K change, re-asserted every " +
      "~45 min.",
    tip_hp_heat:
      "Optional: while heating, Tortoise writes the water setpoint from a " +
      "simple weather curve (base + slope × shortfall below the neutral " +
      "temperature of the curve, clamped to its minimum and maximum — " +
      "20–40 °C by default). If you leave the entity " +
      "unconfigured, the pump follows its own curve — the default and fully " +
      "supported. " +
      "The write goes through the heating setpoint gate (card below).",
    hp_active_cap: "Pump available for UFH",
    hp_active_unknown: "unknown",
    tip_hp_active:
      "The “pump serves the UFH” signal: while it is off (DHW/defrost), the " +
      "room controllers freeze their integral term so it does not wind up " +
      "while no water reaches the floor.",
    hp_writes_paused: "Writes paused — no room is in control (Live).",
    // Force cooling start (issue #7, 2026-07-15).
    tune_hp_flicker_enabled: "Force cooling start",
    tune_hp_flicker_band_k: "Target cooling deadband",
    tune_hp_flicker_min_open_pct: "Min demand: total loop opening",
    tune_hp_flicker_stuck_minutes: "Delay before forcing a start",
    tune_hp_flicker_min_off_minutes: "Min time between forced starts",
    tune_hp_flicker_max_starts_per_h: "Max forced starts per hour",
    tip_knob_hp_flicker_enabled:
      "Enables forcing the cooling compressor to start (Panasonic Aquarea only). " +
      "When rooms call for cooling but the compressor sits idle, it briefly drops " +
      "the written water setpoint for one cycle to cross the fixed 3 K return " +
      "hysteresis and start the compressor, then restores the dew-safe value. " +
      "Needs the return and compressor-frequency entities. Off by default. Global.",
    tip_knob_hp_flicker_band_k:
      "Target effective cooling-water deadband [K]. Must be below the pump's " +
      "fixed 3 K hysteresis to tighten it — a pulse arms once the return climbs " +
      "this far above the written setpoint. Global.",
    tip_knob_hp_flicker_min_open_pct:
      "Demand gate: the minimum total valve opening of the rooms calling for " +
      "cooling, weighted by their loop count (valve % × loops), before a start " +
      "is forced. 100 = one fully open loop; the maximum = all loops × 100. " +
      "Below the threshold the buffer tank covers the draw and no start is " +
      "forced — forcing one would merely knock the buffer down and " +
      "short-cycle the compressor. Global.",
    tip_knob_hp_flicker_stuck_minutes:
      "How long the return must sit stuck & armed (idle in the deadband with " +
      "genuine demand) before a pulse fires [min]. Global.",
    tip_knob_hp_flicker_min_off_minutes:
      "Compressor protection: the minimum gap between two forced starts [min]. " +
      "Global.",
    tip_knob_hp_flicker_max_starts_per_h:
      "Hard cap on forced compressor starts per rolling hour. Global.",
    hp_sec_flicker: "Force cooling start",
    tip_hp_flicker:
      "Panasonic: when the pump idles in its deadband while rooms still call for " +
      "cooling, Tortoise drops the cooling setpoint for one cycle to trip the " +
      "compressor out of its fixed 3 K return hysteresis, then restores the " +
      "dew-safe value. Net effect: colder average water while the return stays " +
      "dew-safe.",
    hp_flicker_on: "enabled",
    hp_flicker_off: "disabled",
    hp_flicker_pulsing_badge: "pulsing",
    hp_flicker_state: "State",
    hp_flicker_st_idle: "idle",
    hp_flicker_st_pulse: "pulse",
    hp_flicker_st_cooldown: "cooldown",
    hp_flicker_trigger: "Arm threshold",
    hp_flicker_demand: "Demand (Σ loop opening)",
    hp_flicker_stuck: "Stuck to pulse",
    hp_flicker_cooldown: "Cooldown left",
    hp_flicker_pulses: "Forced starts (last h)",
    hp_flicker_pulses_24h: "Forced starts (last 24 h)",
    hp_flicker_last_pulse: "Last pulse target",
    hp_flicker_return: "Return / inlet",
    hp_flicker_outlet: "Outlet / supply",
    hp_flicker_freq: "Compressor",
    hp_sec_heat_gate: "Heating setpoint gate",
    tip_hp_heat_gate:
      "Panasonic: a heating water setpoint step while the compressor is " +
      "idle can block its restart. The gate compares the new setpoint with " +
      "the one the pump holds and with the outlet temperature: safe steps " +
      "are written at once, the others are held and re-checked every cycle. " +
      "Without the compressor-frequency or outlet entity the gate is " +
      "inactive and the setpoint is written at once.",
    hp_gate_active: "active",
    hp_gate_inactive: "inactive",
    hp_gate_held_badge: "held",
    hp_gate_reason: "Decision",
    hp_gate_target: "Desired target",
    hp_gate_pump: "Pump setpoint",
    hp_gate_curve: "Curve (raw)",
    hp_gate_compressor: "Compressor",
    hp_gate_running: "running",
    hp_gate_idle: "idle",
    hp_gate_outlet: "Outlet",
    hp_gate_band: "Restart band",
    hp_gate_band_entity: "from pump",
    hp_gate_band_knob: "knob",
    hp_gate_stop: "Stop offset",
    hp_gate_held_for: "Held for",
    hp_gate_r_no_target: "no target in this mode",
    hp_gate_r_in_sync: "pump already holds the target",
    hp_gate_r_ungated_disabled: "gate off — written at once",
    hp_gate_r_ungated_no_input:
      "gate inactive: no compressor/outlet input — written at once",
    hp_gate_r_write_increase_running: "increase written (compressor running)",
    hp_gate_r_write_decrease_running:
      "decrease written (outlet below target + stop offset)",
    hp_gate_r_write_increase_idle:
      "increase written (outlet below the restart band — compressor starts)",
    hp_gate_r_write_decrease_idle: "decrease written (outlet at or above the new target)",
    hp_gate_r_write_unknown_running:
      "written (pump setpoint unknown, compressor running)",
    hp_gate_r_write_unknown_idle:
      "written (pump setpoint unknown, outlet below the restart band)",
    hp_gate_r_hold_decrease_running: "decrease held: it would end the compressor run",
    hp_gate_r_hold_increase_idle:
      "increase held: compressor idle, outlet inside the restart band",
    hp_gate_r_hold_decrease_idle:
      "decrease held: compressor idle, outlet below the new target",
    hp_gate_r_hold_unknown_running: "held: pump setpoint unknown, outlet too high",
    hp_gate_r_hold_unknown_idle: "held: pump setpoint unknown, compressor idle",
    tune_heat_kick_enabled: "Heating compressor kick",
    tune_kick_idle_minutes: "Compressor idle before a kick",
    tune_kick_deficit_k: "Outlet below the pump target",
    tune_kick_start_timeout_minutes: "Wait for the compressor start",
    tip_knob_heat_kick_enabled:
      "Panasonic: after a DHW cycle the heating compressor can stay off " +
      "for hours although the outlet is several kelvin below the setpoint " +
      "(a locked-out restart). Once the compressor has been idle for at " +
      "least “Compressor idle before a kick” and the outlet is at least " +
      "“Outlet below the pump target” below the pump’s setpoint, Tortoise " +
      "raises the heating setpoint by 1 K (one entity step if larger) — a " +
      "step like that started the compressor in the same minute. After " +
      "the start the normal setpoint returns through the heating setpoint " +
      "gate (a decrease while running). At most one kick per idle period; " +
      "without a start within “Wait for the compressor start” Tortoise " +
      "gives up until the compressor has run again. Needs the heating " +
      "setpoint gate and its entities. Global parameter. Default off.",
    tip_knob_kick_idle_minutes:
      "How long the compressor must have been idle (frequency 0) before a " +
      "kick [min]. Global parameter. Default 15 min.",
    tip_knob_kick_deficit_k:
      "How far the outlet must be below the pump’s heating setpoint " +
      "before a kick [K]. Also keeps the kick out of a DHW cycle — the " +
      "outlet is hot then. Global parameter. Default 3.5 K.",
    tip_knob_kick_start_timeout_minutes:
      "How long to wait for the compressor start after a kick [min]. " +
      "Without a start Tortoise removes the kick and does not try again " +
      "until the compressor has run; the gate lowers the raised setpoint " +
      "only once that is safe (usually after the compressor starts). " +
      "Global parameter. Default 5 min.",
    hp_sec_heat_kick: "Heating compressor kick",
    tip_hp_heat_kick:
      "Panasonic: a locked-out heating restart (e.g. after DHW) is " +
      "released by a setpoint step that leaves the outlet more than 3 K " +
      "below the new setpoint. The kick makes such a step (+1 K) at most " +
      "once per idle period, and once the compressor starts the heating " +
      "setpoint gate restores the normal setpoint. The 24 h count comes " +
      "from the history of the kick state sensor.",
    hp_kick_state: "State",
    hp_kick_reason: "Decision",
    hp_kick_idle: "Compressor idle",
    hp_kick_deficit: "Outlet below pump target",
    hp_kick_target: "Kick setpoint",
    hp_kick_timeout: "Start wait left",
    hp_kick_24h: "Kicks (last 24 h)",
    hp_kick_last: "Last kick",
    hp_kick_st_off: "inactive",
    hp_kick_st_ready: "ready",
    hp_kick_st_waiting: "idle",
    hp_kick_st_kick: "kick",
    hp_kick_st_done: "spent",
    hp_kick_r_disabled: "disabled",
    hp_kick_r_gate_disabled: "heating setpoint gate off",
    hp_kick_r_inactive: "no heating setpoint write this cycle",
    hp_kick_r_no_input: "no compressor, outlet or pump setpoint reading",
    hp_kick_r_running: "compressor running",
    hp_kick_r_idle_wait: "compressor idle — counting the idle time",
    hp_kick_r_no_deficit: "outlet not far enough below the setpoint (e.g. DHW)",
    hp_kick_r_gate_increase:
      "the gate writes an increase that starts the compressor by itself",
    hp_kick_r_blocked_limit: "the raised setpoint would exceed the maximum",
    hp_kick_r_blocked_band:
      "the raised setpoint would leave the outlet inside the restart band",
    hp_kick_r_fired: "kick written",
    hp_kick_r_kicking: "waiting for the compressor start",
    hp_kick_r_started:
      "compressor started — the normal setpoint returns through the gate",
    hp_kick_r_timeout: "no start — kick removed",
    hp_kick_r_cancelled: "kick cancelled",
    hp_kick_r_done: "this idle period's kick is already spent",
    hp_kick_res_started: "started",
    hp_kick_res_timeout: "no start",
    hp_kick_res_cancelled: "cancelled",
    test_running_min: "~{m} min",
    // Manifolds tab (v0.21.0) — the KAN-therm InoxFlow UFST drawing.
    mf_empty:
      "No manifolds configured. Add them in the integration options: " +
      "Settings → Devices & services → Tortoise-UFH → Configure → Manifolds " +
      "(circuit count, main-connection side, the Z0/P0 probes, then a room " +
      "loop on every circuit).",
    mf_sub: "{n} {circuits}",
    mf_circuits_one: "circuit",
    mf_circuits_few: "circuits",
    mf_circuits_many: "circuits",
    mf_supply: "supply",
    mf_return: "return",
    mf_dt: "ΔT",
    mf_open: "open",
    mf_flags: "flags",
    mf_th_loop: "Loop",
    mf_th_open: "Opening",
    mf_th_supply: "Supply",
    mf_th_return: "Return",
    mf_th_dt: "ΔT",
    mf_th_state: "State",
    mf_state_ok: "ok",
    mf_free: "free",
    mf_link_tip: "Open the history of {id}",
  },
  de: {
    status_running: "Läuft · Zyklus {age}",
    status_stale: "Veraltet",
    status_error: "Algorithmusfehler",
    status_nodata: "Keine Daten",
    age_sec: "vor {s} s",
    age_min_sec: "vor {m} min {s} s",
    age_min: "vor {m} min",
    age_hour_min: "vor {h} h {m} min",
    age_unknown: "kein Zeitstempel",
    rooms_live_cap: "Aktiv",
    flags_cap: "Meldungen",
    flag_legend_title: "Meldungen — Schutzstatus",
    flag_legend_info:
      "Alle Diagnosemeldungen. Abgeblendete sind inaktiv; leuchtende (farbige) haben in " +
      "mindestens einem Raum ausgelöst. Klicken Sie auf das Info-Symbol neben einer Meldung " +
      "für die vollständige Erklärung. Das vollständige Verzeichnis steht auch im Handbuch " +
      "(docs/manual/de.md §12).",
    flag_grp_safety: "Sicherheit (S1–S5)",
    flag_grp_assist: "Zusatzquelle (Split)",
    flag_grp_config: "Konfiguration / Zustand",
    flag_grp_other: "Sonstige",
    flag_active_in: "Aktiv in: ",
    flag_active: "aktiv",
    flag_none_active: "Keine aktiven Meldungen",
    flag_desc_unknown: "Neue oder undokumentierte Meldung — siehe Handbuch (docs/manual/de.md §12).",
    dew_cap: "Sicherer Taupunkt",
    home_cap: "Haustemperatur",
    mode_cap: "Modus",
    mode_heating: "Heizung",
    mode_transitional: "Übergang",
    mode_cooling: "Kühlung",
    mode_off: "Aus",
    tab_rooms: "Räume",
    tab_flags: "Meldungen",
    tab_tuning: "Abstimmung",
    tab_assist: "Zusatzquelle",
    tab_hp: "Wärmepumpe",
    tab_manifolds: "Verteiler",
    tab_overview: "Übersicht",
    // Übersicht (Dashboard)
    ov_comfort_cap: "Komfort",
    ov_comfort_sub: "geregelte Räume im Komfortband",
    ov_none_live: "Kein Raum steht auf „Aktiv“.",
    ov_cold: "zu kalt",
    ov_warm: "zu warm",
    ov_nodata: "ohne Messwert",
    ov_off: "aus",
    ov_no_cool: "ohne Kühlung",
    ov_house_off: "Hausmodus: {mode} — nichts wird geregelt.",
    ov_band_cap: "Komfortband ±{v} K",
    tip_ov_comfort:
      "Ein Raum liegt im Komfortband, wenn der Messwert höchstens um das " +
      "Totband des Reglers vom Sollwert abweicht (Parameter „Totband um den " +
      "Sollwert“, global oder je Raum). Es zählen nur geregelte Räume: auf " +
      "„Aktiv“, beim Kühlen nur die am Kühlen teilnehmenden, im Hausmodus „Aus“ " +
      "keiner. Auf der Achse ist jeder Punkt ein Raum: links kälter, rechts " +
      "wärmer als der Sollwert; das grüne Band ist das globale Band (ein Raum " +
      "mit eigenem Band wird nach diesem bewertet). Ein Klick auf einen Punkt " +
      "öffnet die Raumdetails.",
    ov_dev_below: "{v} K unter dem Sollwert",
    ov_dev_above: "{v} K über dem Sollwert",
    ov_dev_ok: "im Komfortband",
    ov_att_cap: "Erfordert Aufmerksamkeit",
    ov_att_ok: "Alles in Ordnung",
    ov_att_ok_sub: "Der Algorithmus läuft, keine Meldung erfordert eine Reaktion.",
    ov_att_info: "Zur Information:",
    ov_att_more: "+{n} weitere im Tab Meldungen",
    ov_dev_cap: "Abweichung vom Sollwert",
    ov_inband_sub: "der Zeit im Komfortband (24 Std.)",
    ov_fan_tip: "Median {mid} · Streuung {lo} … {hi}",
    tip_ov_dev:
      "Die letzten 24 Stunden der Räume auf „Aktiv“ (5-Minuten-Mittelwerte des " +
      "Recorders; oben = wärmer als der Sollwert). Die Linie ist der Median der " +
      "Abweichung, die graue Fläche die Streuung vom kältesten bis zum wärmsten " +
      "Raum, das grüne Band das Komfortband; Rot und Blau markieren die Streuung " +
      "über und unter dem Band. Die Zahl ist der Zeitanteil, den die Räume in " +
      "ihrem Komfortband verbracht haben. Details je Raum stehen im Tab Räume.",
    ov_minmax_24h: "min. {min} · max. {max} (24 Std.)",
    ov_floor_cap: "Fußbodenkreise",
    ov_floor_sub: "mittlere Ventilöffnung",
    ov_loops_open: "offene Kreise: {n} von {m}",
    tip_ov_floor:
      "Mittlere Ventilöffnung der Räume auf „Aktiv“, gewichtet mit der " +
      "Kreisanzahl des Raums (ein Raum mit drei Kreisen zählt dreifach). Ein " +
      "Kreis gilt ab 5 % als offen. Das Diagramm: derselbe Mittelwert über die " +
      "letzten 24 Stunden (5-Minuten-Mittelwerte des Recorders).",
    ov_assist_note: "jetzt in Betrieb",
    ov_assist_idle: "alle in Ruhe",
    ov_assist_manual: "manuelle Steuerung: {n}",
    ov_dhw: "WW",
    ov_dew_cap: "Kondensationsschutz",
    ov_dew_worst: "höchster Raum-Taupunkt: {v}",
    ov_dew_throttled: "gedrosselte Räume: {n}",
    ov_plant_cap: "Anlage",
    manage_rooms: "Räume verwalten",
    menu_btn: "Seitenmenü",
    loading: "Wird geladen…",
    no_rooms: "Keine Räume konfiguriert.",
    th_room: "Raum",
    th_measured: "Gemessen",
    th_setpoint: "Sollwert",
    th_error: "Abweichung",
    th_valve: "Ventil",
    th_supply: "Vorlauf",
    th_return: "Rücklauf",
    th_assist_mode: "Modus",
    th_assist_temp: "Temp.",
    th_assist_group: "Zusatzquelle",
    th_state: "Regelung",
    card_setpoint: "Sollwert",
    card_offset: "Offset {v}",
    state_off: "Aus",
    state_live: "Aktiv",
    confirm_state_live:
      "Regelung von „{room}“ aktivieren? Tortoise beginnt, Befehle an die " +
      "Ventile und die Zusatzquelle zu schreiben.",
    confirm_state_off:
      "Regelung von „{room}“ deaktivieren? Die Aktoren werden sicher geparkt " +
      "und es wird nichts mehr geschrieben.",
    confirm_mode: "Den Hausmodus auf „{mode}“ ändern? Dies betrifft jeden Raum.",
    confirm_yes: "Ja, ändern",
    confirm_cancel: "Abbrechen",
    assist_split: "Split",
    assist_heater: "Heizgerät",
    assist_none: "—",
    assist_no_source: "keine Zusatzquelle",
    assist_state_off: "aus",
    assist_state_heating: "heizt",
    assist_state_cooling: "kühlt",
    assist_state_dry: "entfeuchtet",
    kind_split: "Klimagerät (Split)",
    kind_heater: "Elektroheizung",
    detail_close: "Schließen",
    sec_wiring: "Sensoren & Signale",
    wire_source_tip: "Quelle anzeigen (Entität)",
    sec_decision: "Reglerentscheidung",
    sec_history: "Verlauf",
    sec_diagnostics: "Diagnose-Entitäten",
    chart_temp: "Temperatur",
    chart_setpoint: "Sollwert",
    chart_valve: "Ventil",
    chart_hum: "Luftfeuchte",
    chart_error: "Abweichung",
    chart_split: "Assistenz",
    chart_loading: "Verlauf wird geladen…",
    chart_nodata: "Keine Verlaufsdaten",
    chart_unavailable: "Keine darstellbaren Entitäten",
    win_6h: "6 Std.",
    win_24h: "24 Std.",
    win_7d: "7 Tage",
    role_entity_temp_room: "Raumtemperatur",
    role_entity_humidity: "Luftfeuchtigkeit",
    role_entity_temp_outdoor: "Außentemperatur",
    role_entity_valves: "Ventile",
    role_entity_supply: "Vorlauf",
    role_entity_return: "Rücklauf",
    role_entity_fast_source: "Zusatzquelle",
    wire_missing: "fehlt",
    wire_unset: "— nicht zugewiesen —",
    wire_unavailable: "nicht verfügbar",
    dec_error: "Regelabweichung",
    dec_dew: "Taupunkt des Raums",
    dec_terms: "Anteile der Glieder an der Ventilstellung",
    term_p: "P — Proportional",
    term_i: "I — Integral",
    term_trend: "Trend — Dämpfung",
    term_ff: "FF — Wetter",
    dec_raw_valve: "Rohwert Ventil (vor Begrenzung)",
    dec_final_valve: "Endgültige Ventilstellung",
    dec_throttle: "Kühl-Durchfluss",
    dec_integrator: "Integrator",
    integ_frozen: "eingefroren",
    integ_active: "aktiv",
    dec_saturated: "Ventilsättigung",
    dec_floor: "Ventil-Mindestöffnung (Minimum)",
    dec_fast: "Zusatzquelle",
    dec_explanation: "Erläuterung",
    yes: "ja",
    no: "nein",
    raw_show: "Rohdaten anzeigen",
    no_live_room: "Noch keine Live-Daten für diesen Raum.",
    tune_intro:
      "Globale Reglerabstimmung plus vereinzelte Übersteuerungen je Raum. " +
      "Gespeichert werden nur Werte, die vom Standard abweichen — ein Wert " +
      "auf dem Standard folgt künftigen Änderungen des Standards. " +
      "Das Speichern lädt den Regler neu (der PID-Integrator wird zurückgesetzt).",
    tune_scope_global: "Global",
    tune_save: "Speichern",
    tune_saving: "Wird gespeichert…",
    tune_saved: "Gespeichert",
    tune_overridden: "übersteuert",
    tune_revert: "Auf global zurücksetzen",
    tune_custom: "angepasst",
    tune_reset_default: "Auf Standard zurücksetzen",
    tune_old_default: "alter Standard",
    tune_old_default_tip:
      "Dieser Wert ist ein früherer Standardwert, den eine ältere Version " +
      "der Integration dauerhaft gespeichert hat — nicht Ihre Wahl. Setzen " +
      "Sie ihn auf den Standard zurück, sofern Sie ihn nicht bewusst gewählt haben.",
    tune_on: "ein",
    tune_off: "aus",
    tune_grp_pid: "PI-Regler & Trend",
    tune_grp_band: "Komfortband & Ventil",
    tune_grp_weather: "Wettervorsteuerung (Ventilöffnung)",
    tune_grp_fast: "Zusatzquelle (Split/Heizgerät)",
    tune_grp_dew: "Kondensationsschutz (Kühlung)",
    tune_grp_cooling_water: "Kühlwasser — Wärmepumpe (global)",
    tune_grp_heating_water: "Heizwasser — Wärmepumpe (global)",
    tune_grp_other: "Sonstige",
    tune_kp: "Proportionalverstärkung (P)",
    tune_ki: "Integralverstärkung (I)",
    tune_kt: "Dämpfung des Temperaturtrends",
    tune_deadband_c: "Totband um den Sollwert",
    tune_valve_floor_pct: "Minimale Ventilöffnung beim Heizen",
    tune_boost_offset_c: "Zuschaltschwelle der Zusatzquelle",
    tune_fast_min_on_minutes: "Mindestlaufzeit der Zusatzquelle",
    tune_fast_min_off_minutes: "Mindeststillstandszeit der Zusatzquelle",
    tune_fast_target_offset_k: "Sollwert-Überhöhung der Zusatzquelle",
    tune_dry_enabled: "Entfeuchtungs-Assistent",
    tune_dry_dew_max_c: "Taupunktschwelle der Entfeuchtung",
    tune_fast_manual_hold_minutes: "Haltezeit der manuellen Steuerung",
    tune_dew_margin_k: "Sicherheitsmarge über dem Taupunkt",
    tune_dew_ramp_k: "Rampenbreite der Kühldrosselung",
    tune_outdoor_ff_enabled: "Wettervorsteuerung aktiviert",
    tune_ff_neutral_c: "Neutrale Außentemperatur der Vorsteuerung",
    tune_ff_gain_pct_per_k: "Verstärkung der Vorsteuerung",
    tune_ff_max_pct: "Obergrenze der Vorsteuerung",
    tune_cooling_supply_base_c: "Basistemperatur Kühlwasser",
    tune_heating_supply_base_c: "Basistemperatur Heizwasser",
    tune_heating_supply_slope: "Kurvensteigung Heizwasser",
    tune_heating_curve_neutral_c: "Neutrale Außentemperatur der Heizkurve",
    tune_heating_supply_min_c: "Minimale Heizwassertemperatur",
    tune_heating_supply_max_c: "Maximale Heizwassertemperatur",
    tune_heating_gate_enabled: "Auf sicheren Verdichterzustand warten",
    tune_heating_curve_hysteresis_k: "Hysterese der Heizwasserkurve",
    tune_heating_restart_band_k: "Wiederanlaufband der Pumpe",
    tune_heating_stop_offset_k: "Abschaltschwelle der Pumpe",
    ast_th_kind: "Art",
    ast_th_group: "Gruppe",
    ast_th_command: "Befehl",
    ast_th_actual: "Ist-Zustand",
    ast_th_timer: "Timer",
    ast_th_hours: "Erlaubte Zeiten",
    ast_hours_always: "immer",
    assist_window_sub: "erlaubt {start}–{end}",
    ast_th_flags: "Meldungen",
    ast_th_entity: "Entität",
    ast_none_line: "Keine Zusatzquelle: {rooms}",
    ast_timer_unlock: "entsperrt in ~{n} min",
    ast_timer_locked: "Mindestlaufzeit-Sperre",
    ast_timer_conflict: "Gruppenkonflikt",
    ast_tune_link: "abstimmen",
    ast_actual_unknown: "—",
    ast_empty: "Kein Raum hat eine Zusatzquelle.",
    dew_reason_no_humidity: "kein Luftfeuchtigkeitssensor",
    dew_reason_cooling_disabled: "Kühlung deaktiviert",
    dew_reason_not_cooling_mode: "Raum kühlt nicht",
    dew_reason_no_temperature: "keine Temperaturmessung",
    dew_none_collective: "{n} Räume ohne Taupunktdaten",
    dec_dew_reason: "Ausschluss vom sicheren Taupunkt",
    err_ws: "Integrationsaufruf fehlgeschlagen: {detail}",
    tooltip_show: "Erläuterung anzeigen",
    tooltip_hide: "Erläuterung ausblenden",
    // Tooltips der Abstimmungsparameter (tip_knob_*) — Inhalt gemeinsam mit
    // docs/manual/de.md §8: BEIDE Stellen zusammen ändern.
    tip_knob_kp:
      "Proportionalverstärkung: wie viel Prozent Ventilöffnung jede 1 K " +
      "Regelabweichung (Sollwert − gemessen) hinzufügt. Höher reagiert schneller, " +
      "riskiert aber Überschwingen des trägen Bodens mit hoher Masse. Standard 14; " +
      "typisch 8–20. In kleinen Schritten ändern und einen ganzen Tag beobachten.",
    tip_knob_ki:
      "Integralverstärkung: wie schnell der Regler Öffnung hinzufügt, während eine " +
      "kleine Regelabweichung über Stunden anhält — beseitigt die bleibende " +
      "Regelabweichung. Die Werte sind bewusst sehr klein (Sekunden im Nenner). " +
      "Standard 0,0015. Zu hoch = langsame Temperaturschwingung um den Sollwert.",
    tip_knob_kt:
      "Trenddämpfung: beobachtet, wie schnell sich die Temperatur bewegt. Wenn sich " +
      "der Raum dem Sollwert schnell nähert, schließt sie das Ventil frühzeitig etwas " +
      "und zähmt das Überschwingen des trägen Bodens. Standard 12. Muss selten " +
      "geändert werden.",
    tip_knob_deadband_c:
      "Totband um den Sollwert (±): innerhalb davon jagt der Regler keine winzigen " +
      "Abweichungen, was Ventilbewegungen und Takten reduziert. Standard 0,3 K; " +
      "typisch 0,2–0,5 K.",
    tip_knob_valve_floor_pct:
      "Minimale Ventilöffnung, solange der Raum Wärme anfordert, um einen sinnvollen " +
      "Durchfluss im Heizkreis zu halten. Standard 15 %. 0 deaktiviert die " +
      "Mindestöffnung.",
    tip_knob_boost_offset_c:
      "Zuschaltschwelle: sobald die Regelabweichung diesen Wert überschreitet, " +
      "schaltet sich die Zusatzquelle (Split/Heizgerät) zu. Sie gibt innerhalb des " +
      "Komfortbands wieder frei; der Boden bleibt stets die Basisquelle. Muss größer " +
      "als das Totband sein. Standard 1,5 K.",
    tip_knob_fast_min_on_minutes:
      "Mindestlaufzeit der Zusatzquelle nach dem Einschalten. Schützt den " +
      "Split-Kompressor vor Takten und beruhigt Richtungswechsel in einer " +
      "Multisplit-Gruppe. Standard 30 min; Minimum 3.",
    tip_knob_fast_min_off_minutes:
      "Mindeststillstandszeit der Zusatzquelle, bevor sie wieder starten darf. " +
      "Standard 31 min; Minimum 3.",
    tip_knob_fast_target_offset_k:
      "Um wie viel Kelvin der an den Split gesendete Sollwert während eines Boosts " +
      "über den Raum-Sollwert hinaus angehoben wird (Heizung: Sollwert + Wert, " +
      "Kühlung: Sollwert − Wert). Der deckenmontierte Sensor des Splits misst wärmer " +
      "als der Raumsensor — ohne die Überhöhung drosselt sich die Einheit selbst, " +
      "bevor der Boost geliefert wird. Über das Abschalten entscheidet weiterhin der " +
      "Raumsensor. 0 = keine Überhöhung (der Split erhält den reinen Sollwert). " +
      "Standard 0 — erhöhen Sie ihn (z. B. 1 K), falls sich der Split vor dem " +
      "Boost selbst drosselt.",
    tip_knob_dry_enabled:
      "Beim Kühlen wird der Split (nur Klimagerät) in seinem Entfeuchtungsmodus " +
      "eingeschaltet, sobald der Taupunkt des Raums die Schwelle überschreitet — " +
      "auch ohne Temperaturanforderung. Der Boden kühlt nur sensibel und kann " +
      "nicht entfeuchten; ein steigender Taupunkt hebt das Kühlwasser an und " +
      "nimmt dem Boden Leistung. Entfeuchten senkt den Taupunkt und gibt dem " +
      "Boden Leistung zurück. Ein Temperatur-Boost hat immer Vorrang. " +
      "Standardmäßig aus.",
    tip_knob_dry_dew_max_c:
      "Taupunkt des Raums, oberhalb dessen die Entfeuchtung startet [°C]. " +
      "Start nur, wenn der Raum auf oder über dem Sollwert liegt (ein Split " +
      "kühlt auch im Entfeuchten). Freigabe 1 K unter der Schwelle " +
      "(Hysterese) oder sobald der Raum über das Totband hinaus unterkühlt. " +
      "Standard 16 °C — die Entfeuchtung startet, bevor die Luft stickig " +
      "wirkt (ab ~17–18 °C Taupunkt).",
    tip_knob_fast_manual_hold_minutes:
      "Tut der Split physisch etwas anderes als der letzte Befehl (per " +
      "Fernbedienung eingeschaltet, ausgeschaltet, Richtung geändert), wertet " +
      "der Regler das als Ihre Entscheidung: Er übernimmt den Zustand des " +
      "Geräts und spiegelt ihn für so viele Minuten seit der letzten Berührung " +
      "nur — schreibt nichts, überschreibt ihn nicht durch das erneute " +
      "Erzwingen und kippt einen kühlenden Split nie direkt auf Heizen. Danach " +
      "läuft die normale Logik weiter (ein Richtungswechsel geht weiterhin " +
      "über AUS und die Mindeststillstandszeit). Die Sicherheitsregeln S3/S4 " +
      "und ein verlorener Sensor beenden das Halten. 0 = aus (wie früher: " +
      "Abweichungsmeldung und erneutes Erzwingen nach ~45 min). Standard 60 min.",
    tip_knob_dew_margin_k:
      "Kühlung: solange der Vorlauf mindestens so viele K über dem Taupunkt liegt, " +
      "läuft das Ventil uneingeschränkt; näher am Taupunkt wird der Durchfluss " +
      "gedrosselt (Kondensationsschutz am Boden). Standard 1,5 K (die " +
      "Wärmepumpe hält das Wasser 2 K über dem Taupunkt, normale Verzögerung " +
      "des Wassers drosselt also nicht); erhöhen Sie ihn, " +
      "falls Sie jemals Feuchtigkeit sehen.",
    tip_knob_dew_ramp_k:
      "Breite der Drosselrampe unterhalb der Taupunkt-Marge: über diese Spanne fällt " +
      "der Durchfluss linear von 100 % auf 0. Kleiner = schärferes Abschneiden. " +
      "Standard 2 K.",
    tip_knob_outdoor_ff_enabled:
      "Wettervorsteuerung: fügt eine Grundöffnung des Ventils basierend auf der " +
      "Außentemperatur hinzu, bevor der Raum Zeit hat auszukühlen. Nützlich bei " +
      "großen Glasflächen und Kälteeinbrüchen. Standardmäßig aus.",
    tip_knob_ff_neutral_c:
      "Außentemperatur, bei der das Wetterglied null ist; darunter fügt jede 1 K " +
      "Kälte Öffnung gemäß der FF-Verstärkung hinzu. Verschiebt die Heizwasserkurve " +
      "nicht (sie hat eine eigene neutrale Temperatur). Standard 15 °C.",
    tip_knob_ff_gain_pct_per_k:
      "Wie viel Prozent Öffnung das Wetterglied je 1 K unterhalb der neutralen " +
      "Temperatur hinzufügt. Standard 1 %/K.",
    tip_knob_ff_max_pct:
      "Obergrenze für das Wetterglied — die Vorsteuerung fügt nie mehr als diesen " +
      "Prozentsatz an Öffnung hinzu. Standard 20 %.",
    tip_knob_cooling_supply_base_c:
      "Der an die Wärmepumpe geschriebene Kühlwasser-Sollwert, während das Haus " +
      "kühlt: Der geschriebene Wert ist das Maximum aus dieser Basis und dem globalen " +
      "sicheren Taupunkt — nie unter dem Taupunkt. Nur aktiv, wenn die " +
      "Kühl-Sollwert-Entität konfiguriert ist (Optionen → Wärmepumpe). Globaler " +
      "Parameter — kann nicht je Raum übersteuert werden. Standard 15 °C.",
    tip_knob_heating_supply_base_c:
      "Heizungsvorlauf bei der neutralen Außentemperatur der Kurve (Parameter " +
      "„Neutrale Außentemperatur der Heizkurve“, Standard 15 °C). Darunter " +
      "steigt der Sollwert entlang der Kurvensteigung; das Ergebnis wird auf " +
      "Minimum und Maximum des Heizwassers begrenzt (Standard 20–40 °C). Nur " +
      "an die Pumpe geschrieben, wenn die Heiz-Sollwert-Entität konfiguriert ist " +
      "— standardmäßig folgt die Pumpe ihrer eigenen Kurve. Globaler Parameter. " +
      "Standard 26 °C.",
    tip_knob_heating_supply_slope:
      "Um wie viele Kelvin der Heizwasser-Sollwert je 1 K Außentemperatur unterhalb " +
      "des Neutralpunkts der Kurve steigt. Schrittweite 0,01, damit sich eine " +
      "Pumpenkurve nachbilden lässt: 25 °C bei +15 °C und 32 °C bei −15 °C ergibt " +
      "7/30 ≈ 0,23 K/K. Standard 0,5 K/K; typisch 0,2–0,8 für ein gut gedämmtes " +
      "Fußbodenheizungshaus.",
    tip_knob_heating_curve_neutral_c:
      "Außentemperatur, bei der die Heizwasserkurve genau die Basis liefert; " +
      "darunter steigt der Sollwert entlang der Steigung. Ein eigener Parameter " +
      "der Kurve — eine Änderung der neutralen Temperatur der Vorsteuerung " +
      "(Ventile) verschiebt sie nicht mehr. Globaler Parameter. Standard 15 °C.",
    tip_knob_heating_supply_min_c:
      "Untere Grenze der Heizwasserkurve: Der Sollwert fällt nie darunter. Muss " +
      "unter dem Maximum liegen, und die Basis muss dazwischen liegen. Globaler " +
      "Parameter. Standard 20 °C.",
    tip_knob_heating_supply_max_c:
      "Obere Grenze der Heizwasserkurve: Der Sollwert überschreitet sie nie. Eine " +
      "reine Fußbodenheizung will oft weniger als 40 °C (Estrich). Globaler " +
      "Parameter. Standard 40 °C.",
    tip_knob_heating_gate_enabled:
      "Panasonic: Eine Änderung des Heizwasser-Sollwerts bei stehendem " +
      "Verdichter kann dessen Wiederanlauf blockieren. Mit verbundenen " +
      "Entitäten für Verdichterfrequenz und Vorlauftemperatur schreibt " +
      "Tortoise einen neuen Sollwert nur, wenn es sicher ist: bei laufendem " +
      "Verdichter eine Erhöhung sofort und eine Absenkung erst, wenn der " +
      "Vorlauf unter dem neuen Sollwert + Abschaltschwelle liegt; bei " +
      "stehendem Verdichter eine Erhöhung nur, wenn der Vorlauf unter dem " +
      "neuen Sollwert − Wiederanlaufband liegt (der Verdichter startet " +
      "sofort), und eine Absenkung nur, wenn der Vorlauf auf oder über dem " +
      "neuen Sollwert liegt. Sonst behält die Pumpe ihren bisherigen " +
      "Sollwert, und die Entscheidung wird jeden Zyklus neu geprüft. Ein " +
      "unveränderter Sollwert wird nicht erneut gesendet. Aus = jeder " +
      "Sollwert wird sofort geschrieben (wie vor Issue #33). Globaler " +
      "Parameter. Standard ein.",
    tip_knob_heating_curve_hysteresis_k:
      "Spiel auf dem rohen Kurvenwert, bevor er auf die Schrittweite der " +
      "Pumpen-Entität gerundet wird [K]. Der Sollwert ändert sich erst, " +
      "wenn die Kurve so weit über eine Rundungsgrenze gewandert ist — ein " +
      "Schwanken der Außentemperatur um 0,1 K schaltet den Sollwert nicht " +
      "mehr jeden Zyklus um. 0 schaltet es ab. Gilt auch bei " +
      "ausgeschalteter Sperre. Globaler Parameter. Standard 0,5 K.",
    tip_knob_heating_restart_band_k:
      "Das eigene „ΔT für Heizen EIN“ der Pumpe [K]: Der Verdichter startet " +
      "wieder, sobald der Vorlauf so weit unter den Sollwert fällt. Eine " +
      "Erhöhung bei stehendem Verdichter wird nur geschrieben, wenn der " +
      "Vorlauf unter dem neuen Sollwert minus diesem Band liegt. Ist in den " +
      "Optionen (Wärmepumpe) eine Entität für das Wiederanlaufband gesetzt " +
      "und liefert sie 1–10 K, hat sie Vorrang. Globaler Parameter. " +
      "Standard 3 K.",
    tip_knob_heating_stop_offset_k:
      "Die eigene Abschaltschwelle der Pumpe [K]: Ein laufender Verdichter " +
      "stoppt, sobald der Vorlauf den Sollwert um so viel überschreitet. " +
      "Eine Absenkung bei laufendem Verdichter wird erst geschrieben, wenn " +
      "der Vorlauf unter dem neuen Sollwert plus dieser Schwelle liegt — " +
      "sonst würde der Schreibvorgang den Lauf sofort beenden. Globaler " +
      "Parameter. Standard 2 K.",
    // Tooltips der Raumdetails (tip_dec_*) und der Tabellenköpfe (tip_ast_* /
    // tip_th_*) — Inhalt gemeinsam mit docs/manual/de.md: zusammen
    // ändern.
    tip_th_assist:
      "Der Befehl für die Zusatzquelle des Raums (Split/Heizgerät): Betriebsrichtung " +
      "und Zieltemperatur der Einheit. „—“ = der Raum hat keine Zusatzquelle; " +
      "„aus“ = die Zusatzquelle ruht (der Boden hält den Raum).",
    tip_assist_target:
      "Der Sollwert des Splits kann um den Parameter „Sollwert-Überhöhung der " +
      "Zusatzquelle“ vom Raum-Sollwert abweichen (Standard 0 — der Split erhält " +
      "das Ziel; Heizung: +, Kühlung: −). Der eigene Sensor des Splits sitzt nahe " +
      "der Decke und kann wärmer messen als unser Raumsensor — dann drosselt ein " +
      "Sollwert gleich dem Ziel die Einheit, bevor der Boost den Raum tatsächlich " +
      "erreicht, und eine Erhöhung (z. B. 1 K) hilft. Über das Abschalten entscheidet " +
      "weiterhin UNSER Raumsensor mit Hysterese, sodass der Raum nicht davonlaufen " +
      "kann. Passen Sie ihn in der Abstimmung an (0 = keine Überhöhung). Im " +
      "Übergangsmodus entspricht der Sollwert dem Ziel.",
    tip_dec_error:
      "Regelabweichung = Sollwert − gemessen. Positiv bedeutet zu kalt (öffnet beim " +
      "Heizen das Ventil); bei der Kühlung wirkt das Vorzeichen umgekehrt.",
    tip_dec_trend:
      "Gefilterte Änderungsrate der Raumtemperatur in K/h. Speist das Trendglied — " +
      "ein schneller Anstieg zum Sollwert schließt das Ventil frühzeitig etwas. Nach " +
      "einer Datenlücke braucht der Filter ~30–45 min zum Einschwingen.",
    tip_dec_terms:
      "Woraus sich die Rohöffnung zusammensetzt: P reagiert auf die aktuelle " +
      "Regelabweichung, I auf eine über die Zeit anhaltende Abweichung, Trend dämpft " +
      "eine sich schnell ändernde Temperatur, FF fügt die Wetter-Grundlinie hinzu. " +
      "Ihre Summe = Rohwert Ventil.",
    tip_dec_raw_valve:
      "Vom Regler BERECHNETE Öffnung VOR den Begrenzungen: Ventil-Mindestöffnung, " +
      "0–100-%-Begrenzung und Sicherheitsregeln.",
    tip_dec_final_valve:
      "Die tatsächlich an die Aktoren gesendete Öffnung (nach Begrenzungen und " +
      "Sicherheitsregeln). Ein Wert für alle Heizkreise des Raums.",
    tip_dec_throttle:
      "Taupunkt-Drosselfaktor (nur Kühlung): 100 % = uneingeschränkt, 0 % = Ventil " +
      "geschlossen, weil der Vorlauf den Taupunkt erreicht hat (Meldung s2_throttle). " +
      "0 % wird auch vorsorglich angewendet, wenn Luftfeuchtigkeits- oder " +
      "Vorlaufdaten fehlen.",
    tip_dec_integrator:
      "Das Gedächtnis des Integralanteils. „Eingefroren“ bedeutet bewusst " +
      "angehalten — z. B. die Wärmepumpe führt WW-Bereitung/Abtauung durch, ein " +
      "Sensor ist verloren oder der Raum ist aus — damit der Integralanteil nicht " +
      "fälschlich aufläuft.",
    tip_dec_saturated:
      "Das Ventil hat die Grenze 0 oder 100 % erreicht. Der Regler will mehr, als " +
      "die Physik zulässt; das Anti-Windup verhindert, dass der Integralanteil gegen " +
      "die Wand läuft.",
    tip_dec_floor:
      "Ob die Ventil-Mindestöffnung beim Heizen (Parameter „Ventil-Mindestöffnung“) " +
      "in diesem Zyklus angewendet wurde.",
    tip_dec_dew:
      "Taupunkt, berechnet aus Raumtemperatur und Luftfeuchtigkeit. Darunter " +
      "kondensiert Wasser am Boden — daher beide Kühlschutzmechanismen. Daneben: der " +
      "Grund, wenn der Raum nichts zum globalen sicheren Taupunkt beiträgt.",
    tip_dec_fast:
      "Befehl für den Split/das Heizgerät: ein + Richtung + Zieltemperatur. Der " +
      "Split regelt sich selbst; er ersetzt nie den Boden, sondern unterstützt nur " +
      "oberhalb der Zuschaltschwelle.",
    tip_dec_flags:
      "Die Diagnosemeldungen dieses Zyklus — jede benennt den Schutz oder die " +
      "Begrenzung, die gewirkt hat. Das vollständige Meldungsverzeichnis steht im " +
      "Handbuch (docs/manual/de.md §12).",
    tip_ast_timer:
      "Sperre der Mindest-Ein-/Ausschaltzeit: so viel Zeit muss noch vergehen, bevor " +
      "die Zusatzquelle den Zustand wechseln darf. Schützt den Kompressor.",
    tip_ast_group:
      "Räume an einem gemeinsamen Außengerät (Multisplit) müssen alle heizen oder " +
      "alle kühlen. Der Verlierer der Abwägung wird zwangsweise abgeschaltet, bis " +
      "sich die Gruppenrichtung ändert (der größte Bedarf gewinnt; die bestehende " +
      "Richtung erhält einen Bonus von 0,5 K).",
    tip_ast_hours:
      "Das Zeitfenster, in dem die Zusatzquelle dieses Raums laufen darf (z. B. " +
      "07:00–22:00; das Fenster darf über Mitternacht reichen). Außerhalb schaltet " +
      "sich der Split nicht ein; läuft er beim Fensterende noch, stoppt er erst nach " +
      "seiner Mindestlaufzeit — der Kompressorschutz steht über der Pünktlichkeit. " +
      "Notheizen/-kühlen (S3/S4) darf die Ruhezeiten durchbrechen. Leer = immer " +
      "erlaubt.",
    // Reiter Wärmepumpe (B2) — Inhalt gemeinsam mit docs/manual/de.md §11.
    hp_empty:
      "Die Wärmepumpen-Anbindung ist nicht konfiguriert. Verweisen Sie die " +
      "Integration auf die Entitäten der Pumpe unter Einstellungen → Geräte & " +
      "Dienste → Tortoise-UFH → Konfigurieren → Wärmepumpe. Alles ist optional — " +
      "unkonfiguriert berührt Tortoise die Pumpe nie.",
    hp_sec_mode: "Betriebsmodus",
    hp_tortoise_mode: "Tortoise-Modus",
    hp_current_option: "Wärmepumpen-Modus",
    hp_desired_option: "Zielmodus",
    hp_in_sync: "synchron",
    hp_diverged: "abweichend",
    hp_dhw_only_note:
      "Die Pumpe ist derzeit nur im WW-Betrieb — Tortoise schreibt den Modus nicht, " +
      "bis die WW-Automatik die Richtung zurückgibt.",
    hp_no_force:
      "Im Übergang und Aus erzwingt Tortoise nie eine Richtung — die eigene " +
      "Automatik der Pumpe und die WW-Bereitung bleiben zuständig.",
    tip_hp_mode:
      "Tortoise synchronisiert nur die RICHTUNG des Pumpenmodus: Heizung → die " +
      "Variante „Heat“, Kühlung → die Variante „Cool“, stets unter Beibehaltung des " +
      "Teils „+DHW“ (das WW-Flag gehört der externen Automatik). Schreibvorgänge sind " +
      "selten: bei einem Tortoise-Moduswechsel oder wenn eine Abweichung ≥2 Zyklen " +
      "anhält, höchstens alle 15 Minuten. Tortoise steuert nie den Kompressor oder " +
      "die Kurve der Pumpe.",
    hp_sec_dhw: "Warmwasserbereitung (WW)",
    hp_dhw_switch: "WW-Flag (+DHW)",
    hp_dhw_warning:
      "Hinweis: Die externe WW-Automatik ist Eigentümerin dieses Flags und darf es " +
      "jederzeit überschreiben — das ist ihr Recht. Dieser Schalter ist eine manuelle " +
      "Anforderung („Wasser jetzt aufheizen / stopp“), keine Sperre.",
    tip_hp_dhw:
      "Fügt den Teil „+DHW“ der Pumpenmodus-Entität hinzu oder entfernt ihn (z. B. " +
      "„Heat only“ ↔ „Heat+DHW“). Solange die Pumpe im „DHW only“ ist, ist das " +
      "Entfernen des Flags unmöglich — es gibt keine Richtung, zu der zurückgekehrt " +
      "werden könnte.",
    hp_sec_setpoints: "Wasser-Sollwerte",
    hp_cool_target: "Kühlwasser-Sollwert",
    hp_cool_calc: "max(Basis {base}, sicherer Taupunkt {dew})",
    hp_heat_target: "Heizwasser-Sollwert",
    hp_heat_calc:
      "Kurve: {base} + {slope} · ({neutral} − Außen {tout}), innerhalb {min}–{max}",
    hp_curve_cap: "Vorschau der Heizwasserkurve",
    hp_curve_tout: "Außen",
    hp_curve_target: "Wasser",
    hp_curve_unsaved: "enthält ungespeicherte Änderungen",
    hp_curve_invalid: "Ungültige Werte: {detail}",
    tip_hp_curve:
      "Der Heizwasser-Sollwert, den Tortoise bei der jeweiligen Außentemperatur " +
      "schreiben würde (auf Minimum und Maximum begrenzt). Vergleichen Sie ihn " +
      "mit der eigenen Kurve der Pumpe, bevor Sie die Heiz-Sollwert-Entität " +
      "verbinden.",
    hp_not_written: "in diesem Modus nicht geschrieben",
    hp_not_configured_entity: "Entität nicht konfiguriert",
    tip_hp_cool:
      "Beim Kühlen schreibt Tortoise einen Wasser-Sollwert = das Maximum aus der " +
      "Basis (ein Abstimmungsparameter) und dem globalen sicheren Taupunkt (höchster " +
      "Taupunkt der gekühlten Räume + 2 K) — das Wasser gelangt nie in die " +
      "Kondensationszone. Geschrieben bei einer Änderung ≥ 0,5 K, alle ~45 min " +
      "erneut erzwungen.",
    tip_hp_heat:
      "Optional: Beim Heizen schreibt Tortoise den Wasser-Sollwert aus einer " +
      "einfachen Wetterkurve (Basis + Steigung × Unterschreitung der neutralen " +
      "Temperatur der Kurve, begrenzt auf ihr Minimum und Maximum — standardmäßig " +
      "20–40 °C). Wenn Sie die Entität unkonfiguriert " +
      "lassen, folgt die Pumpe ihrer eigenen Kurve — der Standard und vollständig " +
      "unterstützt. " +
      "Der Schreibvorgang läuft über die Heizsollwert-Sperre (Karte unten).",
    hp_active_cap: "Pumpe für Fußbodenheizung verfügbar",
    hp_active_unknown: "unbekannt",
    tip_hp_active:
      "Das Signal „Pumpe bedient die Fußbodenheizung“: Solange es aus ist " +
      "(WW-Bereitung/Abtauung), frieren die Raumregler ihren Integralanteil ein, " +
      "damit er nicht aufläuft, während kein Wasser den Boden erreicht.",
    hp_writes_paused: "Schreibvorgänge pausiert — kein Raum ist in Regelung (Aktiv).",
    // Kühlstart erzwingen (issue #7, 2026-07-15).
    tune_hp_flicker_enabled: "Kühlstart erzwingen",
    tune_hp_flicker_band_k: "Angestrebtes Kühl-Totband",
    tune_hp_flicker_min_open_pct: "Min. Abnahme: Gesamtöffnung der Kreise",
    tune_hp_flicker_stuck_minutes: "Verzögerung vor dem Erzwingen",
    tune_hp_flicker_min_off_minutes: "Min. Zeit zwischen erzwungenen Starts",
    tune_hp_flicker_max_starts_per_h: "Max. erzwungene Starts pro Stunde",
    tip_knob_hp_flicker_enabled:
      "Erzwingt den Start des Kühlverdichters (nur Panasonic Aquarea). Wenn " +
      "Räume kühlen wollen, der Verdichter aber stillsteht, senkt es den " +
      "geschriebenen Wasser-Sollwert für einen Zyklus, um die feste 3-K-" +
      "Rücklaufhysterese zu überschreiten und den Verdichter zu starten, und " +
      "stellt dann den taupunktsicheren Wert wieder her. Benötigt die Rücklauf- " +
      "und Verdichterfrequenz-Entitäten. Standardmäßig aus. Global.",
    tip_knob_hp_flicker_band_k:
      "Angestrebtes effektives Kühlwasser-Totband [K]. Muss unter der festen " +
      "3-K-Hysterese der Pumpe liegen, um sie zu verengen — ein Impuls wird " +
      "scharf, sobald der Rücklauf so weit über den geschriebenen Sollwert " +
      "steigt. Global.",
    tip_knob_hp_flicker_min_open_pct:
      "Bedarfsschwelle: die minimale Gesamt-Ventilöffnung der kühlenden Räume, " +
      "gewichtet mit ihrer Kreiszahl (Ventil % × Kreise), bevor ein Start " +
      "erzwungen wird. 100 = ein voll geöffneter Kreis; Maximum = alle Kreise " +
      "× 100. Unterhalb der Schwelle deckt der Pufferspeicher die Abnahme und " +
      "kein Start wird erzwungen — er würde nur den Puffer herunterkühlen und " +
      "den Verdichter takten lassen. Global.",
    tip_knob_hp_flicker_stuck_minutes:
      "Wie lange der Rücklauf scharf verharren muss (Stillstand im Totband mit " +
      "echtem Bedarf), bevor ein Impuls ausgelöst wird [min]. Global.",
    tip_knob_hp_flicker_min_off_minutes:
      "Verdichterschutz: Mindestabstand zwischen zwei erzwungenen Starts [min]. " +
      "Global.",
    tip_knob_hp_flicker_max_starts_per_h:
      "Harte Obergrenze erzwungener Verdichterstarts pro gleitender Stunde. " +
      "Global.",
    hp_sec_flicker: "Kühlstart erzwingen",
    tip_hp_flicker:
      "Panasonic: Wenn die Pumpe in ihrem Totband stillsteht, während Räume noch " +
      "Kühlung anfordern, senkt Tortoise den Kühl-Sollwert für einen Zyklus, um " +
      "den Verdichter aus seiner festen 3-K-Rücklaufhysterese zu lösen, und " +
      "stellt dann den taupunktsicheren Wert wieder her. Nettoeffekt: kälteres " +
      "Durchschnittswasser, während der Rücklauf taupunktsicher bleibt.",
    hp_flicker_on: "aktiviert",
    hp_flicker_off: "deaktiviert",
    hp_flicker_pulsing_badge: "Impuls",
    hp_flicker_state: "Zustand",
    hp_flicker_st_idle: "Ruhe",
    hp_flicker_st_pulse: "Impuls",
    hp_flicker_st_cooldown: "Sperrzeit",
    hp_flicker_trigger: "Schärfschwelle",
    hp_flicker_demand: "Abnahme (Σ Kreisöffnung)",
    hp_flicker_stuck: "Verharren bis Impuls",
    hp_flicker_cooldown: "Sperrzeit übrig",
    hp_flicker_pulses: "Erzwungene Starts (letzte h)",
    hp_flicker_pulses_24h: "Erzwungene Starts (letzte 24 h)",
    hp_flicker_last_pulse: "Letztes Impulsziel",
    hp_flicker_return: "Rücklauf / Eintritt",
    hp_flicker_outlet: "Vorlauf / Austritt",
    hp_flicker_freq: "Verdichter",
    hp_sec_heat_gate: "Heizsollwert-Sperre",
    tip_hp_heat_gate:
      "Panasonic: Ein Schritt des Heizwasser-Sollwerts bei stehendem " +
      "Verdichter kann dessen Wiederanlauf blockieren. Die Sperre " +
      "vergleicht den neuen Sollwert mit dem der Pumpe und mit der " +
      "Vorlauftemperatur: sichere Schritte werden sofort geschrieben, die " +
      "anderen zurückgehalten und jeden Zyklus neu geprüft. Ohne Entität " +
      "für Verdichterfrequenz oder Vorlauf ist die Sperre inaktiv, und der " +
      "Sollwert wird sofort geschrieben.",
    hp_gate_active: "aktiv",
    hp_gate_inactive: "inaktiv",
    hp_gate_held_badge: "zurückgehalten",
    hp_gate_reason: "Entscheidung",
    hp_gate_target: "Zielsollwert",
    hp_gate_pump: "Sollwert in der Pumpe",
    hp_gate_curve: "Kurve (roh)",
    hp_gate_compressor: "Verdichter",
    hp_gate_running: "läuft",
    hp_gate_idle: "steht",
    hp_gate_outlet: "Vorlauf",
    hp_gate_band: "Wiederanlaufband",
    hp_gate_band_entity: "von der Pumpe",
    hp_gate_band_knob: "Parameter",
    hp_gate_stop: "Abschaltschwelle",
    hp_gate_held_for: "Zurückgehalten seit",
    hp_gate_r_no_target: "kein Sollwert in diesem Modus",
    hp_gate_r_in_sync: "Pumpe hat den Sollwert bereits",
    hp_gate_r_ungated_disabled: "Sperre aus — sofort geschrieben",
    hp_gate_r_ungated_no_input:
      "Sperre inaktiv: kein Verdichter-/Vorlaufwert — sofort geschrieben",
    hp_gate_r_write_increase_running: "Erhöhung geschrieben (Verdichter läuft)",
    hp_gate_r_write_decrease_running:
      "Absenkung geschrieben (Vorlauf unter Sollwert + Abschaltschwelle)",
    hp_gate_r_write_increase_idle:
      "Erhöhung geschrieben (Vorlauf unter dem Wiederanlaufband — " +
      "Verdichter startet)",
    hp_gate_r_write_decrease_idle:
      "Absenkung geschrieben (Vorlauf auf oder über dem neuen Sollwert)",
    hp_gate_r_write_unknown_running:
      "geschrieben (Pumpensollwert unbekannt, Verdichter läuft)",
    hp_gate_r_write_unknown_idle:
      "geschrieben (Pumpensollwert unbekannt, Vorlauf unter dem " +
      "Wiederanlaufband)",
    hp_gate_r_hold_decrease_running:
      "Absenkung zurückgehalten: würde den Verdichterlauf beenden",
    hp_gate_r_hold_increase_idle:
      "Erhöhung zurückgehalten: Verdichter steht, Vorlauf im " +
      "Wiederanlaufband",
    hp_gate_r_hold_decrease_idle:
      "Absenkung zurückgehalten: Verdichter steht, Vorlauf unter dem neuen " +
      "Sollwert",
    hp_gate_r_hold_unknown_running:
      "zurückgehalten: Pumpensollwert unbekannt, Vorlauf zu hoch",
    hp_gate_r_hold_unknown_idle:
      "zurückgehalten: Pumpensollwert unbekannt, Verdichter steht",
    tune_heat_kick_enabled: "Verdichter-Anstoß Heizen",
    tune_kick_idle_minutes: "Verdichter-Stillstand vor dem Anstoß",
    tune_kick_deficit_k: "Vorlauf unter dem Pumpensollwert",
    tune_kick_start_timeout_minutes: "Wartezeit auf den Verdichterstart",
    tip_knob_heat_kick_enabled:
      "Panasonic: Nach einem WW-Zyklus kann der Heizverdichter " +
      "stundenlang stehen, obwohl der Vorlauf einige Kelvin unter dem " +
      "Sollwert liegt (blockierter Wiederanlauf). Steht der Verdichter " +
      "mindestens „Verdichter-Stillstand vor dem Anstoß“ lang und liegt " +
      "der Vorlauf mindestens „Vorlauf unter dem Pumpensollwert“ unter " +
      "dem Sollwert der Pumpe, hebt Tortoise den Heizsollwert um 1 K an " +
      "(um einen Entitätsschritt, falls größer) — ein solcher Schritt " +
      "startete den Verdichter in derselben Minute. Nach dem Start kehrt " +
      "der normale Sollwert über die Heizsollwert-Sperre zurück " +
      "(Absenkung im Lauf). Höchstens ein Anstoß pro Stillstand; ohne " +
      "Start innerhalb von „Wartezeit auf den Verdichterstart“ gibt " +
      "Tortoise bis zum nächsten Verdichterlauf auf. Benötigt die " +
      "Heizsollwert-Sperre und ihre Entitäten. Globaler Parameter. " +
      "Standard aus.",
    tip_knob_kick_idle_minutes:
      "Wie lange der Verdichter stehen muss (Frequenz 0), bevor " +
      "angestoßen wird [min]. Globaler Parameter. Standard 15 min.",
    tip_knob_kick_deficit_k:
      "Wie weit der Vorlauf unter dem Heizsollwert der Pumpe liegen muss, " +
      "bevor angestoßen wird [K]. Hält den Anstoß auch aus einem " +
      "WW-Zyklus heraus — der Vorlauf ist dann heiß. Globaler Parameter. " +
      "Standard 3,5 K.",
    tip_knob_kick_start_timeout_minutes:
      "Wie lange nach einem Anstoß auf den Verdichterstart gewartet wird " +
      "[min]. Ohne Start nimmt Tortoise den Anstoß zurück und versucht es " +
      "erst nach dem nächsten Verdichterlauf wieder; den angehobenen " +
      "Sollwert senkt die Sperre erst, wenn es sicher ist (meist nach dem " +
      "Verdichterstart). Globaler Parameter. Standard 5 min.",
    hp_sec_heat_kick: "Verdichter-Anstoß Heizen",
    tip_hp_heat_kick:
      "Panasonic: Ein blockierter Wiederanlauf beim Heizen (z. B. nach " +
      "WW) wird durch einen Sollwertschritt gelöst, nach dem der Vorlauf " +
      "mehr als 3 K unter dem neuen Sollwert liegt. Der Anstoß macht " +
      "einen solchen Schritt (+1 K) höchstens einmal pro Stillstand; " +
      "sobald der Verdichter startet, stellt die Heizsollwert-Sperre den " +
      "normalen Sollwert wieder her. Die 24-h-Zahl stammt aus dem Verlauf " +
      "des Anstoß-Zustandssensors.",
    hp_kick_state: "Zustand",
    hp_kick_reason: "Entscheidung",
    hp_kick_idle: "Verdichter-Stillstand",
    hp_kick_deficit: "Vorlauf unter Pumpensollwert",
    hp_kick_target: "Anstoß-Sollwert",
    hp_kick_timeout: "Wartezeit übrig",
    hp_kick_24h: "Anstöße (letzte 24 h)",
    hp_kick_last: "Letzter Anstoß",
    hp_kick_st_off: "inaktiv",
    hp_kick_st_ready: "bereit",
    hp_kick_st_waiting: "Stillstand",
    hp_kick_st_kick: "Anstoß",
    hp_kick_st_done: "verbraucht",
    hp_kick_r_disabled: "ausgeschaltet",
    hp_kick_r_gate_disabled: "Heizsollwert-Sperre aus",
    hp_kick_r_inactive: "kein Heizsollwert-Schreiben in diesem Zyklus",
    hp_kick_r_no_input: "kein Verdichter-, Vorlauf- oder Pumpensollwert",
    hp_kick_r_running: "Verdichter läuft",
    hp_kick_r_idle_wait: "Verdichter steht — Stillstand wird gezählt",
    hp_kick_r_no_deficit: "Vorlauf nicht weit genug unter dem Sollwert (z. B. WW)",
    hp_kick_r_gate_increase:
      "die Sperre schreibt selbst eine Erhöhung, die den Verdichter " +
      "startet",
    hp_kick_r_blocked_limit: "der angehobene Sollwert läge über dem Maximum",
    hp_kick_r_blocked_band:
      "der angehobene Sollwert ließe den Vorlauf im Wiederanlaufband",
    hp_kick_r_fired: "Anstoß geschrieben",
    hp_kick_r_kicking: "warte auf den Verdichterstart",
    hp_kick_r_started:
      "Verdichter gestartet — der normale Sollwert kehrt über die Sperre " +
      "zurück",
    hp_kick_r_timeout: "kein Start — Anstoß zurückgenommen",
    hp_kick_r_cancelled: "Anstoß abgebrochen",
    hp_kick_r_done: "der Anstoß dieses Stillstands ist verbraucht",
    hp_kick_res_started: "Start",
    hp_kick_res_timeout: "kein Start",
    hp_kick_res_cancelled: "abgebrochen",
    test_running_min: "~{m} min",
    // Reiter Verteiler (v0.21.0) — die Zeichnung des KAN-therm InoxFlow UFST.
    mf_empty:
      "Keine Verteiler konfiguriert. Fügen Sie sie in den Integrationsoptionen " +
      "hinzu: Einstellungen → Geräte & Dienste → Tortoise-UFH → Konfigurieren → " +
      "Verteiler (Anzahl der Heizkreise, Seite des Hauptanschlusses, die Sonden " +
      "Z0/P0, danach ein Raumkreis je Anschluss).",
    mf_sub: "{n} {circuits}",
    mf_circuits_one: "Heizkreis",
    mf_circuits_few: "Heizkreise",
    mf_circuits_many: "Heizkreise",
    mf_supply: "Vorlauf",
    mf_return: "Rücklauf",
    mf_dt: "ΔT",
    mf_open: "offen",
    mf_flags: "Meldungen",
    mf_th_loop: "Heizkreis",
    mf_th_open: "Öffnung",
    mf_th_supply: "Vorlauf",
    mf_th_return: "Rücklauf",
    mf_th_dt: "ΔT",
    mf_th_state: "Zustand",
    mf_state_ok: "ok",
    mf_free: "frei",
    mf_link_tip: "Verlauf von {id} öffnen",
  },
};

/**
 * The single flag registry — one source of truth for every diagnostic flag.
 *
 * Codes mirror the exact strings the report carries: the safety-rule names
 * merged into the report (`s1_floor_overheat`, `s2_condensation`,
 * `s3_emergency_heat`, `s4_emergency_cool`, `s5_watchdog`); the
 * room-controller flags (`s2_throttle` — the
 * graduated local dew throttle, split from the hard rule 2026-07-12 —
 * `rh_stale_gated`, `fast_source_*`, `cooling_disabled`, `unknown_room`,
 * `controller_error`, `sensor_lost`); and the adapter-stamped `valve_mismatch`
 * (persistent command-vs-feedback divergence).
 *
 * Each entry carries: `pl`/`en` (short label), `sev` (drives the room status
 * dot + chip colour: `ok` < `info` < `warn` < `problem` < `alarm`, where `info`
 * is a neutral, *intentional steady state* — e.g. a room deliberately opted out
 * of cooling — that must NOT read as a fault and never escalates the roll-up
 * above the quiet neutral tint), `sx` (safety-rule code `"S1".."S5"` or `null`),
 * `group`
 * (`"safety"|"assist"|"config"` — the annunciator bucket), and `descPl`/`descEn`
 * (the full "i"-tooltip explanation). The flag annunciator, the severity
 * roll-up and the detail chips all render FROM this map, so **adding a flag is
 * one entry here** and it appears everywhere; a live code missing from the map
 * still renders (raw label, `warn`, group "other") via the fallbacks below.
 *
 * The report booleans `saturated` / `valve_floor_applied` are rendered from
 * their own STR keys in the decision view, never through this map.
 *
 * Order is logical (S1→S5, then assist, then config) so the annunciator can
 * iterate `Object.entries()` without a separate sort table.
 */
const FLAG_LABELS = {
  s1_floor_overheat: {
    pl: "Przegrzanie podłogi",
    en: "Floor overheat",
    de: "Bodenüberhitzung",
    sev: "problem", sx: "S1", group: "safety",
    descPl:
      "Zabezpieczenie przed przegrzaniem podłogi: woda zasilająca za gorąca " +
      "(>40 °C) → zawór zamknięty aż ostygnie. Sprawdź krzywą grzania pompy / " +
      "temperaturę zasilania.",
    descEn:
      "Floor-overheat protection: supply water too hot (>40 °C) → valve closed " +
      "until it cools. Check the heat-pump heating curve / supply temperature.",
    descDe:
      "Bodenüberhitzungsschutz: Vorlaufwasser zu heiß (>40 °C) → Ventil geschlossen, bis es abkühlt. Prüfen Sie die Heizkurve der Wärmepumpe / die Vorlauftemperatur.",
  },
  s2_condensation: {
    pl: "Ryzyko kondensacji",
    en: "Condensation risk",
    de: "Kondensationsrisiko",
    sev: "problem", sx: "S2", group: "safety",
    descPl:
      "Twardy bezpiecznik kondensacji: zasilanie osiągnęło punkt rosy pokoju → " +
      "zawór chłodzenia zamknięty. Sprawdź wilgotność i temperaturę wody " +
      "chłodniczej / globalny bezpieczny punkt rosy.",
    descEn:
      "Condensation backstop: supply reached the room dew point → cooling valve " +
      "closed. Check humidity and the chilled-water temperature / global safe " +
      "dew point.",
    descDe:
      "Kondensations-Rückfallschutz: Vorlauf hat den Taupunkt des Raums erreicht → Kühlventil geschlossen. Prüfen Sie die Luftfeuchtigkeit und die Kaltwassertemperatur / den globalen sicheren Taupunkt.",
  },
  s2_throttle: {
    pl: "Dławienie przy punkcie rosy",
    en: "Dew-point throttling",
    de: "Taupunkt-Drosselung",
    sev: "warn", sx: "S2", group: "safety",
    descPl:
      "Dławienie chłodzenia — zasilanie zbliża się do punktu rosy, przepływ " +
      "<100 %. Normalne w wilgotne dni; jeśli się utrzymuje, podnieś limit wody " +
      "chłodniczej lub osusz powietrze.",
    descEn:
      "Cooling throttled — supply approaching the dew point, flow <100 %. Normal " +
      "on humid days; if persistent, raise the chilled-water limit or dehumidify.",
    descDe:
      "Kühlung gedrosselt — Vorlauf nähert sich dem Taupunkt, Durchfluss <100 %. An feuchten Tagen normal; hält es an, erhöhen Sie die Kaltwassergrenze oder entfeuchten Sie.",
  },
  rh_stale_gated: {
    pl: "Nieświeża wilgotność (+1 K marginesu)",
    en: "Stale humidity (+1 K margin)",
    de: "Veraltete Luftfeuchtigkeit (+1 K Marge)",
    sev: "warn", sx: "S2", group: "safety",
    descPl:
      "Odczyt wilgotności nieświeży (60–120 min) → punkt rosy zawyżony o max " +
      "+1 K (ostrożnie). Sprawdź czujnik wilgotności; po 120 min pokój wypada z " +
      "ochron przeciwrosowych.",
    descEn:
      "Humidity reading stale (60–120 min) → dew point padded by up to +1 K " +
      "(conservative). Check the humidity sensor; after 120 min the room drops " +
      "out of the dew-point protections.",
    descDe:
      "Luftfeuchtigkeitsmesswert veraltet (60–120 min) → Taupunkt um bis zu +1 K aufgeschlagen (vorsichtig). Prüfen Sie den Luftfeuchtigkeitssensor; nach 120 min fällt der Raum aus den Taupunktschutzmechanismen.",
  },
  s3_emergency_heat: {
    pl: "Awaryjne grzanie",
    en: "Emergency heat",
    de: "Notheizen",
    sev: "problem", sx: "S3", group: "safety",
    descPl:
      "Awaryjne grzanie: pokój mocno wychłodzony (<5 °C) → wymuszone grzanie " +
      "wszystkimi źródłami. Sprawdź przyczynę (otwarte okno? awaria?).",
    descEn:
      "Emergency heat: room badly under-heated (<5 °C) → forced heating from " +
      "every source. Investigate the cause (open window? fault?).",
    descDe:
      "Notheizen: Raum stark unterversorgt (<5 °C) → erzwungenes Heizen aus jeder Quelle. Untersuchen Sie die Ursache (offenes Fenster? Störung?).",
  },
  s4_emergency_cool: {
    pl: "Awaryjne chłodzenie",
    en: "Emergency cooling",
    de: "Notkühlen",
    sev: "problem", sx: "S4", group: "safety",
    descPl:
      "Awaryjne chłodzenie: pokój mocno przegrzany (>35 °C) → wymuszone " +
      "chłodzenie. Sprawdź źródło ciepła (słońce? awaria?).",
    descEn:
      "Emergency cool: room badly overheated (>35 °C) → forced cooling. " +
      "Investigate the heat source (sun? fault?).",
    descDe:
      "Notkühlen: Raum stark überhitzt (>35 °C) → erzwungenes Kühlen. Untersuchen Sie die Wärmequelle (Sonne? Störung?).",
  },
  s5_watchdog: {
    pl: "Watchdog",
    en: "Watchdog",
    de: "Watchdog",
    sev: "problem", sx: "S5", group: "safety",
    descPl:
      "Brak świeżych danych z pokoju >15 min → neutralna pozycja zaworu + alarm. " +
      "Znika po 5 min ciągłych świeżych danych.",
    descEn:
      "No fresh room data for >15 min → neutral valve position + alarm. Clears " +
      "after 5 min of continuous fresh data.",
    descDe:
      "Keine frischen Raumdaten seit >15 min → neutrale Ventilstellung + Alarm. Verschwindet nach 5 min durchgehend frischer Daten.",
  },

  probe_drift: {
    pl: "Dryf czujnika pętli",
    en: "Loop probe drift",
    de: "Drift eines Heizkreisfühlers",
    sev: "problem", sx: null, group: "config",
    descPl:
      "Czujnik zasilania lub powrotu pętli odszedł od wyuczonej linii bazowej " +
      "przy stojącej wodzie albo przy przepływie odbiega od zasilania głównego " +
      "rozdzielacza → wyłączony z danych S1/S2 (zasilanie zastępuje główne). " +
      "Sprawdź, czy czujnik nie zsunął się z rury; atrybuty encji „dryf " +
      "czujnika pętli” pokazują linię bazową i odchyłkę.",
    descEn:
      "A loop supply or return probe left its learned still-water baseline, or " +
      "disagrees with the manifold main supply while flowing → it is dropped from " +
      "the S1/S2 inputs (a supply falls back to the main supply). Check that " +
      "the probe has not slipped on its pipe; the loop-probe-drift entity's " +
      "attributes show the baseline and the deviation.",
    descDe:
      "Ein Vorlauf- oder Rücklauffühler eines Heizkreises hat seine gelernte Stillwasser-Basislinie verlassen oder weicht bei Durchfluss vom Hauptvorlauf des Verteilers ab → er wird aus den S1/S2-Eingaben genommen (ein Vorlauf fällt auf den Hauptvorlauf zurück). Prüfen Sie, ob der Fühler vom Rohr gerutscht ist; die Attribute der Entität „Drift eines Heizkreisfühlers“ zeigen Basislinie und Abweichung.",
  },


  fast_source_mismatch: {
    pl: "Wspomaganie w innym stanie niż komenda",
    en: "Assist state differs from command",
    de: "Zusatzquelle weicht vom Befehl ab",
    sev: "warn", sx: null, group: "assist",
    descPl:
      "Split jest w innym stanie niż zadany (np. zmieniony pilotem). Przy " +
      "domyślnym czasie ręcznego sterowania (§8) stan zostaje przyjęty po 1–2 " +
      "cyklach (flaga fast_source_manual); nadpisany przy najbliższym re-assert " +
      "(~45 min) tylko przy czasie = 0. Dla trwałej ręcznej kontroli ustaw " +
      "pokój na WYŁ.",
    descEn:
      "The split is in a different state than commanded (e.g. changed by remote). " +
      "With the default manual-hold time (§8) the state is adopted after 1–2 " +
      "cycles (flag fast_source_manual); overwritten on the next re-assert " +
      "(~45 min) only with the time set to 0. For permanent manual control " +
      "set the room to OFF.",
    descDe:
      "Der Split ist in einem anderen Zustand als befohlen (z. B. per Fernbedienung geändert). Bei der Standard-Haltezeit der manuellen Steuerung (§8) wird der Zustand nach 1–2 Zyklen übernommen (Meldung fast_source_manual); nur bei Haltezeit 0 wird er beim nächsten erneuten Erzwingen (~45 min) überschrieben. Für dauerhafte manuelle Steuerung stellen Sie den Raum auf AUS.",
  },
  fast_source_manual: {
    pl: "Sterowanie ręczne",
    en: "Manual control",
    de: "Manuelle Steuerung",
    sev: "info", sx: null, group: "assist",
    descPl:
      "Split został przestawiony poza integracją (pilot, aplikacja producenta) " +
      "— regulator przyjął jego stan i przez czas ręcznego sterowania (§8) " +
      "tylko go odzwierciedla: nic nie wysyła, nic nie nadpisuje. Zamierzone; " +
      "po upływie czasu wraca normalna logika (zmiana kierunku przez OFF).",
    descEn:
      "The split was changed outside the integration (remote, vendor app) — " +
      "the controller adopted its state and only mirrors it for the manual-" +
      "hold time (§8): nothing is written, nothing overwritten. Intentional; " +
      "the normal logic resumes when the hold elapses (direction via OFF).",
    descDe:
      "Der Split wurde außerhalb der Integration verstellt (Fernbedienung, Hersteller-App) — der Regler hat seinen Zustand übernommen und spiegelt ihn für die Haltezeit der manuellen Steuerung (§8) nur: nichts wird geschrieben, nichts überschrieben. Beabsichtigt; nach Ablauf läuft die normale Logik weiter (Richtungswechsel über AUS).",
  },
  fast_source_min_runtime: {
    pl: "Wspomaganie: blokada min. czasu pracy",
    en: "Assist: min-runtime lock",
    de: "Zusatzquelle: Mindestlaufzeit-Sperre",
    sev: "warn", sx: null, group: "assist",
    descPl:
      "Blokada minimalnego czasu pracy/postoju — wspomaganie nie może jeszcze " +
      "zmienić stanu (ochrona sprężarki). Nic nie trzeba robić; licznik w " +
      "zakładce Wspomaganie.",
    descEn:
      "Min ON/OFF dwell lock — assist can't change state yet (compressor " +
      "protection). Nothing to do; the timer is shown in the Assist tab.",
    descDe:
      "Sperre der Mindest-Ein-/Ausschaltzeit — die Zusatzquelle kann den Zustand noch nicht wechseln (Kompressorschutz). Nichts zu tun; der Timer wird im Reiter Zusatzquelle angezeigt.",
  },
  fast_source_quiet_hours: {
    pl: "Ciche godziny wspomagania",
    en: "Assist quiet hours",
    de: "Ruhezeiten der Zusatzquelle",
    sev: "ok", sx: null, group: "assist",
    descPl:
      "Pokój poza dozwolonym oknem wspomagania; split się nie załączy (już " +
      "pracujący dokończy min. czas). Informacyjnie; okno zmienisz w " +
      "konfiguracji pokoju.",
    descEn:
      "Room is outside its allowed assist window; the split won't engage (a " +
      "running one finishes its min-ON). Informational; edit the window in the " +
      "room config.",
    descDe:
      "Der Raum ist außerhalb seines erlaubten Zusatzquellen-Fensters; der Split schaltet sich nicht ein (ein laufender beendet seine Mindestlaufzeit). Informativ; bearbeiten Sie das Fenster in der Raumkonfiguration.",
  },
  fast_source_group_conflict: {
    pl: "Konflikt kierunków na wspólnym agregacie",
    en: "Direction conflict on shared outdoor unit",
    de: "Richtungskonflikt am gemeinsamen Außengerät",
    sev: "warn", sx: null, group: "assist",
    descPl:
      "Pokój przegrał arbitraż kierunku na wspólnym agregacie multisplit → split " +
      "wymuszony OFF. Wróci, gdy kierunek grupy się zmieni.",
    descEn:
      "The room lost the direction arbitration on a shared multisplit outdoor " +
      "unit → the split is forced OFF. Returns when the group's direction changes.",
    descDe:
      "Der Raum hat die Richtungsabwägung an einem gemeinsamen Multisplit-Außengerät verloren → der Split wird zwangsweise AUS geschaltet. Kehrt zurück, wenn sich die Gruppenrichtung ändert.",
  },
  fast_source_cannot_cool: {
    pl: "Wspomaganie nie chłodzi",
    en: "Assist can't cool",
    de: "Zusatzquelle kann nicht kühlen",
    sev: "warn", sx: null, group: "assist",
    descPl:
      "Pokój chce chłodzić, ale jego szybkie źródło to grzejnik (nie chłodzi). " +
      "Informacyjnie.",
    descEn:
      "The room wants cooling but its fast source is a heater (cannot cool). " +
      "Informational.",
    descDe:
      "Der Raum möchte kühlen, aber seine Zusatzquelle ist ein Heizgerät (kann nicht kühlen). Informativ.",
  },
  dry_assist: {
    pl: "Osuszanie wspomagające",
    en: "Dry assist",
    de: "Entfeuchtungs-Assistent",
    sev: "info", sx: null, group: "assist",
    descPl:
      "Punkt rosy pokoju przekroczył próg osuszania — split pracuje w trybie " +
      "osuszania, choć temperatura niczego nie woła. Obniżenie rosy pozwala " +
      "też obniżyć wodę chłodzącą i uwalnia moc podłogi. Zamierzony stan; " +
      "dogrzew/dochłodzenie temperaturowe ma pierwszeństwo.",
    descEn:
      "The room dew point exceeded the dry-assist threshold — the split runs " +
      "in dry mode although temperature calls for nothing. Lowering the dew " +
      "point also lets the cooling water drop, freeing floor capacity. " +
      "Intentional; a temperature boost takes priority.",
    descDe:
      "Der Taupunkt des Raums hat die Entfeuchtungsschwelle überschritten — " +
      "der Split läuft im Entfeuchtungsmodus, obwohl die Temperatur nichts " +
      "anfordert. Ein niedrigerer Taupunkt senkt auch das Kühlwasser und gibt " +
      "dem Boden Leistung zurück. Beabsichtigt; ein Temperatur-Boost hat " +
      "Vorrang.",
  },
  dry_unsupported: {
    pl: "Split bez trybu osuszania",
    en: "Split has no dry mode",
    de: "Split ohne Entfeuchtungsmodus",
    sev: "warn", sx: null, group: "assist",
    descPl:
      "Osuszanie chciało zadziałać, ale encja climate splita nie zgłasza " +
      "trybu „dry\" (atrybut hvac_modes) — komenda została zdegradowana do " +
      "OFF. Sprawdź integrację splita albo wyłącz osuszanie dla tego pokoju.",
    descEn:
      "The dry assist wanted to engage but the split's climate entity does " +
      "not advertise a \"dry\" hvac mode — the command was demoted to OFF. " +
      "Check the split's integration or disable the dry assist for this room.",
    descDe:
      "Der Entfeuchtungs-Assistent wollte starten, aber die Climate-Entität " +
      "des Splits meldet keinen „dry\"-Modus (Attribut hvac_modes) — der " +
      "Befehl wurde auf AUS herabgestuft. Prüfen Sie die Split-Integration " +
      "oder deaktivieren Sie die Entfeuchtung für diesen Raum.",
  },
  sensor_lost: {
    pl: "Utrata czujnika",
    en: "Sensor lost",
    de: "Sensor verloren",
    sev: "problem", sx: null, group: "config",
    descPl:
      "Utrata czujnika temperatury (brak / niewiarygodny / starszy niż limit). " +
      "Grzanie trzyma ostatnią zdrową pozycję zaworu, chłodzenie parkuje na 0, " +
      "split OFF. Wraca po świeżym poprawnym odczycie.",
    descEn:
      "Temperature sensor lost (missing / implausible / older than the staleness " +
      "limit). Heating holds the last healthy valve, cooling parks at 0, split " +
      "OFF. Recovers on a fresh valid reading.",
    descDe:
      "Temperatursensor verloren (fehlend / unplausibel / älter als die Veralterungsgrenze). Heizen hält die letzte gesunde Ventilstellung, Kühlen parkt auf 0, Split AUS. Erholt sich bei einem frischen, gültigen Messwert.",
  },
  cooling_disabled: {
    pl: "Chłodzenie wyłączone w tym pokoju",
    en: "Cooling disabled in this room",
    de: "Kühlung in diesem Raum deaktiviert",
    sev: "info", sx: null, group: "config",
    descPl:
      "Chłodzenie wyłączone dla tego pokoju w konfiguracji — pomijany w trybie " +
      "chłodzenia. Celowe; zmień w opcjach pokoju, by dopuścić chłodzenie.",
    descEn:
      "Cooling is disabled for this room in the config — it is skipped in cooling " +
      "mode. Intentional; change it in the room options to allow cooling.",
    descDe:
      "Kühlung ist für diesen Raum in der Konfiguration deaktiviert — er wird im Kühlmodus übersprungen. Beabsichtigt; ändern Sie es in den Raumoptionen, um Kühlung zu erlauben.",
  },
  unknown_room: {
    pl: "Brak konfiguracji",
    en: "Unknown room",
    de: "Unbekannter Raum",
    sev: "problem", sx: null, group: "config",
    descPl:
      "Pokój bez konfiguracji regulatora (przejściowo po zmianach konfiguracji). " +
      "Przeładuj integrację; jeśli się utrzymuje, usuń i dodaj pokój ponownie.",
    descEn:
      "Room has no controller config (transient after config changes). Reload the " +
      "integration; if it persists, remove and re-add the room.",
    descDe:
      "Der Raum hat keine Reglerkonfiguration (vorübergehend nach Konfigurationsänderungen). Laden Sie die Integration neu; hält es an, entfernen Sie den Raum und fügen Sie ihn erneut hinzu.",
  },
  controller_error: {
    pl: "Błąd regulatora",
    en: "Controller error",
    de: "Reglerfehler",
    sev: "problem", sx: null, group: "config",
    descPl:
      "Wyjątek w regulatorze pokoju → bezpieczne wyhamowanie (grzanie trzyma " +
      "zawór, inaczej 0; split OFF). Sprawdź logi HA i zgłoś.",
    descEn:
      "Exception inside the room controller → safe degrade (heating holds the " +
      "valve, else 0; split OFF). Check the HA logs and report.",
    descDe:
      "Ausnahme im Raumregler → sichere Degradierung (Heizen hält das Ventil, sonst 0; Split AUS). Prüfen Sie die HA-Protokolle und melden Sie es.",
  },
  valve_mismatch: {
    pl: "Zawór nie wykonuje komend",
    en: "Valve not following commands",
    de: "Ventil folgt den Befehlen nicht",
    sev: "problem", sx: null, group: "config",
    descPl:
      "Siłownik od ≥3 cykli raportuje pozycję inną niż komenda. Sprawdź siłownik " +
      "/ przekaźnik / encję; porównaj Komenda vs Sprzężenie w zakładce Zawory.",
    descEn:
      "The actuator has reported a position different from the command for ≥3 " +
      "cycles. Check the actuator / relay / entity; compare Command vs Feedback " +
      "in the Valves tab.",
    descDe:
      "Der Aktor meldet seit ≥3 Zyklen eine andere Position als der Befehl. Prüfen Sie Aktor / Relais / Entität; vergleichen Sie Befehl vs. Rückmeldung im Reiter Ventile.",
  },
  flicker_pulsing: {
    pl: "Wymuszony start chłodzenia",
    en: "Forced cooling start",
    de: "Erzwungener Kühlstart",
    sev: "info", sx: null, group: "assist",
    descPl:
      "Wymuszanie startu chłodzenia (Panasonic): na ten jeden cykl obniżono zapisaną " +
      "nastawę do bezpiecznego dla rosy progu, aby wytrącić sprężarkę z jej stałej " +
      "3 K histerezy powrotu; następny cykl przywraca normalną nastawę.",
    descEn:
      "Force cooling start (Panasonic): for this one cycle the written setpoint " +
      "was dropped to the dew-safe floor to trip the compressor out of its fixed 3 K " +
      "return hysteresis; the next cycle restores the normal setpoint.",
    descDe:
      "Kühlstart erzwingen (Panasonic): Für diesen einen Zyklus wurde der geschriebene Sollwert auf den taupunktsicheren Boden gesenkt, um den Verdichter aus seiner festen 3-K-Rücklaufhysterese zu lösen; der nächste Zyklus stellt den normalen Sollwert wieder her.",
  },
  flicker_dew_blocked: {
    pl: "Wymuszenie startu zablokowane punktem rosy",
    en: "Forced start blocked by dew point",
    de: "Erzwungener Start durch Taupunkt blockiert",
    sev: "info", sx: null, group: "assist",
    descPl:
      "Wymuszenie startu byłoby uzbrojone, ale impuls musiałby przekroczyć surowy " +
      "punkt rosy — nie ma zapasu, by obniżyć nastawę. Impuls wstrzymany (chłodzenie " +
      "pozostaje bezpieczne dla rosy). Normalne w wilgotne dni.",
    descEn:
      "A forced start would be armed, but the pulse would have to cross the raw dew " +
      "point — there is no headroom to drop the setpoint. The pulse is withheld " +
      "(cooling stays dew-safe). Normal on humid days.",
    descDe:
      "Ein erzwungener Start wäre scharf, aber ein Impuls müsste den rohen Taupunkt überschreiten — es gibt keinen Spielraum, den Sollwert zu senken. Der Impuls wird zurückgehalten (die Kühlung bleibt taupunktsicher). An feuchten Tagen normal.",
  },
  flicker_no_sensor: {
    pl: "Wymuszanie startu: brak czujnika pompy",
    en: "Force cooling start: pump sensor missing",
    de: "Kühlstart erzwingen: Pumpensensor fehlt",
    sev: "warn", sx: null, group: "assist",
    descPl:
      "Wymuszanie startu chłodzenia włączone, ale brakuje odczytu powrotu lub " +
      "częstotliwości sprężarki (encja nieskonfigurowana lub nieświeża) — impulsy " +
      "wstrzymane. Sprawdź encje temperatury powrotu i częstotliwości sprężarki.",
    descEn:
      "Force cooling start is enabled but the return-water or compressor-frequency " +
      "reading is missing (entity unset or stale) — pulses are withheld. Check the " +
      "return-temperature and compressor-frequency entities.",
    descDe:
      "Kühlstart erzwingen ist aktiviert, aber der Rücklauf- oder Verdichterfrequenz-Messwert fehlt (Entität nicht gesetzt oder veraltet) — Impulse werden zurückgehalten. Prüfen Sie die Entitäten für Rücklauftemperatur und Verdichterfrequenz.",
  },
};

/** Group display order + fallback bucket for codes missing a `group`. */
const FLAG_GROUP_ORDER = ["safety", "assist", "config"];
const FLAG_GROUP_FALLBACK = "other";

const SEV_RANK = { ok: 0, info: 1, warn: 2, problem: 3, alarm: 4 };

/**
 * Tuning-tab knob groups (A4, 2026-07-12): the Tuning tab renders a vertical
 * list of parameters split into these titled groups instead of a flat card
 * grid. Every exposed knob must appear in EXACTLY one group (enforced by
 * tests/unit/test_panel_i18n.py); a knob the backend exposes but no group
 * lists falls into a trailing "other" group instead of silently vanishing.
 * `globalOnly` groups render only in the Global scope (the heat-pump water
 * setpoints are building-level physics; the backend rejects per-room
 * overrides too). Issue #35 split the old mixed "hp" group: "Cooling water"
 * (the base + the setpoint-flicker) and "Heating water" (the curve, plus the
 * issue #33 write gate: hysteresis, gate switch, restart band, stop offset;
 * and the issue #34 compressor kick: switch, idle time, deficit, start
 * timeout). A `curvePreview`
 * group gets the heating-curve preview table under its knobs.
 */
const KNOB_GROUPS = [
  { key: "pid", labelKey: "tune_grp_pid", knobs: ["kp", "ki", "kt"] },
  {
    key: "band",
    labelKey: "tune_grp_band",
    knobs: ["deadband_c", "valve_floor_pct"],
  },
  {
    key: "weather",
    labelKey: "tune_grp_weather",
    knobs: [
      "outdoor_ff_enabled",
      "ff_neutral_c",
      "ff_gain_pct_per_k",
      "ff_max_pct",
    ],
  },
  {
    key: "fast",
    labelKey: "tune_grp_fast",
    knobs: [
      "boost_offset_c",
      "fast_min_on_minutes",
      "fast_min_off_minutes",
      "fast_target_offset_k",
      "dry_enabled",
      "dry_dew_max_c",
      "fast_manual_hold_minutes",
    ],
  },
  {
    key: "dew",
    labelKey: "tune_grp_dew",
    knobs: ["dew_margin_k", "dew_ramp_k"],
  },
  {
    key: "heating_water",
    labelKey: "tune_grp_heating_water",
    knobs: [
      "heating_curve_neutral_c",
      "heating_supply_base_c",
      "heating_supply_slope",
      "heating_supply_min_c",
      "heating_supply_max_c",
      "heating_curve_hysteresis_k",
      "heating_gate_enabled",
      "heating_restart_band_k",
      "heating_stop_offset_k",
      "heat_kick_enabled",
      "kick_idle_minutes",
      "kick_deficit_k",
      "kick_start_timeout_minutes",
    ],
    globalOnly: true,
    curvePreview: true,
  },
  {
    key: "cooling_water",
    labelKey: "tune_grp_cooling_water",
    knobs: [
      "cooling_supply_base_c",
      "hp_flicker_enabled",
      "hp_flicker_band_k",
      "hp_flicker_min_open_pct",
      "hp_flicker_stuck_minutes",
      "hp_flicker_min_off_minutes",
      "hp_flicker_max_starts_per_h",
    ],
    globalOnly: true,
  },
];

/** Knobs whose edit refreshes the heating-curve preview (issue #35). */
const HEATING_CURVE_KNOBS = new Set(
  KNOB_GROUPS.filter((g) => g.curvePreview).flatMap((g) => g.knobs),
);

/** Debounce [ms] of the heating-curve preview while a knob is being nudged. */
const CURVE_PREVIEW_DEBOUNCE_MS = 250;

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

/** Map a core `dew_excluded_reason` code to its localised STR key. */
const DEW_REASON_KEYS = {
  no_humidity: "dew_reason_no_humidity",
  cooling_disabled: "dew_reason_cooling_disabled",
  not_cooling_mode: "dew_reason_not_cooling_mode",
  no_temperature: "dew_reason_no_temperature",
};

/** Config `entities` keys carrying per-loop valve / water-probe entity lists. */
const LOOP_VALVE_KEY = "entity_valves";
const LOOP_SUPPLY_KEY = "entity_supply";
const LOOP_RETURN_KEY = "entity_return";
/** Room config key of the outdoor-temperature entity (Overview tile). */
const OUTDOOR_KEY = "entity_temp_outdoor";

/** Command↔feedback divergence [%] above which the valve row flags a mismatch. */
const VALVE_MISMATCH_PCT = 12;

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

/**
 * Whether two knob values are the same (issue #31): booleans strictly,
 * numbers within a relative 1e-9 (a stepper's float noise must not count as
 * a custom value).
 */
function sameKnob(a, b) {
  if (typeof a === "boolean" || typeof b === "boolean") {
    return a === b;
  }
  const x = num(a);
  const y = num(b);
  if (x === null || y === null) {
    return false;
  }
  return Math.abs(x - y) <= 1e-9 * Math.max(1, Math.abs(x), Math.abs(y));
}

/** Clamp `v` to the inclusive `[lo, hi]` range. */
function clamp(v, lo, hi) {
  return Math.min(hi, Math.max(lo, v));
}

/** Round `v` to the nearest multiple of `step`. */
function roundStep(v, step) {
  return Math.round(v / step) * step;
}

/** Number of decimals implied by a stepper `step` (0.001 → 3, 1 → 0). */
function stepDecimals(step) {
  if (!(step > 0)) {
    return 0;
  }
  const str = String(step);
  const dot = str.indexOf(".");
  return dot < 0 ? 0 : str.length - dot - 1;
}

/**
 * Honest stepper-value formatting: at least the step's decimals, extended
 * (up to 6) until the shown text round-trips to the actual value — so e.g.
 * ki = 0.0015 with step 0.001 renders "0.0015", never a false "0.002".
 */
function fmtKnobValue(value, step) {
  let dec = stepDecimals(step);
  while (dec < 6 && parseFloat(value.toFixed(dec)) !== value) {
    dec += 1;
  }
  return value.toFixed(dec);
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
 * CSS mode class for a fast-source mode or climate `hvac_action` string.
 *
 * One colour per direction across the whole panel (badges, actual-state
 * text, chart band): heating red, cooling blue, dry orange. Unknown or
 * inactive states map to "" so the caller's base styling stays in charge.
 */
function fastModeCls(mode) {
  if (mode === "heating" || mode === "heat") {
    return "mode-heating";
  }
  if (mode === "cooling" || mode === "cool") {
    return "mode-cooling";
  }
  if (mode === "dry" || mode === "drying") {
    return "mode-dry";
  }
  return "";
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

/** Display a raw HA entity state: a fractional numeric state (e.g. a float32
 * sensor reading `21.4799995422363`) is rounded to one decimal; integers and
 * non-numeric states (`on`, `heat`, …) pass through unchanged. */
function fmtState(raw) {
  const s = String(raw);
  if (/^-?\d+\.\d+$/.test(s.trim())) {
    return parseFloat(s).toFixed(1);
  }
  return s;
}

/** Parse a Home Assistant state to its raw string, or null (gap) when
 * unavailable/unknown — the textual twin of `parseState`. */
function parseTextState(raw) {
  if (raw == null || UNAVAILABLE_STATES.has(String(raw))) {
    return null;
  }
  return String(raw);
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
 * Down-sample a series to per-bucket means on the absolute epoch grid.
 *
 * Buckets are `Math.floor(t / bucketMs)` (absolute, so consecutive refetches
 * land in the same buckets). The series is split at explicit null samples
 * FIRST (via `segments()`) and each run is bucketed independently, with one
 * null separator re-inserted between runs — so real `unavailable` outages
 * keep their gap while sparse sampling never creates one. Recorder history
 * stores only state CHANGES: a value that sits still for an hour yields no
 * samples at all, and that absence means "unchanged", not "unknown", so
 * bucket spacing must NOT be treated as an outage (that fabricated broken
 * temperature lines). Presentation only; a falsy `bucketMs` returns the
 * series untouched.
 */
function bucketMean(points, bucketMs) {
  if (!bucketMs || !points.length) {
    return points;
  }
  const out = [];
  for (const seg of segments(points)) {
    if (out.length) {
      out.push({ t: seg[0].t, v: null });
    }
    let idx = null;
    let tSum = 0;
    let vSum = 0;
    let n = 0;
    const flush = () => {
      if (n) {
        out.push({ t: tSum / n, v: vSum / n });
        tSum = vSum = n = 0;
      }
    };
    for (const p of seg) {
      const i = Math.floor(p.t / bucketMs);
      if (idx !== null && i !== idx) {
        flush();
      }
      idx = i;
      tSum += p.t;
      vSum += p.v;
      n += 1;
    }
    flush();
  }
  return out;
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

/**
 * Step-after value of a series at time `t`, matching the stepped valve line.
 *
 * The recorder stores only CHANGES, so a sample holds until the next one:
 * this returns the value of the last real sample <= `t`. Returns null before
 * the first sample or when an explicit gap (null sample) lies between that
 * sample and `t`; PAST the last sample it keeps the value — consistent with
 * the drawn extension to "now".
 */
function stepAt(points, t) {
  if (!points || points.length === 0) {
    return null;
  }
  let last = null;
  for (const p of points) {
    if (p.t > t) {
      break;
    }
    last = p.v == null || !Number.isFinite(p.v) ? null : p.v;
  }
  return last;
}

/**
 * Collapse a textual mode series into runs `{m, t0, t1}` of consecutive
 * samples with the same non-"off" state.
 *
 * A state holds until the NEXT sample (change-only recorder history), so a
 * run ends where the next differing sample starts; the final run extends to
 * `tEnd`. A null sample (unavailable) ends the current run without starting
 * a new one.
 */
function modeRuns(points, tEnd) {
  const runs = [];
  let cur = null;
  const close = (t) => {
    if (cur) {
      cur.t1 = t;
      runs.push(cur);
      cur = null;
    }
  };
  for (const p of points) {
    const m = p.v != null && p.v !== "off" ? p.v : null;
    if (cur && cur.m === m) {
      continue;
    }
    close(p.t);
    if (m != null) {
      cur = { m, t0: p.t, t1: p.t };
    }
  }
  close(tEnd);
  return runs;
}

/**
 * Recorder statistics rows (`recorder/statistics_during_period`, `start` in
 * epoch MILLISECONDS) → sorted `[{t, v = mean}]`. The recorder OMITS empty
 * buckets (an outage yields no row at all, not a `mean: null` row), so a gap
 * arrives as two adjacent rows: an explicit null goes between rows spaced
 * more than ~1.5 periods so `segments()` breaks the line instead of
 * interpolating across it — mirroring the `unavailable` breaks history mode
 * gets for free.
 */
function statPoints(rows, periodMs) {
  const pts = (Array.isArray(rows) ? rows : [])
    .map((row) => ({
      t: num(row && row.start),
      v: row && row.mean != null ? num(row.mean) : null,
    }))
    .filter((pt) => pt.t !== null)
    .sort((a, b) => a.t - b.t);
  const out = [];
  let prevT = null;
  for (const pt of pts) {
    if (prevT !== null && pt.t - prevT > periodMs * 1.5) {
      out.push({ t: prevT + periodMs, v: null });
    }
    out.push(pt);
    prevT = pt.t;
  }
  return out;
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

// --- Overview helpers (pure) ------------------------------------------------

/** Chart viewBox [user units]; the SVG is stretched to its box. */
const OV_SVG_W = 100;
const OV_SVG_H = 32;

/** Symmetric deviation scale [K]: the largest |value| rounded up to 0.5 K, clamped. */
function ovScale(maxAbs) {
  const n = num(maxAbs);
  const up = n === null ? 0 : Math.ceil(Math.abs(n) * 2 - 1e-9) / 2;
  return clamp(up, OV_SCALE_MIN_K, OV_SCALE_MAX_K);
}

/** Chart y [viewBox units] of a deviation `v` [K] on a ±`scale` axis. */
function ovDevY(v, scale) {
  const sc = scale > 0 ? scale : 1;
  return OV_SVG_H / 2 - (clamp(v, -sc, sc) / sc) * (OV_SVG_H / 2 - 1);
}

/** Chart x [viewBox units] of time `t` in the [t0, t1] window. */
function ovX(t, t0, t1) {
  return clamp(((t - t0) / Math.max(1, t1 - t0)) * OV_SVG_W, 0, OV_SVG_W);
}

/**
 * Stack comfort-strip dots that would overlap: items (`x` in 0..1) are taken
 * left to right and each lands on the lowest level whose previous dot sits at
 * least `gap` away. Returns new objects carrying `level` (0 = on the axis).
 */
function ovSwarm(items, gap) {
  const lastX = [];
  return items
    .slice()
    .sort((a, b) => a.x - b.x)
    .map((it) => {
      let level = 0;
      while (level < lastX.length && it.x - lastX[level] < gap) {
        level += 1;
      }
      lastX[level] = it.x;
      return { ...it, level };
    });
}

/**
 * Aggregate aligned statistics series bucket by bucket: `reduce(values,
 * weights)` over the non-null values the `{pts, w}` series hold at each
 * timestamp (5-minute statistics share one epoch grid). A timestamp no
 * series holds is simply missing, so a null goes between rows spaced more
 * than ~1.5 periods (the `statPoints` rule) and the line breaks instead of
 * bridging the outage.
 */
function ovAggregate(series, periodMs, reduce) {
  const byT = new Map();
  for (const { pts, w } of series) {
    for (const p of pts) {
      if (p.v === null) {
        continue;
      }
      let b = byT.get(p.t);
      if (!b) {
        b = { vals: [], ws: [] };
        byT.set(p.t, b);
      }
      b.vals.push(p.v);
      b.ws.push(w);
    }
  }
  const out = [];
  let prevT = null;
  for (const t of [...byT.keys()].sort((a, b) => a - b)) {
    if (prevT !== null && t - prevT > periodMs * 1.5) {
      out.push({ t: prevT + periodMs, v: null });
    }
    const b = byT.get(t);
    out.push({ t, v: reduce(b.vals, b.ws) });
    prevT = t;
  }
  return out;
}

/** Lowest, median and highest of a non-empty list of numbers. */
function ovSpread(vals) {
  const v = vals.slice().sort((a, b) => a - b);
  const k = v.length;
  const mid = k % 2 ? v[(k - 1) / 2] : (v[k / 2 - 1] + v[k / 2]) / 2;
  return { lo: v[0], mid, hi: v[k - 1] };
}

/** Mean of `vals` weighted by `ws` (in step), or null without any weight. */
function ovWeightedMean(vals, ws) {
  let sw = 0;
  let sv = 0;
  vals.forEach((v, i) => {
    sv += v * ws[i];
    sw += ws[i];
  });
  return sw > 0 ? sv / sw : null;
}

/** Shared attributes of the stretched Overview chart SVGs. */
function ovSvg(kids) {
  return s(
    "svg",
    {
      class: "ov-svg",
      viewBox: `0 0 ${OV_SVG_W} ${OV_SVG_H}`,
      preserveAspectRatio: "none",
      "aria-hidden": "true",
      focusable: "false",
    },
    kids,
  );
}

/**
 * The rooms' deviation-spread chart.
 *
 * `points` carry `v = {lo, mid, hi}` — the coldest, the median and the
 * warmest room's measured − setpoint [K] (up = warmer) — or null for a gap.
 * Draws the comfort band (±`band`), the zero line, the lo–hi envelope, a
 * warm wash where the warmest room sits above the band and a cold wash where
 * the coldest sits below it (each clamped to the band edge, so no
 * intersection maths), then the median line. Values beyond ±`scale` clamp.
 */
function ovFanSvg(points, t0, t1, band, scale) {
  const b = clamp(band, 0, scale > 0 ? scale : 1);
  const xy = (t, v) => `${ovX(t, t0, t1).toFixed(2)},${ovDevY(v, scale).toFixed(2)}`;
  const kids = [
    s("rect", {
      class: "ov-band",
      x: 0,
      y: ovDevY(b, scale).toFixed(2),
      width: OV_SVG_W,
      height: Math.max(0.6, ovDevY(-b, scale) - ovDevY(b, scale)).toFixed(2),
    }),
    s("line", {
      class: "ov-zero",
      x1: 0,
      x2: OV_SVG_W,
      y1: OV_SVG_H / 2,
      y2: OV_SVG_H / 2,
    }),
  ];
  // Runs of consecutive real buckets (a null bucket is a gap).
  const runs = [];
  let cur = [];
  for (const p of points) {
    if (p.t < t0 || p.t > t1) {
      continue;
    }
    if (p.v === null) {
      if (cur.length) {
        runs.push(cur);
      }
      cur = [];
    } else {
      cur.push(p);
    }
  }
  if (cur.length) {
    runs.push(cur);
  }
  let env = "";
  let warm = "";
  let cold = "";
  let mid = "";
  for (const run of runs) {
    const back = run.slice().reverse();
    env +=
      "M" +
      run.map((p) => xy(p.t, p.v.hi)).join("L") +
      "L" +
      back.map((p) => xy(p.t, p.v.lo)).join("L") +
      "Z";
    mid += "M" + run.map((p) => xy(p.t, p.v.mid)).join("L");
    if (run.length < 2) {
      mid += "h0.4"; // a lone bucket draws as a dot
      continue;
    }
    const a = run[0].t;
    const z = run[run.length - 1].t;
    warm +=
      `M${xy(a, b)}L` +
      run.map((p) => xy(p.t, Math.max(p.v.hi, b))).join("L") +
      `L${xy(z, b)}Z`;
    cold +=
      `M${xy(a, -b)}L` +
      run.map((p) => xy(p.t, Math.min(p.v.lo, -b))).join("L") +
      `L${xy(z, -b)}Z`;
  }
  for (const [cls, d] of [
    ["ov-env", env],
    ["ov-warm", warm],
    ["ov-cold", cold],
    ["ov-line", mid],
  ]) {
    if (d) {
      kids.push(s("path", { class: cls, d }));
    }
  }
  return ovSvg(kids);
}

/** Chart y [viewBox units] of `v` on the fixed [`lo`, `hi`] scale (clamped). */
function ovSeriesY(v, lo, hi) {
  const range = hi > lo ? hi - lo : 1;
  return OV_SVG_H - 1 - ((clamp(v, lo, hi) - lo) / range) * (OV_SVG_H - 2);
}

/**
 * A single-series chart on the fixed [`lo`, `hi`] scale: the line, over an
 * area down to the floor when `area` (the valve opening). Values clamp; a
 * lone bucket between gaps draws as a dot (a hair-long round-capped line).
 */
function ovSeriesSvg(points, t0, t1, lo, hi, area) {
  const xy = (p) => `${ovX(p.t, t0, t1).toFixed(2)},${ovSeriesY(p.v, lo, hi).toFixed(2)}`;
  let line = "";
  let fill = "";
  for (const seg of segments(points.filter((p) => p.t >= t0 && p.t <= t1))) {
    const path = seg.map(xy).join("L");
    line += "M" + path + (seg.length === 1 ? "h0.4" : "");
    if (area && seg.length > 1) {
      const xa = ovX(seg[0].t, t0, t1).toFixed(2);
      const xz = ovX(seg[seg.length - 1].t, t0, t1).toFixed(2);
      fill += `M${xa},${OV_SVG_H}L${path}L${xz},${OV_SVG_H}Z`;
    }
  }
  const kids = [];
  if (fill) {
    kids.push(s("path", { class: "ov-area", d: fill }));
  }
  if (line) {
    kids.push(s("path", { class: "ov-line", d: line }));
  }
  return ovSvg(kids);
}

// --- Custom element ---------------------------------------------------------

// --- Manifold drawing (Manifolds tab) ----------------------------------------
//
// A parametric KAN-therm InoxFlow UFST manifold in millimetres — x along the
// bars, y up, z towards the viewer. The dimensions follow the maker's drawings
// (50 mm circuit pitch, 235 mm between the bar axes, 84 mm depth with the
// return bar 25 mm in front of the supply bar, Ø36 bars, flow meters on the
// supply bar, M30×1.5 inserts under Möhlenhoff-sized actuators on the return
// bar, the drain / vent section on the far end). One model per manifold,
// projected axonometrically (yaw 25°, pitch 20° — the view of the sibling OAS
// panel's air-handling-unit drawing) and painted back-to-front into an SVG
// STRING. That string is this module's one use of innerHTML: every number is
// our own geometry and everything that originates in Home Assistant (labels,
// entity ids) passes through `escXml`.

const MF_D = {
  // Bars: circuit pitch, outer diameter, axis distance, margins to the rails.
  pitch: 50,
  barOD: 36,
  barGap: 235,
  edgeIn: 45,
  edgeOut: 95,
  plugLen: 7.5,
  plugR: 21,
  // Depth of the two bar axes and the rails / clamps holding them.
  zS: 35,
  zR: 60,
  railW: 37,
  clampW: 22,
  clampX: 11,
  railT: 2,
  railTop: 45,
  railBot: 45,
  railUpY: 150,
  railLoY: 95,
  clamp: 44,
  // Main connection stubs (G1") and their probe blocks.
  stubLen: 30,
  stubR: 16.85,
  // Flow meter on the supply bar: base, ring, sight tube with its float, cap.
  fmBaseR: 13.5,
  fmBaseH: 12,
  fmRingR: 13,
  fmRingH: 8,
  fmTubeR: 8,
  fmTubeH: 28,
  fmCapR: 7,
  fmCapH: 6,
  floatR: 5,
  floatH: 7,
  // Valve insert + adapter + actuator on the return bar.
  vBodyR: 14,
  vBodyH: 10,
  adR: 15,
  adH: 6,
  actW: 44.3,
  actH: 48,
  actD: 48.4,
  actTop: 4.2,
  // Circuit outlets (G¾"), nuts and the pipes running down to the floor.
  outR: 14,
  outLen: 17,
  nutR: 16,
  nutLen: 12,
  pipeR: 8,
  pipeDown: 150,
  // Vent / drain section and probe placement.
  ventR: 5.5,
  ventH: 22,
  drainR: 11,
  drainH: 22,
  drainX: 55,
  supplyBackZ: 18,
};

/** Valve opening at or above which a circuit counts as open (mirrors the core). */
const MF_OPEN_THRESHOLD_PCT = 5;

/** Escape text for interpolation into the SVG string (text and attributes). */
function escXml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function mfCyl(list, role, axis, c, r, len, extra) {
  list.push(Object.assign({ t: "cyl", role, axis, c, r, len }, extra || {}));
}

function mfBox(list, role, c, size, extra) {
  list.push(Object.assign({ t: "box", role, c, s: size }, extra || {}));
}

function mfTube(list, role, pts, r) {
  list.push({ t: "tube", role, pts, r });
}

/**
 * Build the primitive list + label anchors for one manifold view.
 *
 * Every circuit position gets its hardware (flow meter, valve, actuator,
 * outlets, pipes); only ASSIGNED positions get label anchors (name, opening,
 * supply / return), and the main probes only when an entity is configured.
 */
function mfBuildModel(view) {
  const D = MF_D;
  const L = [];
  const n = Math.max(1, Number(view.circuits) || 1);
  const positions = Array.isArray(view.positions) ? view.positions : [];
  const xFirst = D.edgeIn;
  const xLast = xFirst + D.pitch * (n - 1);
  const Lbar = xLast + D.edgeOut;
  const mirror = view.side === "right";
  const xmin = -D.stubLen;
  const xmax = Lbar + D.plugLen;
  const X = (x) => (mirror ? xmax - x : x - xmin);
  const yR = 0;
  const yS = D.barGap;
  const rb = D.barOD / 2;
  const anchors = [];
  const bars = [
    [yS, D.zS, "supply"],
    [yR, D.zR, "return"],
  ];
  const main = view.main || {};
  for (const [yBar, zBar, kind] of bars) {
    const sup = kind === "supply";
    // Cut the bar midway between neighbouring circuits: every fitting then sits
    // in the middle of its own segment, so no neighbouring segment (nearer by
    // its centre) is painted over a flowmeter or a eurocone nut.
    // The end pieces are cut off too, so the outermost circuits get their own
    // centred segments as well (the end piece towards the plug is nearer by
    // its centre and used to be painted over the last flowmeter and actuator).
    // The pieces under the rails are cut off as well: a clamp's front half must
    // come after the bar under it and before the fittings next to it, which the
    // depth of a segment centred on the clamp gives without any bias.
    const cuts = [D.railW, Lbar - D.railW].map((cx) => X(cx) - (X(Lbar / 2) - Lbar / 2));
    for (let cx = xFirst - D.pitch / 2; cx <= xLast + D.pitch / 2; cx += D.pitch) {
      cuts.push(X(cx) - (X(Lbar / 2) - Lbar / 2));
    }
    mfCyl(L, "steel", "x", [X(Lbar / 2), yBar, zBar], rb, Lbar, { cuts });
    mfCyl(L, "brass", "x", [X(-D.stubLen / 2), yBar, zBar], D.stubR, D.stubLen);
    const mainReading = sup ? main.supply : main.return;
    if (mainReading && mainReading.entity_id) {
      anchors.push({
        kind: sup ? "mainS" : "mainR",
        p: [X(-D.stubLen / 2), yBar + (sup ? D.stubR + 6 : -D.stubR - 6), zBar + D.stubR],
        origin: sup ? "bottom" : "top",
      });
    }
    mfCyl(L, "steel", "x", [X(Lbar + D.plugLen / 2), yBar, zBar], D.plugR, D.plugLen + 1, {
      seg: 6,
    });
    const xd = X(Lbar - D.drainX);
    mfCyl(L, "brass", "y", [xd, yBar + rb + D.ventH / 2, zBar], D.ventR, D.ventH);
    mfCyl(L, "dark", "y", [xd, yBar + rb + D.ventH + 3, zBar], 4, 6);
    mfCyl(L, "brass", "y", [xd, yBar - rb - 5, zBar], 8, 10);
    mfCyl(L, "dark", "y", [xd, yBar - rb - 10 - D.drainH / 2, zBar], D.drainR, D.drainH);
    mfCyl(L, "brass", "y", [xd, yBar - rb - 10 - D.drainH - 7, zBar], 5, 14);
  }
  const zUp = D.zS - D.clamp / 2 - D.railT;
  const zLo = D.zR - D.clamp / 2 - D.railT;
  for (const xr of [D.railW / 2, Lbar - D.railW / 2]) {
    const yTop = yS + D.railTop;
    const yBot = yR - D.railBot;
    const yU = D.railUpY;
    const yL = D.railLoY;
    mfBox(L, "bracket", [X(xr), (yU + yTop) / 2, zUp + D.railT / 2], [D.railW, yTop - yU, D.railT], { wBias: -40 });
    mfBox(L, "bracket", [X(xr), (yBot + yL) / 2, zLo + D.railT / 2], [D.railW, yL - yBot, D.railT], { wBias: -40 });
    const dz = zLo - zUp;
    const dl = Math.hypot(yU - yL, dz);
    mfBox(L, "bracket", [X(xr), (yU + yL) / 2, (zUp + zLo) / 2 + D.railT / 2], [D.railW, dl, D.railT], {
      wBias: -40,
      rot: -Math.atan2(dz, yU - yL),
    });
    // The clamp sits at the outer end of the bar (clampX from the bar end), away
    // from the first circuit; the rail plate behind it keeps its catalogue width.
    const xc = xr < Lbar / 2 ? D.clampX : Lbar - D.clampX;
    for (const [yBar, zBar] of bars) {
      mfBox(L, "rubber", [X(xc), yBar, zBar - D.clamp / 4], [D.clampW, D.clamp, D.clamp / 2]);
      mfBox(L, "rubber", [X(xc), yBar, zBar + D.clamp / 4], [D.clampW, D.clamp, D.clamp / 2]);
    }
  }
  for (let i = 0; i < n; i++) {
    const x = X(xFirst + i * D.pitch);
    const v = positions[i] || {};
    const assigned = v.room_name != null;
    const pctRaw = assigned && v.valve ? num(v.valve.pct) : null;
    const pct = pctRaw === null ? 0 : clamp(pctRaw, 0, 100);
    const flagged = assigned && Array.isArray(v.flags) && v.flags.length > 0;
    let y = yS + rb;
    mfCyl(L, "brass", "y", [x, y + D.fmBaseH / 2, D.zS], D.fmBaseR, D.fmBaseH, { seg: 6 });
    y += D.fmBaseH;
    mfCyl(L, "brass", "y", [x, y + D.fmRingH / 2, D.zS], D.fmRingR, D.fmRingH);
    y += D.fmRingH;
    // The flow pushes the indicator down the scale: 0 stands at the top, full flow at the bottom.
    mfCyl(L, "float", "y", [x, y + 4 + ((D.fmTubeH - 8) * (100 - pct)) / 100, D.zS], D.floatR, D.floatH);
    mfCyl(L, "glass", "y", [x, y + D.fmTubeH / 2, D.zS], D.fmTubeR, D.fmTubeH);
    y += D.fmTubeH;
    mfCyl(L, "dark", "y", [x, y + D.fmCapH / 2, D.zS], D.fmCapR, D.fmCapH, { r2: D.fmCapR - 1.5 });
    y = yR + rb;
    mfCyl(L, "brass", "y", [x, y + D.vBodyH / 2, D.zR], D.vBodyR, D.vBodyH, { seg: 6 });
    y += D.vBodyH;
    mfCyl(L, "grey", "y", [x, y + D.adH / 2, D.zR], D.adR, D.adH);
    y += D.adH;
    mfBox(L, "act", [x, y + D.actH / 2, D.zR], [D.actW, D.actH, D.actD], { tint: pct, flag: flagged });
    mfBox(L, "act", [x, y + D.actH + D.actTop / 2, D.zR], [D.actW - 5, D.actTop, D.actD - 5], {
      tint: pct,
      flag: flagged,
    });
    if (assigned) {
      anchors.push({ kind: "pct", i, p: [x, y + D.actH * 0.5, D.zR + D.actD / 2], origin: "center" });
    }
    for (const [yBar, zBar, kind] of bars) {
      let yb = yBar - rb;
      mfCyl(L, "steel", "y", [x, yb - D.outLen / 2, zBar], D.outR, D.outLen, { seg: 6 });
      yb -= D.outLen;
      mfCyl(L, "brass", "y", [x, yb - D.nutLen / 2, zBar], D.nutR, D.nutLen, { seg: 6 });
      yb -= D.nutLen;
      const floorY = -D.pipeDown;
      if (kind === "return") {
        mfTube(L, "pipe", [[x, yb + 2, zBar], [x, floorY, zBar]], D.pipeR);
      } else {
        const back = D.supplyBackZ;
        mfTube(
          L,
          "pipe",
          [
            [x, yb + 2, zBar],
            [x, yb - 24, zBar],
            [x, yb - 40, zBar - 5],
            [x, yb - 58, back + 6],
            [x, yb - 72, back],
            [x, floorY, back],
          ],
          D.pipeR,
        );
      }
      if (assigned) {
        anchors.push({ kind, i, p: [x, yb - 5, zBar + 8], origin: "top" });
      }
    }
    if (assigned) {
      anchors.push({
        kind: "name",
        i,
        text: String(v.loop_label || v.room_name || ""),
        p: [x, yS + 72 + 10, D.zS],
        origin: "bottom",
      });
    }
  }
  return { prims: L, anchors, B: Lbar };
}

// Axonometric projection (yaw 25°, pitch 20°); the model has z towards the
// viewer, so z' = -z before projecting.
const MF_YAW = (25 * Math.PI) / 180;
const MF_PITCH = (20 * Math.PI) / 180;
const MF_U = [Math.cos(MF_YAW), 0, Math.sin(MF_YAW)];
const MF_V = [-Math.sin(MF_PITCH) * Math.sin(MF_YAW), Math.cos(MF_PITCH), Math.sin(MF_PITCH) * Math.cos(MF_YAW)];
const MF_W = [-Math.cos(MF_PITCH) * Math.sin(MF_YAW), -Math.sin(MF_PITCH), Math.cos(MF_PITCH) * Math.cos(MF_YAW)];
const mfDot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const mfAdd = (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
const mfMul = (a, k) => [a[0] * k, a[1] * k, a[2] * k];
const mfCross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const mfFlip = (v) => [v[0], v[1], -v[2]];
function mfP(p) {
  const q = mfFlip(p);
  return [mfDot(q, MF_U), -mfDot(q, MF_V), mfDot(q, MF_W)];
}
// A face is visible when its outward normal points at the viewer.
const mfFacing = (nrm) => mfDot(mfFlip(nrm), MF_W) < 0;
const MF_LIGHT = (() => {
  const l = [0.35, 0.8, 0.55];
  return mfMul(l, 1 / Math.hypot(l[0], l[1], l[2]));
})();
const mfF1 = (v) => (Math.round(v * 10) / 10).toFixed(1);
const mfPoly = (pts) => pts.map((pt) => mfF1(pt[0]) + "," + mfF1(pt[1])).join(" ");
const MF_AXES = { x: [1, 0, 0], y: [0, 1, 0], z: [0, 0, 1] };

function mfDrawBox(b, out) {
  const [w, hh, d] = b.s;
  const th = b.rot || 0;
  const rot = (v) =>
    th ? [v[0], v[1] * Math.cos(th) - v[2] * Math.sin(th), v[1] * Math.sin(th) + v[2] * Math.cos(th)] : v;
  const corner = (sx, sy, sz) => mfAdd(b.c, rot([(sx * w) / 2, (sy * hh) / 2, (sz * d) / 2]));
  const faces = [
    [[0, 0, 1], [[-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1]]],
    [[0, 1, 0], [[-1, 1, -1], [1, 1, -1], [1, 1, 1], [-1, 1, 1]]],
    [[1, 0, 0], [[1, -1, -1], [1, 1, -1], [1, 1, 1], [1, -1, 1]]],
    [[0, 0, -1], [[-1, -1, -1], [1, -1, -1], [1, 1, -1], [-1, 1, -1]]],
    [[0, -1, 0], [[-1, -1, -1], [1, -1, -1], [1, -1, 1], [-1, -1, 1]]],
    [[-1, 0, 0], [[-1, -1, -1], [-1, 1, -1], [-1, 1, 1], [-1, -1, 1]]],
  ];
  let svg = "";
  const ext = [];
  for (const [n0, cs] of faces) {
    const nrm = rot(n0);
    const pp = cs.map((c) => mfP(corner(c[0], c[1], c[2])));
    ext.push(...pp);
    if (!mfFacing(nrm)) {
      continue;
    }
    const ax = Math.abs(nrm[0]);
    const ay = Math.abs(nrm[1]);
    const az = Math.abs(nrm[2]);
    const tone = ay >= ax && ay >= az ? "top" : ax >= az ? "right" : "front";
    svg += `<polygon class="f-${tone} r-${b.role}" points="${mfPoly(pp)}"/>`;
  }
  if (b.tint != null) {
    const mix = Math.round(b.tint * 0.6);
    svg = `<g class="r-act${b.flag ? " flag" : ""}" style="--open-mix:${mix}%">${svg}</g>`;
  }
  // wBias pushes a part towards the viewer (+) or away (-) in the painter's order;
  // only the rails use it: they sit on the wall behind everything.
  out.push({ w: mfP(b.c)[2] - (b.wBias || 0), svg, ext });
}

function mfDrawCyl(c, out, part) {
  const which = part || "whole";
  const hasA = which === "whole" || which === "first";
  const hasB = which === "whole" || which === "last";
  const a = MF_AXES[c.axis];
  const e1 = c.axis === "y" ? [1, 0, 0] : [0, 1, 0];
  const e2 = mfCross(a, e1);
  const rA = c.r;
  const rB = c.r2 != null ? c.r2 : c.r;
  const cA = mfAdd(c.c, mfMul(a, -c.len / 2));
  const cB = mfAdd(c.c, mfMul(a, c.len / 2));
  const pA = mfP(cA);
  const pB = mfP(cB);
  const p0 = mfP(c.c);
  const E1 = [mfP(mfAdd(c.c, e1))[0] - p0[0], mfP(mfAdd(c.c, e1))[1] - p0[1]];
  const E2 = [mfP(mfAdd(c.c, e2))[0] - p0[0], mfP(mfAdd(c.c, e2))[1] - p0[1]];
  const d = [pB[0] - pA[0], pB[1] - pA[1]];
  const cr2 = (u, v) => u[0] * v[1] - u[1] * v[0];
  const pt = (pc, rr, t) => [
    pc[0] + rr * (E1[0] * Math.cos(t) + E2[0] * Math.sin(t)),
    pc[1] + rr * (E1[1] * Math.cos(t) + E2[1] * Math.sin(t)),
  ];
  const nearIsB = mfFacing(a);
  let body;
  if (Math.hypot(d[0], d[1]) < 0.5) {
    body = "";
  } else {
    const t0 = Math.atan2(cr2(E2, d), cr2(E1, d));
    const arc = (pc, rr, from, dir, N) => {
      const pts = [];
      for (let k = 0; k <= N; k++) {
        pts.push(pt(pc, rr, from + (dir * Math.PI * k) / N));
      }
      return pts;
    };
    // The end-B arc bulges away from A (further along d), the end-A arc away from B.
    const away = (pc, rr, from, sign) => {
      const m = pt(pc, rr, from + (sign * Math.PI) / 2);
      return (m[0] - pc[0]) * d[0] + (m[1] - pc[1]) * d[1];
    };
    const dirB = away(pB, rB, t0, 1) > away(pB, rB, t0, -1) ? 1 : -1;
    const dirA = away(pA, rA, t0 + Math.PI, 1) < away(pA, rA, t0 + Math.PI, -1) ? 1 : -1;
    const arcB = arc(pB, rB, t0, dirB, 10);
    const arcA = arc(pA, rA, t0 + Math.PI, dirA, 10);
    body = `<polygon class="f-side nostroke r-${c.role}" points="${mfPoly([...arcB, ...arcA])}"/>`;
    let edge =
      `M${mfPoly([pt(pA, rA, t0), pt(pB, rB, t0)]).replace(" ", " L")} ` +
      `M${mfPoly([pt(pA, rA, t0 + Math.PI), pt(pB, rB, t0 + Math.PI)]).replace(" ", " L")}`;
    if (hasB) {
      edge += " M" + mfPoly(arcB).replace(/ /g, " L");
    }
    if (hasA) {
      edge += " M" + mfPoly(arcA).replace(/ /g, " L");
    }
    body += `<path class="cyl-edge" d="${edge}"/>`;
    // A highlight on the lit side, only on the visible half of the shell.
    const th = Math.atan2(mfDot(e2, MF_LIGHT), mfDot(e1, MF_LIGHT));
    const nl = mfAdd(mfMul(e1, Math.cos(th)), mfMul(e2, Math.sin(th)));
    if (mfFacing(nl) && c.role !== "glass" && c.role !== "float") {
      const q = [pt(pA, rA, th - 0.4), pt(pB, rB, th - 0.4), pt(pB, rB, th + 0.4), pt(pA, rA, th + 0.4)];
      body += `<polygon class="hl" points="${mfPoly(q)}"/>`;
    }
  }
  const capTone = c.axis === "y" ? "top" : c.axis === "x" ? "right" : "front";
  const pc = nearIsB ? pB : pA;
  const rr = nearIsB ? rB : rA;
  const capHere = nearIsB ? hasB : hasA;
  const cap = capHere
    ? `<circle class="f-${capTone} r-${c.role}" r="${rr}" transform="matrix(${mfF1(E1[0])} ${mfF1(E1[1])} ${mfF1(E2[0])} ${mfF1(E2[1])} ${mfF1(pc[0])} ${mfF1(pc[1])})"/>`
    : "";
  const rx = Math.max(rA, rB) * (Math.hypot(E1[0], E1[1]) + Math.hypot(E2[0], E2[1]));
  const ext = [
    [pA[0] - rx, pA[1] - rx],
    [pA[0] + rx, pA[1] + rx],
    [pB[0] - rx, pB[1] - rx],
    [pB[0] + rx, pB[1] + rx],
  ];
  out.push({ w: p0[2], svg: `<g class="r-${c.role}">${body}${cap}</g>`, ext });
}

function mfDrawTube(t, out) {
  const pts = t.pts.map(mfP);
  const dpath = pts.map((pt, i) => (i ? "L" : "M") + mfF1(pt[0]) + " " + mfF1(pt[1])).join(" ");
  const w = Math.max(...pts.map((pt) => pt[2]));
  const ext = pts.flatMap((pt) => [
    [pt[0] - t.r, pt[1] - t.r],
    [pt[0] + t.r, pt[1] + t.r],
  ]);
  out.push({
    w,
    svg:
      `<path class="r-pipe-edge" d="${dpath}" style="stroke-width:${2 * t.r + 1.4}"/>` +
      `<path class="r-pipe" d="${dpath}" style="stroke-width:${2 * t.r}"/>`,
    ext,
  });
}

/**
 * One label on a plate. Values that come from an entity get `ua-link` and a
 * `data-entity` attribute (the card delegates the click to `_moreInfo`).
 * `caps` holds the localised supply / return captions of the main probes.
 * Loop names and the loop temperatures read along their pipe: rotated a
 * quarter turn counter-clockwise about the anchor, the name climbing from the
 * flowmeter top, the temperature running down the pipe from the probe - so
 * they fit the 50 mm pitch without alternating rows.
 */
function mfLabel(a, fs, view, stagger, caps) {
  const p = mfP(a.p);
  const positions = Array.isArray(view.positions) ? view.positions : [];
  const v = a.i != null ? positions[a.i] || {} : null;
  let text;
  let cls = "ua-text";
  let cap = null;
  let entity = null;
  let size = fs;
  const rotated = a.kind === "name" || a.kind === "supply" || a.kind === "return";
  if (a.kind === "name") {
    text = a.text;
    cls += " ua-cap";
    size *= 0.95;
  } else if (a.kind === "pct") {
    const pct = v.valve ? num(v.valve.pct) : null;
    text = pct === null ? "—" : Math.round(pct) + " %";
    const flagged = Array.isArray(v.flags) && v.flags.length > 0;
    cls += " ua-val" + (flagged ? " ua-err" : "");
    entity = v.valve ? v.valve.entity_id : null;
  } else if (a.kind === "supply" || a.kind === "return") {
    const r = (a.kind === "supply" ? v.supply : v.return) || {};
    text = fmt(r.value, 1, " °C");
    cls += " ua-val";
    entity = r.entity_id;
  } else {
    const main = view.main || {};
    const r = (a.kind === "mainS" ? main.supply : main.return) || {};
    text = fmt(r.value, 1, " °C");
    cls += " ua-val";
    entity = r.entity_id;
    cap = a.kind === "mainS" ? caps.supply : caps.return;
  }
  if (entity) {
    cls += " ua-link";
  }
  const cw = text.length * size * 0.58 + 8;
  const ch = size * 1.35;
  const link = entity ? ` data-entity="${escXml(entity)}"` : "";
  if (rotated) {
    // In the rotated frame +x is screen up: the name starts at the anchor and
    // climbs, the temperature ends at the anchor and runs down the pipe.
    const up = a.kind === "name";
    const x0 = p[0];
    const y0 = p[1];
    const tx = up ? x0 + 3 : x0 - 3;
    const rx = up ? x0 : x0 - cw;
    let g = `<g transform="rotate(-90 ${mfF1(x0)} ${mfF1(y0)})">`;
    g += `<rect class="ua-plate" x="${mfF1(rx)}" y="${mfF1(y0 - ch / 2)}" width="${mfF1(cw)}" height="${mfF1(ch)}" rx="3"/>`;
    g += `<text class="${cls}"${link} x="${mfF1(tx)}" y="${mfF1(y0)}" font-size="${mfF1(size)}" text-anchor="${up ? "start" : "end"}" dominant-baseline="central">${escXml(text)}</text>`;
    g += "</g>";
    const box = up ? [x0 - ch / 2, y0 - cw - 3, x0 + ch / 2, y0] : [x0 - ch / 2, y0, x0 + ch / 2, y0 + cw + 3];
    return { svg: g, box };
  }
  const x = p[0];
  let y = p[1];
  const dy = stagger && a.i != null ? (a.i % 2) * ch * 1.05 : 0;
  if (a.origin === "bottom") {
    y -= ch / 2 + dy;
  } else if (a.origin === "top") {
    y += ch / 2 + dy;
  }
  let svg = `<rect class="ua-plate" x="${mfF1(x - cw / 2)}" y="${mfF1(y - ch / 2)}" width="${mfF1(cw)}" height="${mfF1(ch)}" rx="3"/>`;
  svg += `<text class="${cls}"${link} x="${mfF1(x)}" y="${mfF1(y)}" font-size="${mfF1(size)}" text-anchor="middle" dominant-baseline="central">${escXml(text)}</text>`;
  if (cap) {
    const capCls = a.kind === "mainS" ? "ua-sup" : "ua-ret";
    const cy = a.origin === "bottom" ? y - ch * 0.95 : y + ch * 0.95;
    const capW = cap.length * size * 0.5 + 8;
    svg += `<rect class="ua-plate" x="${mfF1(x - capW / 2)}" y="${mfF1(cy - ch * 0.42)}" width="${mfF1(capW)}" height="${mfF1(ch * 0.84)}" rx="3"/>`;
    svg += `<text class="ua-text ua-cap ${capCls}" x="${mfF1(x)}" y="${mfF1(cy)}" font-size="${mfF1(size * 0.86)}" text-anchor="middle" dominant-baseline="central">${escXml(cap)}</text>`;
  }
  return { svg, box: [x - cw / 2, y - ch, x + cw / 2, y + ch] };
}

/**
 * Render one manifold view into an SVG string sized for `widthPx`.
 *
 * Long cylinders are split into segments so the painter's order (far to near
 * by projected depth) interleaves correctly with the parts crossing them;
 * label plates are laid out last and widen the viewBox as needed.
 */
function mfRenderSvg(model, view, widthPx, caps, ariaLabel) {
  const out = [];
  for (const prim of model.prims) {
    if (prim.t === "box") {
      mfDrawBox(prim, out);
      continue;
    }
    if (prim.t === "tube") {
      // One path per pipe: per-edge pieces showed their overlapping ends as
      // dots through the translucent stroke. A pipe sorts by its deepest
      // point, so a supply pipe is laid down before the return bar it passes.
      mfDrawTube(prim, out);
      continue;
    }
    let bounds;
    if (Array.isArray(prim.cuts) && prim.cuts.length) {
      bounds = [0, ...prim.cuts.filter((v) => v > 0 && v < prim.len).sort((p, q) => p - q), prim.len];
    } else {
      const k = prim.axis === "z" ? 1 : Math.max(1, Math.ceil(prim.len / 50));
      bounds = Array.from({ length: k + 1 }, (_, i) => (prim.len * i) / k);
    }
    if (bounds.length <= 2 || prim.r2 != null) {
      mfDrawCyl(prim, out);
      continue;
    }
    const a = MF_AXES[prim.axis];
    for (let i = 0; i + 1 < bounds.length; i++) {
      const len = bounds[i + 1] - bounds[i];
      const c = mfAdd(prim.c, mfMul(a, -prim.len / 2 + bounds[i] + len / 2));
      mfDrawCyl(Object.assign({}, prim, { c, len }), out, i === 0 ? "first" : i + 2 === bounds.length ? "last" : "mid");
    }
  }
  out.sort((a, b) => b.w - a.w);
  let minX = 1e9;
  let minY = 1e9;
  let maxX = -1e9;
  let maxY = -1e9;
  const ext = (pt) => {
    minX = Math.min(minX, pt[0]);
    maxX = Math.max(maxX, pt[0]);
    minY = Math.min(minY, pt[1]);
    maxY = Math.max(maxY, pt[1]);
  };
  out.forEach((o) => o.ext.forEach(ext));
  const vbW0 = maxX - minX + 40;
  const scale = widthPx / vbW0;
  const fs = 11.5 / scale;
  const pitchPx = MF_D.pitch * MF_U[0] * scale;
  const stagger = pitchPx < 60;
  const labels = model.anchors.map((a) => mfLabel(a, fs, view, stagger, caps));
  labels.forEach((l) => {
    ext([l.box[0], l.box[1]]);
    ext([l.box[2], l.box[3]]);
  });
  const m = 8;
  const vb = [minX - m, minY - m, maxX - minX + 2 * m, maxY - minY + 2 * m];
  return (
    `<svg class="mf-svg" viewBox="${vb.map(mfF1).join(" ")}" role="img" aria-label="${escXml(ariaLabel)}">` +
    out.map((o) => o.svg).join("") +
    labels.map((l) => l.svg).join("") +
    "</svg>"
  );
}

class TortoiseUfhPanel extends HTMLElement {
  // --------------------------------------------------------------------------
  // Lifecycle, wiring & timers
  // --------------------------------------------------------------------------

  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._config = null; // last get_config payload
    this._live = null; // last get_live payload
    // Local home-setpoint edits (+/-): a generation bumped when an edit starts
    // and when it ends, plus a count of set_home_temperature calls still in
    // flight, so the poll never overwrites the optimistic value with a reply
    // that may predate the write.
    this._homeEditGen = 0;
    this._homeEditsInFlight = 0;
    this._selectedRoom = null;
    this._error = "";
    this._lang = "en";

    this._built = false;
    this._builtLang = null;
    this._els = null; // stable skeleton element refs
    this._hero = null; // hero part refs
    this._tabs = null; // tab-bar + section refs
    this._activeTab = this._loadActiveTab();
    this._rows = new Map(); // room name -> table <tr> element
    this._detail = null; // detail part refs
    this._detailRoom = null; // room name the detail is currently built for
    this._wiringOpen = true; // signals <details> state, persists across rooms
    this._diagOpen = false; // diagnostics <details> state (A5: collapsed)
    this._flagLegendOpen = true; // flag annunciator <details> state (default open)

    this._view = []; // normalized room view-models
    this._viewByName = new Map();
    this._lastUpdateMs = null; // parsed last_update_timestamp (epoch ms)

    // Tuning tab (lazy-loaded on first activation; not part of the 5 s poll).
    this._tuning = null; // last get_tuning payload
    this._tuningScope = "global"; // "global" | <room name>
    this._tuningDraft = null; // { values: {field: val}, overridden: Set<field> }
    this._tuningEls = null; // stable Tuning-section refs
    this._tuningLoading = false; // in-flight guard for the lazy fetch
    this._hpEls = null; // stable Heat-pump-section refs (B2)
    this._ovEls = null; // stable Overview-section refs (strip dots, rows by key)
    // Overview 24 h chart batch: { at, key (requested ids), data: Map<id, [{t, v}]> }.
    this._ovHist = { at: 0, key: "", data: new Map(), inflight: false };
    this._manifoldEls = null; // Manifolds-section refs + cards by manifold id
    this._mfRO = null; // ResizeObserver repainting a manifold drawing on resize
    // Errors are suppressed until this epoch-ms (a save reloads the entry, so
    // get_live / get_tuning briefly return not_found — that is expected).
    this._suppressErrorUntil = 0;

    this._pollTimer = null;
    this._ageTimer = null;
    this._onVisibility = () => this._handleVisibility();
    this._hasHaIcon = false;
    this._narrow = false; // HA layout flag; drives the sidebar hamburger

    // Shared "i" tooltip: ONE positioned element serves every info button.
    this._tipEl = null; // the floating bubble (built with the skeleton)
    this._tipTextEl = null; // its text node holder
    this._tipArrowEl = null; // its arrow
    this._tipAnchor = null; // the info button the bubble is open for
    this._tipHideTimer = null; // delayed-close timer (hover hand-off)

    // Shared confirmation popover (A2): ONE fixed-position mini-dialog serves
    // the room-state segment and the home-mode switch. Never the native
    // confirm() — it blocks the event loop and cannot be styled.
    this._confirmEls = null; // { el, text, yes } built with the skeleton
    this._confirmResolve = null; // pending Promise resolver, or null

    // History-chart data cache, keyed by `${entityId}|${window}`.
    this._histCache = new Map(); // key -> { at: epochMs, data: [{t, v}] }
    this._histInflight = new Map(); // key -> Promise<{at, data}> (dedup in-flight)
    this._chartRO = null; // ResizeObserver redrawing the detail chart on resize
    this._chartWindow = "24h"; // persisted across room switches
    this._chartVisible = { temp: true, setpoint: true, valve: true, hum: true };

    // If a host set `hass` before this element's definition was registered,
    // the own property shadows the setter; re-apply it through the setter.
    this._upgradeProperty("hass");
    this._upgradeProperty("narrow");
  }

  /** Canonical custom-element property-upgrade guard (see MDN). */
  _upgradeProperty(prop) {
    if (Object.prototype.hasOwnProperty.call(this, prop)) {
      const value = this[prop];
      delete this[prop];
      this[prop] = value;
    }
  }

  /** HA assigns this on every state change; keep the latest handle. */
  set hass(hass) {
    const first = this._hass === null;
    this._hass = hass;
    this._lang = this._resolveLang(hass);
    this._updateMenuBtn();
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

  /** HA assigns this on layout changes (mobile <-> desktop). */
  set narrow(value) {
    this._narrow = !!value;
    this._updateMenuBtn();
  }

  get narrow() {
    return this._narrow;
  }

  /**
   * Show the sidebar hamburger only when HA gives the user no other way in:
   * the panel is a bare custom element (``embed_iframe: false``), so HA
   * renders NO toolbar around it — on a narrow (phone) layout or with the
   * sidebar set to "always hidden" the menu would be unreachable without it.
   */
  _updateMenuBtn() {
    const btn = this._hero && this._hero.menuBtn;
    if (!btn) {
      return;
    }
    const alwaysHidden =
      !!this._hass && this._hass.dockedSidebar === "always_hidden";
    const display = this._narrow || alwaysHidden ? "" : "none";
    // `set hass` fires on every state change — only touch the CSSOM on a flip.
    if (btn.style.display !== display) {
      btn.style.display = display;
    }
  }

  connectedCallback() {
    this._activeTab = this._loadActiveTab();
    this._hasHaIcon = !!(window.customElements && customElements.get("ha-icon"));
    if (!this._hasHaIcon && window.customElements && customElements.whenDefined) {
      customElements.whenDefined("ha-icon").then(
        () => {
          this._hasHaIcon = true;
          // Force the detail AND the table rows to rebuild so the native icon
          // affordances (segment control, close, links) swap in.
          this._detailRoom = null;
          for (const [, el] of this._rows) {
            el.remove();
          }
          this._rows.clear();
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
    this._disconnectMfRO();
    document.removeEventListener("visibilitychange", this._onVisibility);
  }

  _disconnectChartRO() {
    if (this._chartRO) {
      this._chartRO.disconnect();
      this._chartRO = null;
    }
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

  /** Restore the persisted active tab, defaulting to `overview` (storage may throw). */
  _loadActiveTab() {
    try {
      const v = window.localStorage.getItem(ACTIVE_TAB_STORAGE_KEY);
      if (v && TAB_ORDER.includes(v)) {
        return v;
      }
    } catch (err) {
      /* private mode / disabled storage: fall through to default */
    }
    return DEFAULT_TAB;
  }

  /** Persist the active tab (best-effort; storage may be unavailable). */
  _saveActiveTab(tab) {
    try {
      window.localStorage.setItem(ACTIVE_TAB_STORAGE_KEY, tab);
    } catch (err) {
      /* ignore */
    }
  }

  // --------------------------------------------------------------------------
  // i18n & lookups
  // --------------------------------------------------------------------------

  _resolveLang(hass) {
    const raw =
      (hass &&
        ((hass.locale && hass.locale.language) ||
          hass.language ||
          hass.selectedLanguage)) ||
      "en";
    const lang = String(raw).toLowerCase();
    if (lang.startsWith("pl")) {
      return "pl";
    }
    if (lang.startsWith("de")) {
      return "de";
    }
    return "en";
  }

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

  /** Full "i"-tooltip explanation for a flag (localised); generic fallback. */
  _flagDesc(code) {
    const meta = FLAG_LABELS[code];
    if (meta) {
      const byLang =
        this._lang === "pl"
          ? meta.descPl
          : this._lang === "de"
            ? meta.descDe
            : meta.descEn;
      return byLang || meta.descEn || "";
    }
    return this._t("flag_desc_unknown");
  }

  /** Safety-rule code (`"S1".."S5"`) for a flag, or `null`. */
  _flagSx(code) {
    const meta = FLAG_LABELS[code];
    return meta && meta.sx ? meta.sx : null;
  }

  /** Annunciator bucket for a flag; unknown codes fall into the "other" group. */
  _flagGroup(code) {
    const meta = FLAG_LABELS[code];
    return meta && meta.group ? meta.group : FLAG_GROUP_FALLBACK;
  }

  _modeLabel(mode) {
    const key = "mode_" + mode;
    return key in STR.en ? this._t(key) : mode;
  }

  _knobLabel(name) {
    const key = "tune_" + name;
    return key in STR.en ? this._t(key) : name;
  }

  /** Localised text for a `dew_excluded_reason` code (raw code as fallback). */
  _dewReasonText(code) {
    const key = DEW_REASON_KEYS[code];
    return key ? this._t(key) : String(code);
  }

  _icon(name, fallback) {
    if (this._hasHaIcon) {
      return h("ha-icon", { class: "hicon", icon: name });
    }
    return h("span", { class: "hicon-fallback", text: fallback || "›" });
  }

  // --------------------------------------------------------------------------
  // Shared "i" info tooltip (one element; hover + focus + tap; CSP-safe)
  // --------------------------------------------------------------------------

  /**
   * Build an accessible "i" info button whose bubble text is `STR[strKey]`.
   *
   * Every info button shares the ONE `_tipEl` bubble (never dozens of hidden
   * copies in the DOM). Desktop: hover shows, with a short close delay so the
   * pointer can travel onto the bubble; keyboard: focus shows, blur/Escape
   * hides; mobile: tap toggles (`pointerdown` snapshots the open state so the
   * tap's focus event cannot re-open what the click just closed).
   */
  _infoIcon(strKey) {
    return this._infoBtn(() => this._t(strKey), null);
  }

  /**
   * "i" info button whose bubble shows a LITERAL text (A6: e.g. the source
   * entity id of a signal row) instead of an STR key; `ariaKey` names the
   * accessible label (localised).
   */
  _infoIconText(text, ariaKey) {
    return this._infoBtn(() => text, this._t(ariaKey));
  }

  /** Shared builder behind `_infoIcon` / `_infoIconText`. */
  _infoBtn(getText, ariaLabel) {
    const btn = h(
      "button",
      {
        class: "info-btn",
        type: "button",
        "aria-label": ariaLabel || this._t("tooltip_show"),
        "aria-expanded": "false",
        on: {
          pointerdown: () => {
            btn._tipWasOpen = this._tipAnchor === btn;
          },
          click: (e) => {
            e.stopPropagation();
            e.preventDefault();
            if (btn._tipWasOpen) {
              this._hideTip();
            } else {
              this._showTip(btn, getText());
            }
          },
          mouseenter: () => this._showTip(btn, getText()),
          mouseleave: () => this._scheduleTipHide(),
          focus: () => this._showTip(btn, getText()),
          blur: () => this._hideTip(),
        },
      },
      [this._icon("mdi:information-outline", "ⓘ")],
    );
    // Fixed accessible labels (a literal-text icon keeps its own label; the
    // STR-key icons toggle show/hide like before).
    btn._ariaShow = ariaLabel || this._t("tooltip_show");
    btn._ariaHide = ariaLabel || this._t("tooltip_hide");
    return btn;
  }

  /** Show the shared bubble for `btn` with `text`, in viewport coordinates. */
  _showTip(btn, text) {
    const tip = this._tipEl;
    if (!tip || !btn.isConnected) {
      return;
    }
    this._cancelTipHide();
    if (this._tipAnchor && this._tipAnchor !== btn) {
      this._clearTipAnchor();
    }
    this._tipAnchor = btn;
    this._tipTextEl.textContent = text;
    // Reset before measuring so a previous clamp cannot skew the size.
    tip.style.left = "0px";
    tip.style.top = "0px";
    tip.style.display = "";
    const rect = btn.getBoundingClientRect();
    const tw = tip.offsetWidth;
    const th = tip.offsetHeight;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    // Horizontal: centred on the icon, clamped to the viewport.
    const left = clamp(rect.left + rect.width / 2 - tw / 2, 8, Math.max(8, vw - tw - 8));
    // Vertical: below the icon by default, flipped above when short of room.
    let top = rect.bottom + 8;
    let above = false;
    if (top + th > vh - 8 && rect.top - th - 8 >= 8) {
      top = rect.top - th - 8;
      above = true;
    }
    top = Math.max(8, top);
    tip.style.left = left.toFixed(0) + "px";
    tip.style.top = top.toFixed(0) + "px";
    tip.classList.toggle("above", above);
    // The arrow tracks the icon centre even when the bubble is clamped.
    const arrowX = clamp(rect.left + rect.width / 2 - left, 12, Math.max(12, tw - 12));
    this._tipArrowEl.style.left = arrowX.toFixed(0) + "px";
    btn.setAttribute("aria-expanded", "true");
    btn.setAttribute("aria-describedby", "tufh-tip");
    btn.setAttribute("aria-label", btn._ariaHide || this._t("tooltip_hide"));
  }

  /** Hide the shared bubble and clear the anchor's ARIA state. */
  _hideTip() {
    this._cancelTipHide();
    if (this._tipEl) {
      this._tipEl.style.display = "none";
    }
    this._clearTipAnchor();
  }

  _clearTipAnchor() {
    const btn = this._tipAnchor;
    this._tipAnchor = null;
    if (btn) {
      btn.setAttribute("aria-expanded", "false");
      btn.removeAttribute("aria-describedby");
      btn.setAttribute("aria-label", btn._ariaShow || this._t("tooltip_show"));
    }
  }

  /** Delayed close (hover hand-off from the icon onto the bubble). */
  _scheduleTipHide() {
    this._cancelTipHide();
    this._tipHideTimer = window.setTimeout(() => {
      this._tipHideTimer = null;
      this._hideTip();
    }, 250);
  }

  _cancelTipHide() {
    if (this._tipHideTimer !== null) {
      window.clearTimeout(this._tipHideTimer);
      this._tipHideTimer = null;
    }
  }

  // --------------------------------------------------------------------------
  // Shared confirmation popover (A2; CSP-safe, no native confirm())
  // --------------------------------------------------------------------------

  /**
   * Ask an inline "are you sure?" anchored at `anchor`; resolves to a boolean.
   *
   * One shared fixed-position mini-dialog (mirror of the shared tooltip):
   * Escape / click-outside / Cancel resolve false, the primary button true.
   * Focus moves onto the confirm button so Enter confirms from the keyboard.
   * A second ask while one is pending cancels the first.
   */
  _confirm(anchor, text) {
    const C = this._confirmEls;
    if (!C || !anchor || !anchor.isConnected) {
      return Promise.resolve(true);
    }
    this._resolveConfirm(false);
    this._hideTip();
    return new Promise((resolve) => {
      this._confirmResolve = resolve;
      C.text.textContent = text;
      // Reset before measuring so a previous clamp cannot skew the size.
      C.el.style.left = "0px";
      C.el.style.top = "0px";
      C.el.style.display = "";
      const rect = anchor.getBoundingClientRect();
      const tw = C.el.offsetWidth;
      const th = C.el.offsetHeight;
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const left = clamp(
        rect.left + rect.width / 2 - tw / 2,
        8,
        Math.max(8, vw - tw - 8),
      );
      let top = rect.bottom + 8;
      if (top + th > vh - 8 && rect.top - th - 8 >= 8) {
        top = rect.top - th - 8;
      }
      top = Math.max(8, top);
      C.el.style.left = left.toFixed(0) + "px";
      C.el.style.top = top.toFixed(0) + "px";
      C.yes.focus();
    });
  }

  /** Settle a pending confirm (hides the popover; no-op when none pending). */
  _resolveConfirm(result) {
    const resolve = this._confirmResolve;
    this._confirmResolve = null;
    if (this._confirmEls) {
      this._confirmEls.el.style.display = "none";
    }
    if (resolve) {
      resolve(result);
    }
  }

  // --------------------------------------------------------------------------
  // Websocket / data layer + user actions
  // --------------------------------------------------------------------------

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
      // Localised prefix; the technical detail stays verbatim after it.
      this._setError(
        fmtStr(this._t("err_ws"), { detail: `${message.type}: ${detail}` }),
      );
      return null;
    }
  }

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

  async _loadConfig() {
    const cfg = await this._callWS({ type: WS.getConfig });
    if (cfg) {
      this._config = cfg;
      this._render();
      // If the Tuning tab was opened before config arrived its per-room scope
      // chips were empty (only Global). Now that the rooms are known, refresh
      // just the chip row — leaving the knob steppers (and any edit draft) be.
      if (this._tuning) {
        this._renderTuningScopes();
      }
    }
  }

  async _poll() {
    const homeGen = this._homeEditGen;
    const live = await this._callWS({ type: WS.getLive });
    if (live) {
      this._live = live;
      const ts = live.last_update_timestamp
        ? Date.parse(live.last_update_timestamp)
        : NaN;
      this._lastUpdateMs = Number.isFinite(ts) ? ts : null;
      // get_config is fetched only on open and after the panel's own edits, so
      // follow an external home-setpoint change (an automation, the number
      // entity) from the polled copy — unless a local edit started or ended
      // while this request was out, or is still in flight (the reply may
      // predate the write; the edit's own follow-up poll reconciles).
      const home = num(live.home_setpoint_c);
      if (
        home !== null &&
        this._config &&
        this._homeEditsInFlight === 0 &&
        homeGen === this._homeEditGen &&
        num(this._config.home_setpoint_c) !== home
      ) {
        this._config = { ...this._config, home_setpoint_c: home };
      }
      this._render();
    }
  }

  // -- History fetch (recorder; cached per entity+window, silent on error) --

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

  /**
   * Resolve a time series for one entity + window, cached and de-duplicated.
   *
   * Returns `{ at, data }` where `data` is `[{t: epochMs, v: number|null}]`
   * sorted ascending and `v` is null for unavailable/unknown samples (a gap).
   * With `text` truthy, `v` is the raw state string instead (mode sensors).
   * Fresh cache hits skip the network entirely, so the 5 s live poll never
   * refetches history; concurrent requests for the same key share one call.
   */
  _series(entityId, windowKey, text) {
    const key = entityId + "|" + windowKey + (text ? "|text" : "");
    const now = Date.now();
    const cached = this._histCache.get(key);
    if (cached && now - cached.at < HIST_MAXAGE_MS) {
      return Promise.resolve(cached);
    }
    const inflight = this._histInflight.get(key);
    if (inflight) {
      return inflight;
    }
    const p = this._fetchOne(entityId, windowKey, text)
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
  async _fetchOne(entityId, windowKey, text) {
    const w = WINDOWS[windowKey] || WINDOWS[SPARK_WINDOW];
    const end = new Date();
    const start = new Date(end.getTime() - w.hours * 3600 * 1000);
    // Textual sensors ALWAYS use history mode (long-term statistics do not
    // exist for them, even on the 7d window); the volume stays tiny because
    // the recorder stores only mode changes.
    if (!text && w.mode === "stats") {
      const res = await this._callWSSilent({
        type: WS.statistics,
        start_time: start.toISOString(),
        end_time: end.toISOString(),
        statistic_ids: [entityId],
        period: w.period,
        types: ["mean"],
      });
      return statPoints(res && res[entityId], w.periodMs || 3600 * 1000);
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
        v: text ? parseTextState(pt.s) : parseState(pt.s),
      }))
      .filter((pt) => pt.t !== null)
      .sort((a, b) => a.t - b.t);
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
    this._homeEditGen += 1;
    this._homeEditsInFlight += 1;
    this._patchConfig((c) => {
      c.home_setpoint_c = next;
    });
    try {
      await this._callWS({ type: WS.setHome, temperature: next });
    } finally {
      this._homeEditsInFlight -= 1;
      this._homeEditGen += 1;
    }
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

  async _setMode(mode, anchor) {
    if (!MODES.includes(mode)) {
      return;
    }
    // No-op click on the already-active mode: no question, no write.
    const current = pick(this._live || {}, ["mode"], pick(this._config || {}, ["mode"], null));
    if (mode === current) {
      return;
    }
    // Light confirmation (A2): the whole-home mode is too easy to fat-finger.
    const ok = await this._confirm(
      anchor,
      fmtStr(this._t("confirm_mode"), { mode: this._modeLabel(mode) }),
    );
    if (!ok) {
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

  async _setRoomState(room, state, anchor) {
    if (!ROOM_STATES.includes(state)) {
      return;
    }
    const view = this._viewByName.get(room);
    if (view && view.state === state) {
      return;
    }
    // Light confirmation (A2): the two-state segment is too easy to hit by
    // accident, and Live starts writing to the actuators immediately.
    const key = state === STATE_LIVE ? "confirm_state_live" : "confirm_state_off";
    const ok = await this._confirm(anchor, fmtStr(this._t(key), { room }));
    if (!ok) {
      return;
    }
    // Optimistic patch of both payloads: config (authoritative on next
    // get_config) and the live view (polled), so the segment reflects the
    // click before the round-trip and the poll reconciles.
    this._patchConfig((c) => {
      const r = (c.rooms || []).find((x) => x.name === room);
      if (r) {
        r.control_state = state;
      }
    });
    if (this._live && this._live.rooms && this._live.rooms[room]) {
      const lr = { ...this._live.rooms[room], control_state: state };
      this._live = {
        ...this._live,
        rooms: { ...this._live.rooms, [room]: lr },
      };
      this._render();
    }
    await this._callWS({ type: WS.setRoomState, room, state });
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

  // --------------------------------------------------------------------------
  // View-model & entity-state readers
  // --------------------------------------------------------------------------

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
      // Prefer the measured room temperature echoed in the report. Only fall
      // back to reconstructing it from `setpoint - error` when the field is
      // ABSENT (older backend); a present-but-null value means the sensor is
      // lost and must render as "—", never as a fabricated number.
      const current =
        report.room_temperature_c !== undefined
          ? num(report.room_temperature_c)
          : setpoint !== null && errorC !== null
            ? setpoint - errorC
            : null;
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
        fastKind: String(pick(c, ["fast_source_kind"], "none") || "none"),
        // Multisplit outdoor-unit group key (K9, 2026-07-12) — "" ungrouped.
        fastGroup: String(pick(c, ["fast_source_group"], "") || ""),
        // Quiet-hours window (B1, 2026-07-12) — "" when unrestricted.
        fastWindowStart: String(pick(c, ["fast_source_window_start"], "") || ""),
        fastWindowEnd: String(pick(c, ["fast_source_window_end"], "") || ""),
        // Canonical two-state control: prefer the polled live value, then the
        // config value, defaulting to off (safe, matches the adapter).
        state: this._normState(pick(lv, ["control_state"], pick(c, ["control_state"], STATE_OFF))),
        cooling: !!pick(c, ["cooling_enabled"], false),
        // Additive report fields (F5): why the room is excluded from the global
        // safe dew point, and the fast-source min ON/OFF dwell lock remaining.
        dewReason: pick(report, ["dew_excluded_reason"], null) || null,
        fastDwell: num(report.fast_dwell_remaining_s),
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

  _severity(flags) {
    let rank = 0;
    for (const f of flags) {
      rank = Math.max(rank, SEV_RANK[this._flagSev(f)] || 0);
    }
    return this._sevName(rank);
  }

  /** Map a SEV_RANK integer back to its severity name. */
  _sevName(rank) {
    return rank >= 4
      ? "alarm"
      : rank === 3
        ? "problem"
        : rank === 2
          ? "warn"
          : rank === 1
            ? "info"
            : "ok";
  }

  /** Coerce any control-state value to one of `ROOM_STATES` (default off). */
  _normState(value) {
    const s = String(value);
    return ROOM_STATES.includes(s) ? s : STATE_OFF;
  }

  /**
   * Localised fast-source (assist) badge for a room view-model.
   *
   * Returns `{ text, cls }` where `cls` is `off` / `none` / `on` plus the
   * mode colour class (`fastModeCls`). Composes the source kind
   * (Split / Heater) with its command state and target, e.g.
   * "Split: heating → 22.0 °C"; a room with no fast source reads as an em dash.
   */
  _assistLabel(r) {
    if (r.fastKind === "none") {
      return { text: this._t("assist_none"), cls: "none" };
    }
    const short =
      r.fastKind === "heater" ? this._t("assist_heater") : this._t("assist_split");
    if (!r.fastOn || r.fastMode === "off") {
      return { text: short + ": " + this._t("assist_state_off"), cls: "off" };
    }
    const verb =
      r.fastMode === "dry"
        ? this._t("assist_state_dry")
        : r.fastMode === "cooling"
          ? this._t("assist_state_cooling")
          : this._t("assist_state_heating");
    const target = r.fastTarget !== null ? " → " + fmt(r.fastTarget, 1, " °C") : "";
    return {
      text: short + ": " + verb + target,
      cls: ("on " + fastModeCls(r.fastMode)).trim(),
    };
  }

  /**
   * Localised fast-source MODE only (Off / Cool / Heat), for the Rooms table's
   * split-out "Tryb" column. Same on/off/mode reading as `_assistLabel`, minus
   * the source-kind prefix and target temperature.
   *
   * Returns `{ text, cls }` where `cls` is `off` / `none` / `on` plus the
   * mode colour class (`fastModeCls`).
   */
  _assistModeLabel(r) {
    if (r.fastKind === "none") {
      return { text: this._t("assist_none"), cls: "none" };
    }
    if (!r.fastOn || r.fastMode === "off") {
      return { text: this._t("assist_state_off"), cls: "off" };
    }
    const verb =
      r.fastMode === "dry"
        ? this._t("assist_state_dry")
        : r.fastMode === "cooling"
          ? this._t("assist_state_cooling")
          : this._t("assist_state_heating");
    return { text: verb, cls: ("on " + fastModeCls(r.fastMode)).trim() };
  }

  /** Aggregated reason why the safe dew point is unavailable (hero subline). */
  _dewReasonSummary() {
    const excluded = this._view.filter((r) => r.dewReason);
    if (!excluded.length) {
      return "";
    }
    if (excluded.length <= 3) {
      return excluded
        .map((r) => r.name + ": " + this._dewReasonText(r.dewReason))
        .join(" · ");
    }
    return fmtStr(this._t("dew_none_collective"), { n: excluded.length });
  }

  /** Read a raw HA state string for an entity, or null when absent. */
  _hassState(entityId) {
    if (!entityId) {
      return null;
    }
    const st = this._hass && this._hass.states && this._hass.states[entityId];
    return st || null;
  }

  /** Numeric temperature/state for an entity (state number), or null. */
  _stateNum(entityId) {
    const st = this._hassState(entityId);
    if (!st) {
      return null;
    }
    if (UNAVAILABLE_STATES.has(String(st.state))) {
      return null;
    }
    return num(st.state);
  }

  /**
   * Valve feedback position [%] for an entity: `current_position` for the
   * native `valve` domain, else the numeric state (a `number`/`input_number`).
   */
  _valvePosition(entityId) {
    const st = this._hassState(entityId);
    if (!st) {
      return null;
    }
    const attrs = st.attributes || {};
    if (attrs.current_position !== undefined && attrs.current_position !== null) {
      return num(attrs.current_position);
    }
    if (UNAVAILABLE_STATES.has(String(st.state))) {
      return null;
    }
    return num(st.state);
  }

  /** Split a config `entities` value into a clean id list (single or array). */
  _entityList(value) {
    if (Array.isArray(value)) {
      return value.filter(Boolean);
    }
    return value ? [value] : [];
  }

  /** Loop descriptors (valve / supply / return ids) zipped by index. */
  _roomLoops(r) {
    const ent = r.entities || {};
    const valves = this._entityList(ent[LOOP_VALVE_KEY]);
    const supplies = this._entityList(ent[LOOP_SUPPLY_KEY]);
    const returns = this._entityList(ent[LOOP_RETURN_KEY]);
    const count = Math.max(valves.length, supplies.length, returns.length);
    const loops = [];
    for (let i = 0; i < count; i += 1) {
      loops.push({
        valveId: valves[i] || null,
        supplyId: supplies[i] || null,
        returnId: returns[i] || null,
      });
    }
    return loops;
  }

  _ageMs() {
    if (this._lastUpdateMs === null) {
      return null;
    }
    return Math.max(0, Date.now() - this._lastUpdateMs);
  }

  _ageText(ms) {
    const totalSec = Math.floor(ms / 1000);
    if (totalSec < 60) {
      return fmtStr(this._t("age_sec"), { s: totalSec });
    }
    const totalMin = Math.floor(totalSec / 60);
    if (totalMin < 10) {
      return fmtStr(this._t("age_min_sec"), { m: totalMin, s: totalSec % 60 });
    }
    if (totalMin < 60) {
      return fmtStr(this._t("age_min"), { m: totalMin });
    }
    return fmtStr(this._t("age_hour_min"), {
      h: Math.floor(totalMin / 60),
      m: totalMin % 60,
    });
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

  // --------------------------------------------------------------------------
  // Skeleton, tabs & hero
  // --------------------------------------------------------------------------

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
      this._rows = new Map();
      this._tabs = null;
      this._detail = null;
      this._detailRoom = null;
      this._tuningEls = null;
    }
    this._hasHaIcon = !!(window.customElements && customElements.get("ha-icon"));
    this._builtLang = this._lang;
    this._built = true;
    this._buildSkeleton();
  }

  _buildSkeleton() {
    const banner = h("div", { class: "banner", style: "display:none" });
    const hero = this._buildHero();
    const tabbar = this._buildTabs();

    // The tab sections, built once and toggled with `display` (never rebuilt), so
    // switching tabs is instant and never disturbs scroll or in-flight fetches.
    const roomsSection = this._buildRoomsSection();
    const sections = {
      overview: this._buildOverviewSection(),
      rooms: roomsSection.el,
      flags: this._buildFlagsSection(),
      tuning: this._buildTuningSection(),
      assist: this._buildAssistSection(),
      hp: this._buildHpSection(),
      manifolds: this._buildManifoldsSection(),
    };
    this._tabs.sections = sections;

    const main = h("div", { class: "col-main" }, TAB_ORDER.map((k) => sections[k]));
    // The room inspector: a sticky side column on wide screens, a right-side
    // overlay drawer (with a click-to-close scrim) at or below OVERLAY_MAX_PX.
    const detail = h("section", {
      class: "detail",
      role: "dialog",
      tabindex: "-1",
      style: "display:none",
    });
    const scrim = h("div", {
      class: "detail-scrim",
      "aria-hidden": "true",
      on: { click: () => this._deselect() },
    });
    const layout = h("div", { class: "layout" }, [main, scrim, detail]);

    // The single shared info tooltip (position: fixed, so no scroll container
    // — the detail drawer included — can ever clip it).
    this._tipTextEl = h("span", { class: "info-tip-text" });
    this._tipArrowEl = h("span", { class: "info-tip-arrow", "aria-hidden": "true" });
    this._tipEl = h(
      "div",
      {
        class: "info-tip",
        role: "tooltip",
        id: "tufh-tip",
        style: "display:none",
        on: {
          // Let the pointer travel onto the bubble (text selection).
          mouseenter: () => this._cancelTipHide(),
          mouseleave: () => this._scheduleTipHide(),
        },
      },
      [this._tipArrowEl, this._tipTextEl],
    );
    this._tipAnchor = null;

    // The single shared confirmation popover (A2): text + [yes] [cancel],
    // positioned next to the clicked control (same fixed-position pattern as
    // the tooltip). Escape / click-outside cancels; focus lands on "yes" so
    // the keyboard flow works.
    const confirmText = h("div", { class: "confirm-text" });
    const confirmYes = h("button", {
      class: "confirm-yes",
      type: "button",
      text: this._t("confirm_yes"),
      on: { click: () => this._resolveConfirm(true) },
    });
    const confirmNo = h("button", {
      class: "confirm-no",
      type: "button",
      text: this._t("confirm_cancel"),
      on: { click: () => this._resolveConfirm(false) },
    });
    const confirmEl = h(
      "div",
      {
        class: "confirm-pop",
        role: "dialog",
        "aria-modal": "false",
        style: "display:none",
        on: {
          keydown: (e) => {
            if (e.key === "Escape") {
              e.stopPropagation();
              this._resolveConfirm(false);
            }
          },
        },
      },
      [confirmText, h("div", { class: "confirm-btns" }, [confirmYes, confirmNo])],
    );
    this._confirmEls = { el: confirmEl, text: confirmText, yes: confirmYes };

    const wrap = h(
      "div",
      {
        class: "wrap",
        on: {
          // Escape closes the confirm popover / info tooltip first, then the
          // room inspector.
          keydown: (e) => {
            if (e.key === "Escape" && this._confirmResolve) {
              e.stopPropagation();
              this._resolveConfirm(false);
              return;
            }
            if (e.key === "Escape" && this._tipAnchor) {
              e.stopPropagation();
              this._hideTip();
              return;
            }
            if (e.key === "Escape" && this._selectedRoom) {
              e.stopPropagation();
              this._deselect();
            }
          },
          // A pointer press anywhere outside the tooltip / confirm popover
          // closes it (tap-away on mobile; a press outside = cancel).
          pointerdown: (e) => {
            const path = e.composedPath ? e.composedPath() : [];
            if (this._confirmResolve && !path.includes(confirmEl)) {
              this._resolveConfirm(false);
            }
            if (!this._tipAnchor) {
              return;
            }
            if (!path.includes(this._tipEl) && !path.includes(this._tipAnchor)) {
              this._hideTip();
            }
          },
        },
      },
      [banner, hero, tabbar, layout, this._tipEl, confirmEl],
    );
    // Any inner scroll (table wrap, detail drawer) invalidates the fixed
    // positions — just close the bubble and cancel a pending confirm.
    wrap.addEventListener(
      "scroll",
      () => {
        this._hideTip();
        this._resolveConfirm(false);
      },
      true,
    );

    this._els = {
      wrap,
      banner,
      tbody: roomsSection.tbody,
      empty: roomsSection.empty,
      tableWrap: roomsSection.wrapEl,
      detail,
      layout,
    };
    this.shadowRoot.appendChild(wrap);
  }

  /** Build the tablist between the hero and the layout; stores refs on `_tabs`. */
  _buildTabs() {
    const btns = {};
    const bar = h("div", { class: "tabbar", role: "tablist" });
    TABS.forEach((t, i) => {
      const btn = h("button", {
        class: "tab",
        type: "button",
        role: "tab",
        text: this._t(t.label),
        dataset: { tab: t.key },
        tabindex: "-1",
        on: {
          click: () => this._setActiveTab(t.key),
          keydown: (e) => this._onTabKey(e, i),
        },
      });
      btns[t.key] = btn;
      bar.appendChild(btn);
    });
    this._tabs = { bar, btns, sections: null };
    return bar;
  }

  /** Arrow-key navigation across the tablist (roving tabindex). */
  _onTabKey(ev, index) {
    let next = null;
    if (ev.key === "ArrowRight" || ev.key === "ArrowDown") {
      next = (index + 1) % TAB_ORDER.length;
    } else if (ev.key === "ArrowLeft" || ev.key === "ArrowUp") {
      next = (index - 1 + TAB_ORDER.length) % TAB_ORDER.length;
    } else if (ev.key === "Home") {
      next = 0;
    } else if (ev.key === "End") {
      next = TAB_ORDER.length - 1;
    } else {
      return;
    }
    ev.preventDefault();
    const key = TAB_ORDER[next];
    this._setActiveTab(key);
    const btn = this._tabs && this._tabs.btns[key];
    if (btn) {
      btn.focus();
    }
  }

  /** Switch the active tab, persist it, and reveal / lazily update its section. */
  _setActiveTab(tab) {
    if (!TAB_ORDER.includes(tab)) {
      return;
    }
    if (tab !== this._activeTab) {
      this._activeTab = tab;
      this._saveActiveTab(tab);
    }
    this._syncTabs();
  }

  /**
   * Toggle section visibility + tab ARIA to match `_activeTab`, then update only
   * the visible section (hidden sections stay stale until shown — lazy).
   */
  _syncTabs() {
    const T = this._tabs;
    if (!T || !T.sections) {
      return;
    }
    for (const key of TAB_ORDER) {
      const on = key === this._activeTab;
      T.sections[key].style.display = on ? "" : "none";
      const btn = T.btns[key];
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
      btn.tabIndex = on ? 0 : -1;
    }
    if (this._activeTab === "overview") {
      this._renderOverview();
    } else if (this._activeTab === "rooms") {
      this._reconcileTable();
    } else if (this._activeTab === "flags") {
      this._updateFlagLegend();
    } else if (this._activeTab === "tuning") {
      this._ensureTuningLoaded();
    } else if (this._activeTab === "assist") {
      this._renderAssist();
    } else if (this._activeTab === "hp") {
      this._renderHp();
    } else if (this._activeTab === "manifolds") {
      this._renderManifolds();
    }
  }

  _buildHero() {
    const H = {};

    // Sidebar hamburger: opens the HA menu via the standard bubbled
    // `hass-toggle-menu` event. Hidden on wide layouts (`_updateMenuBtn`).
    H.menuBtn = h(
      "button",
      {
        class: "ghost-btn icon-only",
        type: "button",
        style: "display:none",
        title: this._t("menu_btn"),
        "aria-label": this._t("menu_btn"),
        on: {
          click: () =>
            this.dispatchEvent(
              new CustomEvent("hass-toggle-menu", {
                bubbles: true,
                composed: true,
              }),
            ),
        },
      },
      [this._icon("mdi:menu", "☰")],
    );

    // Brand mark. The mark is the served brand icon; on load failure (e.g. dev
    // preview without the static path) it swaps itself for the mdi/glyph mark.
    const brandImg = h("img", { class: "brand-img", src: BRAND_ICON_URL, alt: "" });
    brandImg.addEventListener("error", () => {
      brandImg.replaceWith(this._icon("mdi:tortoise", "🐢"));
    });
    const brand = h("div", { class: "brand" }, [
      brandImg,
      h("span", { class: "brand-name", text: "Tortoise-UFH" }),
    ]);

    // Status pill + inline stats (single-row hero — caption beside value).
    H.pillDot = h("span", { class: "pill-dot" });
    H.pillText = h("span", { class: "pill-text" });
    H.pill = h("div", { class: "pill" }, [H.pillDot, H.pillText]);

    H.liveVal = h("span", { class: "stat-val" });
    const liveMetric = h("div", { class: "stat" }, [
      h("span", { class: "stat-cap", text: this._t("rooms_live_cap") }),
      H.liveVal,
    ]);

    // The flags stat doubles as a shortcut to the dedicated Flags tab.
    H.flagsVal = h("span", { class: "stat-val" });
    const gotoFlags = () => this._setActiveTab("flags");
    H.flagsMetric = h(
      "div",
      {
        class: "stat flags-stat",
        role: "button",
        tabindex: "0",
        on: {
          click: gotoFlags,
          keydown: (e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              gotoFlags();
            }
          },
        },
      },
      [h("span", { class: "stat-cap", text: this._t("flags_cap") }), H.flagsVal],
    );

    H.dewVal = h("span", { class: "stat-val" });
    H.dewChip = h("div", { class: "chip chip-dew", style: "display:none" }, [
      h("span", { class: "stat-cap", text: this._t("dew_cap") }),
      H.dewVal,
    ]);

    // Controls: home stepper + mode segmented + manage (icon-only).
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
      { class: "seg", role: "group", "aria-label": this._t("mode_cap") },
      MODES.map((m) => {
        const b = h("button", {
          class: "seg-btn",
          type: "button",
          text: this._modeLabel(m),
          dataset: { mode: m },
          on: { click: () => this._setMode(m, b) },
        });
        H.modeBtns[m] = b;
        return b;
      }),
    );
    const modeCtl = h("div", { class: "ctl" }, [
      h("span", { class: "ctl-cap", text: this._t("mode_cap") }),
      seg,
    ]);

    H.manageBtn = h(
      "button",
      {
        class: "ghost-btn icon-only",
        type: "button",
        title: this._t("manage_rooms"),
        "aria-label": this._t("manage_rooms"),
        on: { click: () => this._manageRooms() },
      },
      [this._icon("mdi:cog-outline", "⚙")],
    );

    this._hero = H;
    this._updateMenuBtn();
    return h("header", { class: "hero" }, [
      H.menuBtn,
      brand,
      H.pill,
      liveMetric,
      H.flagsMetric,
      H.dewChip,
      h("span", { class: "spacer" }),
      homeCtl,
      modeCtl,
      H.manageBtn,
    ]);
  }

  /** Press-and-hold repeat with acceleration; keyboard-activated once. */
  _bindHold(btn, fn) {
    let timer = null;
    let delay = 350;
    const stop = () => {
      if (timer) {
        window.clearTimeout(timer);
        timer = null;
      }
    };
    const repeat = () => {
      timer = window.setTimeout(() => {
        fn();
        delay = Math.max(45, delay * 0.82);
        repeat();
      }, delay);
    };
    btn.addEventListener("pointerdown", (e) => {
      if (e.button != null && e.button !== 0) {
        return;
      }
      if (btn.disabled) {
        return;
      }
      fn();
      delay = 350;
      stop();
      repeat();
    });
    for (const ev of ["pointerup", "pointerleave", "pointercancel"]) {
      btn.addEventListener(ev, stop);
    }
    btn.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        fn();
      }
    });
  }

  _render() {
    this._ensureBuilt();
    this._computeView();

    if (this._els.banner) {
      this._els.banner.textContent = this._error;
      this._els.banner.style.display = this._error ? "" : "none";
    }

    this._updateHero();
    this._updateFlagLegend();
    this._syncTabs();
    this._syncDetail();
  }

  _setError(msg) {
    // While a save is reloading the entry, transient errors (get_live /
    // get_tuning → not_found) are expected: swallow non-empty messages during
    // the suppression window. Clears (empty msg) always pass through.
    if (msg && Date.now() < this._suppressErrorUntil) {
      return;
    }
    if (msg === this._error) {
      return;
    }
    this._error = msg;
    if (this._els && this._els.banner) {
      this._els.banner.textContent = msg;
      this._els.banner.style.display = msg ? "" : "none";
    }
  }

  _applyPill() {
    if (!this._hero) {
      return;
    }
    const { sev, text } = this._pillState();
    this._hero.pill.className = "pill pill-" + sev;
    this._hero.pillText.textContent = text;
    // Tooltip: the exact local timestamp (with seconds) of the last cycle.
    this._hero.pill.title =
      this._lastUpdateMs === null
        ? this._t("age_unknown")
        : new Date(this._lastUpdateMs).toLocaleString(this._lang, {
            year: "numeric",
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
          });
  }

  /** 1-second tick: refresh only the age-dependent pill (cheap, no reflow). */
  _tickAge() {
    if (this._built) {
      this._applyPill();
      // The Overview's attention list carries the same ticking status line.
      const O = this._ovEls;
      if (O && O.attStatusLabel) {
        O.attStatusLabel.textContent = this._pillState().text;
      }
    }
  }

  _updateHero() {
    const H = this._hero;
    const cfg = this._config || {};
    const live = this._live || {};

    this._applyPill();

    const total = this._view.length;
    const liveN = this._view.filter((r) => r.state === STATE_LIVE).length;
    H.liveVal.textContent = total ? `${liveN}/${total}` : "—";

    // Compact, severity-aware summary: how many DISTINCT flags are active and
    // the worst severity among them (the annunciator below breaks them down).
    const activeFlags = new Set();
    let flagRank = 0;
    for (const r of this._view) {
      for (const code of r.flags) {
        activeFlags.add(code);
        flagRank = Math.max(flagRank, SEV_RANK[this._flagSev(code)] || 0);
      }
    }
    H.flagsVal.textContent = String(activeFlags.size);
    H.flagsMetric.className =
      "stat flags-stat" + (activeFlags.size ? " sev-" + this._sevName(flagRank) : "");

    // Prefer live.mode: it is refreshed on every poll, whereas config.mode is
    // only re-fetched on writes / initial load (so it can lag an external
    // mode change made through the HA mode entity).
    const mode = pick(live, ["mode"], pick(cfg, ["mode"], "heating"));
    for (const m of MODES) {
      H.modeBtns[m].classList.toggle("active", m === mode);
    }

    // Safe dew point is only meaningful while cooling; the chip stays visible
    // throughout cooling and, when there is no value, explains why (per room
    // for a small house, collectively above three rooms).
    const dew = num(live.global_safe_dew_point_c);
    if (mode === "cooling") {
      H.dewChip.style.display = "";
      if (dew !== null) {
        H.dewVal.textContent = fmt(dew, 1, " °C");
        H.dewChip.title = "";
      } else {
        H.dewVal.textContent = "—";
        H.dewChip.title = this._dewReasonSummary();
      }
    } else {
      H.dewChip.style.display = "none";
    }

    const homeTemp = num(cfg.home_setpoint_c);
    H.homeVal.textContent = fmt(homeTemp, 1, " °C");
    H.homeMinus.disabled = homeTemp === null || homeTemp <= HOME_MIN;
    H.homePlus.disabled = homeTemp === null || homeTemp >= HOME_MAX;
  }

  // --------------------------------------------------------------------------
  // Overview tab (dashboard)
  // --------------------------------------------------------------------------

  /**
   * Build the Overview tab once: a one-screen dashboard of house-level
   * aggregates (the per-room detail lives on the Rooms tab). The main column
   * holds the hero — comfort (controlled rooms in band, the counts outside
   * it, the "now" deviation strip) beside what needs attention, counted per
   * flag — over three 24 h charts on one time axis (the rooms' deviation
   * spread, the loop-weighted floor valve opening, the outdoor temperature);
   * the side column holds the plant rows (heat pump, assist, condensation
   * guard) and one line per manifold. A container query stacks the columns
   * when the tab itself is narrow (phone, open inspector). `_renderOverview`
   * updates every part in place on each poll; strip dots and manifold rows
   * are reconciled by key, never rebuilt by the poll.
   */
  _buildOverviewSection() {
    const O = {
      dots: new Map(), // room name -> comfort-strip dot button
      mfRows: [], // manifold row parts, in payload order
      mfSig: null, // manifold ids + circuit counts the rows were built for
      countSig: null, // comfort counts (rebuilt on change only)
      tickSig: null, // strip scale the tick labels were built for
      attSig: null, // attention entries (rebuilt on change only)
      attStatusLabel: null, // the status entry's label (written in place)
    };

    // Hero, left: the comfort headline, the counts and the deviation strip.
    O.comfortVal = h("span", { class: "ov-hero-val" });
    O.comfortSub = h("span", { class: "ov-hero-sub" });
    O.counts = h("div", { class: "ov-counts" });
    O.stripBand = h("div", { class: "ov-strip-band" });
    O.stripPlot = h("div", { class: "ov-strip-plot" }, [
      O.stripBand,
      h("div", { class: "ov-strip-zero" }),
    ]);
    O.stripTicks = h("div", { class: "ov-strip-ticks", "aria-hidden": "true" });
    O.stripBandCap = h("span", { class: "ov-strip-cap" });
    const comfort = h("div", { class: "ov-hero-comfort" }, [
      h("div", { class: "ov-eyebrow" }, [
        this._t("ov_comfort_cap"),
        this._infoIcon("tip_ov_comfort"),
        O.stripBandCap,
      ]),
      h("div", { class: "ov-hero-fig" }, [O.comfortVal, O.comfortSub]),
      O.counts,
      h("div", { class: "ov-strip" }, [O.stripPlot, O.stripTicks]),
    ]);

    // Hero, right: what needs a look, worst first; a calm OK state.
    O.attCount = h("span", { class: "chip", style: "display:none" });
    O.attList = h("div", { class: "ov-att-list", style: "display:none" });
    O.attOk = h("div", { class: "ov-att-ok", style: "display:none" }, [
      h("span", { class: "ov-ichip ok" }, [this._icon("mdi:check", "✓")]),
      h("div", null, [
        h("div", { class: "ov-att-ok-title", text: this._t("ov_att_ok") }),
        h("div", { class: "ov-att-ok-sub", text: this._t("ov_att_ok_sub") }),
      ]),
    ]);
    O.attInfo = h("div", { class: "ov-att-info", style: "display:none" });
    const attention = h("div", { class: "ov-hero-att" }, [
      h("div", { class: "ov-eyebrow" }, [this._t("ov_att_cap"), O.attCount]),
      O.attOk,
      O.attList,
      O.attInfo,
    ]);
    const hero = h("div", { class: "ov-card ov-hero" }, [comfort, attention]);

    // House charts: small multiples sharing the 24 h time axis.
    O.cDev = this._ovChartCard("ov_dev_cap", "tip_ov_dev");
    O.cFloor = this._ovChartCard("ov_floor_cap", "tip_ov_floor");
    O.cOut = this._ovChartCard("role_entity_temp_outdoor", null);
    const charts = h("div", { class: "ov-charts" }, [
      O.cDev.el,
      O.cFloor.el,
      O.cOut.el,
    ]);

    // Manifold rows (hidden without manifolds); the card opens the tab.
    O.mfList = h("div", { class: "ov-mf-list" });
    const openMf = () => this._setActiveTab("manifolds");
    O.mfCard = h(
      "div",
      {
        class: "ov-card ov-mf",
        role: "button",
        tabindex: "0",
        style: "display:none",
        on: {
          click: openMf,
          keydown: (e) => {
            if (e.target === O.mfCard && (e.key === "Enter" || e.key === " ")) {
              e.preventDefault();
              openMf();
            }
          },
        },
      },
      [h("div", { class: "ov-eyebrow", text: this._t("tab_manifolds") }), O.mfList],
    );

    // Plant rows; the heat pump and the assist open their tabs.
    O.pHp = this._ovPlantRow("tab_hp", "mdi:heat-pump-outline", "◎", () =>
      this._setActiveTab("hp"),
    );
    O.pAssist = this._ovPlantRow("tab_assist", "mdi:air-conditioner", "~", () =>
      this._setActiveTab("assist"),
    );
    O.pDew = this._ovPlantRow("ov_dew_cap", "mdi:water-alert-outline", "•", null);
    O.plant = h("div", { class: "ov-card ov-plant" }, [
      h("div", { class: "ov-eyebrow", text: this._t("ov_plant_cap") }),
      O.pHp.el,
      O.pAssist.el,
      O.pDew.el,
    ]);
    O.layout = h("div", { class: "ov-layout" }, [
      h("div", { class: "ov-main" }, [hero, charts]),
      h("div", { class: "ov-side" }, [O.plant, O.mfCard]),
    ]);

    const el = h(
      "section",
      {
        class: "tab-section",
        role: "tabpanel",
        style: "display:none",
        dataset: { tab: "overview" },
      },
      [
        h("div", { class: "ov" }, [O.layout]),
      ],
    );
    this._ovEls = O;
    return el;
  }

  /**
   * One plant row: an icon chip, the caption, the value with a details line
   * (`sub`) and an `extra` row (chips). With `onOpen` the row is a button.
   */
  _ovPlantRow(capKey, icon, glyph, onOpen) {
    const K = {};
    K.val = h("span", { class: "ov-prow-val" });
    K.sub = h("span", { class: "ov-prow-sub" });
    K.extra = h("div", { class: "ov-prow-extra" });
    const attrs = { class: "ov-prow" };
    if (onOpen) {
      attrs.class += " link";
      attrs.role = "button";
      attrs.tabindex = "0";
      attrs.on = {
        click: onOpen,
        keydown: (e) => {
          if (e.target === K.el && (e.key === "Enter" || e.key === " ")) {
            e.preventDefault();
            onOpen();
          }
        },
      };
    }
    K.el = h("div", attrs, [
      h("span", { class: "ov-ichip" }, [this._icon(icon, glyph)]),
      h("div", { class: "ov-prow-body" }, [
        h("span", { class: "ov-prow-cap", text: this._t(capKey) }),
        h("div", { class: "ov-prow-fig" }, [K.val, K.sub]),
        K.extra,
      ]),
    ]);
    return K;
  }

  /**
   * One house-chart card: the caption (+ "i"), the current value with a
   * details line, then the plot — the stretched SVG, its two scale labels,
   * the "now" dot, a hover guide and tooltip — over the time labels.
   * `_ovPaintChart` fills it on every poll.
   */
  _ovChartCard(capKey, infoKey) {
    const C = { sig: null, xSig: null, hover: [], t0: 0, t1: 1 };
    C.val = h("span", { class: "ov-chart-val" });
    C.sub = h("span", { class: "ov-chart-sub" });
    C.box = h("div", { class: "ov-chart-box" }, [ovSvg([])]);
    C.yTop = h("span");
    C.yBot = h("span");
    C.now = h("span", { class: "ov-chart-now", style: "display:none" });
    C.guide = h("div", { class: "ov-chart-guide", style: "display:none" });
    C.tip = h("div", { class: "ov-chart-tip", style: "display:none" });
    C.plot = h(
      "div",
      {
        class: "ov-chart",
        on: {
          pointermove: (e) => this._ovChartHover(C, e),
          pointerleave: () => this._ovChartHover(C, null),
        },
      },
      [
        h("div", { class: "ov-chart-y", "aria-hidden": "true" }, [C.yTop, C.yBot]),
        C.box,
        C.now,
        C.guide,
        C.tip,
      ],
    );
    C.xTicks = h("div", { class: "ov-chart-x", "aria-hidden": "true" });
    const cap = [this._t(capKey)];
    if (infoKey) {
      cap.push(this._infoIcon(infoKey));
    }
    C.el = h("div", { class: "ov-card ov-chart-card" }, [
      h("div", { class: "ov-eyebrow" }, cap),
      h("div", { class: "ov-chart-fig" }, [C.val, C.sub]),
      C.plot,
      C.xTicks,
    ]);
    return C;
  }

  /**
   * Fill a chart card: the SVG from `draw()` (rebuilt only when `sig`
   * changes), the "now" dot at `nowY` [viewBox units] (null hides it — the
   * live value stays visible even after a gap), the top / bottom scale labels
   * and the hover points `{t, text}` of the [t0, t1] window.
   */
  _ovPaintChart(C, P) {
    if (C.sig !== P.sig) {
      C.sig = P.sig;
      C.box.replaceChild(P.draw(), C.box.firstChild);
    }
    C.now.style.display = P.nowY === null ? "none" : "";
    if (P.nowY !== null) {
      C.now.className = "ov-chart-now" + (P.nowCls ? " " + P.nowCls : "");
      C.now.style.top = ((P.nowY / OV_SVG_H) * 100).toFixed(2) + "%";
    }
    C.yTop.textContent = P.yTop;
    C.yBot.textContent = P.yBot;
    C.hover = P.hover;
    C.t0 = P.t0;
    C.t1 = P.t1;
  }

  /** Hover guide + tooltip of a house chart: the bucket nearest the pointer. */
  _ovChartHover(C, e) {
    const hide = () => {
      C.guide.style.display = "none";
      C.tip.style.display = "none";
    };
    const box = e && C.hover.length ? C.box.getBoundingClientRect() : null;
    if (!box || !box.width) {
      hide();
      return;
    }
    const span = Math.max(1, C.t1 - C.t0);
    const t = C.t0 + clamp((e.clientX - box.left) / box.width, 0, 1) * span;
    let best = C.hover[0];
    for (const p of C.hover) {
      if (Math.abs(p.t - t) < Math.abs(best.t - t)) {
        best = p;
      }
    }
    // Over a gap (no bucket close by) the chart draws nothing to read.
    if (Math.abs(best.t - t) > OV_HIST_PERIOD_MS * 3) {
      hide();
      return;
    }
    const x = ((best.t - C.t0) / span) * box.width;
    C.guide.style.display = "";
    C.guide.style.left = x.toFixed(1) + "px";
    C.tip.textContent = `${this._tipTime("24h", best.t)} · ${best.text}`;
    C.tip.style.display = "";
    // Follow the pointer inside the plot; a tooltip wider than the plot
    // (it wraps at the plot + 32 px) centres over it instead, into the axis
    // gutter and the card padding.
    const w = C.tip.offsetWidth;
    const left = w > box.width ? (box.width - w) / 2 : clamp(x - w / 2, 0, box.width - w);
    C.tip.style.left = left.toFixed(1) + "px";
  }

  /** Update the Overview tab in place from the current view + live payload. */
  _renderOverview() {
    const O = this._ovEls;
    if (!O) {
      return;
    }
    // Each room's comfort band is its effective deadband (tuning payload).
    this._ensureTuningLoaded();
    const live = this._live || {};
    const mode = pick(live, ["mode"], pick(this._config || {}, ["mode"], "heating"));
    const items = this._view.map((r) => this._ovDerive(r, mode));
    this._ovRenderHero(items, mode);
    this._ovRenderAttention(items);
    this._ovRenderCharts(items);
    this._ovRenderPlant(items, mode);
    this._ovRenderManifolds();
    // An empty side column gives its width back to the main one.
    O.layout.classList.toggle(
      "no-side",
      O.plant.style.display === "none" && O.mfCard.style.display === "none",
    );
    this._ovFetchHistory();
  }

  /** Effective tuning knob for a room (override, else global), or `fallback`. */
  _ovKnob(room, key, fallback) {
    const T = this._tuning;
    if (T) {
      const over = room && T.rooms && T.rooms[room] ? num(T.rooms[room][key]) : null;
      if (over !== null) {
        return over;
      }
      const glob = T.global ? num(T.global[key]) : null;
      if (glob !== null) {
        return glob;
      }
    }
    return fallback;
  }

  /**
   * Per-room Overview facts. `dev` = measured − setpoint [K] (positive =
   * warmer than the target, the reverse sign of the table's "Uchyb" — so the
   * Overview words it instead of signing it); `zone` places it against the
   * room's comfort band (cold / ok / warm, or none without a reading). `ctl`
   * = under control: live AND not run passively by the core — with the house
   * mode Off, or in cooling for a room that does not take part (`passive`;
   * valve 0, split off, flagged `cooling_disabled`).
   */
  _ovDerive(r, mode) {
    const band = this._ovKnob(r.name, "deadband_c", OV_BAND_FALLBACK_K);
    const dev = r.current !== null && r.setpoint !== null ? r.current - r.setpoint : null;
    let zone = "none";
    if (dev !== null) {
      // Below the display step, or inside the band with float slack
      // (21.3 − 21.0 = 0.3000000000000007), reads as "in band".
      const abs = Math.abs(dev);
      zone = abs < 0.05 || abs <= band + 1e-6 ? "ok" : dev < 0 ? "cold" : "warm";
    }
    const live = r.state === STATE_LIVE;
    const passive = live && (mode === "off" || (mode === "cooling" && !r.cooling));
    return { r, band, dev, zone, live, passive, ctl: live && !passive };
  }

  /** The deviation in words ("0.6 K below the setpoint" / "in the comfort band"). */
  _ovDevText(d) {
    if (d.dev === null) {
      return "—";
    }
    if (d.zone === "ok") {
      return this._t("ov_dev_ok");
    }
    return fmtStr(this._t(d.dev < 0 ? "ov_dev_below" : "ov_dev_above"), {
      v: fmt(Math.abs(d.dev), 1),
    });
  }

  /**
   * The outdoor sensor, or null. It is one global entity the config flow
   * fans out into every room, so the first room that carries it names it.
   */
  _ovOutdoorId() {
    for (const r of this._view) {
      const id = r.entities ? r.entities[OUTDOOR_KEY] : null;
      if (id) {
        return id;
      }
    }
    return null;
  }

  /** Comfort hero: controlled rooms in band, the counts, the deviation strip. */
  _ovRenderHero(items, mode) {
    const O = this._ovEls;
    const ctl = items.filter((d) => d.ctl);
    const n = { cold: 0, warm: 0, none: 0 };
    for (const d of ctl) {
      if (d.zone !== "ok") {
        n[d.zone] += 1;
      }
    }
    if (ctl.length) {
      const ok = ctl.length - n.cold - n.warm - n.none;
      O.comfortVal.textContent = `${ok}/${ctl.length}`;
      O.comfortSub.textContent = this._t("ov_comfort_sub");
    } else {
      O.comfortVal.textContent = "—";
      let sub = this._t("ov_comfort_sub");
      if (!this._config) {
        sub = this._t("loading");
      } else if (!items.length) {
        sub = this._t("no_rooms");
      } else if (mode === "off") {
        sub = fmtStr(this._t("ov_house_off"), { mode: this._t("mode_off") });
      } else if (!items.some((d) => d.live)) {
        sub = this._t("ov_none_live");
      }
      O.comfortSub.textContent = sub;
    }
    const counts = [
      ["cold", "ov_cold", n.cold],
      ["warm", "ov_warm", n.warm],
      ["nodata", "ov_nodata", n.none],
      [
        "passive",
        "ov_no_cool",
        mode === "cooling" ? items.filter((d) => d.passive).length : 0,
      ],
      ["off", "ov_off", items.filter((d) => !d.live).length],
    ].filter((c) => c[2] > 0);
    const sig = JSON.stringify(counts);
    if (sig !== O.countSig) {
      O.countSig = sig;
      O.counts.textContent = "";
      for (const [cls, key, count] of counts) {
        O.counts.appendChild(
          h("span", { class: "ov-count " + cls }, [
            h("i"),
            h("span", { text: this._t(key) }),
            h("b", { text: String(count) }),
          ]),
        );
      }
    }
    this._ovRenderStrip(ctl.filter((d) => d.dev !== null));
  }

  /**
   * The "now" deviation strip: one dot per live room on a shared ±scale axis
   * — a distribution, not a list (a dot names its room on hover and opens
   * it on click).
   */
  _ovRenderStrip(read) {
    const O = this._ovEls;
    let maxAbs = 0;
    for (const d of read) {
      maxAbs = Math.max(maxAbs, Math.abs(d.dev));
    }
    const S = ovScale(maxAbs);
    const pct = (v) => ((clamp(v, -S, S) + S) / (2 * S)) * 100;
    const band = this._ovKnob(null, "deadband_c", OV_BAND_FALLBACK_K);
    O.stripBand.style.left = pct(-band) + "%";
    O.stripBand.style.width = pct(band) - pct(-band) + "%";
    O.stripBandCap.textContent = fmtStr(this._t("ov_band_cap"), { v: fmt(band, 1) });

    if (O.tickSig !== S) {
      O.tickSig = S;
      O.stripTicks.textContent = "";
      const step = S <= 1 ? 0.5 : 1;
      for (let v = Math.ceil(-S / step) * step; v <= S + 1e-9; v += step) {
        const val = Math.abs(v) < 1e-9 ? 0 : v;
        O.stripTicks.appendChild(
          h("span", {
            text:
              val === 0 ? this._t("chart_setpoint") : signed(val, step < 1 ? 1 : 0),
            style: `left:${pct(val)}%`,
          }),
        );
      }
    }

    const axisPx = O.stripPlot.clientWidth;
    const placed = ovSwarm(
      read.map((d) => ({ d, x: pct(d.dev) / 100 })),
      axisPx > 0 ? Math.min(0.25, OV_SWARM_DOT_PX / axisPx) : OV_SWARM_GAP,
    );
    const levels = placed.reduce((m, p) => Math.max(m, p.level + 1), 1);
    const step =
      levels > OV_SWARM_MAX_LEVELS
        ? (OV_SWARM_STEP_PX * OV_SWARM_MAX_LEVELS) / levels
        : OV_SWARM_STEP_PX;
    const seen = new Set();
    for (const p of placed) {
      const name = p.d.r.name;
      seen.add(name);
      let dot = O.dots.get(name);
      if (!dot) {
        dot = h(
          "button",
          { class: "ov-dot", type: "button", on: { click: () => this._select(name) } },
          [h("i")],
        );
        O.dots.set(name, dot);
        O.stripPlot.appendChild(dot);
      }
      const label = `${name}: ${this._ovDevText(p.d)}`;
      dot.className =
        "ov-dot z-" + p.d.zone + (name === this._selectedRoom ? " selected" : "");
      dot.style.left = (p.x * 100).toFixed(2) + "%";
      dot.style.bottom = (p.level * step - 4).toFixed(1) + "px";
      dot.title = label;
      dot.setAttribute("aria-label", label);
    }
    for (const [name, dot] of [...O.dots]) {
      if (!seen.has(name)) {
        dot.remove();
        O.dots.delete(name);
      }
    }
    // Keep the Tab order the axis order (moved only when it changed).
    const order = placed.map((p) => O.dots.get(p.d.r.name));
    const now = [...O.stripPlot.querySelectorAll(".ov-dot")];
    if (order.some((dot, i) => now[i] !== dot)) {
      for (const dot of order) {
        O.stripPlot.appendChild(dot);
      }
    }
    O.stripPlot.style.height =
      Math.min(levels, OV_SWARM_MAX_LEVELS) * OV_SWARM_STEP_PX + 16 + "px";
  }

  /**
   * Attention: the algorithm status and every active warn-or-worse flag,
   * rooms' and the heat pump's, one line per flag with HOW MANY rooms raise
   * it (which ones is the Rooms and Flags tabs' job), worst first. Room flags
   * beyond `OV_ATT_MAX` fold behind a link to the Flags tab, which lists
   * them; the status and the heat pump's flags are not there, so they always
   * show. `ok` / `info` states — intentional, nothing to fix — go to one
   * quiet line below. Rebuilt only when the content changes.
   */
  _ovRenderAttention(items) {
    const O = this._ovEls;
    const entries = [];
    const info = [];
    const pill = this._pillState();
    if (pill.sev !== "ok") {
      entries.push({ key: "status", sev: pill.sev, label: pill.text, n: 0 });
    }
    const add = (code, n, tab) => {
      const sev = this._flagSev(code);
      const entry = {
        key: (tab || "") + ":" + code,
        code,
        sev,
        label: this._flagLabel(code),
        sx: this._flagSx(code),
        n,
        tab,
      };
      ((SEV_RANK[sev] || 0) >= SEV_RANK.warn ? entries : info).push(entry);
    };
    const byCode = new Map();
    for (const d of items) {
      for (const code of d.r.flags) {
        byCode.set(code, (byCode.get(code) || 0) + 1);
      }
    }
    for (const [code, n] of byCode) {
      add(code, n, null);
    }
    const hp = (this._live && this._live.heat_pump) || null;
    if (hp && hp.flicker && Array.isArray(hp.flicker.flags)) {
      for (const code of hp.flicker.flags) {
        add(code, 0, "hp");
      }
    }
    entries.sort((a, b) => (SEV_RANK[b.sev] || 0) - (SEV_RANK[a.sev] || 0));
    // Where an entry comes from: "Rooms: 2", or the heat pump's tab name.
    const where = (e) =>
      e.tab ? this._t("tab_" + e.tab) : e.n ? `${this._t("tab_rooms")}: ${e.n}` : "";

    // The status label carries a ticking age ("stale · 8 min ago"): it is
    // written in place below, outside the signature, so it never forces a
    // rebuild (and a lost focus) on every poll.
    const sig = JSON.stringify([
      entries.map((e) => [e.key, e.sev, e.key === "status" ? "" : e.label, e.n]),
      info.map((e) => [e.key, e.n]),
    ]);
    const status = entries.find((e) => e.key === "status");
    if (sig === O.attSig) {
      if (status && O.attStatusLabel) {
        O.attStatusLabel.textContent = status.label;
      }
      return;
    }
    O.attSig = sig;
    O.attStatusLabel = null;

    const worst = entries.length ? entries[0].sev : null;
    O.attCount.style.display = entries.length ? "" : "none";
    O.attCount.className =
      "chip chip-" + (worst === "alarm" || worst === "problem" ? worst : "warn");
    O.attCount.textContent = String(entries.length);
    O.attOk.style.display = entries.length ? "none" : "";
    O.attList.style.display = entries.length ? "" : "none";
    O.attList.textContent = "";
    const folded = entries.filter((e) => e.key !== "status" && !e.tab).slice(OV_ATT_MAX);
    for (const e of entries.filter((x) => !folded.includes(x))) {
      const high = (SEV_RANK[e.sev] || 0) >= SEV_RANK.problem;
      const head = [];
      if (e.sx) {
        head.push(h("span", { class: "flag-sx", text: e.sx }));
      }
      const labelEl = h("span", { class: "ov-att-label", text: e.label });
      if (e.key === "status") {
        O.attStatusLabel = labelEl;
      } else {
        labelEl.title = e.label;
      }
      head.push(labelEl);
      if (e.code) {
        const code = e.code;
        head.push(this._infoBtn(() => this._flagDesc(code), this._t("tooltip_show")));
      }
      const from = where(e);
      O.attList.appendChild(
        h("div", { class: "ov-att-row sev-" + e.sev }, [
          h("span", { class: "ov-ichip " + (high ? "problem" : "warn") }, [
            this._icon(high ? "mdi:alert-circle-outline" : "mdi:alert-outline", "!"),
          ]),
          h("div", { class: "ov-att-head" }, head),
          from ? h("span", { class: "ov-att-where", text: from }) : null,
        ]),
      );
    }
    if (folded.length) {
      O.attList.appendChild(
        h("button", {
          class: "ov-link ov-att-more",
          type: "button",
          text: fmtStr(this._t("ov_att_more"), { n: folded.length }),
          on: { click: () => this._setActiveTab("flags") },
        }),
      );
    }

    O.attInfo.textContent = "";
    O.attInfo.style.display = info.length ? "" : "none";
    if (info.length) {
      O.attInfo.appendChild(
        h("span", { class: "ov-att-info-cap", text: this._t("ov_att_info") }),
      );
      info.forEach((e, i) => {
        const from = e.tab ? ` (${this._t("tab_" + e.tab)})` : `: ${e.n}`;
        O.attInfo.appendChild(
          document.createTextNode((i ? " · " : " ") + e.label + from),
        );
      });
    }
    // One line in the hero; the whole list on hover.
    O.attInfo.title = O.attInfo.textContent;
  }

  /**
   * The three house charts over the last 24 h. Every series aggregates the
   * rooms under control NOW bucket by bucket — the recorder keeps no history
   * of a room's control state — and ends in the live value at "now", marked
   * by the chart's dot (the floor and outdoor headlines are that value).
   */
  _ovRenderCharts(items) {
    const O = this._ovEls;
    const H = this._ovHist;
    const now = Date.now();
    const t1 = now - (now % 60000);
    const t0 = t1 - OV_HIST_HOURS * 3600 * 1000;
    const ctl = items.filter((d) => d.ctl);
    const loopsOf = (d) =>
      Math.max(1, this._entityList(d.r.entities[LOOP_VALVE_KEY]).length);
    // Which rooms (and weights) an aggregate covers: part of the signatures.
    const ctlSig = ctl.map((d) => `${d.r.name}:${loopsOf(d)}`).join("|");
    // One decimal with an explicit sign, never "-0.0".
    const k1 = (v, suffix) => signed(Math.round(v * 10) / 10 || 0, 1, suffix);
    const series = (d, key) => {
      const id = d.r.diagnosticEntities && d.r.diagnosticEntities[key];
      return id ? H.data.get(id) || [] : [];
    };
    // The live value closes a series at t1, after a gap when the newest
    // bucket is more than four periods old (compile delay + the batch age).
    const close = (pts, v) => {
      const out = pts.filter((p) => p.t >= t0 && p.t < t1);
      if (v !== null) {
        const last = out.length ? out[out.length - 1] : null;
        if (last && t1 - last.t > OV_HIST_PERIOD_MS * 4) {
          out.push({ t: last.t + OV_HIST_PERIOD_MS, v: null });
        }
        out.push({ t: t1, v });
      }
      return out;
    };
    const hoverOf = (pts, text) =>
      pts.filter((p) => p.v !== null).map((p) => ({ t: p.t, text: text(p.v) }));

    // Deviation spread: measured − setpoint (= −error_c) of the live rooms;
    // the headline is the share of room-time spent inside the comfort band.
    const devSeries = [];
    let inBand = 0;
    let samples = 0;
    for (const d of ctl) {
      const pts = series(d, "error_c")
        .filter((p) => p.t >= t0)
        .map((p) => ({ t: p.t, v: p.v === null ? null : -p.v }));
      for (const p of pts) {
        if (p.v !== null) {
          samples += 1;
          inBand += Math.abs(p.v) <= d.band + 1e-6 ? 1 : 0;
        }
      }
      devSeries.push({ pts, w: 1 });
    }
    const nowDev = ctl.filter((d) => d.dev !== null).map((d) => d.dev);
    const nowSpread = nowDev.length ? ovSpread(nowDev) : null;
    const fan = close(ovAggregate(devSeries, OV_HIST_PERIOD_MS, ovSpread), nowSpread);
    let maxAbs = 0;
    for (const p of fan) {
      if (p.v) {
        maxAbs = Math.max(maxAbs, Math.abs(p.v.lo), Math.abs(p.v.hi));
      }
    }
    const scale = ovScale(maxAbs);
    const sd = scale % 1 ? 1 : 0;
    const band = this._ovKnob(null, "deadband_c", OV_BAND_FALLBACK_K);
    O.cDev.val.textContent = samples ? fmt((100 * inBand) / samples, 0, " %") : "—";
    O.cDev.sub.textContent = samples ? this._t("ov_inband_sub") : "";
    this._ovPaintChart(O.cDev, {
      sig: JSON.stringify([H.at, t1, band, scale, nowDev, ctlSig]),
      draw: () => ovFanSvg(fan, t0, t1, band, scale),
      nowY: nowSpread ? ovDevY(nowSpread.mid, scale) : null,
      nowCls: "",
      yTop: signed(scale, sd, " K"),
      yBot: signed(-scale, sd, " K"),
      hover: hoverOf(fan, (v) =>
        fmtStr(this._t("ov_fan_tip"), {
          mid: k1(v.mid, " K"),
          lo: k1(v.lo, ""),
          hi: k1(v.hi, " K"),
        }),
      ),
      t0,
      t1,
    });

    // Floor loops: the valve opening weighted by each room's loop count.
    let wSum = 0;
    let loops = 0;
    let open = 0;
    for (const d of ctl) {
      if (d.r.valve === null) {
        continue;
      }
      const n = loopsOf(d);
      wSum += d.r.valve * n;
      loops += n;
      open += d.r.valve >= MF_OPEN_THRESHOLD_PCT ? n : 0;
    }
    const mean = loops ? wSum / loops : null;
    const valve = close(
      ovAggregate(
        ctl.map((d) => ({ pts: series(d, "recommended_valve"), w: loopsOf(d) })),
        OV_HIST_PERIOD_MS,
        ovWeightedMean,
      ),
      mean,
    );
    O.cFloor.val.textContent = fmt(mean, 0, " %");
    O.cFloor.sub.textContent = [
      this._t("ov_floor_sub"),
      loops ? fmtStr(this._t("ov_loops_open"), { n: open, m: loops }) : "",
    ]
      .filter((x) => x)
      .join(" · ");
    this._ovPaintChart(O.cFloor, {
      sig: JSON.stringify([H.at, t1, mean, ctlSig]),
      draw: () => ovSeriesSvg(valve, t0, t1, 0, 100, true),
      nowY: mean === null ? null : ovSeriesY(mean, 0, 100),
      nowCls: "primary",
      yTop: "100 %",
      yBot: "0 %",
      hover: hoverOf(valve, (v) => fmt(v, 0, " %")),
      t0,
      t1,
    });

    // Outdoor: the shared sensor (else the pump's curve input), on whole
    // degrees at least 2 K apart.
    const outId = this._ovOutdoorId();
    let tOut = outId ? this._stateNum(outId) : null;
    const hp = (this._live && this._live.heat_pump) || null;
    if (tOut === null && hp && hp.heating) {
      tOut = num(hp.heating.t_out_c);
    }
    const outPts = close(outId ? H.data.get(outId) || [] : [], tOut);
    const outVals = outPts.filter((p) => p.v !== null).map((p) => p.v);
    let lo = outVals.length ? Math.floor(Math.min(...outVals)) : 0;
    let hi = outVals.length ? Math.ceil(Math.max(...outVals)) : 2;
    if (hi - lo < 2) {
      lo = Math.floor((lo + hi) / 2) - 1;
      hi = lo + 2;
    }
    O.cOut.val.textContent = fmt(tOut, 1, " °C");
    O.cOut.sub.textContent =
      outVals.length > 1
        ? fmtStr(this._t("ov_minmax_24h"), {
            min: fmt(Math.min(...outVals), 1, " °C"),
            max: fmt(Math.max(...outVals), 1, " °C"),
          })
        : "";
    this._ovPaintChart(O.cOut, {
      sig: JSON.stringify([H.at, t1, tOut, outId]),
      draw: () => ovSeriesSvg(outPts, t0, t1, lo, hi, false),
      nowY: tOut === null ? null : ovSeriesY(tOut, lo, hi),
      nowCls: "",
      yTop: fmt(hi, 0, "°"),
      yBot: fmt(lo, 0, "°"),
      hover: hoverOf(outPts, (v) => fmt(v, 1, " °C")),
      t0,
      t1,
    });

    // The shared time axis: full six hours inside the window.
    const xSig = `${t1}|${this._lang}`;
    if (O.cDev.xSig === xSig) {
      return;
    }
    // Local wall-clock steps (setHours), so a DST change keeps 00/06/12/18.
    const tick = new Date(t0);
    tick.setMinutes(0, 0, 0);
    do {
      tick.setHours(tick.getHours() + 1);
    } while (tick.getHours() % 6);
    const first = tick.getTime();
    // "18:00", or a short "6 PM" where the locale counts twelve hours.
    let hourFmt = null;
    try {
      hourFmt = new Intl.DateTimeFormat(this._lang, { hour: "2-digit", minute: "2-digit" });
      if (hourFmt.resolvedOptions().hour12) {
        hourFmt = new Intl.DateTimeFormat(this._lang, { hour: "numeric" });
      }
    } catch (err) {
      hourFmt = null;
    }
    const ticks = [];
    for (const d = new Date(first); d.getTime() < t1 - 3600 * 1000; ) {
      ticks.push(new Date(d));
      d.setHours(d.getHours() + 6);
    }
    for (const C of [O.cDev, O.cFloor, O.cOut]) {
      C.xSig = xSig;
      C.xTicks.textContent = "";
      for (const d of ticks) {
        C.xTicks.appendChild(
          h("span", {
            text: hourFmt ? hourFmt.format(d) : d.toLocaleTimeString(),
            style: `left:${(((d.getTime() - t0) / (t1 - t0)) * 100).toFixed(2)}%`,
          }),
        );
      }
    }
  }

  /** Plant rows: heat pump, assist, condensation guard (cooling only). */
  _ovRenderPlant(items, mode) {
    const O = this._ovEls;
    const live = this._live || {};
    const hp = live.heat_pump || null;
    const line = (parts) => parts.filter((x) => x).join(" · ");
    const setChips = (K, chips) => {
      const sig = JSON.stringify(chips);
      if (K.extraSig !== sig) {
        K.extraSig = sig;
        K.extra.textContent = "";
        for (const [cls, text] of chips) {
          K.extra.appendChild(h("span", { class: cls, text }));
        }
      }
    };

    // Heat pump: the water setpoint for the mode (else the pump's mode), the
    // compressor, then the mode-sync and DHW chips.
    const PH = O.pHp;
    PH.el.style.display = hp ? "" : "none";
    if (hp) {
      const water = mode === "cooling" ? hp.cooling : hp.heating;
      const target = water ? num(water.target_c) : null;
      let note;
      if (target !== null) {
        PH.val.textContent = fmt(target, 1, " °C");
        PH.val.classList.remove("text");
        note = this._t(mode === "cooling" ? "hp_cool_target" : "hp_heat_target");
      } else {
        PH.val.textContent = hp.current_option || "—";
        PH.val.classList.add("text");
        note = this._t("hp_current_option");
      }
      const gate = hp.heating && hp.heating.gate ? hp.heating.gate : null;
      let freq = gate ? num(gate.compressor_freq_hz) : null;
      if (freq === null && hp.flicker) {
        freq = num(hp.flicker.compressor_freq_hz);
      }
      const comp =
        freq === null
          ? ""
          : `${this._t("hp_flicker_freq")}: ` +
            (freq > 0
              ? `${this._t("hp_gate_running")} ${fmt(freq, 0, " Hz")}`
              : this._t("hp_gate_idle"));
      PH.sub.textContent = line([note, comp]);
      // Mode sync + DHW exist only with the pump-mode select (as on the tab).
      const chips = [];
      if (hp.mode_entity_id) {
        if (hp.in_sync === true || hp.in_sync === false) {
          chips.push([
            hp.in_sync ? "chip chip-ok" : "chip chip-warn",
            `${this._t("hp_sec_mode")}: ` +
              this._t(hp.in_sync ? "hp_in_sync" : "hp_diverged"),
          ]);
        }
        chips.push([
          "chip",
          `${this._t("ov_dhw")} ${this._t(hp.dhw_active ? "tune_on" : "tune_off")}`,
        ]);
      }
      setChips(PH, chips);
    }

    // Assist: running sources out of all, by direction (mode colours).
    const PA = O.pAssist;
    const withSrc = items.filter((d) => d.r.fastKind !== "none");
    PA.el.style.display = withSrc.length ? "" : "none";
    if (withSrc.length) {
      const active = withSrc.filter(
        (d) => d.ctl && d.r.fastOn && d.r.fastMode !== "off",
      );
      PA.val.textContent = `${active.length}/${withSrc.length}`;
      const manual = withSrc.filter((d) => d.r.flags.includes("fast_source_manual")).length;
      PA.sub.textContent = line([
        this._t(active.length ? "ov_assist_note" : "ov_assist_idle"),
        manual ? fmtStr(this._t("ov_assist_manual"), { n: manual }) : "",
      ]);
      const counts = new Map();
      for (const d of active) {
        const cls = fastModeCls(d.r.fastMode);
        if (cls) {
          counts.set(cls, (counts.get(cls) || 0) + 1);
        }
      }
      const chips = [];
      for (const [cls, key] of [
        ["mode-heating", "assist_state_heating"],
        ["mode-cooling", "assist_state_cooling"],
        ["mode-dry", "assist_state_dry"],
      ]) {
        if (counts.get(cls)) {
          chips.push(["ov-mode " + cls, `${this._t(key)}: ${counts.get(cls)}`]);
        }
      }
      setChips(PA, chips);
    }

    // Condensation guard (cooling only): safe dew point, the highest room
    // dew point, the S2 counts.
    const PD = O.pDew;
    const cooling = mode === "cooling";
    PD.el.style.display = cooling ? "" : "none";
    if (cooling) {
      const dew = num(live.global_safe_dew_point_c);
      PD.val.textContent = fmt(dew, 1, " °C");
      let worst = null;
      for (const d of items) {
        if (d.ctl && d.r.dew !== null && (worst === null || d.r.dew > worst)) {
          worst = d.r.dew;
        }
      }
      // Counted, never named: the rooms are the Rooms tab's job.
      const lacking = this._view.filter((r) => r.dewReason).length;
      PD.sub.textContent = line([
        this._t("dew_cap"),
        dew === null
          ? lacking
            ? fmtStr(this._t("dew_none_collective"), { n: lacking })
            : ""
          : worst !== null
            ? fmtStr(this._t("ov_dew_worst"), { v: fmt(worst, 1, " °C") })
            : "",
      ]);
      const throttled = items.filter((d) => d.r.flags.includes("s2_throttle")).length;
      const chips = [];
      if (items.some((d) => d.r.flags.includes("s2_condensation"))) {
        chips.push([
          "chip chip-" + this._flagSev("s2_condensation"),
          this._flagLabel("s2_condensation"),
        ]);
      }
      if (throttled) {
        chips.push([
          "chip chip-warn",
          fmtStr(this._t("ov_dew_throttled"), { n: throttled }),
        ]);
      }
      setChips(PD, chips);
    }
    O.plant.style.display = hp || withSrc.length || cooling ? "" : "none";
  }

  /** Manifold rows: name, one bar per circuit filled to its opening, temps. */
  _ovRenderManifolds() {
    const O = this._ovEls;
    const views =
      this._live && Array.isArray(this._live.manifolds) ? this._live.manifolds : [];
    O.mfCard.style.display = views.length ? "" : "none";
    const circuitsOf = (v) => Math.max(1, Number(v.circuits) || 1);
    const sig = views.map((v) => `${v.id}:${circuitsOf(v)}`).join("|");
    if (sig !== O.mfSig) {
      O.mfSig = sig;
      O.mfList.textContent = "";
      O.mfRows = views.map((v) => {
        const row = {
          name: h("span", { class: "ov-mf-name" }),
          temps: h("span", { class: "ov-mf-temps" }),
          slots: [],
        };
        const slotsEl = h("div", { class: "ov-mf-slots" });
        for (let i = 0; i < circuitsOf(v); i += 1) {
          const fill = h("i");
          const slot = h("span", { class: "ov-mf-slot" }, [fill]);
          row.slots.push({ slot, fill });
          slotsEl.appendChild(slot);
        }
        O.mfList.appendChild(
          h("div", { class: "ov-mf-row" }, [row.name, slotsEl, row.temps]),
        );
        return row;
      });
    }
    views.forEach((v, idx) => {
      const row = O.mfRows[idx];
      const name = String(v.name || "");
      row.name.textContent = name;
      const main = v.main || {};
      const sVal = num((main.supply || {}).value);
      const rVal = num((main.return || {}).value);
      const parts = [];
      if (sVal !== null) {
        parts.push(`${this._t("mf_supply")} ${fmt(sVal, 1, " °C")}`);
      }
      if (rVal !== null) {
        parts.push(`${this._t("mf_return")} ${fmt(rVal, 1, " °C")}`);
      }
      if (sVal !== null && rVal !== null) {
        parts.push(`${this._t("mf_dt")} ${signed(sVal - rVal, 1, " K")}`);
      }
      // Compact "supply → return" on the row; the words and ΔT in the title.
      row.temps.textContent =
        sVal === null && rVal === null ? "" : `${fmt(sVal, 1)} → ${fmt(rVal, 1, "°")}`;
      row.temps.title = parts.join(" · ");
      const positions = Array.isArray(v.positions) ? v.positions : [];
      let open = 0;
      row.slots.forEach((sl, i) => {
        const pos = positions[i] || {};
        const assigned = pos.room_name != null;
        const pct = assigned ? num((pos.valve || {}).pct) : null;
        const flagged = assigned && Array.isArray(pos.flags) && pos.flags.length > 0;
        sl.slot.className =
          "ov-mf-slot" + (assigned ? "" : " free") + (flagged ? " flag" : "");
        sl.fill.style.height = (pct === null ? 0 : clamp(pct, 0, 100)) + "%";
        sl.slot.title =
          `${i + 1}. ` +
          (assigned
            ? `${pos.loop_label || pos.room_name} · ${fmt(pct, 0, " %")}`
            : this._t("mf_free"));
        if (pct !== null && pct >= MF_OPEN_THRESHOLD_PCT) {
          open += 1;
        }
      });
      row.name.title = `${name} · ${this._t("mf_open")} ${open}/${row.slots.length}`;
    });
  }

  /**
   * Fetch the 24 h chart batch — every room's `error_c` and
   * `recommended_valve` diagnostic sensors plus the outdoor sensor — as
   * 5-minute recorder statistics in ONE call, at most once per control cycle
   * and only while the Overview is visible. Fails soft: a missing or failed
   * series just draws no line.
   */
  _ovFetchHistory() {
    const S = this._ovHist;
    if (S.inflight || this._activeTab !== "overview" || !this._hass) {
      return;
    }
    const ids = [];
    for (const r of this._view) {
      const diag = r.diagnosticEntities || {};
      for (const key of ["error_c", "recommended_valve"]) {
        if (diag[key] && !ids.includes(diag[key])) {
          ids.push(diag[key]);
        }
      }
    }
    const outId = this._ovOutdoorId();
    if (outId && !ids.includes(outId)) {
      ids.push(outId);
    }
    const key = ids.join(",");
    if (key === S.key && Date.now() - S.at < OV_HIST_MAXAGE_MS) {
      return;
    }
    if (!ids.length) {
      S.key = key;
      S.at = Date.now();
      S.data = new Map();
      return;
    }
    S.inflight = true;
    const end = new Date();
    const start = new Date(end.getTime() - OV_HIST_HOURS * 3600 * 1000);
    this._callWSSilent({
      type: WS.statistics,
      start_time: start.toISOString(),
      end_time: end.toISOString(),
      statistic_ids: ids,
      period: OV_HIST_PERIOD,
      types: ["mean"],
    }).then((res) => {
      const data = new Map();
      for (const id of ids) {
        data.set(id, statPoints(res && res[id], OV_HIST_PERIOD_MS));
      }
      S.data = data;
      S.key = key;
      S.at = Date.now();
      S.inflight = false;
      if (this._activeTab === "overview") {
        this._renderOverview();
      }
    });
  }

  // --------------------------------------------------------------------------
  // Rooms tab: table
  // --------------------------------------------------------------------------

  /** Build the Rooms tab: an accessible table with a sticky header. */
  _buildRoomsSection() {
    const empty = h("div", { class: "empty", text: this._t("loading") });

    // Two-row header (A3): the first eight columns span both rows; the two
    // assist columns ("Tryb"/"Temp.") sit under a shared "Wspomaganie" group
    // header so an idle split / a room without assist reads in context.
    const topCells = [
      h("th", { class: "col-state", scope: "col", rowspan: "2" }, [
        h("span", { class: "sr-only", text: this._t("th_state") }),
      ]),
      h("th", {
        class: "col-room", scope: "col", rowspan: "2", text: this._t("th_room"),
      }),
      h("th", {
        class: "col-measured",
        scope: "col",
        rowspan: "2",
        text: this._t("th_measured"),
      }),
      h("th", {
        class: "col-setpoint",
        scope: "col",
        rowspan: "2",
        text: this._t("th_setpoint"),
      }),
      h("th", {
        class: "col-error", scope: "col", rowspan: "2", text: this._t("th_error"),
      }),
      h("th", {
        class: "col-valve", scope: "col", rowspan: "2", text: this._t("th_valve"),
      }),
      h("th", {
        class: "col-supply", scope: "col", rowspan: "2", text: this._t("th_supply"),
      }),
      h("th", {
        class: "col-return", scope: "col", rowspan: "2", text: this._t("th_return"),
      }),
      h("th", { class: "col-assist-group", scope: "colgroup", colspan: "2" }, [
        this._t("th_assist_group"),
        this._infoIcon("tip_th_assist"),
      ]),
    ];
    const subCells = [
      h("th", {
        class: "col-assist-mode",
        scope: "col",
        text: this._t("th_assist_mode"),
      }),
      h("th", { class: "col-assist-temp", scope: "col" }, [
        this._t("th_assist_temp"),
        this._infoIcon("tip_assist_target"),
      ]),
    ];
    const thead = h("thead", null, [
      h("tr", null, topCells),
      h("tr", { class: "subhead" }, subCells),
    ]);
    const tbody = h("tbody");
    const table = h("table", { class: "rooms-table" }, [thead, tbody]);
    const wrapEl = h("div", { class: "table-wrap" }, [table]);

    const el = h("section", {
      class: "tab-section",
      role: "tabpanel",
      dataset: { tab: "rooms" },
    });
    el.appendChild(empty);
    el.appendChild(wrapEl);
    return { el, tbody, empty, wrapEl };
  }

  /**
   * Build the "Flags" tab: the flag annunciator on its own tab (moved out of
   * the Rooms tab). `_buildFlagLegend` owns all the rendering and sets
   * `this._legend`; here we just host it in a tab section.
   */
  _buildFlagsSection() {
    const el = h("section", {
      class: "tab-section",
      role: "tabpanel",
      dataset: { tab: "flags" },
    });
    el.appendChild(this._buildFlagLegend().el);
    return el;
  }

  /**
   * Build the flag annunciator once, driven ENTIRELY by `FLAG_LABELS`. Styled
   * like the Tuning tab: a collapsible `<details>` whose body is a set of
   * grouped cards (`.tune-group` look), one flag PER ROW — dimmed when
   * inactive, lit (severity) with a status when a room raises it. Adding a flag
   * to the registry is enough; a live code missing from the registry is
   * materialised on the fly into the "other" group, so nothing is invisible.
   */
  _buildFlagLegend() {
    const L = { rows: {}, otherRows: {}, groupEls: {} };

    L.summary = h("span", { class: "flag-legend-summary" });
    // The summary line doubles as the collapse toggle (chevron via .sub-fold).
    const summary = h("summary", { class: "flag-legend-head" }, [
      h("span", { class: "flag-legend-title", text: this._t("flag_legend_title") }),
      this._infoIcon("flag_legend_info"),
      L.summary,
    ]);

    // Bucket the registry by group, preserving registry (logical) order.
    const buckets = {};
    for (const code of Object.keys(FLAG_LABELS)) {
      const g = this._flagGroup(code);
      (buckets[g] = buckets[g] || []).push(code);
    }
    const groups = [
      ...FLAG_GROUP_ORDER,
      ...Object.keys(buckets).filter((g) => !FLAG_GROUP_ORDER.includes(g)),
    ];
    if (!groups.includes(FLAG_GROUP_FALLBACK)) {
      groups.push(FLAG_GROUP_FALLBACK);
    }

    const groupsWrap = h("div", { class: "flag-legend-groups" });
    for (const g of groups) {
      const rowsEl = h("div", { class: "flag-rows" });
      for (const code of buckets[g] || []) {
        const row = this._buildFlagRow(code);
        L.rows[code] = row;
        rowsEl.appendChild(row.el);
      }
      const grpEl = h("div", { class: "flag-grp", dataset: { group: g } }, [
        h("div", {
          class: "flag-grp-title",
          text: this._t("flag_grp_" + g) || g,
        }),
        rowsEl,
      ]);
      // The "other" group holds only runtime-materialised rows; hide it until
      // an unknown flag actually fires.
      if (g === FLAG_GROUP_FALLBACK && !(buckets[g] || []).length) {
        grpEl.style.display = "none";
      }
      L.groupEls[g] = { el: grpEl, rowsEl };
      groupsWrap.appendChild(grpEl);
    }

    const details = h(
      "details",
      { class: "flag-legend sub-fold", "aria-label": this._t("flag_legend_title") },
      [summary, groupsWrap],
    );
    details.open = this._flagLegendOpen;
    details.addEventListener("toggle", () => {
      this._flagLegendOpen = details.open;
    });
    L.el = details;
    this._legend = L;
    return L;
  }

  /** A single annunciator ROW (works for registry AND unknown codes). */
  _buildFlagRow(code) {
    const sx = this._flagSx(code);
    const head = [];
    if (sx) {
      head.push(h("span", { class: "flag-sx", text: sx }));
    }
    head.push(h("span", { class: "flag-row-label", text: this._flagLabel(code) }));
    // Live getter so the bubble tracks the current language.
    head.push(this._infoBtn(() => this._flagDesc(code), this._t("tooltip_show")));
    const statusEl = h("span", { class: "flag-row-status" });
    const el = h("div", { class: "flag-row is-off", dataset: { code } }, [
      h("div", { class: "flag-row-head" }, head),
      statusEl,
    ]);
    return { el, statusEl };
  }

  /** Light up the rows for flags active in any room; dim the rest. */
  _updateFlagLegend() {
    const L = this._legend;
    if (!L) {
      return;
    }
    // code -> [room names], in stable (sorted) view order.
    const active = new Map();
    for (const r of this._view) {
      for (const code of r.flags) {
        if (!active.has(code)) {
          active.set(code, []);
        }
        active.get(code).push(r.name);
      }
    }

    const applyRow = (code, row) => {
      const rooms = active.get(code);
      const on = !!rooms;
      row.el.classList.toggle("is-on", on);
      row.el.classList.toggle("is-off", !on);
      row.el.classList.remove(
        "sev-ok",
        "sev-info",
        "sev-warn",
        "sev-problem",
        "sev-alarm",
      );
      row.statusEl.textContent = "";
      if (on) {
        row.el.classList.add("sev-" + this._flagSev(code));
        const label =
          this._t("flag_active") + (rooms.length > 1 ? " ×" + rooms.length : "");
        row.statusEl.appendChild(h("span", { class: "flag-dot" }));
        row.statusEl.appendChild(h("span", { text: label }));
        row.el.title = this._t("flag_active_in") + rooms.join(", ");
      } else {
        row.statusEl.textContent = "—";
        row.el.title = "";
      }
    };

    for (const code of Object.keys(L.rows)) {
      applyRow(code, L.rows[code]);
    }

    // Unknown (unregistered) active codes → materialise/refresh in "other".
    const otherGroup = L.groupEls[FLAG_GROUP_FALLBACK];
    let otherCount = 0;
    for (const [code, rooms] of active) {
      if (code in FLAG_LABELS) {
        continue;
      }
      let row = L.otherRows[code];
      if (!row) {
        row = this._buildFlagRow(code);
        L.otherRows[code] = row;
        otherGroup.rowsEl.appendChild(row.el);
      }
      applyRow(code, row);
      otherCount += 1;
      void rooms;
    }
    // Drop stale unknown rows that no longer fire (keep the group tidy).
    for (const code of Object.keys(L.otherRows)) {
      if (!active.has(code)) {
        L.otherRows[code].el.remove();
        delete L.otherRows[code];
      }
    }
    if (otherGroup) {
      otherGroup.el.style.display = otherCount ? "" : "none";
    }

    // Head summary: worst severity + active count (mirrors the hero metric).
    const activeCount = active.size;
    let rank = 0;
    for (const code of active.keys()) {
      rank = Math.max(rank, SEV_RANK[this._flagSev(code)] || 0);
    }
    L.summary.textContent = activeCount ? String(activeCount) : this._t("flag_none_active");
    L.summary.className = "flag-legend-summary" + (activeCount ? " sev-" + this._sevName(rank) : "");
  }

  _reconcileTable() {
    const tbody = this._els.tbody;
    const rows = this._view;

    if (rows.length === 0) {
      for (const [, el] of this._rows) {
        el.remove();
      }
      this._rows.clear();
      const loading = !this._config && !this._live;
      this._els.empty.textContent = loading ? this._t("loading") : this._t("no_rooms");
      this._els.empty.style.display = "";
      this._els.tableWrap.style.display = "none";
      return;
    }
    this._els.empty.style.display = "none";
    this._els.tableWrap.style.display = "";

    const desired = new Set(rows.map((r) => r.name));
    for (const [name, el] of [...this._rows]) {
      if (!desired.has(name)) {
        el.remove();
        this._rows.delete(name);
      }
    }

    rows.forEach((r, i) => {
      let tr = this._rows.get(r.name);
      if (!tr) {
        tr = this._buildRow(r.name);
        this._rows.set(r.name, tr);
      }
      const at = tbody.children[i];
      if (at !== tr) {
        tbody.insertBefore(tr, at || null);
      }
      this._updateRow(tr, r);
    });
  }

  _buildRow(name) {
    const p = {};

    // Column 1: two-state control segment (Off / Live).
    p.stateBtns = {};
    const seg = h("div", { class: "seg-state", role: "group" });
    for (const meta of STATE_META) {
      const btn = h(
        "button",
        {
          class: "seg-state-btn",
          type: "button",
          title: this._t(meta.key),
          "aria-label": this._t(meta.key),
          dataset: { state: meta.state },
          on: {
            click: (e) => {
              e.stopPropagation();
              this._setRoomState(name, meta.state, btn);
            },
          },
        },
        [this._icon(meta.icon, meta.glyph)],
      );
      p.stateBtns[meta.state] = btn;
      seg.appendChild(btn);
    }
    const stateCell = h("td", { class: "col-state" }, [seg]);

    // Column 2: severity dot + name.
    p.dot = h("span", { class: "dot" });
    p.name = h("span", { class: "card-name", text: name });
    const nameCell = h("td", { class: "col-room" }, [
      h("div", { class: "name-cell" }, [p.dot, p.name]),
    ]);

    // Column 3: measured room temperature + trend arrow.
    p.temp = h("span", { class: "meas-val" });
    p.trend = h("span", { class: "trend" });
    const measCell = h("td", { class: "col-measured" }, [
      h("div", { class: "meas-cell" }, [p.temp, p.trend]),
    ]);

    // Column 4: setpoint + offset annotation.
    p.spVal = h("span", { class: "sp-val" });
    p.spOffset = h("span", { class: "cell-sub" });
    const spCell = h("td", { class: "col-setpoint" }, [
      h("div", { class: "sp-cell" }, [p.spVal, p.spOffset]),
    ]);

    // Column 5: error = setpoint - measured (signed, computed in the panel).
    p.err = h("span", { class: "err-val" });
    const errCell = h("td", { class: "col-error" }, [p.err]);

    // Column 6: valve percent (no bar here — the per-loop bars are on the
    // Manifolds tab).
    p.valveVal = h("span", { class: "valve-val" });
    const valveCell = h("td", { class: "col-valve" }, [p.valveVal]);

    // Columns 7-8: loop supply / return temperature, read live from hass.states
    // (first loop when a room has several; the Manifolds tab shows every loop).
    p.supplyVal = h("span", { class: "meas-val" });
    const supplyCell = h("td", { class: "col-supply" }, [p.supplyVal]);
    p.returnVal = h("span", { class: "meas-val" });
    const returnCell = h("td", { class: "col-return" }, [p.returnVal]);

    // Columns 9-10: fast-source (assist) mode + target temperature.
    p.assistMode = h("span", { class: "fast-badge" });
    const assistModeCell = h("td", { class: "col-assist-mode" }, [p.assistMode]);
    p.assistTemp = h("span", { class: "meas-val" });
    const assistTempCell = h("td", { class: "col-assist-temp" }, [p.assistTemp]);

    const tr = h(
      "tr",
      {
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
      [
        stateCell,
        nameCell,
        measCell,
        spCell,
        errCell,
        valveCell,
        supplyCell,
        returnCell,
        assistModeCell,
        assistTempCell,
      ],
    );
    tr._parts = p;
    return tr;
  }

  _updateRow(tr, r) {
    const p = tr._parts;

    tr.classList.toggle("selected", r.name === this._selectedRoom);
    tr.classList.toggle("off", r.state === STATE_OFF);
    p.dot.className = "dot sev-" + r.severity;

    // Two-state segment: highlight the active state's button.
    for (const meta of STATE_META) {
      p.stateBtns[meta.state].classList.toggle("active", r.state === meta.state);
    }

    // Measured — the echoed room temperature, never setpoint - error.
    p.temp.textContent = fmt(r.current, 1, "°");
    p.trend.textContent = trendArrow(r.trend) + " " + fmt(r.trend, 1, " K/h");

    // Setpoint + offset annotation (shown only when the offset is non-zero).
    p.spVal.textContent = fmt(r.setpoint, 1, "°");
    if (r.offset) {
      p.spOffset.textContent = fmtStr(this._t("card_offset"), {
        v: signed(r.offset, 1),
      });
      p.spOffset.style.display = "";
    } else {
      p.spOffset.textContent = "";
      p.spOffset.style.display = "none";
    }

    // Error = setpoint - measured (signed K), consistent by construction with
    // the two adjacent columns.
    const err =
      r.setpoint !== null && r.current !== null ? r.setpoint - r.current : null;
    p.err.textContent = signed(err, 1, " K");

    // Valve percent (value only — the per-loop bars are on the Manifolds tab).
    p.valveVal.textContent = fmt(r.valve, 0, "%");

    // Loop supply / return, read live from hass.states (first loop when a
    // room has several — the full per-loop breakdown is on the Manifolds tab).
    const primaryLoop = this._roomLoops(r)[0] || null;
    p.supplyVal.textContent = fmt(
      primaryLoop ? this._stateNum(primaryLoop.supplyId) : null,
      1,
      "°",
    );
    p.returnVal.textContent = fmt(
      primaryLoop ? this._stateNum(primaryLoop.returnId) : null,
      1,
      "°",
    );

    // Assist mode + target temperature (grouped under "Wspomaganie", A3).
    // A room without a fast source reads as muted em dashes with a plain
    // "no assist" title; an idle split keeps its "off" badge.
    const assistMode = this._assistModeLabel(r);
    p.assistMode.className = "fast-badge " + assistMode.cls;
    p.assistMode.textContent = assistMode.text;
    const noSource = r.fastKind === "none";
    const noSourceTitle = noSource ? this._t("assist_no_source") : "";
    p.assistMode.title = noSourceTitle;
    const assistActive = r.fastKind !== "none" && r.fastOn && r.fastMode !== "off";
    p.assistTemp.textContent = assistActive ? fmt(r.fastTarget, 1, "°") : "—";
    p.assistTemp.classList.toggle("muted", !assistActive);
    p.assistTemp.title = noSourceTitle;
  }

  _chip(code) {
    return h("span", {
      class: "chip chip-" + this._flagSev(code),
      text: this._flagLabel(code),
      title: this._flagDesc(code),
    });
  }

  // --------------------------------------------------------------------------
  // Rooms tab: detail drawer (wiring / decision)
  // --------------------------------------------------------------------------

  /** True when the detail renders as a fixed overlay drawer (narrow viewport). */
  _isOverlay() {
    return !!(
      window.matchMedia &&
      window.matchMedia(`(max-width: ${OVERLAY_MAX_PX}px)`).matches
    );
  }

  _select(name) {
    this._selectedRoom = name;
    this._render();
    // Side-by-side layout: keep the sticky column in view. The overlay drawer
    // is fixed, so scrolling would only jump the page behind it.
    if (!this._isOverlay() && this._els.detail && this._els.detail.scrollIntoView) {
      this._els.detail.scrollIntoView({ block: "nearest" });
    }
  }

  _deselect() {
    const prev = this._selectedRoom;
    const detail = this._els && this._els.detail;
    const active = this.shadowRoot ? this.shadowRoot.activeElement : null;
    const hadFocus = !!(detail && active && detail.contains(active));
    this._selectedRoom = null;
    this._render();
    // Hand focus back to the room's table row (or its Overview strip dot)
    // when the close came from inside the inspector (keyboard flow); an
    // outside click keeps its own focus.
    if (hadFocus && prev) {
      // On the Overview a room without a dot hands focus to the tab itself
      // (the Rooms table is hidden there).
      const row =
        this._activeTab === "overview" && this._ovEls
          ? this._ovEls.dots.get(prev) || (this._tabs && this._tabs.btns.overview)
          : this._rows.get(prev);
      if (row && row.isConnected) {
        row.focus();
      }
    }
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

    const fresh = this._detailRoom !== this._selectedRoom;
    if (fresh) {
      this._buildDetail(this._selectedRoom);
      this._detailRoom = this._selectedRoom;
    }
    const r = this._viewByName.get(this._selectedRoom);
    this._updateDetail(r);
    this._refreshWiringStates();
    this._refreshHistory();
    this._updateLegendValues();
    // Overlay drawer: move focus into the (role=dialog) inspector on open so
    // Escape and screen readers land in the right place — never on a poll.
    if (fresh && this._isOverlay()) {
      detail.focus({ preventScroll: true });
    }
  }

  _buildDetail(name) {
    const D = { rows: [] };
    // Publish the new refs before building sections: `_buildHistory` kicks off
    // an async fetch that resolves against `this._detail.history`.
    this._detail = D;
    const detail = this._els.detail;
    detail.textContent = "";
    detail.setAttribute("aria-label", name);

    // Sticky header: severity dot + room name + close.
    D.headDot = h("span", { class: "dot" });
    D.title = h("span", { class: "detail-title", text: name });
    const close = h(
      "button",
      {
        class: "ghost-btn detail-close",
        type: "button",
        title: this._t("detail_close"),
        "aria-label": this._t("detail_close"),
        on: { click: () => this._deselect() },
      },
      [this._icon("mdi:close", "✕")],
    );
    const header = h("div", { class: "detail-head" }, [
      D.headDot,
      D.title,
      h("span", { class: "spacer" }),
      close,
    ]);

    // Active flags surface first — they explain everything below them.
    D.flagsEl = h("div", { class: "chips" });
    D.flagsBlock = h("div", { class: "detail-flags", style: "display:none" }, [
      this._infoIcon("tip_dec_flags"),
      D.flagsEl,
    ]);

    // Key numbers as a 2×2 tile grid: measured (+trend), setpoint (the offset
    // stepper lives here), valve (+bar) and the fast-source command.
    D.statTempVal = h("span", { class: "stat-val" });
    D.statTempTrend = h("span", { class: "stat-sub" });
    D.spMinus = h("button", {
      class: "step-btn",
      type: "button",
      text: "−",
      on: { click: () => this._nudgeOffset(name, -OFFSET_STEP) },
    });
    D.spVal = h("span", { class: "step-val" });
    D.spPlus = h("button", {
      class: "step-btn",
      type: "button",
      text: "+",
      on: { click: () => this._nudgeOffset(name, OFFSET_STEP) },
    });
    D.spOffset = h("span", { class: "stat-sub" });
    D.statValveVal = h("span", { class: "stat-val" });
    D.statValveFill = h("span", { class: "valve-fill" });
    D.statAssist = h("span", { class: "fast-badge stat-badge" });
    D.statAssistSub = h("span", { class: "stat-sub", style: "display:none" });
    const tiles = h("div", { class: "stat-tiles" }, [
      h("div", { class: "stat-tile" }, [
        h("span", { class: "stat-cap" }, [
          this._t("th_measured"),
          this._infoIcon("tip_dec_trend"),
        ]),
        D.statTempVal,
        D.statTempTrend,
      ]),
      h("div", { class: "stat-tile" }, [
        h("span", { class: "stat-cap", text: this._t("card_setpoint") }),
        h("div", { class: "stepper" }, [D.spMinus, D.spVal, D.spPlus]),
        D.spOffset,
      ]),
      h("div", { class: "stat-tile" }, [
        h("span", { class: "stat-cap", text: this._t("th_valve") }),
        D.statValveVal,
        h("div", { class: "valve-track stat-track" }, [D.statValveFill]),
      ]),
      h("div", { class: "stat-tile" }, [
        h("span", { class: "stat-cap" }, [
          this._t("dec_fast"),
          this._infoIcon("tip_dec_fast"),
        ]),
        D.statAssist,
        D.statAssistSub,
      ]),
    ]);

    // Section: controller decision.
    const dec = this._buildDecision(D);

    // Section: history charts, mounted in `.history-mount[data-room]`.
    const historyMount = h("div", {
      class: "history-mount",
      dataset: { room: name },
    });
    this._buildHistory(D, name, historyMount);
    const history = this._section(this._t("sec_history"), historyMount);

    // Section: sensors & signals (A6) — reference material, foldable, at the
    // bottom; the disclosure state persists across room switches
    // (instance-level). The current VALUE of each signal is the headline;
    // the source entity id hides behind an "i" icon.
    D.wiringBody = h("div", { class: "wire-list" });
    const wiring = h(
      "details",
      {
        class: "sub sub-fold",
        open: this._wiringOpen ? true : null,
        on: {
          toggle: (e) => {
            this._wiringOpen = e.target.open;
          },
        },
      },
      [
        h("summary", { class: "sub-title", text: this._t("sec_wiring") }),
        D.wiringBody,
      ],
    );
    // Section: diagnostic entities — an OWN fold, collapsed by default (A5),
    // no longer inheriting the signals section's disclosure state.
    D.diagBody = h("div", { class: "wire-list" });
    D.diagFold = h(
      "details",
      {
        class: "sub sub-fold",
        open: this._diagOpen ? true : null,
        style: "display:none",
        on: {
          toggle: (e) => {
            this._diagOpen = e.target.open;
          },
        },
      },
      [
        h("summary", { class: "sub-title", text: this._t("sec_diagnostics") }),
        D.diagBody,
      ],
    );
    this._buildWiring(D, name);

    const body = h("div", { class: "detail-body" }, [
      D.flagsBlock,
      tiles,
      dec,
      history,
      wiring,
      D.diagFold,
    ]);
    detail.appendChild(header);
    detail.appendChild(body);
  }

  _section(title, body) {
    return h("section", { class: "sub" }, [
      h("div", { class: "sub-title", text: title }),
      body,
    ]);
  }

  /**
   * One key-value cell (small muted caption over a strong value) in a grid.
   *
   * An optional `tipKey` appends a shared-"i" info button to the caption.
   */
  _kv(parent, label, tipKey) {
    const valEl = h("span", { class: "kv-val" });
    const rowEl = h("div", { class: "kv" }, [
      h("span", { class: "kv-cap" }, [label, tipKey ? this._infoIcon(tipKey) : null]),
      valEl,
    ]);
    parent.appendChild(rowEl);
    return { rowEl, valEl };
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

    // Per-room diagnostic entities live in their OWN collapsed fold (A5).
    const diag = room.diagnosticEntities;
    if (diag && Object.keys(diag).length) {
      D.diagFold.style.display = "";
      for (const [key, value] of Object.entries(diag)) {
        const ids = Array.isArray(value) ? value.filter(Boolean) : value ? [value] : [];
        const roleKey = "role_" + key;
        const label = roleKey in STR.en ? this._t(roleKey) : key;
        for (const id of ids) {
          this._addWireRow(D, label, id, D.diagBody);
        }
      }
    }
  }

  /**
   * One signal row (A6): role caption, the CURRENT VALUE as the headline,
   * the unavailable badge, an "i" icon revealing the source entity id, and
   * the more-info affordance (the whole row opens the native dialog).
   */
  _addWireRow(D, label, id, body) {
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
        stateEl,
        badgeEl,
        this._infoIconText(id, "wire_source_tip"),
        this._icon("mdi:open-in-new", "›"),
      ],
    );
    D.rows.push({ entityId: id, stateEl, badgeEl, rowEl });
    (body || D.wiringBody).appendChild(rowEl);
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
        row.stateEl.textContent = fmtState(val) + (unit && !bad ? " " + unit : "");
      }
      row.badgeEl.style.display = bad ? "" : "none";
      row.rowEl.classList.toggle("wire-bad", bad);
    }
  }

  _buildDecision(D) {
    const body = h("div", { class: "dec" });

    // Controller inputs (a 2-column key-value grid; the dew-exclusion cell is
    // hidden entirely while there is no reason to show).
    const gridIn = h("div", { class: "kv-grid" });
    D.errorEl = this._kv(gridIn, this._t("dec_error"), "tip_dec_error").valEl;
    D.dewEl = this._kv(gridIn, this._t("dec_dew"), "tip_dec_dew").valEl;
    const dewReason = this._kv(gridIn, this._t("dec_dew_reason"));
    D.dewReasonEl = dewReason.valEl;
    D.dewReasonRow = dewReason.rowEl;
    body.appendChild(gridIn);

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
        h("div", { class: "dec-block-cap" }, [
          this._t("dec_terms"),
          this._infoIcon("tip_dec_terms"),
        ]),
        termsWrap,
      ]),
    );

    // Outputs and limiter state (the throttle cell hides while not cooling).
    const gridOut = h("div", { class: "kv-grid" });
    D.rawValveEl = this._kv(
      gridOut, this._t("dec_raw_valve"), "tip_dec_raw_valve",
    ).valEl;
    D.finalValveEl = this._kv(
      gridOut, this._t("dec_final_valve"), "tip_dec_final_valve",
    ).valEl;
    const throttle = this._kv(gridOut, this._t("dec_throttle"), "tip_dec_throttle");
    D.throttleEl = throttle.valEl;
    D.throttleRow = throttle.rowEl;
    D.integratorEl = this._kv(
      gridOut, this._t("dec_integrator"), "tip_dec_integrator",
    ).valEl;
    D.saturatedEl = this._kv(
      gridOut, this._t("dec_saturated"), "tip_dec_saturated",
    ).valEl;
    D.floorEl = this._kv(gridOut, this._t("dec_floor"), "tip_dec_floor").valEl;
    body.appendChild(gridOut);

    D.explanationEl = h("div", { class: "explanation" });
    body.appendChild(
      h("div", { class: "dec-block" }, [
        h("div", { class: "dec-block-cap", text: this._t("dec_explanation") }),
        D.explanationEl,
      ]),
    );

    // Raw report JSON in a collapsed <details> (open state survives polls).
    D.rawPre = h("pre", { class: "raw-pre" });
    const details = h("details", { class: "raw" }, [
      h("summary", { text: this._t("raw_show") }),
      D.rawPre,
    ]);
    body.appendChild(details);

    return this._section(this._t("sec_decision"), body);
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
    D.headDot.className = "dot sev-" + r.severity;

    // Stat tiles: measured (+trend), setpoint stepper (+offset), valve, assist.
    D.statTempVal.textContent = fmt(r.current, 1, "°");
    D.statTempTrend.textContent =
      trendArrow(r.trend) + " " + fmt(r.trend, 2, " K/h");
    D.spVal.textContent = fmt(r.setpoint, 1, " °C");
    if (r.offset) {
      D.spOffset.textContent = fmtStr(this._t("card_offset"), {
        v: signed(r.offset, 1),
      });
      D.spOffset.style.display = "";
    } else {
      D.spOffset.textContent = "";
      D.spOffset.style.display = "none";
    }
    D.spMinus.disabled = r.offset <= OFFSET_MIN;
    D.spPlus.disabled = r.offset >= OFFSET_MAX;
    D.statValveVal.textContent = fmt(r.valve, 0, "%");
    D.statValveFill.style.width =
      (r.valve === null ? 0 : clamp(r.valve, 0, 100)) + "%";
    const assist = this._assistLabel(r);
    D.statAssist.className = "fast-badge stat-badge " + assist.cls;
    D.statAssist.textContent = assist.text;
    // Quiet-hours sub-line (B1): the allowed window, when one is configured.
    if (r.fastKind !== "none" && r.fastWindowStart && r.fastWindowEnd) {
      D.statAssistSub.textContent = fmtStr(this._t("assist_window_sub"), {
        start: r.fastWindowStart,
        end: r.fastWindowEnd,
      });
      D.statAssistSub.style.display = "";
    } else {
      D.statAssistSub.textContent = "";
      D.statAssistSub.style.display = "none";
    }

    D.errorEl.textContent = fmt(r.errorC, 2, " K");
    D.dewEl.textContent = fmt(r.dew, 1, " °C");
    D.dewReasonEl.textContent = r.dewReason ? this._dewReasonText(r.dewReason) : "—";
    D.dewReasonRow.style.display = r.dewReason ? "" : "none";

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
    D.throttleRow.style.display = r.throttle === null ? "none" : "";
    D.integratorEl.textContent = r.integratorFrozen
      ? this._t("integ_frozen")
      : this._t("integ_active");
    D.integratorEl.classList.toggle("warn", r.integratorFrozen);
    D.saturatedEl.textContent = r.saturated ? this._t("yes") : this._t("no");
    D.saturatedEl.classList.toggle("warn", r.saturated);
    D.floorEl.textContent = r.valveFloor ? this._t("yes") : this._t("no");
    D.floorEl.classList.toggle("warn", r.valveFloor);

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

  // --------------------------------------------------------------------------
  // Assist tab
  // --------------------------------------------------------------------------

  /** Build the Assist section skeleton (rows populated by `_renderAssist`). */
  _buildAssistSection() {
    const empty = h("div", { class: "empty", text: this._t("loading") });
    // The Group column (K9) is hidden until any room actually carries a
    // multisplit group; `_renderAssist` toggles it together with the cells.
    const groupTh = h("th", { scope: "col", style: "display:none" }, [
      this._t("ast_th_group"),
      this._infoIcon("tip_ast_group"),
    ]);
    const headCells = [
      h("th", { scope: "col", text: this._t("th_room") }),
      h("th", { scope: "col", text: this._t("ast_th_kind") }),
      groupTh,
      h("th", { scope: "col" }, [
        this._t("ast_th_command"),
        this._infoIcon("tip_assist_target"),
      ]),
      h("th", { scope: "col", text: this._t("ast_th_actual") }),
      h("th", { scope: "col" }, [
        this._t("ast_th_timer"),
        this._infoIcon("tip_ast_timer"),
      ]),
      h("th", { scope: "col" }, [
        this._t("ast_th_hours"),
        this._infoIcon("tip_ast_hours"),
      ]),
      h("th", { scope: "col", text: this._t("ast_th_flags") }),
      h("th", { scope: "col", text: this._t("ast_th_entity") }),
    ];
    const thead = h("thead", null, [h("tr", null, headCells)]);
    const tbody = h("tbody");
    const table = h("table", { class: "assist-table" }, [thead, tbody]);
    const wrapEl = h("div", { class: "table-wrap" }, [table]);
    const noneLine = h("div", { class: "assist-none muted", style: "display:none" });
    const el = h(
      "section",
      { class: "tab-section", role: "tabpanel", style: "display:none", dataset: { tab: "assist" } },
      [empty, wrapEl, noneLine],
    );
    this._assistEls = {
      el,
      tbody,
      empty,
      wrapEl,
      noneLine,
      groupTh,
      showGroup: false,
      rows: new Map(),
    };
    return el;
  }

  /** Reconcile the Assist table from the current view + live HA states. */
  _renderAssist() {
    const E = this._assistEls;
    if (!E) {
      return;
    }
    const withAssist = this._view.filter((r) => r.fastKind !== "none");
    const without = this._view.filter((r) => r.fastKind === "none");

    // "No assist" summary line (rooms without a fast source).
    if (without.length) {
      E.noneLine.textContent = fmtStr(this._t("ast_none_line"), {
        rooms: without.map((r) => r.name).join(", "),
      });
      E.noneLine.style.display = "";
    } else {
      E.noneLine.style.display = "none";
    }

    if (!withAssist.length) {
      for (const [, entry] of E.rows) {
        entry.tr.remove();
      }
      E.rows.clear();
      const loading = !this._config && !this._live;
      E.empty.textContent = loading ? this._t("loading") : this._t("ast_empty");
      E.empty.style.display = "";
      E.wrapEl.style.display = "none";
      return;
    }
    E.empty.style.display = "none";
    E.wrapEl.style.display = "";

    // Group column (K9): shown only when ANY room carries a group.
    E.showGroup = withAssist.some((r) => r.fastGroup);
    E.groupTh.style.display = E.showGroup ? "" : "none";

    const desired = new Set(withAssist.map((r) => r.name));
    for (const [name, entry] of [...E.rows]) {
      if (!desired.has(name)) {
        entry.tr.remove();
        E.rows.delete(name);
      }
    }
    for (const r of withAssist) {
      let entry = E.rows.get(r.name);
      if (!entry) {
        entry = this._buildAssistRow(r);
        E.rows.set(r.name, entry);
      }
      E.tbody.appendChild(entry.tr);
      this._updateAssistRow(entry, r);
    }
  }

  _buildAssistRow(r) {
    const p = {};
    p.name = h("span", { class: "card-name", text: r.name });
    const nameCell = h("td", { class: "col-room" }, [p.name]);

    p.kind = h("span");
    const kindCell = h("td", null, [p.kind]);

    p.group = h("span", { class: "mono" });
    p.groupCell = h("td", null, [p.group]);

    p.command = h("span", { class: "fast-badge" });
    const cmdCell = h("td", null, [p.command]);

    p.actual = h("span", { class: "mono" });
    const actualCell = h("td", null, [p.actual]);

    p.timer = h("span");
    const timerCell = h("td", null, [p.timer]);

    p.hours = h("span");
    const hoursCell = h("td", null, [p.hours]);

    p.flags = h("div", { class: "chips" });
    const flagsCell = h("td", null, [p.flags]);

    p.link = h(
      "button",
      {
        class: "loop-link",
        type: "button",
        on: {
          click: (e) => {
            e.stopPropagation();
            this._moreInfo(p.entityId);
          },
        },
      },
      [this._icon("mdi:open-in-new", "›")],
    );
    p.tune = h("button", {
      class: "link-btn",
      type: "button",
      text: this._t("ast_tune_link"),
      on: {
        click: (e) => {
          e.stopPropagation();
          this._tuneRoom(r.name);
        },
      },
    });
    const entityCell = h("td", null, [
      h("div", { class: "assist-actions" }, [p.link, p.tune]),
    ]);

    const tr = h(
      "tr",
      {
        class: "assist-row",
        tabindex: "0",
        dataset: { room: r.name },
        on: {
          click: (e) => {
            if (e.target.closest("button")) {
              return;
            }
            this._select(r.name);
          },
          keydown: (e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              this._select(r.name);
            }
          },
        },
      },
      [
        nameCell,
        kindCell,
        p.groupCell,
        cmdCell,
        actualCell,
        timerCell,
        hoursCell,
        flagsCell,
        entityCell,
      ],
    );
    return { tr, parts: p };
  }

  _updateAssistRow(entry, r) {
    const p = entry.parts;
    entry.tr.classList.toggle("selected", r.name === this._selectedRoom);

    // Kind (§8 labels).
    p.kind.textContent =
      r.fastKind === "heater" ? this._t("kind_heater") : this._t("kind_split");

    // Multisplit group (K9): column rendered only when any room has one.
    const E = this._assistEls;
    p.groupCell.style.display = E && E.showGroup ? "" : "none";
    p.group.textContent = r.fastGroup || "—";
    p.group.className = r.fastGroup ? "mono" : "muted";

    // Command badge (Off / heating → target / cooling → target).
    const assist = this._assistLabel(r);
    p.command.className = "fast-badge " + assist.cls;
    p.command.textContent = assist.text;

    // Actual state from the climate entity (hvac_action, else state).
    const entityId = this._entityList((r.entities || {}).entity_fast_source)[0] || null;
    p.entityId = entityId;
    const st = this._hassState(entityId);
    let actualText = this._t("ast_actual_unknown");
    let action = null;
    if (st) {
      const attrs = st.attributes || {};
      action = attrs.hvac_action ? String(attrs.hvac_action) : String(st.state);
      actualText = action;
    }
    p.actual.textContent = actualText;
    // Colour the real running direction like the command badges do.
    p.actual.className = ("mono " + fastModeCls(action)).trim();
    // Divergence: commanded ON but idle/off, or commanded OFF but running.
    const cmdOn = r.fastOn && r.fastMode !== "off";
    const acting = action === "heating" || action === "cooling";
    const idleish = action === "off" || action === "idle";
    const mismatch = st && ((cmdOn && idleish) || (!cmdOn && acting));
    p.actual.classList.toggle("mismatch", !!mismatch);

    // Timer: remaining dwell → "unlocks in ~N min"; else a group-conflict
    // chip (K9 — the arbitration loser's dwell is cleared by the force-off,
    // so a bare "—" left the OFF unexplained); else min-runtime lock; else —.
    if (r.fastDwell !== null) {
      p.timer.className = "";
      p.timer.textContent = fmtStr(this._t("ast_timer_unlock"), {
        n: Math.max(1, Math.ceil(r.fastDwell / 60)),
      });
    } else if (r.flags.includes("fast_source_group_conflict")) {
      p.timer.className = "chip chip-warn";
      p.timer.textContent = this._t("ast_timer_conflict");
    } else if (r.flags.includes("fast_source_min_runtime")) {
      p.timer.className = "chip chip-warn";
      p.timer.textContent = this._t("ast_timer_locked");
    } else {
      p.timer.className = "muted";
      p.timer.textContent = "—";
    }

    // Allowed hours (B1): the configured window, or "always" when free.
    if (r.fastWindowStart && r.fastWindowEnd) {
      p.hours.className = "mono";
      p.hours.textContent = r.fastWindowStart + "–" + r.fastWindowEnd;
    } else {
      p.hours.className = "muted";
      p.hours.textContent = this._t("ast_hours_always");
    }

    // Flags: the assist-relevant subset (K9 extended it with the group
    // conflict and the physical-state mismatch; B1 added the quiet hours;
    // §28 the manual hold).
    p.flags.textContent = "";
    for (const f of [
      "fast_source_min_runtime",
      "fast_source_quiet_hours",
      "fast_source_cannot_cool",
      "fast_source_group_conflict",
      "fast_source_mismatch",
      "fast_source_manual",
      "dry_assist",
      "dry_unsupported",
    ]) {
      if (r.flags.includes(f)) {
        p.flags.appendChild(this._chip(f));
      }
    }

    // Entity link visibility.
    p.link.style.display = entityId ? "" : "none";
    p.link.title = entityId || "";
  }

  // --------------------------------------------------------------------------
  // Heat-pump tab (B2, 2026-07-12)
  // --------------------------------------------------------------------------

  /** Build the Heat-pump section skeleton (values updated by `_renderHp`). */
  _buildHpSection() {
    const P = {};
    P.empty = h("div", { class: "empty", text: this._t("hp_empty") });

    // Card: operating mode (Tortoise vs pump; sync badge; DHW-only note).
    P.tortoiseMode = h("span", { class: "kv-val" });
    P.currentOpt = h("span", { class: "kv-val" });
    P.desiredOpt = h("span", { class: "kv-val" });
    P.syncBadge = h("span", { class: "chip", style: "display:none" });
    const modeGrid = h("div", { class: "kv-grid hp-grid" }, [
      h("div", { class: "kv" }, [
        h("span", { class: "kv-cap", text: this._t("hp_tortoise_mode") }),
        P.tortoiseMode,
      ]),
      h("div", { class: "kv" }, [
        h("span", { class: "kv-cap", text: this._t("hp_current_option") }),
        P.currentOpt,
      ]),
      h("div", { class: "kv" }, [
        h("span", { class: "kv-cap", text: this._t("hp_desired_option") }),
        P.desiredOpt,
      ]),
    ]);
    P.dhwOnlyNote = h("div", {
      class: "hp-note hp-warn",
      text: this._t("hp_dhw_only_note"),
      style: "display:none",
    });
    P.modeCard = h("div", { class: "hp-card" }, [
      h("div", { class: "hp-card-cap" }, [
        this._t("hp_sec_mode"),
        this._infoIcon("tip_hp_mode"),
        P.syncBadge,
      ]),
      modeGrid,
      P.dhwOnlyNote,
      h("div", { class: "hp-note muted", text: this._t("hp_no_force") }),
    ]);

    // Card: the manual DHW switch (+ the permanent ownership warning).
    P.dhwToggle = h("button", {
      class: "tune-toggle",
      type: "button",
      on: { click: () => this._toggleHpDhw() },
    });
    P.dhwCard = h("div", { class: "hp-card" }, [
      h("div", { class: "hp-card-cap" }, [
        this._t("hp_sec_dhw"),
        this._infoIcon("tip_hp_dhw"),
      ]),
      h("div", { class: "hp-dhw-row" }, [
        h("span", { class: "kv-cap", text: this._t("hp_dhw_switch") }),
        P.dhwToggle,
      ]),
      h("div", { class: "hp-note hp-warn", text: this._t("hp_dhw_warning") }),
    ]);

    // Card: water setpoints (what we write and why — the components).
    P.coolVal = h("span", { class: "kv-val" });
    P.coolCalc = h("span", { class: "hp-calc muted" });
    P.heatVal = h("span", { class: "kv-val" });
    P.heatCalc = h("span", { class: "hp-calc muted" });
    // Issue #35: the curve at a few outdoor points, next to the setpoint.
    P.heatCurve = h("table", { class: "curve-table", style: "display:none" });
    P.setpointsCard = h("div", { class: "hp-card" }, [
      h("div", { class: "hp-card-cap", text: this._t("hp_sec_setpoints") }),
      h("div", { class: "kv" }, [
        h("span", { class: "kv-cap" }, [
          this._t("hp_cool_target"),
          this._infoIcon("tip_hp_cool"),
        ]),
        P.coolVal,
        P.coolCalc,
      ]),
      h("div", { class: "kv" }, [
        h("span", { class: "kv-cap" }, [
          this._t("hp_heat_target"),
          this._infoIcon("tip_hp_heat"),
        ]),
        P.heatVal,
        P.heatCalc,
      ]),
      P.heatCurve,
    ]);

    // Card: heating-setpoint write gate (issue #33). Hidden unless the link
    // exposes heating.gate (a heating setpoint entity is configured).
    P.gateBadge = h("span", { class: "chip", style: "display:none" });
    P.gateReason = h("span", { class: "kv-val" });
    P.gateTarget = h("span", { class: "kv-val" });
    P.gatePump = h("span", { class: "kv-val" });
    P.gateCurve = h("span", { class: "kv-val" });
    P.gateCompressor = h("span", { class: "kv-val" });
    P.gateOutlet = h("span", { class: "kv-val" });
    P.gateBand = h("span", { class: "kv-val" });
    P.gateStop = h("span", { class: "kv-val" });
    P.gateHeldFor = h("span", { class: "kv-val" });
    const gateKv = (labelKey, valEl) =>
      h("div", { class: "kv" }, [
        h("span", { class: "kv-cap", text: this._t(labelKey) }),
        valEl,
      ]);
    P.gateCard = h("div", { class: "hp-card", style: "display:none" }, [
      h("div", { class: "hp-card-cap" }, [
        this._t("hp_sec_heat_gate"),
        this._infoIcon("tip_hp_heat_gate"),
        P.gateBadge,
      ]),
      h("div", { class: "kv-grid hp-grid" }, [
        gateKv("hp_gate_reason", P.gateReason),
        gateKv("hp_gate_target", P.gateTarget),
        gateKv("hp_gate_pump", P.gatePump),
        gateKv("hp_gate_curve", P.gateCurve),
        gateKv("hp_gate_compressor", P.gateCompressor),
        gateKv("hp_gate_outlet", P.gateOutlet),
        gateKv("hp_gate_band", P.gateBand),
        gateKv("hp_gate_stop", P.gateStop),
        gateKv("hp_gate_held_for", P.gateHeldFor),
      ]),
    ]);

    // Card: heating compressor kick (issue #34). Hidden unless the link
    // exposes heating.kick (the opt-in kick is enabled).
    P.kickBadge = h("span", { class: "chip", style: "display:none" });
    P.kickReason = h("span", { class: "kv-val" });
    P.kickIdle = h("span", { class: "kv-val" });
    P.kickDeficit = h("span", { class: "kv-val" });
    P.kickTarget = h("span", { class: "kv-val" });
    P.kickTimeout = h("span", { class: "kv-val" });
    P.kick24h = h("span", { class: "kv-val" });
    P.kickLast = h("span", { class: "kv-val" });
    P.kickCard = h("div", { class: "hp-card", style: "display:none" }, [
      h("div", { class: "hp-card-cap" }, [
        this._t("hp_sec_heat_kick"),
        this._infoIcon("tip_hp_heat_kick"),
        P.kickBadge,
      ]),
      h("div", { class: "kv-grid hp-grid" }, [
        gateKv("hp_kick_reason", P.kickReason),
        gateKv("hp_kick_idle", P.kickIdle),
        gateKv("hp_kick_deficit", P.kickDeficit),
        gateKv("hp_kick_target", P.kickTarget),
        gateKv("hp_kick_timeout", P.kickTimeout),
        gateKv("hp_kick_24h", P.kick24h),
        gateKv("hp_kick_last", P.kickLast),
      ]),
    ]);

    // Card: cooling setpoint-flicker diagnostics (issue #7). Hidden unless the
    // link exposes a flicker payload (enabled, or its diagnostic sensors set).
    P.flickerBadge = h("span", { class: "chip", style: "display:none" });
    P.flickerState = h("span", { class: "kv-val" });
    P.flickerReturn = h("span", { class: "kv-val" });
    P.flickerOutlet = h("span", { class: "kv-val" });
    P.flickerFreq = h("span", { class: "kv-val" });
    P.flickerTrigger = h("span", { class: "kv-val" });
    P.flickerDemand = h("span", { class: "kv-val" });
    P.flickerStuck = h("span", { class: "kv-val" });
    P.flickerCooldown = h("span", { class: "kv-val" });
    P.flickerPulses = h("span", { class: "kv-val" });
    P.flickerPulses24h = h("span", { class: "kv-val" });
    P.flickerLastPulse = h("span", { class: "kv-val" });
    // Active GLOBAL flicker flags (not per-room, so the Rooms-tab annunciator
    // never surfaces them — they live here) rendered from FLAG_LABELS.
    P.flickerFlags = h("div", { class: "hp-flicker-flags", style: "display:none" });
    const flickerKv = (labelKey, valEl) =>
      h("div", { class: "kv" }, [
        h("span", { class: "kv-cap", text: this._t(labelKey) }),
        valEl,
      ]);
    P.flickerCard = h("div", { class: "hp-card", style: "display:none" }, [
      h("div", { class: "hp-card-cap" }, [
        this._t("hp_sec_flicker"),
        this._infoIcon("tip_hp_flicker"),
        P.flickerBadge,
      ]),
      h("div", { class: "kv-grid hp-grid" }, [
        flickerKv("hp_flicker_state", P.flickerState),
        flickerKv("hp_flicker_return", P.flickerReturn),
        flickerKv("hp_flicker_outlet", P.flickerOutlet),
        flickerKv("hp_flicker_freq", P.flickerFreq),
        flickerKv("hp_flicker_trigger", P.flickerTrigger),
        flickerKv("hp_flicker_demand", P.flickerDemand),
        flickerKv("hp_flicker_stuck", P.flickerStuck),
        flickerKv("hp_flicker_cooldown", P.flickerCooldown),
        flickerKv("hp_flicker_pulses", P.flickerPulses),
        flickerKv("hp_flicker_pulses_24h", P.flickerPulses24h),
        flickerKv("hp_flicker_last_pulse", P.flickerLastPulse),
      ]),
      P.flickerFlags,
    ]);

    // Card: "pump available for UFH" (only when the entity is configured).
    P.activeVal = h("span", { class: "kv-val" });
    P.activeCard = h("div", { class: "hp-card", style: "display:none" }, [
      h("div", { class: "hp-card-cap" }, [
        this._t("hp_active_cap"),
        this._infoIcon("tip_hp_active"),
      ]),
      P.activeVal,
    ]);

    P.paused = h("div", {
      class: "hp-note hp-warn",
      text: this._t("hp_writes_paused"),
      style: "display:none",
    });
    P.body = h("div", { class: "hp", style: "display:none" }, [
      P.paused,
      P.modeCard,
      P.dhwCard,
      P.setpointsCard,
      P.gateCard,
      P.kickCard,
      P.flickerCard,
      P.activeCard,
    ]);

    const el = h(
      "section",
      {
        class: "tab-section",
        role: "tabpanel",
        style: "display:none",
        dataset: { tab: "hp" },
      },
      [P.empty, P.body],
    );
    this._hpEls = P;
    return el;
  }

  /** Update the Heat-pump tab in place from the polled live payload. */
  _renderHp() {
    const P = this._hpEls;
    if (!P) {
      return;
    }
    const hp = (this._live && this._live.heat_pump) || null;
    if (!hp) {
      P.empty.style.display = "";
      P.body.style.display = "none";
      return;
    }
    P.empty.style.display = "none";
    P.body.style.display = "";
    P.paused.style.display = hp.writes_enabled ? "none" : "";

    // Mode + DHW cards need the pump-mode select.
    const hasMode = !!hp.mode_entity_id;
    P.modeCard.style.display = hasMode ? "" : "none";
    P.dhwCard.style.display = hasMode ? "" : "none";
    if (hasMode) {
      const mode = pick(this._live || {}, ["mode"], "heating");
      P.tortoiseMode.textContent = this._modeLabel(mode);
      P.currentOpt.textContent = hp.current_option || "—";
      P.desiredOpt.textContent = hp.desired_option || "—";
      if (hp.in_sync === null || hp.in_sync === undefined) {
        P.syncBadge.style.display = "none";
      } else {
        P.syncBadge.style.display = "";
        P.syncBadge.className = "chip " + (hp.in_sync ? "chip-ok" : "chip-warn");
        P.syncBadge.textContent = this._t(hp.in_sync ? "hp_in_sync" : "hp_diverged");
      }
      P.dhwOnlyNote.style.display = hp.dhw_only ? "" : "none";
      P.dhwToggle.classList.toggle("on", !!hp.dhw_active);
      P.dhwToggle.textContent = this._t(hp.dhw_active ? "tune_on" : "tune_off");
      P.dhwToggle.setAttribute("aria-pressed", hp.dhw_active ? "true" : "false");
    }

    // Water setpoints: value + the components that produced it.
    const setRow = (valEl, calcEl, payload, calcKey, calcParams) => {
      if (!payload) {
        valEl.textContent = this._t("hp_not_configured_entity");
        valEl.className = "kv-val muted";
        calcEl.textContent = "";
        return;
      }
      if (payload.target_c === null || payload.target_c === undefined) {
        valEl.textContent = this._t("hp_not_written");
        valEl.className = "kv-val muted";
      } else {
        valEl.textContent = fmt(payload.target_c, 1, " °C");
        valEl.className = "kv-val";
      }
      calcEl.textContent = fmtStr(this._t(calcKey), calcParams(payload));
    };
    setRow(P.coolVal, P.coolCalc, hp.cooling, "hp_cool_calc", (c) => ({
      base: fmt(c.base_c, 1, " °C"),
      dew: fmt(c.safe_dew_c, 1, " °C"),
    }));
    setRow(P.heatVal, P.heatCalc, hp.heating, "hp_heat_calc", (hh) => ({
      base: fmt(hh.base_c, 1, " °C"),
      slope: fmt(hh.slope, 2),
      neutral: fmt(hh.neutral_c, 1, " °C"),
      tout: fmt(hh.t_out_c, 1, " °C"),
      min: fmt(hh.min_c, 1, " °C"),
      max: fmt(hh.max_c, 1, " °C"),
    }));
    this._fillCurveTable(P.heatCurve, (hp.heating && hp.heating.curve) || []);

    // Heating-setpoint write gate (issue #33): the desired vs the pump's
    // setpoint and why a step is held.
    this._renderHeatGate((hp.heating && hp.heating.gate) || null);
    // Heating compressor kick (issue #34): only while the opt-in is enabled.
    this._renderHeatKick((hp.heating && hp.heating.kick) || null);

    // "Pump available for UFH" — only with a configured entity.
    P.activeCard.style.display = hp.hp_active_configured ? "" : "none";
    if (hp.hp_active_configured) {
      P.activeVal.textContent =
        hp.hp_active === null || hp.hp_active === undefined
          ? this._t("hp_active_unknown")
          : this._t(hp.hp_active ? "yes" : "no");
      P.activeVal.classList.toggle("warn", hp.hp_active === false);
    }

    // Cooling setpoint-flicker diagnostics (issue #7) — only when the payload
    // is present (the flicker is enabled or its diagnostic sensors are set).
    const fl = hp.flicker || null;
    P.flickerCard.style.display = fl ? "" : "none";
    if (fl) {
      const st = fl.state || "idle";
      const flags = Array.isArray(fl.flags) ? fl.flags : [];
      const pulsing =
        st === "pulse" ||
        (fl.pulse_target_c !== null && fl.pulse_target_c !== undefined);
      const noSensor = flags.includes("flicker_no_sensor");
      P.flickerBadge.style.display = "";
      if (pulsing) {
        // A pulse is being written this cycle.
        P.flickerBadge.className = "chip chip-warn";
        P.flickerBadge.textContent = this._t("hp_flicker_pulsing_badge");
      } else if (fl.enabled && noSensor) {
        // Enabled but degraded (a pump sensor is missing/stale): NOT green.
        P.flickerBadge.className = "chip chip-warn";
        P.flickerBadge.textContent = this._t("hp_flicker_on");
      } else {
        P.flickerBadge.className = "chip " + (fl.enabled ? "chip-ok" : "");
        P.flickerBadge.textContent = this._t(
          fl.enabled ? "hp_flicker_on" : "hp_flicker_off",
        );
      }
      // Active GLOBAL flicker flags, rendered from FLAG_LABELS (localized label
      // + severity colour + description tooltip) — these are not per-room, so
      // the Rooms-tab annunciator never shows them.
      P.flickerFlags.textContent = "";
      const known = flags.filter((code) => code in FLAG_LABELS);
      P.flickerFlags.style.display = known.length ? "" : "none";
      for (const code of known) {
        P.flickerFlags.appendChild(this._chip(code));
      }
      const stKey =
        {
          idle: "hp_flicker_st_idle",
          pulse: "hp_flicker_st_pulse",
          cooldown: "hp_flicker_st_cooldown",
        }[st] || "hp_flicker_st_idle";
      P.flickerState.textContent = this._t(stKey);
      P.flickerReturn.textContent = fmt(fl.return_c, 1, " °C");
      P.flickerOutlet.textContent = fmt(fl.outlet_c, 1, " °C");
      P.flickerFreq.textContent = fmt(fl.compressor_freq_hz, 0, " Hz");
      P.flickerTrigger.textContent = fmt(fl.trigger_c, 1, " °C");
      // Demand gate (§23): live loop-weighted Σ vs the configured threshold.
      const demandSum = num(fl.demand_open_pct);
      const demandThr = num(fl.demand_threshold_pct);
      P.flickerDemand.textContent =
        demandSum === null || demandThr === null
          ? "—"
          : `${Math.round(demandSum)} / ${Math.round(demandThr)} %`;
      const mins = (s) =>
        num(s) === null
          ? "—"
          : fmtStr(this._t("test_running_min"), {
              m: Math.max(0, Math.round(num(s) / 60)),
            });
      P.flickerStuck.textContent = mins(fl.stuck_remaining_s);
      P.flickerCooldown.textContent = mins(fl.cooldown_remaining_s);
      P.flickerPulses.textContent =
        num(fl.pulses_last_hour) === null ? "—" : String(fl.pulses_last_hour);
      P.flickerLastPulse.textContent = fmt(fl.last_pulse_target_c, 1, " °C");
      this._refreshFlickerPulses24h();
    }
  }

  /** Fill the heating-setpoint write-gate card (issue #33) from `gate`. */
  _renderHeatGate(gate) {
    const P = this._hpEls;
    if (!P || !P.gateCard) {
      return;
    }
    P.gateCard.style.display = gate ? "" : "none";
    if (!gate) {
      return;
    }
    P.gateBadge.style.display = "";
    if (gate.held) {
      P.gateBadge.className = "chip chip-warn";
      P.gateBadge.textContent = this._t("hp_gate_held_badge");
    } else {
      P.gateBadge.className = "chip " + (gate.gate_active ? "chip-ok" : "");
      P.gateBadge.textContent = this._t(
        gate.gate_active ? "hp_gate_active" : "hp_gate_inactive",
      );
    }
    const reason = String(gate.reason || "no_target");
    const reasonKey = "hp_gate_r_" + reason;
    const label = this._t(reasonKey);
    P.gateReason.textContent = label === reasonKey ? reason : label;
    P.gateReason.classList.toggle("warn", !!gate.held);
    P.gateTarget.textContent = fmt(gate.target_c, 1, " °C");
    P.gatePump.textContent = fmt(gate.pump_setpoint_c, 1, " °C");
    P.gateCurve.textContent = fmt(gate.curve_c, 1, " °C");
    const freq = num(gate.compressor_freq_hz);
    P.gateCompressor.textContent =
      freq === null
        ? "—"
        : `${this._t(freq > 0 ? "hp_gate_running" : "hp_gate_idle")} · ${fmt(freq, 0, " Hz")}`;
    P.gateOutlet.textContent = fmt(gate.outlet_c, 1, " °C");
    const src = this._t(
      gate.restart_band_source === "entity" ? "hp_gate_band_entity" : "hp_gate_band_knob",
    );
    P.gateBand.textContent = `${fmt(gate.restart_band_k, 1, " K")} (${src})`;
    P.gateStop.textContent = fmt(gate.stop_offset_k, 1, " K");
    P.gateHeldFor.textContent =
      num(gate.held_for_s) === null
        ? "—"
        : fmtStr(this._t("test_running_min"), {
            m: Math.max(0, Math.round(num(gate.held_for_s) / 60)),
          });
  }

  /** Fill the heating compressor-kick card (issue #34) from `kick`. */
  _renderHeatKick(kick) {
    const P = this._hpEls;
    if (!P || !P.kickCard) {
      return;
    }
    P.kickCard.style.display = kick ? "" : "none";
    if (!kick) {
      return;
    }
    const st = String(kick.state || "off");
    const stKey = "hp_kick_st_" + st;
    const stLabel = this._t(stKey);
    P.kickBadge.style.display = "";
    P.kickBadge.className =
      "chip " +
      (st === "kick" || st === "done"
        ? "chip-warn"
        : st === "ready" || st === "waiting"
          ? "chip-ok"
          : "");
    P.kickBadge.textContent = stLabel === stKey ? st : stLabel;
    const reason = String(kick.reason || "disabled");
    const reasonKey = "hp_kick_r_" + reason;
    const label = this._t(reasonKey);
    P.kickReason.textContent = label === reasonKey ? reason : label;
    const mins = (s) =>
      num(s) === null
        ? "—"
        : fmtStr(this._t("test_running_min"), {
            m: Math.max(0, Math.round(num(s) / 60)),
          });
    P.kickIdle.textContent =
      num(kick.idle_s) === null
        ? "—"
        : `${mins(kick.idle_s)} / ${fmt(kick.idle_minutes, 0, " min")}`;
    P.kickDeficit.textContent =
      num(kick.deficit_k) === null
        ? "—"
        : `${fmt(kick.deficit_k, 1, " K")} / ${fmt(kick.deficit_threshold_k, 1, " K")}`;
    P.kickTarget.textContent = fmt(kick.kick_target_c, 1, " °C");
    P.kickTimeout.textContent = mins(kick.timeout_remaining_s);
    const res = kick.last_result;
    if (res === null || res === undefined) {
      P.kickLast.textContent = "—";
    } else {
      const resKey = "hp_kick_res_" + res;
      const resLabel = this._t(resKey);
      P.kickLast.textContent =
        `${resLabel === resKey ? res : resLabel} · ` +
        `${fmt(kick.last_kick_target_c, 1, " °C")} (Δ ${fmt(kick.last_kick_deficit_k, 1, " K")})`;
    }
    this._refreshKicks24h();
  }

  /**
   * Count heating compressor kicks over the last 24 h (issue #34): rising
   * edges into "kick" in the recorder history of the global
   * `hp_heating_kick` sensor — durable across reloads, like the flicker's
   * forced-start count. Cache-gated by `_series`; fails soft to an em dash.
   */
  _refreshKicks24h() {
    const P = this._hpEls;
    if (!P || !P.kick24h) {
      return;
    }
    const id = this._config && this._config.hp_heating_kick_entity_id;
    if (!id) {
      P.kick24h.textContent = "—";
      return;
    }
    this._series(id, "24h", true).then((entry) => {
      const Pn = this._hpEls;
      if (!Pn || !Pn.kick24h) {
        return;
      }
      let count = 0;
      let prev = null;
      for (const p of entry.data || []) {
        if (p.v === "kick" && prev !== "kick") {
          count += 1;
        }
        prev = p.v;
      }
      Pn.kick24h.textContent = String(count);
    });
  }

  /**
   * Count forced compressor starts over the last 24 h and show them in the
   * flicker card.
   *
   * Counted as rising edges into the "pulse" state in the recorder history of
   * the global `hp_flicker_state` sensor — durable across HA restarts and
   * entry reloads, unlike the in-memory last-hour counter (which resets on
   * every tuning save). Cache-gated by `_series`, so the 5 s poll refetches
   * at most once a minute; fails soft to an em dash.
   */
  _refreshFlickerPulses24h() {
    const P = this._hpEls;
    if (!P || !P.flickerPulses24h) {
      return;
    }
    const id = this._config && this._config.hp_flicker_state_entity_id;
    if (!id) {
      P.flickerPulses24h.textContent = "—";
      return;
    }
    this._series(id, "24h", true).then((entry) => {
      const Pn = this._hpEls;
      if (!Pn || !Pn.flickerPulses24h) {
        return;
      }
      let count = 0;
      let prev = null;
      for (const p of entry.data || []) {
        if (p.v === "pulse" && prev !== "pulse") {
          count += 1;
        }
        prev = p.v;
      }
      Pn.flickerPulses24h.textContent = String(count);
    });
  }

  /** Toggle the pump's +DHW flag (optimistic patch; the poll reconciles). */
  async _toggleHpDhw() {
    const hp = (this._live && this._live.heat_pump) || null;
    const P = this._hpEls;
    if (!hp || !hp.mode_entity_id || !P) {
      return;
    }
    const want = !hp.dhw_active;
    P.dhwToggle.disabled = true;
    const res = await this._callWS({ type: WS.setHpDhw, dhw: want });
    P.dhwToggle.disabled = false;
    if (res) {
      this._live = {
        ...this._live,
        heat_pump: {
          ...hp,
          dhw_active: want,
          current_option: res.option || hp.current_option,
        },
      };
      this._renderHp();
    }
    this._poll();
  }

  // --------------------------------------------------------------------------
  // Manifolds tab
  // --------------------------------------------------------------------------

  /**
   * Build the Manifolds section skeleton: an empty-state line and the card
   * grid (cards are reconciled per manifold id by `_renderManifolds`). One
   * ResizeObserver serves every card's drawing container, so a card repaints
   * when its column changes width or when the tab becomes visible (width 0
   * -> real width).
   */
  _buildManifoldsSection() {
    const empty = h("div", { class: "empty", text: this._t("loading") });
    const grid = h("div", { class: "mf-grid", style: "display:none" });
    const el = h(
      "section",
      { class: "tab-section", role: "tabpanel", style: "display:none", dataset: { tab: "manifolds" } },
      [empty, grid],
    );
    this._disconnectMfRO();
    if (typeof ResizeObserver !== "undefined") {
      this._mfRO = new ResizeObserver((entries) => {
        for (const entry of entries) {
          const card = this._manifoldEls && this._manifoldEls.byArt.get(entry.target);
          if (card) {
            this._paintManifold(card);
          }
        }
      });
    }
    this._manifoldEls = { el, empty, grid, cards: new Map(), byArt: new Map() };
    return el;
  }

  _disconnectMfRO() {
    if (this._mfRO) {
      this._mfRO.disconnect();
      this._mfRO = null;
    }
  }

  /** Reconcile the manifold cards from the polled live payload. */
  _renderManifolds() {
    const E = this._manifoldEls;
    if (!E) {
      return;
    }
    const views =
      this._live && Array.isArray(this._live.manifolds) ? this._live.manifolds : [];
    if (!views.length) {
      for (const [, card] of E.cards) {
        this._dropManifoldCard(card);
      }
      E.cards.clear();
      E.empty.textContent = this._live ? this._t("mf_empty") : this._t("loading");
      E.empty.style.display = "";
      E.grid.style.display = "none";
      return;
    }
    E.empty.style.display = "none";
    E.grid.style.display = "";

    const desired = new Set(views.map((v) => String(v.id)));
    for (const [id, card] of [...E.cards]) {
      if (!desired.has(id)) {
        this._dropManifoldCard(card);
        E.cards.delete(id);
      }
    }
    let prevEl = null;
    for (const view of views) {
      const id = String(view.id);
      const circuits = Math.max(1, Number(view.circuits) || 1);
      let card = E.cards.get(id);
      // Rebuild when the circuit count changed (the table rows are fixed).
      if (card && card.circuits !== circuits) {
        this._dropManifoldCard(card);
        E.cards.delete(id);
        card = undefined;
      }
      if (!card) {
        card = this._buildManifoldCard(id, circuits);
        E.cards.set(id, card);
      }
      // Keep payload order, but never detach a node that already sits in place:
      // re-inserting a card on every poll made mobile browsers drop the scroll
      // position back to the top of the tab.
      if (card.el.parentNode !== E.grid || card.el.previousElementSibling !== prevEl) {
        E.grid.insertBefore(card.el, prevEl ? prevEl.nextSibling : E.grid.firstChild);
      }
      prevEl = card.el;
      this._updateManifoldCard(card, view);
    }
  }

  _dropManifoldCard(card) {
    if (this._mfRO) {
      this._mfRO.unobserve(card.art);
    }
    if (this._manifoldEls) {
      this._manifoldEls.byArt.delete(card.art);
    }
    card.el.remove();
  }

  /**
   * A value that MAY link to an entity's native dialog: a plain-text button
   * (keyboard reachable) that only shows a pointer cursor when linked.
   */
  _mfLink(extraClass) {
    const btn = h("button", {
      class: "mf-link" + (extraClass ? " " + extraClass : ""),
      type: "button",
      disabled: true,
    });
    btn.addEventListener("click", () => this._moreInfo(btn.dataset.entity || null));
    return btn;
  }

  _setMfLink(btn, text, entityId) {
    btn.textContent = text;
    if (entityId) {
      btn.disabled = false;
      btn.dataset.entity = entityId;
      btn.title = fmtStr(this._t("mf_link_tip"), { id: entityId });
      btn.classList.add("linked");
    } else {
      btn.disabled = true;
      delete btn.dataset.entity;
      btn.removeAttribute("title");
      btn.classList.remove("linked");
    }
  }

  /** Localised "circuit(s)" word for the card subtitle (Polish plural rules). */
  _mfCircuitsWord(n) {
    if (n === 1) {
      return this._t("mf_circuits_one");
    }
    if (this._lang === "pl") {
      const m10 = n % 10;
      const m100 = n % 100;
      if (m10 >= 2 && m10 <= 4 && !(m100 >= 12 && m100 <= 14)) {
        return this._t("mf_circuits_few");
      }
    }
    return this._t("mf_circuits_many");
  }

  /** Build one manifold card: head with stats, drawing container, circuit table. */
  _buildManifoldCard(id, circuits) {
    const p = {};
    p.title = h("span", { class: "mf-title" });
    p.sub = h("span", { class: "mf-sub" });
    const stat = (capKey, cls) => {
      const val = this._mfLink("stat-val");
      const el = h("span", { class: "stat" + (cls ? " " + cls : "") }, [
        h("span", { class: "stat-cap", text: this._t(capKey) }),
        val,
      ]);
      return { el, val };
    };
    p.supply = stat("mf_supply");
    p.return = stat("mf_return");
    p.dt = stat("mf_dt");
    p.open = stat("mf_open");
    p.flags = stat("mf_flags", "sev-warn");
    p.flags.el.style.display = "none";
    const stats = h("span", { class: "mf-stats" }, [
      p.supply.el,
      p.return.el,
      p.dt.el,
      p.open.el,
      p.flags.el,
    ]);
    const head = h("div", { class: "mf-head" }, [p.title, p.sub, stats]);

    // The drawing: an SVG string swapped in by `_paintManifold`; clicks on a
    // labelled value (data-entity) open the native entity dialog.
    const art = h("div", {
      class: "mf-art",
      on: {
        click: (e) => {
          const target =
            e.target && e.target.closest ? e.target.closest("[data-entity]") : null;
          if (target) {
            this._moreInfo(target.getAttribute("data-entity"));
          }
        },
      },
    });

    const headKeys = ["mf_th_loop", "mf_th_open", "mf_th_supply", "mf_th_return", "mf_th_dt", "mf_th_state"];
    const headCells = headKeys.map((key, i) =>
      h("th", { scope: "col", class: i >= 2 && i <= 4 ? "n" : null, text: this._t(key) }),
    );
    const tbody = h("tbody");
    const rows = [];
    for (let i = 0; i < circuits; i++) {
      const r = {};
      r.label = h("td", { class: "mf-loop" });
      r.barFill = h("i");
      r.pct = this._mfLink();
      r.supply = this._mfLink();
      r.return = this._mfLink();
      r.dt = h("span");
      r.chips = h("div", { class: "mf-chips" });
      r.tr = h("tr", { dataset: { position: String(i + 1) } }, [
        r.label,
        h("td", null, [h("span", { class: "mf-bar" }, [r.barFill]), r.pct]),
        h("td", { class: "n" }, [r.supply]),
        h("td", { class: "n" }, [r.return]),
        h("td", { class: "n" }, [r.dt]),
        h("td", null, [r.chips]),
      ]);
      tbody.appendChild(r.tr);
      rows.push(r);
    }
    const table = h("table", { class: "mf-loops" }, [h("thead", null, [h("tr", null, headCells)]), tbody]);
    const el = h("div", { class: "mf-card", dataset: { manifold: id } }, [
      head,
      art,
      h("div", { class: "mf-tbl" }, [table]),
    ]);

    const card = { el, parts: p, art, rows, circuits, view: null, paintKey: null };
    if (this._mfRO) {
      this._mfRO.observe(art);
    }
    this._manifoldEls.byArt.set(art, card);
    return card;
  }

  /** Update a manifold card's head, table and (when changed) drawing in place. */
  _updateManifoldCard(card, view) {
    const p = card.parts;
    const n = card.circuits;
    p.title.textContent = String(view.name || "");
    p.sub.textContent = fmtStr(this._t("mf_sub"), { n, circuits: this._mfCircuitsWord(n) });

    const main = view.main || {};
    const mainS = main.supply || {};
    const mainR = main.return || {};
    const sVal = num(mainS.value);
    const rVal = num(mainR.value);
    this._setMfLink(p.supply.val, fmt(sVal, 1, " °C"), mainS.entity_id);
    this._setMfLink(p.return.val, fmt(rVal, 1, " °C"), mainR.entity_id);
    this._setMfLink(
      p.dt.val,
      sVal !== null && rVal !== null ? signed(sVal - rVal, 1, " K") : "—",
      null,
    );

    const positions = Array.isArray(view.positions) ? view.positions : [];
    let open = 0;
    let flaggedCount = 0;
    let flagRank = 0;
    for (let i = 0; i < n; i++) {
      const r = card.rows[i];
      const v = positions[i] || {};
      const assigned = v.room_name != null;
      const valve = v.valve || {};
      const supply = v.supply || {};
      const ret = v.return || {};
      const flags = assigned && Array.isArray(v.flags) ? v.flags : [];
      const pct = assigned ? num(valve.pct) : null;

      r.tr.classList.toggle("mf-free", !assigned);
      r.label.textContent =
        `${i + 1}. ` + (assigned ? String(v.loop_label || v.room_name) : this._t("mf_free"));
      r.barFill.style.width = (pct === null ? 0 : clamp(pct, 0, 100)) + "%";
      this._setMfLink(r.pct, pct === null ? "—" : fmt(pct, 0, " %"), assigned ? valve.entity_id : null);
      this._setMfLink(r.supply, fmt(assigned ? supply.value : null, 1, " °C"), assigned ? supply.entity_id : null);
      this._setMfLink(r.return, fmt(assigned ? ret.value : null, 1, " °C"), assigned ? ret.entity_id : null);
      const dt = assigned ? num(v.delta_k) : null;
      r.dt.textContent = dt === null ? "—" : signed(dt, 1, " K");

      r.chips.textContent = "";
      if (!assigned) {
        r.chips.appendChild(h("span", { class: "muted", text: "—" }));
      } else if (flags.length) {
        for (const code of flags) {
          r.chips.appendChild(this._chip(code));
          flagRank = Math.max(flagRank, SEV_RANK[this._flagSev(code)] || 0);
        }
        flaggedCount += 1;
      } else {
        r.chips.appendChild(h("span", { class: "chip chip-ok", text: this._t("mf_state_ok") }));
      }
      if (pct !== null && pct >= MF_OPEN_THRESHOLD_PCT) {
        open += 1;
      }
    }
    this._setMfLink(p.open.val, `${open}/${n}`, null);
    p.flags.el.style.display = flaggedCount ? "" : "none";
    p.flags.el.className = "stat sev-" + this._sevName(flagRank);
    this._setMfLink(p.flags.val, String(flaggedCount), null);

    card.view = view;
    this._paintManifold(card);
  }

  /**
   * Repaint a card's drawing when its data or its width changed. A hidden
   * tab has width 0 and is skipped; the ResizeObserver repaints it when it
   * becomes visible.
   */
  _paintManifold(card) {
    const w = card.art.clientWidth;
    if (!w || !card.view) {
      return;
    }
    const key = JSON.stringify(card.view) + "|" + Math.round(w);
    if (key === card.paintKey) {
      return;
    }
    card.paintKey = key;
    // The captions on the main stubs start with a capital (the header stats stay lower-case).
    const cap1 = (str) => str.charAt(0).toUpperCase() + str.slice(1);
    const caps = { supply: cap1(this._t("mf_supply")), return: cap1(this._t("mf_return")) };
    const html = mfRenderSvg(
      mfBuildModel(card.view),
      card.view,
      w,
      caps,
      String(card.view.name || ""),
    );
    // Morph the existing <svg> instead of replacing it: the element (and the
    // page height under the reader's thumb) stays, only its content changes.
    const svgEl = card.art.querySelector("svg");
    const tpl = document.createElement("template");
    tpl.innerHTML = html;
    const fresh = tpl.content.firstElementChild;
    if (!svgEl || !fresh) {
      card.art.innerHTML = html;
      return;
    }
    for (const attr of ["viewBox", "aria-label"]) {
      const val = fresh.getAttribute(attr);
      if (val !== null && val !== svgEl.getAttribute(attr)) {
        svgEl.setAttribute(attr, val);
      }
    }
    svgEl.replaceChildren(...fresh.childNodes);
  }

  // --------------------------------------------------------------------------
  // Tuning tab
  // --------------------------------------------------------------------------

  /** Build the Tuning section skeleton (populated lazily by `_renderTuning`). */
  _buildTuningSection() {
    const intro = h("div", { class: "tune-intro", text: this._t("tune_intro") });
    const scopeBar = h("div", {
      class: "tune-scopes",
      role: "tablist",
      "aria-label": this._t("tune_scope_global"),
    });
    const body = h("div", { class: "tune-body" }, [
      h("div", { class: "empty", text: this._t("loading") }),
    ]);
    const saveBtn = h("button", {
      class: "tune-save",
      type: "button",
      text: this._t("tune_save"),
      on: { click: () => this._saveTuning() },
    });
    const saveNote = h("span", { class: "tune-note" });
    const footer = h("div", { class: "tune-footer" }, [saveBtn, saveNote]);

    const el = h(
      "section",
      { class: "tab-section", role: "tabpanel", style: "display:none" },
      [h("div", { class: "tune" }, [intro, scopeBar, body, footer])],
    );
    this._tuningEls = { el, scopeBar, body, saveBtn, saveNote, fields: new Map() };
    return el;
  }

  /** Configured room names (for the Tuning scope chips), or an empty list. */
  _configRoomNames() {
    const rooms = (this._config && this._config.rooms) || [];
    return rooms.map((r) => String(r.name)).filter((n) => n);
  }

  /** Fetch the tuning payload once, then render it (in-flight de-duplicated). */
  _ensureTuningLoaded() {
    if (this._tuning || this._tuningLoading) {
      return;
    }
    this._loadTuning();
  }

  async _loadTuning() {
    if (this._tuningLoading) {
      return;
    }
    this._tuningLoading = true;
    const res = await this._callWS({ type: WS.getTuning });
    this._tuningLoading = false;
    if (res && Array.isArray(res.fields)) {
      this._adoptTuning(res);
    }
  }

  /** Adopt a fresh tuning payload, reconciling scope, draft and DOM. */
  _adoptTuning(payload) {
    this._tuning = payload;
    if (
      this._tuningScope !== "global" &&
      !this._configRoomNames().includes(this._tuningScope)
    ) {
      this._tuningScope = "global";
    }
    this._initTuningDraft();
    this._renderTuning();
  }

  /** Seed the working draft from the payload for the active scope. */
  _initTuningDraft() {
    if (!this._tuning) {
      this._tuningDraft = null;
      return;
    }
    const global = this._tuning.global || {};
    if (this._tuningScope === "global") {
      this._tuningDraft = { values: { ...global }, overridden: new Set() };
      return;
    }
    const override = (this._tuning.rooms || {})[this._tuningScope] || {};
    this._tuningDraft = {
      values: { ...global, ...override },
      overridden: new Set(Object.keys(override)),
    };
  }

  /** Switch the tuning scope (global / a room), re-seeding the draft. */
  _setTuningScope(scope) {
    if (scope === this._tuningScope) {
      return;
    }
    if (scope !== "global" && !this._configRoomNames().includes(scope)) {
      return;
    }
    this._tuningScope = scope;
    this._initTuningDraft();
    this._renderTuning();
  }

  /**
   * (Re)build just the scope chip row (Global + one per configured room).
   *
   * Split out from `_renderTuning` so it can run on its own when `get_config`
   * lands after the Tuning tab was already opened — refreshing the room chips
   * without rebuilding the knob steppers (which would disturb an edit draft).
   */
  _renderTuningScopes() {
    const E = this._tuningEls;
    if (!E) {
      return;
    }
    E.scopeBar.textContent = "";
    const scopes = ["global", ...this._configRoomNames()];
    for (const scope of scopes) {
      const label = scope === "global" ? this._t("tune_scope_global") : scope;
      const isActive = scope === this._tuningScope;
      const tab = h("button", {
        class: "tune-scope" + (isActive ? " active" : ""),
        type: "button",
        role: "tab",
        text: label,
        "aria-selected": isActive ? "true" : "false",
        tabindex: isActive ? "0" : "-1",
        on: { click: () => this._setTuningScope(scope) },
      });
      E.scopeBar.appendChild(tab);
    }
  }

  /** (Re)build the scope chips + grouped knob list (A4) for the active scope. */
  _renderTuning() {
    const E = this._tuningEls;
    if (!E) {
      return;
    }
    // Scope chips: Global + one per configured room.
    this._renderTuningScopes();

    // Grouped vertical knob list (A4): one titled section per KNOB_GROUPS
    // entry, parameters one under another. Global-only groups (heat-pump
    // water) never render in a room scope; a knob the backend exposes but no
    // group lists lands in a trailing "other" section — never silently lost.
    E.body.textContent = "";
    E.fields = new Map();
    E.curve = null;
    const fields = (this._tuning && this._tuning.fields) || [];
    if (!this._tuningDraft || fields.length === 0) {
      E.body.appendChild(h("div", { class: "empty", text: this._t("loading") }));
      return;
    }
    const byName = new Map(fields.map((f) => [f.name, f]));
    const isRoom = this._tuningScope !== "global";
    const grouped = new Set();
    for (const group of KNOB_GROUPS) {
      for (const knob of group.knobs) {
        grouped.add(knob);
      }
    }
    const leftovers = fields.map((f) => f.name).filter((n) => !grouped.has(n));
    const sections = [...KNOB_GROUPS];
    if (leftovers.length) {
      sections.push({ key: "other", labelKey: "tune_grp_other", knobs: leftovers });
    }
    for (const group of sections) {
      if (group.globalOnly && isRoom) {
        continue;
      }
      const present = group.knobs.filter((n) => byName.has(n));
      if (!present.length) {
        continue;
      }
      const section = h("div", { class: "tune-group" }, [
        h("div", { class: "tune-group-cap", text: this._t(group.labelKey) }),
      ]);
      for (const name of present) {
        section.appendChild(this._buildTuneCard(byName.get(name)));
      }
      if (group.curvePreview && !isRoom) {
        section.appendChild(this._buildCurvePreview());
      }
      E.body.appendChild(section);
    }
    E.saveNote.textContent = "";
  }

  /** Build one knob card (number stepper or boolean toggle) and register refs. */
  _buildTuneCard(field) {
    const name = field.name;
    const isRoom = this._tuningScope !== "global";
    const label = h("span", { class: "tune-label", text: this._knobLabel(name) });
    const unit = field.unit
      ? h("span", { class: "tune-unit", text: "[" + field.unit + "]" })
      : null;
    // Every knob card carries an "i" explaining what the knob does, its unit,
    // default and when to touch it (guarded: only when the STR key exists).
    const tipKey = "tip_knob_" + name;
    const info = tipKey in STR.en ? this._infoIcon(tipKey) : null;
    // Room scope: "overridden" + revert to the global value. Global scope
    // (issue #31): "custom" + reset to the library default (sent as null),
    // and an "old default" mark for a value an older release froze.
    const badge = h("span", {
      class: "tune-badge",
      text: this._t(isRoom ? "tune_overridden" : "tune_custom"),
      style: "display:none",
    });
    const legacy = h("span", {
      class: "tune-badge tune-badge-legacy",
      text: this._t("tune_old_default"),
      title: this._t("tune_old_default_tip"),
      style: "display:none",
    });
    const revertKey = isRoom ? "tune_revert" : "tune_reset_default";
    const revert = h("button", {
      class: "tune-revert",
      type: "button",
      title: this._t(revertKey),
      "aria-label": this._t(revertKey),
      style: "display:none",
      on: { click: () => this._revertTuneField(name) },
    });
    revert.appendChild(this._icon("mdi:backup-restore", "⟲"));
    const head = h("div", { class: "tune-card-head" }, [
      label,
      unit,
      info,
      badge,
      legacy,
      revert,
    ]);

    const refs = {
      field,
      badge,
      legacy,
      revert,
      valEl: null,
      minus: null,
      plus: null,
      toggle: null,
    };
    let control;
    if (field.type === "bool") {
      const toggle = h("button", {
        class: "tune-toggle",
        type: "button",
        on: { click: () => this._toggleTuneBool(name) },
      });
      refs.toggle = toggle;
      control = toggle;
    } else {
      const minus = h("button", { class: "step-btn", type: "button", text: "−" });
      const val = h("span", { class: "step-val" });
      const plus = h("button", { class: "step-btn", type: "button", text: "+" });
      this._bindHold(minus, () => this._nudgeTune(name, -1));
      this._bindHold(plus, () => this._nudgeTune(name, 1));
      refs.valEl = val;
      refs.minus = minus;
      refs.plus = plus;
      control = h("div", { class: "stepper" }, [minus, val, plus]);
    }

    const card = h("div", { class: "tune-card" + (isRoom ? " inherited" : "") }, [
      head,
      control,
    ]);
    refs.card = card;
    this._tuningEls.fields.set(name, refs);
    this._applyTuneField(name);
    return card;
  }

  /**
   * Build the heating-curve preview (issue #35) shown under the Heating water
   * knobs in the Global scope: the target water at a few outdoor
   * temperatures, computed by the core (`preview_heating_curve`) from the
   * current draft, so unsaved edits show before they are saved.
   */
  _buildCurvePreview() {
    const table = h("table", { class: "curve-table", style: "display:none" });
    const note = h("div", { class: "curve-note muted" });
    const wrap = h("div", { class: "curve-preview" }, [
      h("div", { class: "curve-cap" }, [
        this._t("hp_curve_cap"),
        this._infoIcon("tip_hp_curve"),
      ]),
      table,
      note,
    ]);
    this._tuningEls.curve = { table, note };
    this._scheduleCurvePreview();
    return wrap;
  }

  /** Debounced refresh of the heating-curve preview (one call per burst). */
  _scheduleCurvePreview() {
    if (this._curveTimer) {
      clearTimeout(this._curveTimer);
    }
    this._curveTimer = setTimeout(() => {
      this._curveTimer = null;
      this._refreshCurvePreview();
    }, CURVE_PREVIEW_DEBOUNCE_MS);
  }

  /** Ask the core for the curve of the current draft and fill the preview. */
  async _refreshCurvePreview() {
    const E = this._tuningEls;
    const C = E && E.curve;
    if (!C || !this._tuningDraft || this._tuningScope !== "global") {
      return;
    }
    if (!this._hass || typeof this._hass.callWS !== "function") {
      return;
    }
    const saved = (this._tuning && this._tuning.global) || {};
    const values = {};
    let unsaved = false;
    for (const name of HEATING_CURVE_KNOBS) {
      if (name in this._tuningDraft.values) {
        values[name] = this._tuningDraft.values[name];
        if (!sameKnob(values[name], saved[name])) {
          unsaved = true;
        }
      }
    }
    this._curveGen = (this._curveGen || 0) + 1;
    const gen = this._curveGen;
    let res = null;
    let error = "";
    try {
      res = await this._hass.callWS({ type: WS.previewHeatingCurve, values });
    } catch (err) {
      error = err && err.message ? err.message : String(err);
    }
    // A newer request or a re-render superseded this one.
    if (gen !== this._curveGen || !this._tuningEls || this._tuningEls.curve !== C) {
      return;
    }
    if (!error && res && res.error) {
      error = String(res.error);
    }
    const points = !error && res && Array.isArray(res.points) ? res.points : [];
    this._fillCurveTable(C.table, points);
    if (error) {
      C.note.className = "curve-note hp-warn";
      C.note.textContent = fmtStr(this._t("hp_curve_invalid"), { detail: error });
    } else {
      C.note.className = "curve-note muted";
      C.note.textContent = unsaved ? this._t("hp_curve_unsaved") : "";
    }
  }

  /**
   * Fill a two-row curve table: outdoor temperatures over target water, one
   * column per point (1 decimal; numbers only, set as text).
   */
  _fillCurveTable(table, points) {
    table.textContent = "";
    const list = Array.isArray(points) ? points : [];
    const outRow = h("tr", null, [h("th", { text: this._t("hp_curve_tout") })]);
    const waterRow = h("tr", null, [h("th", { text: this._t("hp_curve_target") })]);
    for (const p of list) {
      outRow.appendChild(h("td", { text: signed(p && p.t_out_c, 0, " °C") }));
      waterRow.appendChild(h("td", { text: fmt(p && p.target_c, 1, " °C") }));
    }
    table.appendChild(outRow);
    table.appendChild(waterRow);
    table.style.display = list.length ? "" : "none";
  }

  /** Update a single knob card's value + override affordances in place. */
  _applyTuneField(name) {
    const E = this._tuningEls;
    if (!E || !this._tuningDraft) {
      return;
    }
    const refs = E.fields.get(name);
    if (!refs) {
      return;
    }
    if (HEATING_CURVE_KNOBS.has(name) && E.curve) {
      this._scheduleCurvePreview();
    }
    const field = refs.field;
    const value = this._tuningDraft.values[name];
    const isRoom = this._tuningScope !== "global";
    const overridden = isRoom && this._tuningDraft.overridden.has(name);

    if (field.type === "bool") {
      const on = !!value;
      refs.toggle.textContent = on ? this._t("tune_on") : this._t("tune_off");
      refs.toggle.classList.toggle("on", on);
      refs.toggle.setAttribute("aria-pressed", on ? "true" : "false");
    } else {
      const nv = num(value);
      refs.valEl.textContent = nv === null ? "—" : fmtKnobValue(nv, field.step);
      refs.minus.disabled = nv === null || nv <= field.min;
      refs.plus.disabled = nv === null || nv >= field.max;
    }

    refs.card.classList.toggle("inherited", isRoom && !overridden);
    if (isRoom) {
      refs.badge.style.display = overridden ? "" : "none";
      refs.revert.style.display = overridden ? "" : "none";
      refs.legacy.style.display = "none";
      return;
    }
    const custom = !sameKnob(value, this._tuneDefault(name));
    refs.badge.style.display = custom ? "" : "none";
    refs.revert.style.display = custom ? "" : "none";
    refs.legacy.style.display = custom && this._isOldDefault(name, value) ? "" : "none";
  }

  /** The library default of a knob (from the get_tuning payload). */
  _tuneDefault(name) {
    const defaults = (this._tuning && this._tuning.defaults) || {};
    return defaults[name];
  }

  /** Whether a value equals an earlier shipped default of the knob (#31). */
  _isOldDefault(name, value) {
    const old = (this._tuning && this._tuning.superseded_defaults) || {};
    const values = Array.isArray(old[name]) ? old[name] : [];
    return values.some((v) => sameKnob(value, v));
  }

  /** Nudge a numeric knob by ±step (clamped); marks it overridden in a room. */
  _nudgeTune(name, dir) {
    if (!this._tuningDraft) {
      return;
    }
    const refs = this._tuningEls && this._tuningEls.fields.get(name);
    if (!refs || refs.field.type === "bool") {
      return;
    }
    const field = refs.field;
    const cur = num(this._tuningDraft.values[name]);
    if (cur === null) {
      return;
    }
    let next = clamp(roundStep(cur + dir * field.step, field.step), field.min, field.max);
    next = Number(next.toFixed(6));
    if (next === cur) {
      return;
    }
    this._tuningDraft.values[name] = next;
    if (this._tuningScope !== "global") {
      this._tuningDraft.overridden.add(name);
    }
    this._applyTuneField(name);
  }

  /** Flip a boolean knob; marks it overridden in a room scope. */
  _toggleTuneBool(name) {
    if (!this._tuningDraft) {
      return;
    }
    this._tuningDraft.values[name] = !this._tuningDraft.values[name];
    if (this._tuningScope !== "global") {
      this._tuningDraft.overridden.add(name);
    }
    this._applyTuneField(name);
  }

  /**
   * Room scope: drop the override, reverting the knob to the global value.
   * Global scope: put the knob back on the library default (saved as null).
   */
  _revertTuneField(name) {
    if (!this._tuningDraft) {
      return;
    }
    if (this._tuningScope === "global") {
      this._tuningDraft.values[name] = this._tuneDefault(name);
      this._applyTuneField(name);
      return;
    }
    this._tuningDraft.overridden.delete(name);
    const global = (this._tuning && this._tuning.global) || {};
    this._tuningDraft.values[name] = global[name];
    this._applyTuneField(name);
  }

  /** Build the `values` payload for `set_tuning` from the working draft. */
  _buildTuneSaveValues() {
    const draft = this._tuningDraft;
    if (this._tuningScope === "global") {
      // Sparse (issue #31): a knob on its default is not sent, and one that
      // was stored as an override is cleared with null, so the backend never
      // freezes a default.
      const stored = new Set((this._tuning && this._tuning.global_overrides) || []);
      const values = {};
      for (const [name, value] of Object.entries(draft.values)) {
        if (!sameKnob(value, this._tuneDefault(name))) {
          values[name] = value;
        } else if (stored.has(name)) {
          values[name] = null;
        }
      }
      return values;
    }
    const prev = (this._tuning && this._tuning.rooms && this._tuning.rooms[this._tuningScope]) || {};
    const values = {};
    const names = ((this._tuning && this._tuning.fields) || []).map((f) => f.name);
    for (const name of names) {
      if (draft.overridden.has(name)) {
        values[name] = draft.values[name];
      } else if (name in prev) {
        // Was overridden, now reverted → null asks the backend to drop it.
        values[name] = null;
      }
    }
    return values;
  }

  async _saveTuning() {
    if (!this._tuning || !this._tuningDraft) {
      return;
    }
    const scope = this._tuningScope;
    const values = this._buildTuneSaveValues();
    // A save reloads the entry; suppress the transient not_found banner.
    this._suppressErrorUntil = Date.now() + 5000;
    const E = this._tuningEls;
    if (E) {
      E.saveBtn.disabled = true;
      E.saveNote.textContent = this._t("tune_saving");
    }
    const res = await this._callWS({ type: WS.setTuning, scope, values });
    if (E) {
      E.saveBtn.disabled = false;
    }
    if (res && Array.isArray(res.fields)) {
      this._adoptTuning(res);
      if (this._tuningEls) {
        this._tuningEls.saveNote.textContent = this._t("tune_saved");
      }
    } else if (E) {
      E.saveNote.textContent = "";
    }
    // Reload the rest of the panel (the entry rebuilds in the background).
    this._loadConfig();
    this._poll();
  }

  /** Jump to the Tuning tab scoped to a room (the assist "tune" shortcut). */
  _tuneRoom(name) {
    this._setActiveTab("tuning");
    this._ensureTuningLoaded();
    if (this._tuning) {
      this._setTuningScope(name);
    } else {
      // Payload not loaded yet: remember the desired scope for `_adoptTuning`.
      this._tuningScope = name;
    }
  }

  // --------------------------------------------------------------------------
  // History & charts
  // --------------------------------------------------------------------------

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
    for (const key of ["temp", "setpoint", "valve", "hum"]) {
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
    const humId = r.entities ? r.entities.entity_humidity : null;
    const diag = r.diagnosticEntities || {};
    const errorId = diag.error_c || null;
    const valveId = diag.recommended_valve || null;
    const modeId = diag.fast_source_mode || null;

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
      modeId ? this._series(modeId, wk, true) : empty,
      humId ? this._series(humId, wk) : empty,
    ];
    const token = ++HI.token;
    Promise.all(jobs).then(([te, ee, ve, me, he]) => {
      // Stale guard: the detail was rebuilt or the window changed mid-flight.
      if (!this._detail || this._detail.history !== HI || HI.token !== token) {
        return;
      }
      const sig = [
        wk, te.at, ee.at, ve.at, me.at, he.at,
        tempId, errorId, valveId, modeId, humId,
      ].join("|");
      if (!force && HI.sig === sig && HI.data) {
        return;
      }
      HI.sig = sig;

      const end = Date.now();
      const t0 = end - WINDOWS[wk].hours * 3600 * 1000;
      // Bucket-mean only the measured continuous series (presentation
      // smoothing; the valve keeps every command and the mode is textual).
      const bucketMs = WINDOWS[wk].bucketMs || 0;
      const temp = bucketMean(te.data || [], bucketMs);
      const error = bucketMean(ee.data || [], bucketMs);
      const hum = bucketMean(he.data || [], bucketMs);
      const valve = ve.data || [];
      const mode = me.data || [];

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

      HI.data = { t0, t1: end, temp, setpoint, valve, error, deadband, mode, hum };
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
    const hasHum = this._hasReal(d.hum);

    if (!hasTemp && !hasSp && !hasValve && !hasHum) {
      HI.ctx = null;
      HI.holder.textContent = "";
      HI.note.textContent = this._t("chart_nodata");
      HI.note.style.display = "";
      this._updateLegend(HI, hasTemp, hasSp, hasValve, hasHum);
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

    // Right axis (valve / humidity %).
    if (hasValve || hasHum) {
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
          y: (plotY + plotH + 26).toFixed(1),
          "text-anchor": i === 0 ? "start" : i === nTicks - 1 ? "end" : "middle",
          text: fmtTime(new Date(t)),
        }),
      );
    }

    // Split-mode band: one thin rect per run of the fast-source mode history,
    // between the plot and the time labels. Always drawn when data exists (no
    // legend entry / toggle); "off" and unknown states draw nothing.
    const bandRuns =
      d.mode && d.mode.length
        ? modeRuns(d.mode, d.t1).filter(
            (run) => run.m === "heating" || run.m === "cooling" || run.m === "dry",
          )
        : [];
    for (const run of bandRuns) {
      const rt0 = Math.max(run.t0, d.t0);
      const rt1 = Math.min(run.t1, d.t1);
      if (!(rt1 > rt0)) {
        continue;
      }
      const bx = xOf(rt0);
      svg.appendChild(
        s("rect", {
          class: "band-" + run.m,
          x: bx.toFixed(1),
          y: (plotY + plotH + 3).toFixed(1),
          width: Math.max(1, xOf(rt1) - bx).toFixed(1),
          height: 8,
          rx: 2,
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
    // Step-after: the recorder stores only CHANGES, so a command holds flat
    // until the next sample; the last run extends to "now" (t1) unless the
    // series ends in an explicit gap (unavailable sensor).
    if (hasValve && HI.visible.valve) {
      const segs = segments(d.valve);
      const rawLast = d.valve[d.valve.length - 1];
      const extend = rawLast && rawLast.v != null && Number.isFinite(rawLast.v);
      const base = (plotY + plotH).toFixed(1);
      const areaParts = [];
      const lineParts = [];
      segs.forEach((seg, si) => {
        if (!seg.length) {
          return;
        }
        let steps = "L" + xOf(seg[0].t).toFixed(1) + " " + yR(seg[0].v).toFixed(1);
        for (let i = 1; i < seg.length; i++) {
          const x = xOf(seg[i].t).toFixed(1);
          steps += " L" + x + " " + yR(seg[i - 1].v).toFixed(1);
          steps += " L" + x + " " + yR(seg[i].v).toFixed(1);
        }
        const held = si === segs.length - 1 && extend;
        let endX = xOf(seg[seg.length - 1].t);
        if (held) {
          endX = xOf(d.t1);
          steps += " L" + endX.toFixed(1) + " " + yR(seg[seg.length - 1].v).toFixed(1);
        }
        areaParts.push(
          "M" + xOf(seg[0].t).toFixed(1) + " " + base + " " + steps +
            " L" + endX.toFixed(1) + " " + base + " Z",
        );
        // A lone sample has no line unless the hold extends it into one.
        if (seg.length > 1 || held) {
          lineParts.push("M" + steps.slice(1));
        }
      });
      if (areaParts.length) {
        svg.appendChild(s("path", { class: "s-valve-area", d: areaParts.join(" ") }));
      }
      if (lineParts.length) {
        svg.appendChild(s("path", { class: "s-valve", d: lineParts.join(" ") }));
      }
    }

    // Humidity line (right axis, thin — context behind the temperature pair).
    if (hasHum && HI.visible.hum) {
      const parts = this._lineParts(segments(d.hum), xOf, yR);
      if (parts) {
        svg.appendChild(s("path", { class: "s-hum", d: parts }));
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
    for (const key of ["valve", "hum", "setpoint", "temp"]) {
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
    if (hasHum && HI.visible.hum) collect(d.hum);
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
        hum: hasHum && HI.visible.hum ? d.hum : null,
      },
      error: d.error && d.error.length ? d.error : null,
      modeRuns: bandRuns,
    };

    this._updateLegend(HI, hasTemp, hasSp, hasValve, hasHum);
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

  /** Live humidity [%] from the room's configured sensor entity, or null. */
  _roomHumidityPct(r) {
    const id = r && r.entities ? r.entities.entity_humidity : null;
    const st = id && this._hass && this._hass.states ? this._hass.states[id] : null;
    return st ? parseState(st.state) : null;
  }

  _updateLegend(HI, hasTemp, hasSp, hasValve, hasHum) {
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
    set("hum", this._roomHumidityPct(r), 0, "%", hasHum);
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
    set("hum", this._roomHumidityPct(r), 0, "%");
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
      // The valve draws step-after (held until the next command), so its
      // readout must step too; the temperature family stays interpolated.
      const v = key === "valve" ? stepAt(series, at) : interpAt(series, at);
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
    place("hum", "R", 0, "%");

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
    // Assist row: shown only while the hovered time falls inside a run of the
    // split-mode band (off / no data adds no row).
    const mrun = ctx.modeRuns.find((b) => b.t0 <= at && at <= b.t1);
    if (mrun) {
      HI.tip.appendChild(
        h("div", { class: "tip-row" }, [
          h("span", { class: "tip-sw band-" + mrun.m }),
          h("span", { class: "tip-lab", text: this._t("chart_split") }),
          h("span", {
            class: "tip-val",
            text: this._t(
              mrun.m === "dry"
                ? "assist_state_dry"
                : mrun.m === "cooling"
                  ? "assist_state_cooling"
                  : "assist_state_heating",
            ),
          }),
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
  --t-heat: var(--t-error, #db4437);
  --t-cool: var(--t-info, #039be5);
  --t-dry: var(--t-accent, #ff9800);
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
.wrap { max-width: 1560px; margin: 0 auto; padding: 16px; }
.hicon { --mdc-icon-size: 18px; color: var(--t-icon); }
.sr-only {
  position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;
}
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
/* Family A — interactive hero controls: 32px-tall rectangles, radius 8, card bg. */
.ghost-btn {
  display: inline-flex; align-items: center; gap: 6px;
  background: var(--t-card); color: var(--t-primary);
  border: 1px solid var(--t-line); border-radius: 8px;
  height: 32px; padding: 0 12px; font-size: 13px;
}
.ghost-btn:hover { border-color: var(--t-primary); }
.ghost-btn.icon-only { padding: 0; width: 32px; height: 32px; justify-content: center; }
.ghost-btn.icon-only .hicon { --mdc-icon-size: 20px; }
/* Stepper is the only frame; the buttons live inside it borderless (no box-in-box). */
.step-btn {
  width: 28px; height: 28px; border-radius: 6px;
  border: 0; background: transparent; color: var(--t-fg);
  font-size: 18px; line-height: 1; display: inline-flex;
  align-items: center; justify-content: center;
}
.step-btn:hover:not(:disabled) { background: color-mix(in srgb, var(--t-fg) 8%, transparent); }
.step-btn:disabled { opacity: .4; cursor: default; }

/* Hero — single compact row (wraps on narrow) */
.hero {
  background: var(--t-card); border: 1px solid var(--t-line);
  border-radius: var(--t-radius); padding: 10px 14px; margin-bottom: 16px;
  display: flex; flex-wrap: wrap; align-items: center; gap: 10px 16px;
}
.brand { display: flex; align-items: center; gap: 8px; font-weight: 600; font-size: 15px; }
.brand .hicon { --mdc-icon-size: 22px; color: var(--t-primary); }
.brand .brand-img { width: 22px; height: 22px; object-fit: contain; display: block; }
/* Family B — indicators: rounded pills, aligned padding, semantic tints kept. */
.pill {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 4px 12px; border-radius: 999px;
  background: var(--t-chip); font-size: 13px; font-weight: 500;
}
.pill-dot { width: 10px; height: 10px; border-radius: 50%; background: var(--t-muted); }
.pill-ok .pill-dot { background: var(--t-ok); }
.pill-warn .pill-dot { background: var(--t-warn); }
.pill-problem .pill-dot { background: var(--t-error); }
.pill-alarm .pill-dot { background: var(--t-error); }
.pill-ok { color: var(--t-ok); }
.pill-warn { color: var(--t-warn); }
.pill-problem { color: var(--t-error); }
.pill-alarm { color: var(--t-error); }

/* Inline stats: caption beside value (single-row hero). */
.stat { display: inline-flex; align-items: baseline; gap: 5px; line-height: 1.15; }
.hero .stat-cap { font-size: 11px; color: var(--t-muted); letter-spacing: .02em; }
.hero .stat-val { font-size: 15px; font-weight: 600; }
.stat.sev-ok .stat-val { color: var(--t-ok); }
.stat.sev-info .stat-val { color: var(--t-info); }
.stat.sev-warn .stat-val { color: var(--t-warn); }
.stat.sev-problem .stat-val { color: var(--t-error); }
.stat.sev-alarm .stat-val { color: var(--t-error); }
/* Flags stat = shortcut to the Flags tab. */
.flags-stat {
  cursor: pointer; padding: 4px 12px;
  border-radius: 999px; border: 1px solid transparent;
}
.flags-stat:hover { background: var(--t-chip); border-color: var(--t-line); }
.flags-stat:focus-visible { outline: 2px solid var(--t-primary); outline-offset: 1px; }
.chip-dew {
  display: inline-flex; align-items: baseline; gap: 6px;
  padding: 4px 12px; border-radius: 999px;
  background: color-mix(in srgb, var(--t-info) 14%, transparent);
  border: 1px solid var(--t-info);
}
.chip-dew .stat-val { font-size: 14px; color: var(--t-info); }

.ctl { display: inline-flex; align-items: center; gap: 8px; }
.ctl-cap { font-size: 12px; color: var(--t-muted); letter-spacing: .02em; }
.stepper {
  display: inline-flex; align-items: center; gap: 2px;
  border: 1px solid var(--t-line); border-radius: 8px; height: 32px; padding: 0 4px; background: var(--t-card);
}
.step-val { min-width: 62px; text-align: center; font-size: 15px; font-weight: 600; }
.seg { display: inline-flex; height: 32px; border: 1px solid var(--t-line); border-radius: 8px; overflow: hidden; }
.seg-btn {
  border: 0; background: var(--t-card); color: var(--t-fg);
  padding: 0 14px; font-size: 13px; border-right: 1px solid var(--t-line);
  display: inline-flex; align-items: center;
}
.seg-btn:last-child { border-right: 0; }
.seg-btn:hover { background: var(--t-chip); }
.seg-btn.active { background: var(--t-primary); color: var(--t-on-primary); }

/* Tab bar */
.tabbar {
  display: flex; gap: 2px; overflow-x: auto;
  border-bottom: 1px solid var(--t-line); margin-bottom: 16px;
}
.tab {
  border: 0; background: transparent; color: var(--t-muted);
  padding: 10px 16px; font-size: 14px; font-weight: 600; white-space: nowrap;
  border-bottom: 2px solid transparent; margin-bottom: -1px;
}
.tab:hover { color: var(--t-fg); }
.tab.active { color: var(--t-primary); border-bottom-color: var(--t-primary); }
.tab:focus-visible { outline: 2px solid var(--t-primary); outline-offset: -2px; border-radius: 6px; }
.tab-section { min-width: 0; }

/* Overview tab (dashboard): one screen of house-level aggregates (the
   per-room detail lives on the Rooms tab), product-grade. Colour = meaning
   only (blue too cold, red too warm, a light green comfort band, grey in
   band); text stays in text tokens. A container query (not the viewport)
   stacks the columns, so an open inspector or a collapsed HA sidebar
   re-flow it correctly. */
.ov {
  container: ov / inline-size;
  --ov-line: color-mix(in srgb, var(--t-fg) 9%, transparent);
  --ov-soft: color-mix(in srgb, var(--t-fg) 5%, transparent);
  --ov-shadow: 0 1px 2px rgba(0, 0, 0, .05), 0 10px 28px -16px rgba(0, 0, 0, .18);
}
.ov-layout {
  display: grid; gap: 16px; align-items: start;
  grid-template-columns: minmax(0, 1fr) minmax(290px, 340px);
  grid-template-areas: "main side";
}
.ov-main, .ov-side { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.ov-main { grid-area: main; }
.ov-side { grid-area: side; }
.ov-card {
  background: var(--t-card); border: 1px solid var(--ov-line); border-radius: 16px;
  box-shadow: var(--ov-shadow); padding: 16px 18px; min-width: 0;
}
.ov-eyebrow {
  display: flex; align-items: center; gap: 6px; margin-bottom: 8px;
  font-size: 11px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase;
  color: var(--t-muted);
}
.ov-eyebrow .info-btn { margin-left: 0; }
.ov-eyebrow .chip { margin-left: auto; letter-spacing: 0; text-transform: none; }
/* Hero: comfort (left) beside what needs attention (right) */
.ov-hero {
  display: grid; gap: 14px 24px; align-items: stretch;
  grid-template-columns: minmax(0, 1.1fr) minmax(0, .9fr); padding: 16px 20px;
  background:
    radial-gradient(130% 170% at 0% 0%, color-mix(in srgb, var(--t-primary) 13%, transparent), transparent 55%),
    var(--t-card);
}
.ov-hero-comfort, .ov-hero-att { display: flex; flex-direction: column; min-width: 0; }
.ov-hero-att { padding-left: 24px; border-left: 1px solid var(--ov-line); }
.ov-hero-fig { display: flex; align-items: baseline; flex-wrap: wrap; gap: 2px 12px; }
.ov-hero-val {
  font-size: 48px; font-weight: 600; line-height: 1; letter-spacing: -.035em;
  font-variant-numeric: tabular-nums;
}
.ov-hero-sub { font-size: 14px; line-height: 1.35; color: var(--t-muted); max-width: 22ch; }
.ov-counts { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.ov-count {
  display: inline-flex; align-items: center; gap: 6px; height: 24px; padding: 0 10px;
  border-radius: 999px; background: var(--ov-soft); font-size: 12px;
}
.ov-count i { width: 8px; height: 8px; border-radius: 50%; background: var(--t-muted); }
.ov-count b { font-weight: 700; font-variant-numeric: tabular-nums; }
.ov-count.cold i { background: var(--t-cool); }
.ov-count.warm i { background: var(--t-heat); }
.ov-count.nodata i { background: transparent; box-shadow: inset 0 0 0 1.5px var(--t-muted); }
.ov-count.off i { background: var(--t-disabled); }
.ov-count.passive i { background: color-mix(in srgb, var(--t-cool) 35%, transparent); }
.ov-strip { display: flex; flex-direction: column; gap: 3px; min-width: 0; margin-top: auto; padding-top: 8px; }
.ov-strip-cap { margin-left: auto; font-weight: 400; letter-spacing: 0; text-transform: none; }
.ov-strip-plot { position: relative; height: 29px; margin: 0 12px; border-bottom: 1px solid var(--ov-line); }
.ov-strip-band {
  position: absolute; top: 0; bottom: 0; border-radius: 6px 6px 0 0;
  background: color-mix(in srgb, var(--t-ok) 16%, transparent);
}
.ov-strip-zero {
  position: absolute; top: 0; bottom: 0; left: 50%; width: 1px;
  background: color-mix(in srgb, var(--t-fg) 18%, transparent);
}
.ov-dot {
  position: absolute; width: 24px; height: 24px; margin-left: -12px; padding: 0;
  border: 0; border-radius: 50%; background: transparent;
  display: inline-flex; align-items: center; justify-content: center;
}
.ov-dot i {
  width: 13px; height: 13px; border-radius: 50%; background: var(--t-muted);
  box-shadow: 0 0 0 2px var(--t-card); transition: transform .12s ease;
}
.ov-dot.z-cold i { background: var(--t-cool); }
.ov-dot.z-warm i { background: var(--t-heat); }
.ov-dot:hover { z-index: 1; }
.ov-dot:hover i { transform: scale(1.3); }
.ov-dot:focus-visible { outline: 2px solid var(--t-primary); outline-offset: -2px; z-index: 1; }
.ov-dot.selected i { box-shadow: 0 0 0 2px var(--t-card), 0 0 0 4px var(--t-primary); }
.ov-strip-ticks {
  position: relative; height: 14px; margin: 0 12px;
  font-size: 10px; color: var(--t-muted); font-variant-numeric: tabular-nums;
}
.ov-strip-ticks span { position: absolute; top: 0; transform: translateX(-50%); white-space: nowrap; }
/* Attention (the hero's right half): one line per flag, worst first */
.ov-att-list { display: flex; flex-direction: column; gap: 6px; }
.ov-att-row {
  display: flex; align-items: center; gap: 8px; min-width: 0;
  padding: 2px 10px 2px 2px; border-radius: 10px;
  background: color-mix(in srgb, var(--t-warn) 8%, transparent);
}
.ov-att-row .ov-ichip, .ov-att-ok .ov-ichip { width: 26px; height: 26px; border-radius: 8px; }
.ov-att-row .ov-ichip .hicon, .ov-att-ok .ov-ichip .hicon { --mdc-icon-size: 16px; }
.ov-att-row.sev-problem { background: color-mix(in srgb, var(--t-error) 7%, transparent); }
.ov-att-row.sev-alarm { background: color-mix(in srgb, var(--t-error) 12%, transparent); }
.ov-att-head {
  flex: 1 1 auto; min-width: 0; display: flex; align-items: center; gap: 6px;
  font-size: 13px; font-weight: 600; line-height: 1.35; white-space: nowrap;
}
.ov-att-head > :not(.ov-att-label) { flex: none; }
.ov-att-head .info-btn { margin-left: 0; }
.ov-att-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.ov-att-row .flag-sx { background: var(--t-warn); color: var(--t-on-primary); }
.ov-att-row.sev-problem .flag-sx, .ov-att-row.sev-alarm .flag-sx { background: var(--t-error); }
.ov-att-where { flex: none; font-size: 12px; color: var(--t-muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
.ov-link {
  border: 0; background: none; padding: 0; font: inherit; color: var(--t-muted); cursor: pointer;
  text-decoration: underline; text-decoration-color: color-mix(in srgb, var(--t-fg) 22%, transparent);
  text-underline-offset: 3px;
}
.ov-link:hover { color: var(--t-primary); text-decoration-color: currentColor; }
.ov-link:focus-visible { outline: 2px solid var(--t-primary); outline-offset: 1px; border-radius: 3px; }
.ov-att-more { align-self: flex-start; margin-left: 6px; font-size: 12px; }
.ov-att-ok { display: flex; align-items: center; gap: 10px; }
.ov-att-ok > div { display: flex; align-items: baseline; flex-wrap: wrap; gap: 2px 10px; }
.ov-att-ok-title { font-size: 14px; font-weight: 600; }
.ov-att-ok-sub { font-size: 12px; color: var(--t-muted); }
.ov-att-info {
  margin-top: 8px; font-size: 12px; line-height: 1.45; color: var(--t-muted);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.ov-att-info-cap { font-weight: 600; }
/* House charts: three small multiples on one 24 h time axis */
.ov-charts { display: grid; gap: 14px; grid-template-columns: repeat(3, minmax(0, 1fr)); }
.ov-chart-card { display: flex; flex-direction: column; padding: 14px 16px 10px; }
.ov-chart-fig { display: flex; align-items: baseline; flex-wrap: wrap; gap: 0 8px; margin-bottom: 12px; }
.ov-chart-val {
  font-size: 26px; font-weight: 600; line-height: 1.15; letter-spacing: -.02em;
  font-variant-numeric: tabular-nums; white-space: nowrap;
}
.ov-chart-sub { font-size: 12px; line-height: 1.35; color: var(--t-muted); }
.ov-chart { position: relative; height: 92px; margin: auto 0 0 34px; }
.ov-chart-box { position: absolute; inset: 0; }
.ov-chart-y {
  position: absolute; top: 0; bottom: 0; right: 100%; margin-right: 6px; white-space: nowrap;
  font-size: 10px; color: var(--t-muted); font-variant-numeric: tabular-nums;
}
.ov-chart-y span { position: absolute; right: 0; }
.ov-chart-y span:first-child { top: -6px; }
.ov-chart-y span:last-child { bottom: -6px; }
.ov-chart-guide {
  position: absolute; top: 0; bottom: 0; width: 1px; margin-left: -.5px; pointer-events: none;
  background: color-mix(in srgb, var(--t-fg) 40%, transparent);
}
.ov-chart-tip {
  position: absolute; top: -8px; transform: translateY(-100%); z-index: 2; pointer-events: none;
  width: max-content; max-width: calc(100% + 32px);
  padding: 4px 8px; border-radius: 8px; font-size: 11px; line-height: 1.3;
  background: var(--t-fg); color: var(--t-card); box-shadow: var(--ov-shadow);
}
.ov-chart-now {
  position: absolute; right: 0; width: 7px; height: 7px; margin: -3.5px -3.5px 0 0; pointer-events: none;
  border-radius: 50%; background: color-mix(in srgb, var(--t-fg) 70%, transparent);
  box-shadow: 0 0 0 2px var(--t-card);
}
.ov-chart-now.primary { background: var(--t-primary); }
/* The time-label row is the container a narrow plot is detected by (as
   wide as the plot; a container on the flex card broke its height). */
.ov-chart-x {
  container: ovchart / inline-size;
  position: relative; height: 14px; margin: 5px 0 0 34px;
  font-size: 10px; color: var(--t-muted); font-variant-numeric: tabular-nums;
}
.ov-chart-x span { position: absolute; top: 0; transform: translateX(-50%); white-space: nowrap; }
.ov-svg { display: block; width: 100%; height: 100%; overflow: visible; }
.ov-band { fill: color-mix(in srgb, var(--t-ok) 18%, transparent); }
.ov-zero { stroke: color-mix(in srgb, var(--t-fg) 16%, transparent); stroke-width: 1; vector-effect: non-scaling-stroke; }
.ov-env { fill: color-mix(in srgb, var(--t-fg) 10%, transparent); }
.ov-warm { fill: color-mix(in srgb, var(--t-heat) 30%, transparent); }
.ov-cold { fill: color-mix(in srgb, var(--t-cool) 30%, transparent); }
.ov-area { fill: color-mix(in srgb, var(--t-primary) 16%, transparent); }
.ov-line {
  fill: none; stroke: color-mix(in srgb, var(--t-fg) 70%, transparent); stroke-width: 1.75;
  vector-effect: non-scaling-stroke; stroke-linejoin: round; stroke-linecap: round;
}
.ov-area ~ .ov-line { stroke: var(--t-primary); }
/* Manifolds */
.ov-mf { cursor: pointer; transition: border-color .12s ease; }
.ov-mf:hover { border-color: color-mix(in srgb, var(--t-primary) 55%, transparent); }
.ov-mf:focus-visible { outline: 2px solid var(--t-primary); outline-offset: 2px; }
.ov-mf-list { display: flex; flex-direction: column; gap: 10px; }
.ov-mf-row {
  display: grid; grid-template-columns: minmax(0, 76px) minmax(0, 1fr) auto;
  align-items: center; gap: 10px;
}
.ov-mf-name { font-size: 13px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ov-mf-temps { font-size: 12px; color: var(--t-muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
.ov-mf-slots { display: flex; gap: 3px; height: 22px; }
.ov-mf-slot {
  flex: 1 1 0; max-width: 22px; border-radius: 5px; background: var(--ov-soft);
  display: flex; align-items: flex-end; overflow: hidden;
}
.ov-mf-slot i { display: block; width: 100%; height: 0; background: var(--t-primary); border-radius: 4px 4px 0 0; transition: height .3s ease; }
.ov-mf-slot.free { background: transparent; box-shadow: inset 0 0 0 1px var(--ov-line); }
.ov-mf-slot.flag { box-shadow: inset 0 0 0 1.5px var(--t-error); }
/* Icon chips (attention, plant) */
.ov-ichip {
  flex: none; width: 30px; height: 30px; border-radius: 9px;
  display: inline-flex; align-items: center; justify-content: center;
  color: var(--t-primary); background: color-mix(in srgb, var(--t-primary) 13%, transparent);
}
.ov-ichip .hicon { --mdc-icon-size: 18px; color: inherit; }
.ov-ichip .hicon-fallback { color: inherit; }
.ov-ichip.ok { color: var(--t-ok); background: color-mix(in srgb, var(--t-ok) 15%, transparent); }
.ov-ichip.warn { color: var(--t-warn); background: color-mix(in srgb, var(--t-warn) 16%, transparent); }
.ov-ichip.problem { color: var(--t-error); background: color-mix(in srgb, var(--t-error) 14%, transparent); }
/* Plant rows */
.ov-plant { display: flex; flex-direction: column; gap: 2px; }
.ov-plant .ov-eyebrow { margin-bottom: 6px; }
.ov-prow { display: flex; align-items: center; gap: 12px; padding: 6px 8px; margin: 0 -8px; border-radius: 12px; }
.ov-prow.link { cursor: pointer; transition: background-color .12s ease; }
.ov-prow.link:hover { background: var(--ov-soft); }
.ov-prow.link:focus-visible { outline: 2px solid var(--t-primary); outline-offset: -2px; }
.ov-prow-body { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; gap: 1px; }
.ov-prow-cap { font-size: 12px; color: var(--t-muted); }
.ov-prow-fig { display: flex; align-items: baseline; flex-wrap: wrap; gap: 0 8px; }
.ov-prow-val {
  font-size: 19px; font-weight: 600; letter-spacing: -.01em; line-height: 1.25;
  font-variant-numeric: tabular-nums; white-space: nowrap;
}
.ov-prow-val.text { font-size: 15px; }
.ov-prow-sub { font-size: 12px; color: var(--t-muted); }
.ov-prow-extra { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: 4px; }
.ov-prow-sub:empty, .ov-prow-extra:empty { display: none; }
.ov-mode { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; }
.ov-mode::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: var(--t-accent); }
.ov-mode.mode-heating::before { background: var(--t-heat); }
.ov-mode.mode-cooling::before { background: var(--t-cool); }
.ov-mode.mode-dry::before { background: var(--t-dry); }
.ov-layout.no-side { grid-template-columns: minmax(0, 1fr); grid-template-areas: "main"; }
.ov-layout.no-side .ov-side { display: none; }
/* Below ~1100 px the main column would drop under ~750 px next to the side
   column (cramped hero, unreadable chart axes): stack, side cards in a row. */
@container ov (max-width: 1100px) {
  .ov-layout { grid-template-columns: minmax(0, 1fr); grid-template-areas: "main" "side"; }
  .ov-side { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); align-items: start; }
}
/* A narrow plot keeps every other time label (12 h apart). */
@container ovchart (max-width: 150px) {
  .ov-chart-x span:nth-child(even) { display: none; }
}
@container ov (max-width: 620px) {
  .ov-hero { grid-template-columns: minmax(0, 1fr); }
  .ov-hero-att { padding: 14px 0 0; border-left: 0; border-top: 1px solid var(--ov-line); }
  .ov-hero-val { font-size: 44px; }
  .ov-charts { grid-template-columns: minmax(0, 1fr); }
}
@media (prefers-reduced-motion: reduce) {
  .ov-dot i, .ov-mf-slot i { transition: none; }
}

/* Tuning tab */
.tune { display: flex; flex-direction: column; gap: 14px; }
.tune-intro { font-size: 12px; color: var(--t-muted); line-height: 1.4; }
/* Scope selector as underline tabs (like the main .tabbar / .tab). */
.tune-scopes {
  display: flex; gap: 2px; overflow-x: auto;
  border-bottom: 1px solid var(--t-line);
}
.tune-scope {
  border: 0; background: transparent; color: var(--t-muted);
  padding: 9px 14px; font-size: 13px; font-weight: 600; white-space: nowrap;
  border-bottom: 2px solid transparent; margin-bottom: -1px;
}
.tune-scope:hover { color: var(--t-fg); }
.tune-scope.active { color: var(--t-primary); border-bottom-color: var(--t-primary); }
.tune-scope:focus-visible { outline: 2px solid var(--t-primary); outline-offset: -2px; border-radius: 6px; }
/* Grouped vertical parameter list (A4): titled sections, one knob per row. */
.tune-body { display: flex; flex-direction: column; gap: 14px; max-width: 860px; }
.tune-group {
  border: 1px solid var(--t-line); border-radius: var(--t-radius);
  background: var(--t-card); overflow: hidden;
}
.tune-group-cap {
  padding: 10px 14px; font-size: 13px; font-weight: 700;
  border-bottom: 1px solid var(--t-line);
  background: color-mix(in srgb, var(--t-chip) 55%, transparent);
}
.tune-card {
  display: flex; align-items: center; gap: 12px;
  padding: 10px 14px; border-bottom: 1px solid var(--t-line);
}
.tune-card:last-child { border-bottom: 0; }
.tune-card.inherited { opacity: .62; }
.tune-card-head {
  flex: 1 1 auto; display: flex; align-items: baseline; flex-wrap: wrap; gap: 6px;
}
.tune-label { font-size: 13px; font-weight: 600; color: var(--t-fg); }
.tune-unit { font-size: 11px; color: var(--t-muted); }
.tune-badge {
  font-size: 10px; text-transform: uppercase; letter-spacing: .04em;
  color: var(--t-primary);
  background: color-mix(in srgb, var(--t-primary) 16%, transparent);
  border-radius: 999px; padding: 2px 8px;
}
.tune-badge-legacy {
  color: var(--t-warn);
  background: color-mix(in srgb, var(--t-warn) 16%, transparent);
  cursor: help;
}
.tune-revert {
  margin-left: auto; border: 1px solid var(--t-line); background: var(--t-card);
  color: var(--t-muted); border-radius: 8px; width: 28px; height: 28px;
  font-size: 15px; line-height: 1; display: inline-flex; align-items: center; justify-content: center;
}
.tune-revert:hover { border-color: var(--t-primary); color: var(--t-primary); }
.tune-toggle {
  border: 1px solid var(--t-line); background: var(--t-card);
  color: var(--t-fg); border-radius: 999px; padding: 6px 16px; font-size: 13px; font-weight: 600;
  min-width: 68px; flex: none;
}
.tune-toggle.on { background: var(--t-primary); color: var(--t-on-primary); border-color: var(--t-primary); }
.tune-footer { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.tune-save {
  border: 1px solid var(--t-primary); background: var(--t-primary); color: var(--t-on-primary);
  border-radius: 8px; padding: 8px 20px; font-size: 14px; font-weight: 600;
}
.tune-save:hover:not(:disabled) { filter: brightness(1.05); }
.tune-save:disabled { opacity: .5; cursor: default; }
.tune-note { font-size: 12px; color: var(--t-muted); }

/* Layout: side-by-side inspector on wide screens, overlay drawer on narrow. */
.layout { display: grid; gap: 16px; grid-template-columns: 1fr; align-items: start; }
.col-main { min-width: 0; }
.empty { padding: 40px 16px; text-align: center; color: var(--t-muted); }
.detail-scrim { display: none; }
@media (min-width: ${OVERLAY_MAX_PX + 1}px) {
  .layout.has-detail { grid-template-columns: minmax(0, 1fr) clamp(460px, 40%, 560px); }
  .layout.has-detail .detail {
    position: sticky; top: 16px;
    max-height: calc(100vh - 32px); overflow: auto;
  }
}
@media (max-width: ${OVERLAY_MAX_PX}px) {
  .layout.has-detail .detail-scrim {
    display: block; position: fixed; inset: 0; z-index: 6;
    background: var(--mdc-dialog-scrim-color, rgba(0, 0, 0, 0.32));
  }
  .layout.has-detail .detail {
    position: fixed; top: 0; right: 0; bottom: 0; z-index: 7;
    width: min(480px, 100%);
    border: 0; border-left: 1px solid var(--t-line); border-radius: 0;
    overflow-y: auto;
    box-shadow: 0 0 32px rgba(0, 0, 0, 0.35);
    animation: detail-slide-in .2s ease;
  }
  .layout.has-detail .detail .detail-head { border-radius: 0; }
}
@keyframes detail-slide-in {
  from { transform: translateX(32px); opacity: .4; }
  to { transform: none; opacity: 1; }
}
@media (prefers-reduced-motion: reduce) {
  .layout.has-detail .detail { animation: none; }
}

/* Room table */
.table-wrap {
  overflow-x: auto;
  border: 1px solid var(--t-line); border-radius: var(--t-radius);
  background: var(--t-card);
}
.rooms-table { width: 100%; border-collapse: collapse; font-size: 13px; }
/* Sentence-case headers (A1): no uppercase transform, one step larger. */
.rooms-table thead th {
  position: sticky; top: 0; z-index: 1;
  background: var(--t-card); text-align: left; white-space: nowrap;
  font-size: 12px; font-weight: 600;
  color: var(--t-muted); padding: 8px 10px; border-bottom: 1px solid var(--t-line);
}
/* Two-row header (A3): the grouped assist header is centred and underlined;
   the sub-row sticks right below the first row (fixed first-row height). */
.rooms-table thead tr:first-child th { height: 34px; }
.rooms-table thead th.col-assist-group {
  text-align: center; border-bottom: 1px solid var(--t-line);
}
.rooms-table thead tr.subhead th { top: 34px; }
.rooms-table tbody td {
  padding: 8px 10px; border-bottom: 1px solid var(--t-line); vertical-align: middle;
}
.rooms-table tbody tr:last-child td { border-bottom: 0; }
.rooms-table tbody tr { cursor: pointer; transition: background-color .1s ease; }
.rooms-table tbody tr:hover { background: var(--t-chip); }
.rooms-table tbody tr:focus-visible { outline: 2px solid var(--t-primary); outline-offset: -2px; }
.rooms-table tbody tr.selected { box-shadow: inset 3px 0 0 var(--t-primary); }
.rooms-table tbody tr.off { opacity: .55; }

/* Two-state segment control (column 1) */
.col-state { width: 1%; }
.seg-state {
  display: inline-flex; border: 1px solid var(--t-line);
  border-radius: 8px; overflow: hidden; background: var(--t-card);
}
.seg-state-btn {
  border: 0; background: transparent; color: var(--t-muted);
  padding: 4px 8px; display: inline-flex; align-items: center; justify-content: center;
  border-right: 1px solid var(--t-line);
}
.seg-state-btn:last-child { border-right: 0; }
.seg-state-btn:hover { background: var(--t-chip); color: var(--t-fg); }
.seg-state-btn.active { background: var(--t-primary); color: var(--t-on-primary); }
.seg-state-btn .hicon { --mdc-icon-size: 16px; color: inherit; }
.seg-state-btn .hicon-fallback { color: inherit; font-size: 13px; }

.dot { width: 11px; height: 11px; border-radius: 50%; flex: none; background: var(--t-muted); }
.dot.sev-ok { background: var(--t-ok); }
.dot.sev-info { background: var(--t-info); }
.dot.sev-warn { background: var(--t-warn); }
.dot.sev-problem { background: var(--t-error); }
.dot.sev-alarm { background: var(--t-error); box-shadow: 0 0 0 3px color-mix(in srgb, var(--t-error) 30%, transparent); }
.name-cell { display: flex; align-items: center; gap: 8px; }
.card-name { font-weight: 600; font-size: 14px; }
.cell-sub { font-size: 11px; color: var(--t-muted); }
.meas-cell { display: flex; align-items: baseline; gap: 6px; }
.meas-val { font-size: 15px; font-weight: 600; }
.sp-cell { display: flex; flex-direction: column; gap: 1px; }
.sp-val { font-weight: 600; }
.err-val { font-weight: 600; white-space: nowrap; }
.trend { font-size: 12px; color: var(--t-muted); white-space: nowrap; }
.valve-val { font-size: 13px; font-weight: 600; }
.valve-track { height: 6px; border-radius: 999px; background: var(--t-chip); overflow: hidden; }
.valve-fill { display: block; height: 100%; width: 0%; background: var(--t-primary); border-radius: 999px; transition: width .3s ease; }
.fast-badge { font-size: 12px; padding: 4px 10px; border-radius: 8px; display: inline-block; white-space: nowrap; }
.fast-badge.on { background: color-mix(in srgb, var(--t-accent) 16%, transparent); color: var(--t-accent); border: 1px solid var(--t-accent); }
.fast-badge.on.mode-heating { background: color-mix(in srgb, var(--t-heat) 16%, transparent); color: var(--t-heat); border-color: var(--t-heat); }
.fast-badge.on.mode-cooling { background: color-mix(in srgb, var(--t-cool) 16%, transparent); color: var(--t-cool); border-color: var(--t-cool); }
.fast-badge.on.mode-dry { background: color-mix(in srgb, var(--t-dry) 16%, transparent); color: var(--t-dry); border-color: var(--t-dry); }
.fast-badge.off { color: var(--t-muted); border: 1px solid var(--t-line); }
.fast-badge.none { color: var(--t-muted); border: 0; padding-left: 0; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { font-size: 11px; padding: 3px 9px; border-radius: 999px; background: var(--t-chip); color: var(--t-fg); }
.chip-warn { background: color-mix(in srgb, var(--t-warn) 20%, transparent); color: var(--t-warn); }
.chip-problem { background: color-mix(in srgb, var(--t-error) 18%, transparent); color: var(--t-error); }
.chip-alarm { background: color-mix(in srgb, var(--t-error) 30%, transparent); color: var(--t-error); font-weight: 600; }

/* Flag annunciator (Rooms tab): a collapsible, grouped LIST styled like the
   Tuning tab — one flag per row, dimmed until a room raises it. */
details.flag-legend { margin-bottom: 14px; }
details.flag-legend > summary { padding: 4px 2px; font-size: 13px; }
details.flag-legend[open] > summary { margin-bottom: 12px; }
.flag-legend-head { min-width: 0; }
.flag-legend-title { font-size: 13px; font-weight: 700; }
.flag-legend-summary {
  margin-left: auto; font-size: 12px; font-weight: 600; color: var(--t-muted);
  padding: 2px 10px; border-radius: 999px; background: var(--t-chip); white-space: nowrap;
}
.flag-legend-summary.sev-ok { color: var(--t-ok); background: color-mix(in srgb, var(--t-ok) 16%, transparent); }
.flag-legend-summary.sev-info { color: var(--t-info); background: color-mix(in srgb, var(--t-info) 16%, transparent); }
.flag-legend-summary.sev-warn { color: var(--t-warn); background: color-mix(in srgb, var(--t-warn) 16%, transparent); }
.flag-legend-summary.sev-problem { color: var(--t-error); background: color-mix(in srgb, var(--t-error) 16%, transparent); }
.flag-legend-summary.sev-alarm { color: var(--t-error); background: color-mix(in srgb, var(--t-error) 26%, transparent); }
.flag-legend-groups { display: flex; flex-direction: column; gap: 14px; max-width: 860px; }
/* Group card + header mirror the Tuning tab's .tune-group / .tune-group-cap. */
.flag-grp {
  border: 1px solid var(--t-line); border-radius: var(--t-radius);
  background: var(--t-card); overflow: hidden;
}
.flag-grp-title {
  padding: 10px 14px; font-size: 13px; font-weight: 700;
  border-bottom: 1px solid var(--t-line);
  background: color-mix(in srgb, var(--t-chip) 55%, transparent);
}
.flag-row {
  display: flex; align-items: center; gap: 12px;
  padding: 9px 14px; border-bottom: 1px solid var(--t-line);
  border-left: 3px solid transparent;
  transition: opacity .15s ease, border-color .15s ease, background .15s ease;
}
.flag-row:last-child { border-bottom: 0; }
.flag-row.is-off { opacity: .5; }
.flag-row-head { flex: 1 1 auto; display: flex; align-items: center; flex-wrap: wrap; gap: 8px; min-width: 0; }
.flag-row-label { font-size: 13px; font-weight: 600; color: var(--t-fg); }
.flag-row .info-btn { margin-left: 0; }
.flag-sx {
  font-size: 10px; font-weight: 700; letter-spacing: .02em;
  padding: 1px 6px; border-radius: 5px; background: var(--t-chip); color: var(--t-muted);
}
.flag-row-status {
  margin-left: auto; font-size: 12px; font-weight: 600; color: var(--t-muted);
  white-space: nowrap; display: inline-flex; align-items: center; gap: 6px;
}
.flag-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--t-muted); flex: none; }
/* Lit rows: a left severity stripe, tinted ground, coloured SX badge + status. */
.flag-row.is-on.sev-ok { border-left-color: var(--t-ok); background: color-mix(in srgb, var(--t-ok) 8%, transparent); }
.flag-row.is-on.sev-ok .flag-sx { background: var(--t-ok); color: var(--t-on-primary); }
.flag-row.is-on.sev-ok .flag-row-status { color: var(--t-ok); }
.flag-row.is-on.sev-ok .flag-dot { background: var(--t-ok); }
.flag-row.is-on.sev-info { border-left-color: var(--t-info); background: color-mix(in srgb, var(--t-info) 8%, transparent); }
.flag-row.is-on.sev-info .flag-sx { background: var(--t-info); color: var(--t-on-primary); }
.flag-row.is-on.sev-info .flag-row-status { color: var(--t-info); }
.flag-row.is-on.sev-info .flag-dot { background: var(--t-info); }
.flag-row.is-on.sev-warn { border-left-color: var(--t-warn); background: color-mix(in srgb, var(--t-warn) 9%, transparent); }
.flag-row.is-on.sev-warn .flag-sx { background: var(--t-warn); color: var(--t-on-primary); }
.flag-row.is-on.sev-warn .flag-row-status { color: var(--t-warn); }
.flag-row.is-on.sev-warn .flag-dot { background: var(--t-warn); }
.flag-row.is-on.sev-problem { border-left-color: var(--t-error); background: color-mix(in srgb, var(--t-error) 9%, transparent); }
.flag-row.is-on.sev-problem .flag-sx { background: var(--t-error); color: var(--t-on-primary); }
.flag-row.is-on.sev-problem .flag-row-status { color: var(--t-error); }
.flag-row.is-on.sev-problem .flag-dot { background: var(--t-error); }
.flag-row.is-on.sev-alarm { border-left-color: var(--t-error); background: color-mix(in srgb, var(--t-error) 15%, transparent); }
.flag-row.is-on.sev-alarm .flag-sx { background: var(--t-error); color: var(--t-on-primary); }
.flag-row.is-on.sev-alarm .flag-row-status { color: var(--t-error); font-weight: 700; }
.flag-row.is-on.sev-alarm .flag-dot { background: var(--t-error); box-shadow: 0 0 0 3px color-mix(in srgb, var(--t-error) 28%, transparent); }

/* Assist table (shares the room-table look) */
.assist-table { width: 100%; border-collapse: collapse; font-size: 13px; }
/* Sentence-case headers (A1) — consistent with the Rooms table. */
.assist-table thead th {
  position: sticky; top: 0; z-index: 1; background: var(--t-card); text-align: left; white-space: nowrap;
  font-size: 12px; font-weight: 600;
  color: var(--t-muted); padding: 8px 10px; border-bottom: 1px solid var(--t-line);
}
.assist-table tbody td {
  padding: 8px 10px; border-bottom: 1px solid var(--t-line); vertical-align: middle;
}
.assist-row { cursor: pointer; transition: background-color .1s ease; }
.assist-row:hover { background: var(--t-chip); }
.assist-row:focus-visible { outline: 2px solid var(--t-primary); outline-offset: -2px; }
.assist-row.selected { box-shadow: inset 3px 0 0 var(--t-primary); }
.mono { font-family: ui-monospace, "Roboto Mono", monospace; font-size: 12px; font-weight: 600; white-space: nowrap; }
.mono.mode-heating { color: var(--t-heat); }
.mono.mode-cooling { color: var(--t-cool); }
.mono.mode-dry { color: var(--t-dry); }
.mono.mismatch { color: var(--t-warn); }
.loop-link { border: 0; background: transparent; color: var(--t-primary); cursor: pointer; display: inline-flex; align-items: center; gap: 3px; padding: 0; font: inherit; }
.loop-link:hover { text-decoration: underline; }
.loop-link .hicon, .loop-link .hicon-fallback { --mdc-icon-size: 14px; opacity: .6; }
.assist-actions { display: inline-flex; align-items: center; gap: 10px; }
.link-btn { border: 0; background: transparent; color: var(--t-primary); cursor: pointer; font: inherit; font-size: 12px; padding: 0; text-decoration: underline; }
.assist-none { font-size: 12px; margin-top: 10px; }

/* Narrow sidebar: drop the least-critical columns (data stays in the detail);
   Measured/Setpoint/Valve/Assist-mode stay visible. The "Wspomaganie" group
   header keeps its colspan=2 — with the Temp. column hidden the browser
   clamps it over the remaining Tryb sub-column. */
@media (max-width: ${NARROW_MAX_PX}px) {
  .rooms-table .col-error,
  .rooms-table .col-supply,
  .rooms-table .col-return,
  .rooms-table .col-assist-temp { display: none; }
}

/* Detail (room inspector) */
.detail {
  background: var(--t-card); border: 1px solid var(--t-line);
  border-radius: var(--t-radius); padding: 0;
  display: flex; flex-direction: column;
}
.detail:focus { outline: none; }
.detail-head {
  position: sticky; top: 0; z-index: 2;
  display: flex; align-items: center; gap: 10px;
  padding: 12px 18px; background: var(--t-card);
  border-bottom: 1px solid var(--t-line);
  border-radius: var(--t-radius) var(--t-radius) 0 0;
}
.detail-title { font-size: 18px; font-weight: 700; line-height: 1.2; overflow-wrap: anywhere; }
.detail-close { padding: 6px; }
.detail-body { display: flex; flex-direction: column; gap: 20px; padding: 16px 18px 20px; min-width: 0; }
.detail-flags { display: flex; }
.sub { display: flex; flex-direction: column; gap: 10px; }
.sub-title { font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: .06em; color: var(--t-muted); border-bottom: 1px solid var(--t-line); padding-bottom: 6px; }
details.sub-fold > summary { cursor: pointer; list-style: none; display: flex; align-items: center; gap: 7px; }
details.sub-fold > summary::-webkit-details-marker { display: none; }
details.sub-fold > summary::before { content: "▸"; font-size: 10px; line-height: 1; transition: transform .15s ease; }
details.sub-fold[open] > summary::before { transform: rotate(90deg); }
details.sub-fold > summary:focus-visible { outline: 2px solid var(--t-primary); outline-offset: 2px; border-radius: 4px; }

/* Detail stat tiles (the four key numbers) */
.stat-tiles { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.stat-tile {
  display: flex; flex-direction: column; gap: 6px; min-width: 0;
  padding: 10px 12px; border-radius: 12px;
  background: var(--t-bg); border: 1px solid var(--t-line);
}
.stat-tile .stat-cap { font-size: 11px; color: var(--t-muted); text-transform: uppercase; letter-spacing: .04em; }
.stat-tile .stat-val { font-size: 23px; font-weight: 700; line-height: 1.15; font-variant-numeric: tabular-nums; }
.stat-sub { font-size: 12px; color: var(--t-muted); }
.stat-tile .stepper { align-self: flex-start; background: var(--t-card); }
.stat-tile .step-val { font-size: 17px; min-width: 68px; }
.stat-track { max-width: 150px; margin-top: 2px; }
.stat-badge { align-self: flex-start; white-space: normal; line-height: 1.35; }

/* Wiring */
.wire-list { display: flex; flex-direction: column; gap: 2px; }
.wire-group-cap { font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: var(--t-muted); margin: 8px 0 2px; }
.wire-row { display: flex; align-items: center; gap: 8px; padding: 7px 8px; border-radius: 8px; }
.wire-row.link { cursor: pointer; }
.wire-row.link:hover { background: var(--t-chip); }
.wire-row.link:focus-visible { outline: 2px solid var(--t-primary); outline-offset: -2px; }
.wire-role { font-size: 12px; color: var(--t-muted); min-width: 118px; }
.wire-id { font-family: ui-monospace, "Roboto Mono", monospace; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1 1 auto; }
/* The live VALUE is the row's headline (A6); the source id hides behind "i". */
.wire-state {
  font-size: 13px; font-weight: 600; white-space: nowrap; flex: 1 1 auto;
  font-variant-numeric: tabular-nums;
}
.wire-badge { font-size: 10px; padding: 2px 7px; border-radius: 999px; background: var(--t-error); color: var(--t-on-primary); }
.wire-row.wire-bad .wire-state { color: var(--t-error); }
.wire-row .hicon, .wire-row .hicon-fallback { margin-left: auto; }
.wire-row.link .hicon, .wire-row.link .hicon-fallback { opacity: .6; }

/* Decision */
.dec { display: flex; flex-direction: column; gap: 14px; }
.kv-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px 16px; }
.kv { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.kv-cap { font-size: 11px; color: var(--t-muted); line-height: 1.3; }
.kv-val { font-size: 14px; font-weight: 600; font-variant-numeric: tabular-nums; }
.kv-val.warn { color: var(--t-warn); }
.dec-block { display: flex; flex-direction: column; gap: 6px; }
.dec-block-cap { font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: var(--t-muted); }
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
.s-hum { fill: none; stroke: var(--t-ok); stroke-width: 1.5; stroke-linejoin: round; opacity: .8; }
/* Split-mode band under the time axis (fill) + its tooltip swatch (background). */
.band-heating { fill: var(--t-heat); background: var(--t-heat); opacity: .85; }
.band-cooling { fill: var(--t-cool); background: var(--t-cool); opacity: .85; }
.band-dry { fill: var(--t-dry); background: var(--t-dry); opacity: .85; }
.chart-guide { stroke: var(--t-muted); stroke-width: 1; stroke-dasharray: 3 3; pointer-events: none; }
.chart-dot { stroke: var(--t-card); stroke-width: 1.5; pointer-events: none; }
.dot-temp { fill: var(--t-primary); }
.dot-setpoint { fill: var(--t-muted); }
.dot-valve { fill: var(--t-accent); }
.dot-hum { fill: var(--t-ok); }

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
.leg-hum { background: var(--t-ok); }
.leg-lab { font-size: 12px; color: var(--t-muted); }
.leg-val { font-size: 12px; font-weight: 600; }

/* Shared "i" info tooltip */
.info-btn {
  border: 0; background: transparent; color: var(--t-muted); cursor: pointer;
  padding: 0 2px; margin-left: 2px; display: inline-flex; align-items: center;
  vertical-align: middle; border-radius: 50%; line-height: 1;
}
.info-btn:hover { color: var(--t-primary); }
.info-btn[aria-expanded="true"] { color: var(--t-primary); }
.info-btn:focus-visible { outline: 2px solid var(--t-primary); outline-offset: 1px; }
.info-btn .hicon { --mdc-icon-size: 14px; color: inherit; }
.info-btn .hicon-fallback { color: inherit; font-size: 12px; }
.info-tip {
  position: fixed; z-index: 40; max-width: 300px;
  background: var(--t-card); color: var(--t-fg);
  border: 1px solid var(--t-line); border-radius: 8px;
  padding: 8px 10px; font-size: 12px; line-height: 1.45;
  font-weight: 400; text-transform: none; letter-spacing: normal;
  box-shadow: 0 4px 18px rgba(0, 0, 0, 0.25);
  overflow-wrap: anywhere; white-space: normal; pointer-events: auto;
}
.info-tip-arrow {
  position: absolute; top: -5.5px; width: 10px; height: 10px;
  transform: translateX(-50%) rotate(45deg); background: var(--t-card);
  border-left: 1px solid var(--t-line); border-top: 1px solid var(--t-line);
}
.info-tip.above .info-tip-arrow {
  top: auto; bottom: -5.5px; border: 0;
  border-right: 1px solid var(--t-line); border-bottom: 1px solid var(--t-line);
}

/* Shared confirmation popover (A2) */
.confirm-pop {
  position: fixed; z-index: 41; max-width: 320px;
  background: var(--t-card); color: var(--t-fg);
  border: 1px solid var(--t-line); border-radius: 10px;
  padding: 12px 14px; font-size: 13px; line-height: 1.45;
  box-shadow: 0 6px 24px rgba(0, 0, 0, 0.3);
}
.confirm-text { margin-bottom: 10px; overflow-wrap: anywhere; }
.confirm-btns { display: flex; gap: 8px; justify-content: flex-end; }
.confirm-yes {
  border: 1px solid var(--t-primary); background: var(--t-primary);
  color: var(--t-on-primary); border-radius: 8px; padding: 6px 14px;
  font-size: 13px; font-weight: 600;
}
.confirm-yes:hover { filter: brightness(1.05); }
.confirm-yes:focus-visible { outline: 2px solid var(--t-fg); outline-offset: 1px; }
.confirm-no {
  border: 1px solid var(--t-line); background: var(--t-card); color: var(--t-fg);
  border-radius: 8px; padding: 6px 14px; font-size: 13px;
}
.confirm-no:hover { border-color: var(--t-primary); color: var(--t-primary); }

/* Heat-pump tab (B2) */
.hp { display: flex; flex-direction: column; gap: 14px; max-width: 720px; }
.hp-card {
  border: 1px solid var(--t-line); border-radius: var(--t-radius);
  background: var(--t-card); padding: 12px 14px;
  display: flex; flex-direction: column; gap: 10px;
}
.hp-card-cap {
  display: flex; align-items: center; gap: 6px;
  font-size: 13px; font-weight: 700;
}
.hp-card-cap .chip { margin-left: auto; }
.hp-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.hp-note { font-size: 12px; line-height: 1.45; }
.hp-warn { color: var(--t-warn); }
.hp-calc { font-size: 12px; }
.hp-dhw-row { display: flex; align-items: center; gap: 12px; }
.chip-ok { background: color-mix(in srgb, var(--t-ok) 18%, transparent); color: var(--t-ok); }
.chip-info { background: color-mix(in srgb, var(--t-info) 16%, transparent); color: var(--t-info); }
.hp-flicker-flags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
/* Heating-curve preview (issue #35): Tuning tab + Heat-pump tab. */
.curve-preview {
  padding: 10px 14px; display: flex; flex-direction: column; gap: 8px;
  border-top: 1px solid var(--t-line);
}
.curve-cap {
  display: flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 700;
}
.curve-table { border-collapse: collapse; font-size: 12px; max-width: 100%; }
.curve-table th, .curve-table td {
  padding: 4px 10px; text-align: right; border-bottom: 1px solid var(--t-line);
  font-variant-numeric: tabular-nums; white-space: nowrap;
}
.curve-table th { text-align: left; font-weight: 600; color: var(--t-muted); }
.curve-table tr:last-child th, .curve-table tr:last-child td { border-bottom: 0; }
.curve-note { font-size: 12px; }
.curve-note:empty { display: none; }

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
/* Manifolds tab: one card per distributor — head with stats, the axonometric
   drawing, the circuit table. The drawing classes mirror the accepted
   mock-up (f-* faces, r-* roles, ua-* label plates) on the panel tokens. */
.mf-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 620px), 1fr)); gap: 16px; align-items: start; }
.mf-card {
  --mf-supply: var(--t-heat); --mf-return: var(--t-cool);
  border: 1px solid var(--t-line); border-radius: var(--t-radius); background: var(--t-card);
  padding: 14px; display: flex; flex-direction: column; gap: 12px; min-width: 0;
}
.mf-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 14px; }
.mf-title { font-size: 16px; font-weight: 600; }
.mf-sub { font-size: 12px; color: var(--t-muted); }
.mf-stats { margin-left: auto; display: flex; flex-wrap: wrap; gap: 6px 14px; }
.mf-stats .stat-cap { font-size: 11px; color: var(--t-muted); letter-spacing: .02em; }
.mf-stats .stat-val { font-size: 15px; font-weight: 600; font-variant-numeric: tabular-nums; }
.mf-art { min-height: 40px; }
.mf-svg { display: block; width: 100%; height: auto; }
.mf-svg path, .mf-svg circle, .mf-svg polygon, .mf-svg rect { vector-effect: non-scaling-stroke; }
.mf-svg text { font-family: inherit; }
/* Solids as in the OAS air-handling-unit drawing: front = card, top 7 %, right 14 % of the text colour, cylinders 11 %. */
.f-front { fill: var(--t-card); stroke: var(--t-line); }
.f-top { fill: color-mix(in srgb, var(--t-fg) 7%, var(--t-card)); stroke: var(--t-line); }
.f-right { fill: color-mix(in srgb, var(--t-fg) 14%, var(--t-card)); stroke: var(--t-line); }
.f-side { fill: color-mix(in srgb, var(--t-fg) 11%, var(--t-card)); stroke: var(--t-line); }
.nostroke { stroke: none; }
.cyl-edge { fill: none; stroke: var(--t-line); stroke-linejoin: round; }
.hl { fill: var(--t-card); fill-opacity: .42; stroke: none; }
/* Rails and clamps darker, so the frame reads as background for the bars. */
.r-bracket.f-front { fill: color-mix(in srgb, var(--t-fg) 18%, var(--t-card)); }
.r-bracket.f-top { fill: color-mix(in srgb, var(--t-fg) 26%, var(--t-card)); }
.r-bracket.f-right { fill: color-mix(in srgb, var(--t-fg) 32%, var(--t-card)); }
.r-rubber.f-front { fill: color-mix(in srgb, var(--t-fg) 34%, var(--t-card)); }
.r-rubber.f-top { fill: color-mix(in srgb, var(--t-fg) 42%, var(--t-card)); }
.r-rubber.f-right { fill: color-mix(in srgb, var(--t-fg) 48%, var(--t-card)); }
.r-dark.f-side, .r-dark.f-top, .r-dark.f-front, .r-dark.f-right { fill: color-mix(in srgb, var(--t-fg) 40%, var(--t-card)); }
.r-glass.f-side { fill: var(--t-card); fill-opacity: .5; }
.r-glass.f-top { fill: color-mix(in srgb, var(--t-fg) 7%, var(--t-card)); fill-opacity: .6; }
.r-glass .hl { display: none; }
.r-float { fill: var(--t-primary); stroke: none; }
/* Pipes are translucent (owner request): whatever sits behind them shows through. */
.r-pipe-edge { fill: none; stroke: var(--t-line); stroke-linejoin: round; stroke-linecap: butt; stroke-opacity: .8; }
.r-pipe { fill: none; stroke: color-mix(in srgb, var(--t-fg) 26%, var(--t-card)); stroke-linejoin: round; stroke-linecap: butt; stroke-opacity: .6; }
/* Pipes are drawn as strokes 16 mm wide in model units, so they must scale with the drawing - the non-scaling rule above is for outlines only. */
.dash-unit-svg path.r-pipe, .dash-unit-svg path.r-pipe-edge { vector-effect: none; }
/* Actuator: the front takes the opening colour (--open-mix = 0…60 % primary); the depth tints stay. */
.r-act .f-front { fill: color-mix(in srgb, var(--t-primary) var(--open-mix, 0%), var(--t-card)); }
.r-act .f-top { fill: color-mix(in srgb, var(--t-fg) 7%, color-mix(in srgb, var(--t-primary) var(--open-mix, 0%), var(--t-card))); }
.r-act .f-right { fill: color-mix(in srgb, var(--t-fg) 14%, color-mix(in srgb, var(--t-primary) var(--open-mix, 0%), var(--t-card))); }
.r-act.flag .f-front { fill: color-mix(in srgb, var(--t-error) 30%, var(--t-card)); }
/* Label plates: card colour at 72 %, values 600, captions muted; a value with an entity is a hidden link (cursor only). */
.ua-plate { fill: var(--t-card); fill-opacity: .72; stroke: none; pointer-events: none; }
.ua-text { fill: var(--t-fg); font-variant-numeric: tabular-nums; }
.ua-cap { fill: var(--t-muted); }
.ua-val { font-weight: 600; }
.ua-sup { fill: var(--mf-supply); }
.ua-ret { fill: var(--mf-return); }
.ua-err { fill: var(--t-error); }
.ua-link { cursor: pointer; pointer-events: all; }
/* Table / head values: plain text that only gains a pointer when it links to an entity. */
.mf-link { border: 0; background: transparent; color: inherit; font: inherit; padding: 0; cursor: default; text-align: inherit; }
.mf-link:disabled { color: inherit; opacity: 1; }
.mf-link.linked { cursor: pointer; }
.mf-link:focus-visible { outline: 2px solid var(--t-primary); outline-offset: 2px; border-radius: 4px; }
.mf-tbl { overflow-x: auto; }
table.mf-loops { border-collapse: collapse; width: 100%; font-size: 13px; font-variant-numeric: tabular-nums; }
.mf-loops th { text-align: left; font-size: 11px; font-weight: 600; color: var(--t-muted); letter-spacing: .02em; padding: 4px 8px; border-bottom: 1px solid var(--t-line); }
.mf-loops td { padding: 5px 8px; border-bottom: 1px solid var(--t-line); }
.mf-loops td.n, .mf-loops th.n { text-align: right; white-space: nowrap; }
.mf-loops tr:last-child td { border-bottom: 0; }
.mf-loops tr.mf-free td { color: var(--t-muted); }
.mf-loop { white-space: nowrap; }
.mf-bar { display: inline-block; vertical-align: middle; width: 56px; height: 6px; border-radius: 3px; background: var(--t-chip); margin-right: 8px; overflow: hidden; }
.mf-bar i { display: block; height: 100%; width: 0; background: var(--t-primary); }
.mf-chips { display: flex; flex-wrap: wrap; gap: 4px; }
`;

if (!customElements.get("tortoise-ufh-panel")) {
  customElements.define("tortoise-ufh-panel", TortoiseUfhPanel);
}
