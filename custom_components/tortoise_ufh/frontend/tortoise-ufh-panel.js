/**
 * Tortoise-UFH sidebar panel — self-contained vanilla-JS ES module.
 *
 * A dependency-free custom element (no CDN / external imports / build step,
 * CSP-safe) that renders the per-room underfloor-heating control state as a
 * health hero + a four-tab workspace (Rooms table, Tuning, Valves and
 * Assist) + a master/detail room inspector (a sticky side column on wide
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
  { key: "rooms", label: "tab_rooms" },
  { key: "tuning", label: "tab_tuning" },
  { key: "valves", label: "tab_valves" },
  { key: "assist", label: "tab_assist" },
  { key: "hp", label: "tab_hp" },
];
const TAB_ORDER = TABS.map((t) => t.key);
const DEFAULT_TAB = "rooms";
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
/** Default window used as a fallback when an unknown window key is requested. */
const SPARK_WINDOW = "24h";

/** Cached history is reused for this long before a background refetch. */
const HIST_MAXAGE_MS = 60 * 1000;

/** Detail-chart geometry (pixels; width is measured at draw time). */
const CHART_H = 240;
const CHART_MARGIN = { l: 42, r: 46, t: 12, b: 26 };
const CHART_MIN_W = 300;
const CHART_MAX_W = 900;

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
    dew_cap: "Bezpieczny punkt rosy",
    home_cap: "Temperatura domu",
    mode_cap: "Tryb",
    mode_heating: "Grzanie",
    mode_transitional: "Przejściowy",
    mode_cooling: "Chłodzenie",
    mode_off: "Wył.",
    tab_rooms: "Pokoje",
    tab_tuning: "Strojenie",
    tab_valves: "Zawory",
    tab_assist: "Wspomaganie",
    tab_hp: "Pompa ciepła",
    manage_rooms: "Zarządzaj pokojami",
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
      "Zapis przeładowuje regulator (integrator PID zostaje wyzerowany).",
    tune_scope_global: "Globalne",
    tune_save: "Zapisz",
    tune_saving: "Zapisywanie…",
    tune_saved: "Zapisano",
    tune_overridden: "nadpisane",
    tune_revert: "Wróć do globalnej",
    tune_on: "wł.",
    tune_off: "wył.",
    tune_grp_pid: "Regulator PI i trend",
    tune_grp_band: "Pasmo komfortu i zawór",
    tune_grp_weather: "Człon pogodowy (otwarcie zaworu)",
    tune_grp_fast: "Szybkie źródło (split/grzałka)",
    tune_grp_dew: "Ochrona przed kondensacją (chłodzenie)",
    tune_grp_hp: "Pompa ciepła — woda (tylko globalnie)",
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
    tune_dew_margin_k: "Margines bezpieczeństwa nad punktem rosy",
    tune_dew_ramp_k: "Szerokość rampy dławienia chłodzenia",
    tune_outdoor_ff_enabled: "Człon pogodowy włączony",
    tune_ff_neutral_c: "Temperatura neutralna członu pogodowego",
    tune_ff_gain_pct_per_k: "Wzmocnienie członu pogodowego",
    tune_ff_max_pct: "Górny limit członu pogodowego",
    tune_cooling_supply_base_c: "Bazowa temperatura wody chłodzenia",
    tune_heating_supply_base_c: "Bazowa temperatura wody grzania",
    tune_heating_supply_slope: "Nachylenie krzywej wody grzania",
    tune_grp_flow: "Watchdog przepływu (S6)",
    tune_flow_epsilon_k: "Minimalna ΔT uznawana za przepływ",
    tune_flow_open_threshold_pct: "Próg komendy otwarcia watchdoga",
    tune_flow_response_window_min: "Okno odpowiedzi hydraulicznej",
    val_th_command: "Komenda",
    val_th_raw: "Surowy",
    val_th_floor: "Podłoga",
    val_th_sat: "Saturacja",
    val_th_s2: "Dławienie S2",
    val_th_feedback: "Feedback",
    val_th_flow: "Przepływ",
    val_th_test: "Test aktuacji",
    val_raw_tooltip: "P {p} · I {i} · Trend {t} · FF {f}",
    val_floor_chip: "min. otwarcie {v}%",
    val_s2_flow: "przepływ {v}%",
    val_s2_condensation: "kondensacja",
    val_loop: "Pętla {n}",
    val_supply: "Zasilanie",
    val_return: "Powrót",
    val_dt: "ΔT",
    val_flow_cap: "Przepływ",
    val_test_cap: "Test",
    val_feedback_pos: "poz. {v}%",
    val_no_loops: "brak przypisanych zaworów",
    val_show_loops: "Pokaż pętle ({n})",
    val_hide_loops: "Ukryj pętle",
    val_empty: "Brak skonfigurowanych pokoi.",
    flow_ok: "ok",
    flow_no_flow: "brak przepływu?",
    flow_stuck: "nie domyka?",
    test_btn_start: "Testuj aktuację",
    test_btn_cancel: "Anuluj test",
    test_running_min: "~{m} min",
    test_passed: "zaliczony",
    test_failed: "niezaliczony",
    test_aborted: "przerwany",
    test_untested: "niezbadana",
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
    // docs/INSTRUKCJA.md §8: zmieniaj OBA miejsca razem.
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
      "strefa nieczułości. Domyślnie 1,0 K.",
    tip_knob_fast_min_on_minutes:
      "Minimalny czas pracy szybkiego źródła po włączeniu. Chroni sprężarkę " +
      "splita przed taktowaniem i uspokaja zmiany kierunku w grupie " +
      "multisplit. Domyślnie 10 min; minimum 3.",
    tip_knob_fast_min_off_minutes:
      "Minimalny czas postoju szybkiego źródła po wyłączeniu, zanim wolno je " +
      "włączyć ponownie. Domyślnie 10 min; minimum 3.",
    tip_knob_fast_target_offset_k:
      "O ile kelwinów nastawa wysyłana do splita jest podbijana poza zadaną " +
      "pokoju podczas dogrzewu/dochładzania (grzanie: zadana + wartość, " +
      "chłodzenie: zadana − wartość). Czujnik splita pod sufitem czyta " +
      "cieplej niż czujnik pokojowy — bez podbicia split dławi się przed " +
      "dostarczeniem dogrzewu. O wyłączeniu i tak decyduje czujnik pokojowy. " +
      "0 = bez podbicia (split dostaje dokładnie zadaną). Domyślnie 1 K.",
    tip_knob_dew_margin_k:
      "Chłodzenie: gdy zasilanie jest co najmniej o tyle K powyżej punktu " +
      "rosy, zawór działa bez ograniczeń; bliżej rosy zaczyna się dławienie " +
      "przepływu (ochrona przed kondensacją na podłodze). Domyślnie 2 K; " +
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
      "FF. Domyślnie 15 °C.",
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
      "się go nadpisać per pokój. Domyślnie 18 °C.",
    tip_knob_heating_supply_base_c:
      "Zasilanie wody grzewczej przy neutralnej temperaturze zewnętrznej " +
      "(parametr „Temperatura neutralna członu pogodowego”, domyślnie " +
      "15 °C). Poniżej niej nastawa rośnie według nachylenia krzywej; całość " +
      "obcinana do 20–40 °C. Pisana do pompy tylko przy skonfigurowanej " +
      "encji nastawy grzania — domyślnie pompa jedzie na własnej krzywej. " +
      "Parametr globalny. Domyślnie 26 °C.",
    tip_knob_heating_supply_slope:
      "O ile kelwinów rośnie nastawa wody grzania na każdy 1 K temperatury " +
      "zewnętrznej poniżej temperatury neutralnej. Domyślnie 0,5 K/K; " +
      "typowo 0,3–0,8 dla dobrze ocieplonego domu z podłogówką.",
    tip_knob_flow_epsilon_k:
      "Watchdog przepływu (S6): minimalna różnica temperatur zasilanie−powrót " +
      "pętli uznawana za dowód przepływu wody. Poniżej niej (i bez ruchu sond " +
      "ku źródłu) otwarta komenda bez odpowiedzi hydraulicznej podnosi flagę " +
      "„brak przepływu”. Domyślnie 0,3 K; podnieś przy szumiących sondach.",
    tip_knob_flow_open_threshold_pct:
      "Watchdog przepływu (S6): od jakiej komendy zaworu pętla ma obowiązek " +
      "pokazać odpowiedź hydrauliczną. Poniżej progu pętla nie jest oceniana " +
      "pod kątem braku przepływu. Domyślnie 15 %.",
    tip_knob_flow_response_window_min:
      "Watchdog przepływu (S6): ile minut ciągłego braku sygnatury przepływu " +
      "(przy otwartej komendzie i wiarygodnej cyrkulacji) podnosi flagę „brak " +
      "przepływu”; to samo okno pilnuje zaworu, który nie domyka. Płyta jest " +
      "wolna — minimum 30 min, żeby uniknąć trzepotania. Domyślnie 45 min; " +
      "1440 praktycznie wyłącza watchdoga.",
    // Tooltipy szczegółów pokoju (tip_dec_*) i nagłówków tabel (tip_val_* /
    // tip_ast_* / tip_th_*) — treść wspólna z docs/INSTRUKCJA.md: zmieniaj razem.
    tip_th_assist:
      "Komenda dla szybkiego źródła pokoju (split/grzałka): kierunek pracy " +
      "i temperatura zadana dla urządzenia. „—” = pokój nie ma wspomagania; " +
      "„wył.” = wspomaganie stoi (pokój trzyma podłoga).",
    tip_assist_target:
      "Nastawa splita celowo różni się od zadanej pokoju o „podbicie " +
      "nastawy wspomagania” (domyślnie 1 K; grzanie: +, chłodzenie: −). " +
      "Czujnik splita wisi pod sufitem i czyta cieplej niż nasz czujnik " +
      "pokojowy — nastawa równa zadanej gasiłaby split, zanim " +
      "dogrzanie/dochłodzenie faktycznie dotrze do pokoju. O wyłączeniu " +
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
      "(docs/INSTRUKCJA.md §12).",
    tip_val_raw:
      "Otwarcie policzone przez regulator przed limitami (minimalne otwarcie, " +
      "obcięcie 0–100 %, reguły bezpieczeństwa). Najedź na wartość, by " +
      "zobaczyć wkład członów P/I/Trend/FF.",
    tip_val_floor:
      "Czy zadziałało minimalne otwarcie przy grzaniu (parametr „Minimalne " +
      "otwarcie zaworu”).",
    tip_val_sat:
      "Zawór uderzył w ogranicznik 0 lub 100 % — regulator chce więcej, niż " +
      "fizycznie się da; anty-nasycenie pilnuje, by całka nie rosła w ścianę.",
    tip_val_s2:
      "Dławienie przepływu przy punkcie rosy (tylko chłodzenie): 100 % = bez " +
      "ograniczeń, 0 % = zawór zamknięty (flaga s2_throttle; także " +
      "zachowawczo przy braku danych o wilgotności lub zasilaniu).",
    tip_val_feedback:
      "Pozycja raportowana przez siłownik; trwała rozbieżność z komendą " +
      "podnosi flagę valve_mismatch.",
    tip_flow_chip:
      "Zdrowie przepływu z sond wody pętli (watchdog S6) — niezależny, " +
      "fizyczny świadek aktuacji, który nie ufa feedbackowi encji zaworu " +
      "(ten kanał potrafi kłamać po restarcie kontrolera). „brak przepływu?” " +
      "= otwarta komenda bez sygnatury hydraulicznej; „nie domyka?” = komenda " +
      "0 z uporczywą sygnaturą źródła; „—” = brak sond lub watchdog " +
      "wstrzymany (cyrkulacja niepewna).",
    tip_actuation_test:
      "Ręczny self-test aktuacji: zawór jest celowo wysterowany na 100 % na " +
      "20–30 min, a o wyniku decyduje odpowiedź hydrauliczna sond pętli. " +
      "Do uruchamiania po serwisie lub zdarzeniu zasilania. Wymaga pokoju w " +
      "stanie Steruje i sond wody; reguły bezpieczeństwa (przegrzanie, " +
      "kondensacja) przerywają test. Podczas testu integrator jest zamrożony.",
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
    // Zakładka Pompa ciepła (B2) — treść wspólna z docs/INSTRUKCJA.md §11.
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
    hp_heat_calc: "krzywa: {base} + {slope} · ({neutral} − T zewn. {tout})",
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
      "neutralnej, obcięte do 20–40 °C). Jeśli encji nie skonfigurujesz, " +
      "pompa jedzie na własnej krzywej — to domyślne i w pełni wspierane.",
    hp_active_cap: "Pompa dostępna dla podłogówki",
    hp_active_unknown: "nieznane",
    tip_hp_active:
      "Sygnał „pompa pracuje dla podłogówki”: gdy jest wyłączony " +
      "(CWU/odszranianie), regulatory pokojów zamrażają człon całkujący, " +
      "żeby nie nabijać całki, kiedy woda i tak nie płynie w podłogę.",
    hp_writes_paused:
      "Zapisy wstrzymane — żaden pokój nie steruje (Steruje).",
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
    dew_cap: "Safe dew point",
    home_cap: "Home temperature",
    mode_cap: "Mode",
    mode_heating: "Heating",
    mode_transitional: "Transitional",
    mode_cooling: "Cooling",
    mode_off: "Off",
    tab_rooms: "Rooms",
    tab_tuning: "Tuning",
    tab_valves: "Valves",
    tab_assist: "Assist",
    tab_hp: "Heat pump",
    manage_rooms: "Manage rooms",
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
      "Saving reloads the controller (the PID integrator is reset).",
    tune_scope_global: "Global",
    tune_save: "Save",
    tune_saving: "Saving…",
    tune_saved: "Saved",
    tune_overridden: "overridden",
    tune_revert: "Revert to global",
    tune_on: "on",
    tune_off: "off",
    tune_grp_pid: "PI controller & trend",
    tune_grp_band: "Comfort band & valve",
    tune_grp_weather: "Weather feedforward (valve opening)",
    tune_grp_fast: "Fast source (split/heater)",
    tune_grp_dew: "Condensation protection (cooling)",
    tune_grp_hp: "Heat pump — water (global only)",
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
    tune_dew_margin_k: "Safety margin above the dew point",
    tune_dew_ramp_k: "Cooling throttle ramp width",
    tune_outdoor_ff_enabled: "Weather feedforward enabled",
    tune_ff_neutral_c: "Feedforward neutral outdoor temperature",
    tune_ff_gain_pct_per_k: "Feedforward gain",
    tune_ff_max_pct: "Feedforward upper limit",
    tune_cooling_supply_base_c: "Cooling water base temperature",
    tune_heating_supply_base_c: "Heating water base temperature",
    tune_heating_supply_slope: "Heating water curve slope",
    val_th_command: "Command",
    val_th_raw: "Raw",
    val_th_floor: "Floor",
    val_th_sat: "Saturation",
    val_th_s2: "S2 throttle",
    val_th_feedback: "Feedback",
    val_raw_tooltip: "P {p} · I {i} · Trend {t} · FF {f}",
    val_floor_chip: "min opening {v}%",
    val_s2_flow: "flow {v}%",
    val_s2_condensation: "condensation",
    val_loop: "Loop {n}",
    val_supply: "Supply",
    val_return: "Return",
    val_dt: "ΔT",
    val_feedback_pos: "pos {v}%",
    val_no_loops: "no valves assigned",
    val_show_loops: "Show loops ({n})",
    val_hide_loops: "Hide loops",
    val_empty: "No rooms configured.",
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
    // docs/INSTRUKCJA.md §8: change BOTH places together.
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
      "always stays the base source. Must exceed the deadband. Default 1.0 K.",
    tip_knob_fast_min_on_minutes:
      "Minimum runtime of the fast source once switched on. Protects the " +
      "split compressor from short-cycling and calms direction changes on a " +
      "multisplit group. Default 10 min; minimum 3.",
    tip_knob_fast_min_off_minutes:
      "Minimum off-time of the fast source before it may start again. " +
      "Default 10 min; minimum 3.",
    tip_knob_fast_target_offset_k:
      "How many kelvin the setpoint sent to the split is pushed beyond the " +
      "room setpoint during a boost (heating: setpoint + value, cooling: " +
      "setpoint − value). The split's ceiling-mounted sensor reads warmer " +
      "than the room sensor — without the overdrive the unit throttles " +
      "itself before the boost is delivered. Release is still decided by " +
      "the room sensor. 0 = no overdrive (the split gets the plain " +
      "setpoint). Default 1 K.",
    tip_knob_dew_margin_k:
      "Cooling: while supply is at least this many K above the dew point the " +
      "valve runs unrestricted; closer to the dew point the flow gets " +
      "throttled (floor condensation protection). Default 2 K; raise it if " +
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
      "1 K colder adds opening per the FF gain. Default 15 °C.",
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
      "parameter — cannot be overridden per room. Default 18 °C.",
    tip_knob_heating_supply_base_c:
      "Heating supply water at the neutral outdoor temperature (the " +
      "“Feedforward neutral outdoor temperature” knob, default 15 °C). " +
      "Below it the setpoint rises along the curve slope; the result is " +
      "clamped to 20–40 °C. Written to the pump only with the " +
      "heating-setpoint entity configured — by default the pump follows its " +
      "own curve. Global parameter. Default 26 °C.",
    tip_knob_heating_supply_slope:
      "How many kelvins the heating-water setpoint rises per 1 K of outdoor " +
      "temperature below the neutral point. Default 0.5 K/K; typically " +
      "0.3–0.8 for a well-insulated UFH house.",
    // Room-detail tooltips (tip_dec_*) and table-header tooltips (tip_val_* /
    // tip_ast_* / tip_th_*) — content shared with docs/INSTRUKCJA.md: change
    // together.
    tip_th_assist:
      "The command for the room's fast source (split/heater): running " +
      "direction and the unit's target temperature. “—” = the room has no " +
      "assist; “off” = the assist is idle (the floor holds the room).",
    tip_assist_target:
      "The split's setpoint deliberately differs from the room target by " +
      "the “assist target overdrive” knob (default 1 K; heating: +, " +
      "cooling: −). The split's own sensor sits near the ceiling and reads " +
      "warmer than our room sensor — a setpoint equal to the target would " +
      "throttle the unit before the boost actually reaches the room. " +
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
      "(docs/INSTRUKCJA.md §12).",
    tip_val_raw:
      "Opening computed by the controller before limits (valve floor, 0–100 % " +
      "clamp, safety rules). Hover the value to see the P/I/Trend/FF term " +
      "contributions.",
    tip_val_floor:
      "Whether the heating valve floor (the 'Valve floor' knob) was applied " +
      "this cycle.",
    tip_val_sat:
      "The valve hit the 0 or 100 % bound — the controller wants more than " +
      "physics allows; anti-windup keeps the integral in check.",
    tip_val_s2:
      "Dew-point flow throttling (cooling only): 100 % = unrestricted, 0 % = " +
      "valve shut (flag s2_throttle; also a conservative 0 % when humidity " +
      "or supply data is missing).",
    tip_val_feedback:
      "The position the actuator reports; a persistent mismatch with the " +
      "command raises valve_mismatch.",
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
    // Heat-pump tab (B2) — content shared with docs/INSTRUKCJA.md §11.
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
    hp_heat_calc: "curve: {base} + {slope} · ({neutral} − outdoor {tout})",
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
      "temperature, clamped to 20–40 °C). If you leave the entity " +
      "unconfigured, the pump follows its own curve — the default and fully " +
      "supported.",
    hp_active_cap: "Pump available for UFH",
    hp_active_unknown: "unknown",
    tip_hp_active:
      "The “pump serves the UFH” signal: while it is off (DHW/defrost), the " +
      "room controllers freeze their integral term so it does not wind up " +
      "while no water reaches the floor.",
    hp_writes_paused: "Writes paused — no room is in control (Live).",
    // S6 hydraulic no-flow watchdog + actuation self-test (issue #4,
    // 2026-07-13). EN mirror of the PL keys above.
    tune_grp_flow: "Flow watchdog (S6)",
    tune_flow_epsilon_k: "Minimum ΔT counted as flow",
    tune_flow_open_threshold_pct: "Watchdog open-command threshold",
    tune_flow_response_window_min: "Hydraulic response window",
    tip_knob_flow_epsilon_k:
      "Flow watchdog (S6): the minimum loop supply−return temperature " +
      "difference counted as evidence of water flow. Below it (and with no " +
      "probe movement toward the source) an open command with no hydraulic " +
      "response raises the “no flow” flag. Default 0.3 K; raise for noisy " +
      "probes.",
    tip_knob_flow_open_threshold_pct:
      "Flow watchdog (S6): the valve command above which a loop is expected " +
      "to show a hydraulic response. Below the threshold the loop is not " +
      "evaluated for no-flow. Default 15 %.",
    tip_knob_flow_response_window_min:
      "Flow watchdog (S6): how many minutes of continuous missing flow " +
      "signature (with an open command and plausible circulation) raise the " +
      "“no flow” flag; the same window guards a valve that never closes. The " +
      "slab is slow — minimum 30 min to avoid flapping. Default 45 min; 1440 " +
      "effectively disables the watchdog.",
    tip_flow_chip:
      "Flow health from the loop water probes (S6 watchdog) — an " +
      "independent, physical witness of actuation that does NOT trust the " +
      "valve entity's feedback (that channel can lie after a controller " +
      "reset). “no flow?” = an open command with no hydraulic signature; " +
      "“stuck open?” = a 0 command with a persistent source-side signature; " +
      "“—” = no probes, or the watchdog is on hold (circulation uncertain).",
    tip_actuation_test:
      "Manual actuation self-test: the valve is deliberately driven to " +
      "100 % for 20–30 min and the verdict comes from the loop probes' " +
      "hydraulic response. Run it after maintenance or a power event. " +
      "Requires the room in Control and water probes; safety rules " +
      "(overheat, condensation) abort the test. The integrator is frozen " +
      "during the test.",
    val_th_flow: "Flow",
    val_th_test: "Actuation test",
    val_flow_cap: "Flow",
    val_test_cap: "Test",
    flow_ok: "ok",
    flow_no_flow: "no flow?",
    flow_stuck: "stuck open?",
    test_btn_start: "Test actuation",
    test_btn_cancel: "Cancel test",
    test_running_min: "~{m} min",
    test_passed: "passed",
    test_failed: "failed",
    test_aborted: "aborted",
    test_untested: "untested",
  },
};

/**
 * Localised, severity-tagged labels for controller flag codes.
 *
 * Codes mirror the exact strings the report carries: the room-controller
 * flags (`sensor_lost`, `s2_throttle` — the graduated local dew throttle,
 * split from the hard rule 2026-07-12 — `rh_stale_gated`,
 * `fast_source_cannot_cool`, `fast_source_min_runtime`,
 * `fast_source_group_conflict`, `cooling_disabled`, `unknown_room`,
 * `controller_error`, `fast_source_mismatch`), the adapter-stamped
 * `valve_mismatch` (persistent command-vs-feedback divergence), plus the
 * safety-rule names merged into the report
 * (`s1_floor_overheat`, `s2_condensation`, `s3_emergency_heat`,
 * `s4_emergency_cool`, `s5_watchdog`). The report booleans `saturated` /
 * `valve_floor_applied` are rendered from their own STR keys in the decision
 * view, never through this map. Unknown codes fall back to the raw string at
 * `warn` severity. `sev` drives the room status dot.
 */
const FLAG_LABELS = {
  sensor_lost: { pl: "Utrata czujnika", en: "Sensor lost", sev: "problem" },
  s2_condensation: { pl: "Ryzyko kondensacji", en: "Condensation risk", sev: "problem" },
  s2_throttle: {
    pl: "Dławienie przy punkcie rosy",
    en: "Dew-point throttling",
    sev: "warn",
  },
  rh_stale_gated: {
    pl: "Nieświeża wilgotność (+1 K marginesu)",
    en: "Stale humidity (+1 K margin)",
    sev: "warn",
  },
  fast_source_group_conflict: {
    pl: "Konflikt kierunków na wspólnym agregacie",
    en: "Direction conflict on shared outdoor unit",
    sev: "warn",
  },
  s1_floor_overheat: { pl: "Przegrzanie podłogi", en: "Floor overheat", sev: "problem" },
  s3_emergency_heat: { pl: "Awaryjne grzanie", en: "Emergency heat", sev: "problem" },
  s4_emergency_cool: { pl: "Awaryjne chłodzenie", en: "Emergency cooling", sev: "problem" },
  s5_watchdog: { pl: "Watchdog", en: "Watchdog", sev: "problem" },
  unknown_room: { pl: "Brak konfiguracji", en: "Unknown room", sev: "problem" },
  controller_error: { pl: "Błąd regulatora", en: "Controller error", sev: "problem" },
  valve_mismatch: {
    pl: "Zawór nie wykonuje komend",
    en: "Valve not following commands",
    sev: "problem",
  },
  fast_source_mismatch: {
    pl: "Wspomaganie w innym stanie niż komenda",
    en: "Assist state differs from command",
    sev: "warn",
  },
  fast_source_cannot_cool: {
    pl: "Wspomaganie nie chłodzi",
    en: "Assist can't cool",
    sev: "warn",
  },
  fast_source_min_runtime: {
    pl: "Wspomaganie: blokada min. czasu pracy",
    en: "Assist: min-runtime lock",
    sev: "warn",
  },
  fast_source_quiet_hours: {
    pl: "Ciche godziny wspomagania",
    en: "Assist quiet hours",
    sev: "ok",
  },
  cooling_disabled: {
    pl: "Chłodzenie wyłączone w tym pokoju",
    en: "Cooling disabled in this room",
    sev: "warn",
  },
  loop_no_flow: {
    pl: "Brak przepływu w pętli (S6)",
    en: "Loop no flow (S6)",
    sev: "alarm",
  },
  loop_stuck_open: {
    pl: "Pętla nie domyka (S6)",
    en: "Loop stuck open (S6)",
    sev: "alarm",
  },
  actuation_test_running: {
    pl: "Trwa test aktuacji",
    en: "Actuation test running",
    sev: "ok",
  },
  actuation_test_failed: {
    pl: "Test aktuacji niezaliczony",
    en: "Actuation test failed",
    sev: "alarm",
  },
};

const SEV_RANK = { ok: 0, warn: 1, problem: 2 };

/**
 * Tuning-tab knob groups (A4, 2026-07-12): the Tuning tab renders a vertical
 * list of parameters split into these titled groups instead of a flat card
 * grid. Every exposed knob must appear in EXACTLY one group (enforced by
 * tests/unit/test_panel_i18n.py); a knob the backend exposes but no group
 * lists falls into a trailing "other" group instead of silently vanishing.
 * `globalOnly` groups render only in the Global scope (the heat-pump water
 * setpoints are building-level physics; the backend rejects per-room
 * overrides too).
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
    ],
  },
  {
    key: "dew",
    labelKey: "tune_grp_dew",
    knobs: ["dew_margin_k", "dew_ramp_k"],
  },
  {
    key: "flow",
    labelKey: "tune_grp_flow",
    knobs: [
      "flow_epsilon_k",
      "flow_open_threshold_pct",
      "flow_response_window_min",
    ],
  },
  {
    key: "hp",
    labelKey: "tune_grp_hp",
    knobs: [
      "cooling_supply_base_c",
      "heating_supply_base_c",
      "heating_supply_slope",
    ],
    globalOnly: true,
  },
];

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
  // --------------------------------------------------------------------------
  // Lifecycle, wiring & timers
  // --------------------------------------------------------------------------

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
    this._tabs = null; // tab-bar + section refs
    this._activeTab = this._loadActiveTab();
    this._rows = new Map(); // room name -> table <tr> element
    this._detail = null; // detail part refs
    this._detailRoom = null; // room name the detail is currently built for
    this._wiringOpen = true; // signals <details> state, persists across rooms
    this._diagOpen = false; // diagnostics <details> state (A5: collapsed)

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
    // Errors are suppressed until this epoch-ms (a save reloads the entry, so
    // get_live / get_tuning briefly return not_found — that is expected).
    this._suppressErrorUntil = 0;

    this._pollTimer = null;
    this._ageTimer = null;
    this._onVisibility = () => this._handleVisibility();
    this._hasHaIcon = false;

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

  /** Restore the persisted active tab, defaulting to `rooms` (storage may throw). */
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
    return String(raw).toLowerCase().startsWith("pl") ? "pl" : "en";
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
    return rank >= 2 ? "problem" : rank === 1 ? "warn" : "ok";
  }

  /** Coerce any control-state value to one of `ROOM_STATES` (default off). */
  _normState(value) {
    const s = String(value);
    return ROOM_STATES.includes(s) ? s : STATE_OFF;
  }

  /**
   * Localised fast-source (assist) badge for a room view-model.
   *
   * Returns `{ text, cls }` where `cls` is `on` / `off` / `none`. Composes the
   * source kind (Split / Heater) with its command state and target, e.g.
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
      r.fastMode === "cooling"
        ? this._t("assist_state_cooling")
        : this._t("assist_state_heating");
    const target = r.fastTarget !== null ? " → " + fmt(r.fastTarget, 1, " °C") : "";
    return { text: short + ": " + verb + target, cls: "on" };
  }

  /**
   * Localised fast-source MODE only (Off / Cool / Heat), for the Rooms table's
   * split-out "Tryb" column. Same on/off/mode reading as `_assistLabel`, minus
   * the source-kind prefix and target temperature.
   *
   * Returns `{ text, cls }` where `cls` is `on` / `off` / `none`.
   */
  _assistModeLabel(r) {
    if (r.fastKind === "none") {
      return { text: this._t("assist_none"), cls: "none" };
    }
    if (!r.fastOn || r.fastMode === "off") {
      return { text: this._t("assist_state_off"), cls: "off" };
    }
    const verb =
      r.fastMode === "cooling"
        ? this._t("assist_state_cooling")
        : this._t("assist_state_heating");
    return { text: verb, cls: "on" };
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

    // Four sections, built once and toggled with `display` (never rebuilt), so
    // switching tabs is instant and never disturbs scroll or in-flight fetches.
    const roomsSection = this._buildRoomsSection();
    const sections = {
      rooms: roomsSection.el,
      tuning: this._buildTuningSection(),
      valves: this._buildValvesSection(),
      assist: this._buildAssistSection(),
      hp: this._buildHpSection(),
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
    if (this._activeTab === "rooms") {
      this._reconcileTable();
    } else if (this._activeTab === "tuning") {
      this._ensureTuningLoaded();
    } else if (this._activeTab === "valves") {
      this._renderValves();
    } else if (this._activeTab === "assist") {
      this._renderAssist();
    } else if (this._activeTab === "hp") {
      this._renderHp();
    }
  }

  _buildHero() {
    const H = {};

    // Brand + manage-rooms deep link. The mark is the served brand icon; on
    // load failure (e.g. dev preview without the static path) it swaps itself
    // for the previous mdi/glyph mark.
    const brandImg = h("img", { class: "brand-img", src: BRAND_ICON_URL, alt: "" });
    brandImg.addEventListener("error", () => {
      brandImg.replaceWith(this._icon("mdi:tortoise", "🐢"));
    });
    const brand = h("div", { class: "brand" }, [
      brandImg,
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
    H.dewSub = h("span", { class: "chip-sub", style: "display:none" });
    H.dewChip = h("div", { class: "chip chip-dew", style: "display:none" }, [
      h("div", { class: "chip-main" }, [
        h("span", { class: "chip-cap", text: this._t("dew_cap") }),
        H.dewVal,
      ]),
      H.dewSub,
    ]);

    const statusRow = h("div", { class: "hero-row status-row" }, [
      H.pill,
      liveMetric,
      H.flagsMetric,
      H.dewChip,
    ]);

    // Controls: home stepper + mode segmented.
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

    const ctlRow = h("div", { class: "hero-row ctl-row" }, [homeCtl, modeCtl]);

    this._hero = H;
    return h("header", { class: "hero" }, [brandRow, statusRow, ctlRow]);
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

    // Safe dew point is only meaningful while cooling; the chip stays visible
    // throughout cooling and, when there is no value, explains why (per room
    // for a small house, collectively above three rooms).
    const dew = num(live.global_safe_dew_point_c);
    if (mode === "cooling") {
      H.dewChip.style.display = "";
      if (dew !== null) {
        H.dewVal.textContent = fmt(dew, 1, " °C");
        H.dewSub.textContent = "";
        H.dewSub.style.display = "none";
        H.dewChip.title = "";
      } else {
        H.dewVal.textContent = "—";
        const reason = this._dewReasonSummary();
        H.dewSub.textContent = reason;
        H.dewSub.style.display = reason ? "" : "none";
        H.dewChip.title = reason;
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

    // Column 6: valve percent (no mini-bar here — that stays on the Valves tab).
    p.valveVal = h("span", { class: "valve-val" });
    const valveCell = h("td", { class: "col-valve" }, [p.valveVal]);

    // Columns 7-8: loop supply / return temperature, read live from hass.states
    // (same entities + helper the Valves tab uses; first loop when a room has
    // several).
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

    // Valve percent (value only — the mini-bar stays on the Valves tab).
    p.valveVal.textContent = fmt(r.valve, 0, "%");

    // Loop supply / return, read live from hass.states (first loop when a
    // room has several — the full per-loop breakdown lives on the Valves tab).
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
    // Hand focus back to the room's table row when the close came from inside
    // the inspector (keyboard flow); an outside click keeps its own focus.
    if (hadFocus && prev) {
      const row = this._rows.get(prev);
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
        row.stateEl.textContent = String(val) + (unit && !bad ? " " + unit : "");
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
  // Valves tab
  // --------------------------------------------------------------------------

  /** Build the Valves section skeleton (rows populated by `_renderValves`). */
  _buildValvesSection() {
    const empty = h("div", { class: "empty", text: this._t("loading") });
    const headCells = [
      h("th", { scope: "col", text: this._t("th_room") }),
      h("th", { scope: "col", text: this._t("val_th_command") }),
      h("th", { scope: "col" }, [this._t("val_th_raw"), this._infoIcon("tip_val_raw")]),
      h("th", { scope: "col" }, [
        this._t("val_th_floor"),
        this._infoIcon("tip_val_floor"),
      ]),
      h("th", { scope: "col" }, [this._t("val_th_sat"), this._infoIcon("tip_val_sat")]),
      h("th", { scope: "col" }, [this._t("val_th_s2"), this._infoIcon("tip_val_s2")]),
      h("th", { scope: "col" }, [
        this._t("val_th_feedback"),
        this._infoIcon("tip_val_feedback"),
      ]),
    ];
    const thead = h("thead", null, [h("tr", null, headCells)]);
    const tbody = h("tbody");
    const table = h("table", { class: "valves-table" }, [thead, tbody]);
    const wrapEl = h("div", { class: "table-wrap" }, [table]);
    const el = h(
      "section",
      { class: "tab-section", role: "tabpanel", style: "display:none", dataset: { tab: "valves" } },
      [empty, wrapEl],
    );
    this._valvesEls = { el, tbody, empty, wrapEl, rows: new Map() };
    return el;
  }

  /** Reconcile the Valves table from the current view + live HA states. */
  _renderValves() {
    const E = this._valvesEls;
    if (!E) {
      return;
    }
    const rows = this._view;
    if (!rows.length) {
      for (const [, entry] of E.rows) {
        entry.tr.remove();
        entry.detailTr.remove();
      }
      E.rows.clear();
      const loading = !this._config && !this._live;
      E.empty.textContent = loading ? this._t("loading") : this._t("val_empty");
      E.empty.style.display = "";
      E.wrapEl.style.display = "none";
      return;
    }
    E.empty.style.display = "none";
    E.wrapEl.style.display = "";

    const desired = new Set(rows.map((r) => r.name));
    for (const [name, entry] of [...E.rows]) {
      if (!desired.has(name)) {
        entry.tr.remove();
        entry.detailTr.remove();
        E.rows.delete(name);
      }
    }

    for (const r of rows) {
      let entry = E.rows.get(r.name);
      const loopCount = this._roomLoops(r).length;
      // Rebuild if the loop wiring changed (e.g. after a config reload).
      if (entry && entry.loopCount !== loopCount) {
        entry.tr.remove();
        entry.detailTr.remove();
        E.rows.delete(r.name);
        entry = undefined;
      }
      if (!entry) {
        entry = this._buildValveRow(r);
        E.rows.set(r.name, entry);
      }
      // Append in sorted order (moving existing nodes is cheap, no flicker).
      E.tbody.appendChild(entry.tr);
      E.tbody.appendChild(entry.detailTr);
      this._updateValveRow(entry, r);
    }
  }

  _buildValveRow(r) {
    const p = {};
    const loops = this._roomLoops(r);
    const many = loops.length > 1;

    // Column 1: name (+ loop disclosure caret when there is more than one loop).
    p.caret = h(
      "button",
      {
        class: "loop-caret",
        type: "button",
        title: this._t("val_show_loops"),
        style: many ? "" : "display:none",
        on: {
          click: (e) => {
            e.stopPropagation();
            this._toggleValveLoops(r.name);
          },
        },
      },
      [this._icon("mdi:chevron-right", "▸")],
    );
    p.name = h("span", { class: "card-name", text: r.name });
    const nameCell = h("td", { class: "col-room" }, [
      h("div", { class: "name-cell" }, [p.caret, p.name]),
    ]);

    // Column 2: commanded valve % + mini-bar.
    p.cmdVal = h("span", { class: "valve-val" });
    p.cmdFill = h("span", { class: "valve-fill" });
    const cmdCell = h("td", null, [
      h("div", { class: "valve-mini" }, [
        p.cmdVal,
        h("div", { class: "valve-track" }, [p.cmdFill]),
      ]),
    ]);

    // Column 3: raw valve % (pre-limits), with the term breakdown in the title.
    p.raw = h("span", { class: "mono" });
    const rawCell = h("td", null, [p.raw]);

    // Column 4: heating valve-floor chip.
    p.floor = h("span");
    const floorCell = h("td", null, [p.floor]);

    // Column 5: saturation chip.
    p.sat = h("span");
    const satCell = h("td", null, [p.sat]);

    // Column 6: S2 cooling throttle chip.
    p.s2 = h("span");
    const s2Cell = h("td", null, [p.s2]);

    // Column 7: aggregated loop feedback %.
    p.feedback = h("span", { class: "mono" });
    const fbCell = h("td", null, [p.feedback]);

    const tr = h(
      "tr",
      {
        class: "valve-row",
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
      [nameCell, cmdCell, rawCell, floorCell, satCell, s2Cell, fbCell],
    );

    // Detail row: per-loop probes (feedback / supply / return / ΔT).
    const loopWrap = h("div", { class: "loop-list" });
    const loopRefs = [];
    loops.forEach((loop, i) => {
      const ref = this._buildLoopRow(loop, i);
      loopRefs.push(ref);
      loopWrap.appendChild(ref.rowEl);
    });
    if (!loops.length) {
      loopWrap.appendChild(
        h("div", { class: "loop-row muted", text: this._t("val_no_loops") }),
      );
    }
    const detailCell = h("td", { colspan: "7" }, [loopWrap]);
    const detailTr = h("tr", { class: "loop-detail", style: "display:none" }, [detailCell]);

    return { tr, detailTr, parts: p, loops: loopRefs, loopCount: loops.length, expanded: false };
  }

  _buildLoopRow(loop, index) {
    const label = h("span", {
      class: "loop-label",
      text: fmtStr(this._t("val_loop"), { n: index + 1 }),
    });
    const fbEl = h("span", { class: "mono" });
    const valveLink = h(
      "button",
      {
        class: "loop-link",
        type: "button",
        style: loop.valveId ? "" : "display:none",
        title: loop.valveId || "",
        on: {
          click: (e) => {
            e.stopPropagation();
            this._moreInfo(loop.valveId);
          },
        },
      },
      [fbEl, this._icon("mdi:open-in-new", "›")],
    );
    const supEl = h("span", { class: "mono" });
    const retEl = h("span", { class: "mono" });
    const dtEl = h("span", { class: "mono" });
    const rowEl = h("div", { class: "loop-row" }, [
      label,
      h("span", { class: "loop-metric" }, [valveLink]),
      h("span", { class: "loop-metric" }, [
        h("span", { class: "loop-cap", text: this._t("val_supply") }),
        supEl,
      ]),
      h("span", { class: "loop-metric" }, [
        h("span", { class: "loop-cap", text: this._t("val_return") }),
        retEl,
      ]),
      h("span", { class: "loop-metric" }, [
        h("span", { class: "loop-cap", text: this._t("val_dt") }),
        dtEl,
      ]),
    ]);
    return { loop, fbEl, supEl, retEl, dtEl, rowEl };
  }

  /** Toggle a room's loop-detail row open/closed (persists across polls). */
  _toggleValveLoops(name) {
    const entry = this._valvesEls && this._valvesEls.rows.get(name);
    if (!entry) {
      return;
    }
    entry.expanded = !entry.expanded;
    entry.detailTr.style.display = entry.expanded ? "" : "none";
    entry.parts.caret.classList.toggle("open", entry.expanded);
    entry.parts.caret.title = entry.expanded
      ? this._t("val_hide_loops")
      : this._t("val_show_loops");
  }

  _updateValveRow(entry, r) {
    const p = entry.parts;
    entry.tr.classList.toggle("selected", r.name === this._selectedRoom);

    // Command valve % + bar.
    const cmd = r.valve === null ? 0 : clamp(r.valve, 0, 100);
    p.cmdFill.style.width = cmd + "%";
    p.cmdVal.textContent = fmt(r.valve, 0, "%");

    // Raw valve % with the term contributions in the tooltip.
    p.raw.textContent = fmt(r.rawValve, 0, "%");
    p.raw.title = fmtStr(this._t("val_raw_tooltip"), {
      p: signed(r.pTerm, 1, "%"),
      i: signed(r.iTerm, 1, "%"),
      t: signed(r.trendTerm, 1, "%"),
      f: signed(r.ffTerm, 1, "%"),
    });

    // Floor chip (only when the heating valve floor was applied).
    if (r.valveFloor) {
      p.floor.className = "chip chip-warn";
      p.floor.textContent = fmtStr(this._t("val_floor_chip"), {
        v: fmt(r.valve, 0),
      });
    } else {
      p.floor.className = "muted";
      p.floor.textContent = "—";
    }

    // Saturation chip.
    if (r.saturated) {
      p.sat.className = "chip chip-warn";
      p.sat.textContent = this._t("yes");
    } else {
      p.sat.className = "muted";
      p.sat.textContent = "—";
    }

    // S2 cooling throttle: 0 → condensation, (0,1) → reduced flow, 1 → open.
    if (r.throttle !== null && r.throttle < 1) {
      if (r.throttle <= 0) {
        p.s2.className = "chip chip-problem";
        p.s2.textContent = this._t("val_s2_condensation");
      } else {
        p.s2.className = "chip chip-warn";
        p.s2.textContent = fmtStr(this._t("val_s2_flow"), {
          v: fmt(r.throttle * 100, 0),
        });
      }
    } else {
      p.s2.className = "muted";
      p.s2.textContent = "—";
    }

    // Aggregated loop feedback + per-loop rows.
    const positions = [];
    for (const ref of entry.loops) {
      const pos = this._valvePosition(ref.loop.valveId);
      ref.fbEl.textContent =
        pos === null
          ? this._t("wire_missing")
          : fmtStr(this._t("val_feedback_pos"), { v: fmt(pos, 0) });
      const sup = this._stateNum(ref.loop.supplyId);
      const ret = this._stateNum(ref.loop.returnId);
      ref.supEl.textContent = fmt(sup, 1, "°");
      ref.retEl.textContent = fmt(ret, 1, "°");
      ref.dtEl.textContent =
        sup !== null && ret !== null ? signed(sup - ret, 1, " K") : "—";
      if (pos !== null) {
        positions.push(pos);
      }
    }
    if (positions.length) {
      const avg = positions.reduce((s, v) => s + v, 0) / positions.length;
      p.feedback.textContent = fmt(avg, 0, "%");
      const mismatch = r.valve !== null && Math.abs(avg - r.valve) > VALVE_MISMATCH_PCT;
      p.feedback.classList.toggle("mismatch", mismatch);
    } else {
      p.feedback.textContent = "—";
      p.feedback.classList.remove("mismatch");
    }
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
    // conflict and the physical-state mismatch; B1 added the quiet hours).
    p.flags.textContent = "";
    for (const f of [
      "fast_source_min_runtime",
      "fast_source_quiet_hours",
      "fast_source_cannot_cool",
      "fast_source_group_conflict",
      "fast_source_mismatch",
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
      slope: fmt(hh.slope, 1),
      neutral: fmt(hh.neutral_c, 1, " °C"),
      tout: fmt(hh.t_out_c, 1, " °C"),
    }));

    // "Pump available for UFH" — only with a configured entity.
    P.activeCard.style.display = hp.hp_active_configured ? "" : "none";
    if (hp.hp_active_configured) {
      P.activeVal.textContent =
        hp.hp_active === null || hp.hp_active === undefined
          ? this._t("hp_active_unknown")
          : this._t(hp.hp_active ? "yes" : "no");
      P.activeVal.classList.toggle("warn", hp.hp_active === false);
    }
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
  // Tuning tab
  // --------------------------------------------------------------------------

  /** Build the Tuning section skeleton (populated lazily by `_renderTuning`). */
  _buildTuningSection() {
    const intro = h("div", { class: "tune-intro", text: this._t("tune_intro") });
    const scopeBar = h("div", { class: "tune-scopes", role: "group" });
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
      const chip = h("button", {
        class: "tune-scope" + (scope === this._tuningScope ? " active" : ""),
        type: "button",
        text: label,
        "aria-pressed": scope === this._tuningScope ? "true" : "false",
        on: { click: () => this._setTuningScope(scope) },
      });
      E.scopeBar.appendChild(chip);
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
    const badge = h("span", {
      class: "tune-badge",
      text: this._t("tune_overridden"),
      style: "display:none",
    });
    const revert = h("button", {
      class: "tune-revert",
      type: "button",
      title: this._t("tune_revert"),
      "aria-label": this._t("tune_revert"),
      style: "display:none",
      on: { click: () => this._revertTuneField(name) },
    });
    revert.appendChild(this._icon("mdi:backup-restore", "⟲"));
    const head = h("div", { class: "tune-card-head" }, [
      label,
      unit,
      info,
      badge,
      revert,
    ]);

    const refs = { field, badge, revert, valEl: null, minus: null, plus: null, toggle: null };
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
    refs.badge.style.display = overridden ? "" : "none";
    refs.revert.style.display = overridden ? "" : "none";
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

  /** Drop a room override for one knob, reverting it to the global value. */
  _revertTuneField(name) {
    if (!this._tuningDraft || this._tuningScope === "global") {
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
      return { ...draft.values };
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
.brand .brand-img { width: 22px; height: 22px; object-fit: contain; display: block; }
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

/* Tuning tab */
.tune { display: flex; flex-direction: column; gap: 14px; }
.tune-intro { font-size: 12px; color: var(--t-muted); line-height: 1.4; }
.tune-scopes { display: flex; flex-wrap: wrap; gap: 8px; }
.tune-scope {
  border: 1px solid var(--t-line); background: var(--t-card); color: var(--t-fg);
  border-radius: 999px; padding: 6px 14px; font-size: 13px; font-weight: 600;
}
.tune-scope:hover { border-color: var(--t-primary); }
.tune-scope.active { background: var(--t-primary); color: var(--t-on-primary); border-color: var(--t-primary); }
.tune-scope:focus-visible { outline: 2px solid var(--t-primary); outline-offset: 2px; }
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
.dot.sev-warn { background: var(--t-warn); }
.dot.sev-problem { background: var(--t-error); }
.name-cell { display: flex; align-items: center; gap: 8px; }
.card-name { font-weight: 600; font-size: 14px; }
.cell-sub { font-size: 11px; color: var(--t-muted); }
.meas-cell { display: flex; align-items: baseline; gap: 6px; }
.meas-val { font-size: 15px; font-weight: 600; }
.sp-cell { display: flex; flex-direction: column; gap: 1px; }
.sp-val { font-weight: 600; }
.err-val { font-weight: 600; white-space: nowrap; }
.trend { font-size: 12px; color: var(--t-muted); white-space: nowrap; }
.valve-mini { display: flex; flex-direction: column; gap: 3px; min-width: 64px; }
.valve-val { font-size: 13px; font-weight: 600; }
.valve-track { height: 6px; border-radius: 999px; background: var(--t-chip); overflow: hidden; }
.valve-fill { display: block; height: 100%; width: 0%; background: var(--t-primary); border-radius: 999px; transition: width .3s ease; }
.fast-badge { font-size: 12px; padding: 4px 10px; border-radius: 8px; display: inline-block; white-space: nowrap; }
.fast-badge.on { background: color-mix(in srgb, var(--t-accent) 16%, transparent); color: var(--t-accent); border: 1px solid var(--t-accent); }
.fast-badge.off { color: var(--t-muted); border: 1px solid var(--t-line); }
.fast-badge.none { color: var(--t-muted); border: 0; padding-left: 0; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { font-size: 11px; padding: 3px 9px; border-radius: 999px; background: var(--t-chip); color: var(--t-fg); }
.chip-warn { background: color-mix(in srgb, var(--t-warn) 20%, transparent); color: var(--t-warn); }
.chip-problem { background: color-mix(in srgb, var(--t-error) 18%, transparent); color: var(--t-error); }

/* Valves + Assist tables (share the room-table look) */
.valves-table, .assist-table { width: 100%; border-collapse: collapse; font-size: 13px; }
/* Sentence-case headers (A1) — consistent with the Rooms table. */
.valves-table thead th, .assist-table thead th {
  position: sticky; top: 0; z-index: 1; background: var(--t-card); text-align: left; white-space: nowrap;
  font-size: 12px; font-weight: 600;
  color: var(--t-muted); padding: 8px 10px; border-bottom: 1px solid var(--t-line);
}
.valves-table tbody td, .assist-table tbody td {
  padding: 8px 10px; border-bottom: 1px solid var(--t-line); vertical-align: middle;
}
.valve-row, .assist-row { cursor: pointer; transition: background-color .1s ease; }
.valve-row:hover, .assist-row:hover { background: var(--t-chip); }
.valve-row:focus-visible, .assist-row:focus-visible { outline: 2px solid var(--t-primary); outline-offset: -2px; }
.valve-row.selected, .assist-row.selected { box-shadow: inset 3px 0 0 var(--t-primary); }
.mono { font-family: ui-monospace, "Roboto Mono", monospace; font-size: 12px; font-weight: 600; white-space: nowrap; }
.mono.mismatch { color: var(--t-warn); }
.loop-caret { border: 0; background: transparent; color: var(--t-muted); cursor: pointer; display: inline-flex; padding: 2px; border-radius: 6px; transition: transform .15s ease; }
.loop-caret:hover { color: var(--t-fg); background: var(--t-chip); }
.loop-caret.open { transform: rotate(90deg); }
.loop-caret .hicon { --mdc-icon-size: 16px; }
.loop-detail td { background: color-mix(in srgb, var(--t-chip) 45%, transparent); }
.loop-list { display: flex; flex-direction: column; gap: 4px; }
.loop-row { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; font-size: 12px; padding: 2px 0; }
.loop-label { font-weight: 600; color: var(--t-muted); min-width: 60px; }
.loop-metric { display: inline-flex; align-items: baseline; gap: 4px; }
.loop-cap { font-size: 11px; color: var(--t-muted); text-transform: uppercase; letter-spacing: .03em; }
.loop-link { border: 0; background: transparent; color: var(--t-primary); cursor: pointer; display: inline-flex; align-items: center; gap: 3px; padding: 0; font: inherit; }
.loop-link:hover { text-decoration: underline; }
.loop-link .hicon, .loop-link .hicon-fallback { --mdc-icon-size: 14px; opacity: .6; }
.assist-actions { display: inline-flex; align-items: center; gap: 10px; }
.link-btn { border: 0; background: transparent; color: var(--t-primary); cursor: pointer; font: inherit; font-size: 12px; padding: 0; text-decoration: underline; }
.assist-none { font-size: 12px; margin-top: 10px; }
.chip-dew .chip-main { display: inline-flex; align-items: baseline; gap: 6px; }
.chip-dew .chip-sub { font-size: 11px; color: var(--t-muted); line-height: 1.3; max-width: 260px; }

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
.stat-cap { font-size: 11px; color: var(--t-muted); text-transform: uppercase; letter-spacing: .04em; }
.stat-val { font-size: 23px; font-weight: 700; line-height: 1.15; font-variant-numeric: tabular-nums; }
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
