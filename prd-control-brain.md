# PRD — „Control Brain": regulator klimatu per pomieszczenie (moduł Home Assistant)

> Status: draft do akceptacji właściciela (przed implementacją)
> Data: 2026-07-08
> Ten dokument opisuje **WYMAGANIA** produktu — co ma robić i czego wymaga od użytkownika — a nie sposób ich realizacji.

---

## 1. Cel i problem

Podłogówka w domu jest **bardzo bezwładna** (gruba wylewka, woda o małym ΔT), a klimatyzatory (splity) reagują **szybko**. Dziś regulację robią luźno powiązane automatyzacje: pozycje zaworów są wpisywane na sztywno, a szybkie źródła włącza/wyłącza prosty próg. Efekt: żaden pokój nie „dąży do zadanej temperatury" w oparciu o własny błąd regulacji — nie ma zamkniętej pętli per pomieszczenie.

Chcemy **jeden spójny moduł**, który robi to, co dziś częściowo ogarnia ręczny dashboard właściciela — ale lepiej i spójnie: **każde pomieszczenie to jeden mały, niezależny problem regulacji w zamkniętej pętli**. Moduł dla każdego pokoju liczy błąd `cel − temperatura` i wypuszcza **dwa wyjścia**: pozycję zaworu podłogówki (wolne, bazowe źródło) oraz decyzję o załączeniu szybkiego źródła (split/grzejnik). Sam algorytm jest **black-boxem** — na zewnątrz czysty kontrakt wejść/wyjść plus czytelny raport diagnostyczny.

Całość ma być **maksymalnie prosta w konfiguracji**: własna zakładka w menu bocznym HA, tabela pokoi, wskazanie encji z dropdownów — i tyle.

---

## 2. Zakres — co budujemy (v1)

- **Niezależny regulator per pomieszczenie** — jedno pomieszczenie = jeden kontroler z własnym stanem. Celowo **nie przesądzamy algorytmu** (PID czy cokolwiek innego — black box, §3); wymaganiem jest radzenie sobie z dużą bezwładnością pętli podłogowej — przeregulowanie to główny wróg.
- **Dwa wyjścia sterujące na pomieszczenie**: pozycja zaworów podłogówki (pomieszczenie może mieć **kilka pętli** — wszystkie dostają wspólną pozycję) oraz decyzja on/off (z kierunkiem) dla szybkiego źródła.
- **Globalna nastawa + offsety**: jeden cel „temperatura domu" dla całości, per pomieszczenie tylko **offset** od niego (cel pokoju = global + offset).
- **Black box z raportem** — czyste wejścia/wyjścia na zewnątrz, algorytm ukryty w środku, bogaty raport „pod maskę" do debugu i strojenia.
- **Prosty panel konfiguracyjny** we własnej zakładce menu bocznego HA: tabela pomieszczeń, wskazanie encji, ~~włączanie/wyłączanie udziału pokoju~~ **[ZAKTUALIZOWANO — patrz Aneks §8.11: udział pokoju to kanoniczny 3-stan `off`/`shadow`/`live`, nie bool]**, podgląd live.

---

## 3. Wymagania funkcjonalne — black box

Regulator każdego pomieszczenia to samodzielny black box: dostaje komplet wejść, zwraca komendy sterujące i raport. **Nie wie, skąd pochodzą dane** — źródło każdej wartości wybiera użytkownik w panelu. Algorytmu nie obchodzi, czy temperatura pokoju idzie z dedykowanego czujnika, z klimatyzatora, czy — gdyby właściciel tak zdecydował — z czujnika w zupełnie innym miejscu. **Ta sama encja może być źródłem dla wielu pomieszczeń** (np. korytarz bez własnego czujnika dostaje temperaturę salonu) — moduł nie wymusza unikalności wskazań ani nie blokuje „już użytych" encji. Black box rozwiązuje problem regulacji; skąd bierze pomiar, to sprawa konfiguracji.

### 3.1 Wejścia

Interfejs wejściowy (wartości **surowe**, bez wstępnego przetwarzania):

- **temperatura pomieszczenia** — pomiar regulowany,
- **temperatura wody zasilającej** (supply) — surowa wartość, per pętla,
- **temperatura wody powracającej** (return) — surowa wartość, per pętla; moduł **sam policzy ΔT**, jeśli go potrzebuje (podawanie gotowego ΔT zaciemniałoby obraz),
- **aktualne pozycje zaworów** — sprzężenie zwrotne z aktuatorów (pomieszczenie ma 1..n pętli),
- **aktualny stan / pozycja szybkiego źródła** — split lub grzejnik,
- **tryb**: grzanie / przejściowy / chłodzenie / off,
- **setpoint** — cel dla pomieszczenia (= globalna „temperatura domu" + offset pomieszczenia, §4),
- opcjonalnie **temperatura zewnętrzna**.

**Założenie infrastrukturalne: każda pętla ma sondy zasilania i powrotu** — projektujemy pod ich obecność (instalacja, której dziś ich brakuje, zostanie doposażona przed objęciem regulacją). Opcjonalna jest tylko temperatura zewnętrzna. Zniknięcie wejścia w trakcie pracy nie wywraca modułu — patrz bezpieczna degradacja (§5). Czujnik podłogi jest **świadomie poza zakresem** (§6).

Przykład (ilustracyjny, nie kontrakt implementacyjny):

```json
{
  "mode": "heat",
  "setpoint_c": 22.5,
  "room_temperature_c": 22.9,
  "outdoor_temperature_c": 20.8,
  "loops": [
    { "valve_position_pct": 0, "supply_temperature_c": 28.4, "return_temperature_c": 27.7 },
    { "valve_position_pct": 0, "supply_temperature_c": 28.1, "return_temperature_c": 27.6 }
  ],
  "fast_source": { "kind": "split", "state": "off" }
}
```

(Przykład pokazuje pomieszczenie z dwiema pętlami — otwartą strefę dzienną.)

### 3.2 Wyjścia

Dwie komendy sterujące:

1. **pozycja zaworów 0–100 %** (100 % = zawór otwarty maksymalnie) — pomieszczenie z wieloma pętlami to **jedna strefa**: regulator liczy jedną pozycję i wysyła ją do wszystkich swoich zaworów,
2. **czy załączyć szybkie źródło** — wartość logiczna (on/off); gdy trzeba, z **kierunkiem** (grzanie/chłodzenie zgodnie z trybem).

Plus **raport „podgląd pod maskę"** — wymaganie obserwowalności. Raport ma być czytelny zarówno dla **człowieka**, jak i dla **agenta AI** (ułatwia debug i strojenie) i zawierać co najmniej:

- błąd regulacji (`cel − temperatura`),
- prognozę / trend temperatury,
- składowe decyzji (co pchnęło pozycję zaworu i decyzję o szybkim źródle),
- flagi stanu (np. nasycenie wyjścia, brak czujnika, zadziałanie zabezpieczenia),
- krótkie wyjaśnienie tekstowe „co i dlaczego".

Przykład (ilustracyjny):

```json
{
  "valve_position_pct": 34,
  "fast_source_on": true,
  "report": {
    "error_c": -0.4,
    "trend_c_per_h": 0.3,
    "flags": ["fast_source_min_runtime"],
    "explanation": "Grzanie, błąd −0.4 K. Zawór 34%. Split ON do dojścia do celu."
  }
}
```

Kontrakt jest **stały niezależnie od pomieszczenia i wyposażenia** (z szybkim źródłem lub bez, z sondami wodnymi lub bez) — pola nieobecne są po prostu puste. Na zewnątrz widać wyłącznie I/O + raport; algorytm w środku pozostaje ukryty.

### 3.3 Semantyka trybów

- **Grzanie** — zawory regulowane w zamkniętej pętli; szybkie źródło wspiera wyłącznie w kierunku grzania.
- **Przejściowy** — zawory **zaparkowane** (pozycja spoczynkowa); reguluje wyłącznie szybkie źródło, **dwukierunkowo** (grzeje albo chłodzi, zależnie od znaku błędu).
- **Chłodzenie** — **[ZAKTUALIZOWANO — patrz Aneks §8.4: floor cooling wchodzi do v1]** zawory **regulowane** do chłodzenia podłogą (PI z odwróconym znakiem) z dwuwarstwową ochroną punktu rosy; dodatkowo chłodzi szybkie źródło.
- **Off** — moduł sprowadza pomieszczenie do stanu spoczynku (zawory zaparkowane, szybkie źródło off) i nie reguluje.

---

## 4. Wymagania UI (rdzeń dokumentu)

Cel: **maksymalna prostota**. Użytkownik wchodzi, przechodzi po wierszach, wskazuje encje z dropdownów, zaznacza udział — koniec.

### 4.1 Zakładka i tabela

- Moduł ma **własną zakładkę w menu bocznym HA** (jak HACS). Nazwa: **„Tortoise-UFH"** (UFH = underfloor heating; żółw = wolna, bezwładna podłogówka — szybkie źródło to zając, który ją wspiera). Ikona `mdi:tortoise`, domena techniczna integracji: `tortoise_ufh`, repo: **`tortoise-ufh`**.
- Nad tabelą jest **globalna nastawa „temperatura domu"** — jedno pole dla całego domu. Cel pomieszczenia = ta wartość + offset pomieszczenia (kolumna w tabeli).
- Po wejściu widać **tabelę generowaną z listy pomieszczeń** (areas zdefiniowanych w HA). **Jeden wiersz = jedno pomieszczenie.** Listę pomieszczeń moduł bierze z HA — użytkownik nie wpisuje jej ręcznie.

### 4.2 Kolumny (jeden wiersz = jedno pomieszczenie)

| Kolumna | Treść |
|---|---|
| **Nazwa** | nazwa pomieszczenia (z HA) |
| **Temp pomieszczenia** | wskazanie encji (dropdown) |
| **Temp wody zasilającej** | wskazanie encji (dropdown), **per pętla** — przy wielu zaworach po jednej sondzie na pętlę |
| **Temp wody powracającej** | wskazanie encji (dropdown), **per pętla** — j.w. |
| **Zawory** | wskazanie **jednego lub wielu** aktuatorów (multi-select) — otwarta strefa może mieć kilka pętli |
| **Typ szybkiego źródła** | wybór: **brak / split / grzejnik**; gdy split lub grzejnik → wskazanie encji tego źródła |
| **Offset temp** | offset pomieszczenia względem globalnej „temperatury domu" (np. −1,0 K); cel pokoju = global + offset |
| **Udział** | czy pomieszczenie bierze udział w regulacji — ~~włącz/wyłącz~~ **[ZAKTUALIZOWANO — §8.11: 3-stan `off` / `shadow` / `live`]** |

### 4.3 Zasady wskazywania encji

- Wskazanie encji = **dropdown przefiltrowany do encji odpowiedniego typu/domeny** (np. kolumna zaworu pokazuje encje zaworów). Bez czarów, bez automatycznego przypisywania.
- Encje **nie są zaszyte** w produkcie — użytkownik wskazuje je sam. To jego wybór, skąd moduł bierze każdą wartość.
- **Bez wymogu unikalności** — ta sama encja może być wskazana w wielu pomieszczeniach (np. temperatura salonu jako źródło dla korytarza). Dla algorytmu to po prostu pomiar temperatury pomieszczenia.
- Po wskazaniu encji użytkownik widzi jej **bieżącą wartość** (potwierdzenie, że wskazał właściwą).

### 4.4 Podgląd live

- Dla pomieszczeń biorących udział panel pokazuje **live**: cel, temperaturę, błąd, pozycję zaworu, stan szybkiego źródła, status — oraz pełny **raport** (§3.2) jako „okno do black-boxa".
- Surowa pozycja zaworu jest w podglądzie **read-only** — użytkownik steruje celem (globalna „temperatura domu" + offset pokoju), nie ręcznie zaworem.

### 4.5 Strojenie — „Zaawansowane"

Parametry regulatora są **domyślnie schowane** — moduł startuje z sensownymi wartościami per pomieszczenie. Ręczna korekta jest możliwa w sekcji „Zaawansowane" (plant jest wolny i różny w każdym pokoju), ale **nigdy nie jest wymagana** do uruchomienia.

> **[v0.3.0]** Zrealizowane jako zakładka **„Strojenie"** panelu: wartości **globalne** + rzadkie
> **nadpisania per pokój** (`entry.options[CONF_ROOM_TUNING]`; wyczyszczone nadpisanie = powrót do
> globalnych), przez komendy WS `tortoise_ufh/get_tuning` / `set_tuning`. Zmiana strojenia
> przeładowuje wpis (czysta przebudowa regulatora); zmiana samego 3-stanu pokoju — nie (§8.11).

---

## 5. Wymagania niefunkcjonalne

- **Prostota konfiguracji.** Skonfigurowanie pomieszczenia = wskazanie kilku encji i zaznaczenie udziału. Bez edycji plików, YAML-a czy znajomości wnętrza modułu.
- **Obserwowalność.** Każda decyzja regulatora musi być **wytłumaczalna przez raport** (§3.2), bez czytania logów. Raport ma być jednocześnie czytelny dla człowieka i parsowalny maszynowo (dla agenta AI).
- **Bezpieczna degradacja.** Pomieszczenie nieskonfigurowane lub bez wskazanego czujnika temperatury **nie robi zamkniętej pętli** i nie steruje „w ciemno" — jest bezpiecznie pomijane, dopóki użytkownik nie uzupełni konfiguracji. Utrata czujnika w trakcie pracy przełącza pomieszczenie w **stan bezpieczny** (zawór w pozycji bezpiecznej, szybkie źródło off) i zgłasza to w raporcie.
- **Ochrona posadzki bez czujnika podłogi.** Zabezpieczenie przed przegrzaniem opiera się na temperaturze wody zasilającej (sondy per pętla — założenie infrastrukturalne, §3.1) i ostrożnych zakresach pozycji — nie na czujniku podłogi (poza zakresem).
- **Zastąpienie dotychczasowych automatyzacji, nie koegzystencja z nimi.** Moduł przejmuje regulację per pomieszczenie w całości: dotychczasowe automatyzacje piszące po zaworach i szybkich źródłach (koordynator pozycji bazowych, bramka splitów, strażnicy zaworów) zostają przy wdrożeniu **wyłączone/usunięte** — ich funkcje ochronne przejmują zabezpieczenia modułu. Na zewnątrz zostają tylko: **globalny tryb domu** (wejście modułu), ~~**nadrzędny wyłącznik awaryjny** (kill-switch: off = moduł nie wydaje żadnych komend)~~ **[USUNIĘTE w v0.3.0 (2026-07-09) — patrz §8.11: „wszystko stop" = wszystkie pokoje w `off`/`shadow`]** oraz **właściciel strony wodnej** (pompa ciepła / CWU — poza zakresem, §6). Dla zaworów i szybkich źródeł pomieszczeń z udziałem moduł jest **jedynym właścicielem**.
- **Trwałość konfiguracji.** Ustawienia pomieszczeń przeżywają restart HA.

---

## 6. Poza zakresem (v1) — jawnie

- **Sterowanie pompą ciepła i stroną wodną.** Duży bilans domu i stronę wodną załatwia **własny regulator pompy ciepła**. Moduł **nie steruje pompą** i nie robi MPC / optymalizacji horyzontu.
- **Model-learning / optimum-start / predykcja pogodowa** — przyszłość, nie v1.
- **Czujnik podłogi** — właściciel go nie ma; nie projektujemy pod niego.
- ~~**Chłodzenie podłogą i ochrona punktu rosy**~~ — **PRZENIESIONE DO v1** (patrz Aneks §8.4): floor cooling z dwuwarstwową ochroną kondensacji (globalny punkt rosy dla PC + lokalne dławienie zaworu per pokój) jest w zakresie v1.
- **Rekuperator, CO₂, free-cooling** — nietknięte (osobne automatyzacje).

---

## 7. Rozstrzygnięcia (dawne otwarte pytania)

1. **Szybkie źródło w pokojach bez splita i bez grzejnika** — pomieszczenie zostaje **„tylko pętla"**: wolna regulacja podłogówką, bez boostu. Nie dokładamy grzejników (typ „grzejnik" pozostaje wspierany na przyszłość).
2. **Strojenie regulatora** — domyślnie schowane, sensowne wartości startowe per pomieszczenie, ręczna korekta w „Zaawansowanych" (§4.5).
3. **Tryb „przejściowy"** — zawory zaparkowane, reguluje wyłącznie szybkie źródło, dwukierunkowo (§3.3).
4. **Nazwa i ikona zakładki** — **„Tortoise-UFH"** (żółw = podłogówka / UFH, zając = szybkie źródło), `mdi:tortoise`, domena `tortoise_ufh`, repo `tortoise-ufh` (§4.1).

---

## 8. Aneks decyzyjny — ustalenia przed implementacją (ULTRACODE, 2026-07-08)

> Ten aneks jest **nadrzędny** wobec wcześniejszych zapisów tam, gdzie występuje sprzeczność
> (w szczególności: **floor cooling wchodzi do v1** — §8.4 — co zmienia §3.3 i §6). Powstał z wywiadu
> 10 pytań właściciela + doprecyzowań. Utrwala kontrakt, wg którego budowany jest kod. Wzorzec
> strukturalny: bliźniaczy projekt `pump-ahead` (HA + symulator RC).

### 8.1 Architektura i pakowanie
- Custom integration `tortoise_ufh` (HACS-installable). **Twardy podział na dwie warstwy:**
  **rdzeń `tortoise_ufh/`** — czysty Python (numpy/scipy), **nigdy nie importuje `homeassistant`**,
  `py.typed`, w pełni testowalny offline; **adapter HA `custom_components/tortoise_ufh/`** —
  coordinator + encje + config flow + websocket + panel.
- **Własny panel boczny** „Tortoise-UFH" (`mdi:tortoise`, url `tortoise-ufh`) — samodzielny moduł JS
  bez build-stepu, rozmawia z integracją przez komendy websocket. Panel = główny podgląd/konfiguracja;
  dodatkowo szablon dashboardu Lovelace jako fallback.
- Pakowanie: `pyproject.toml` (setuptools, py≥3.12), `hacs.json {name, homeassistant, render_readme}`,
  `manifest.json` (integration_type `hub`, iot_class `local_polling`, config_flow, requirements =
  zależności rdzenia). Licencja spójna w pyproject/LICENSE/README.

### 8.2 Konfiguracja i trwałość
- Konfiguracja pokoi w config entry (`entry.data`/`entry.options`) — **przeżywa restart HA**.
- **Globalna „temperatura domu"** i **per-pokój offset** jako **zapisywalne encje `number`** (sterowalne
  z dashboardów/skryptów). Cel pokoju = global + offset.
- Per-pokój flagi: **udział** oraz **udział w chłodzeniu** (np. łazienka wykluczona z cool).
  > **Zmienione w v2 (v0.3.0, 2026-07-09) — patrz §8.11.** Flaga udziału scalona w 3-stan
  > `RoomControlState`; osobną flagą per pokój pozostaje tylko udział-w-chłodzeniu.
- Encje wskazywane w panelu dropdownami filtrowanymi po domenie/device_class; **bez wymogu unikalności**.

### 8.3 Algorytm rdzenia (black-box) — pętla podłogówki
- **Zawory proporcjonalne 0–100%, trzymają nastawę.** Wyjście = jedna liczba pozycji na pokój (strefa).
  **Bez PWM/TPI, bez cyklowania aktuatora.**
- **Regulator: pojedyncza pętla PI na błędzie `T_room` + człon trendu (`dT_room/dt`)** tłumiący
  przeregulowanie (główny wróg przy dużej bezwładności). **Bez członu D po błędzie.** Anti-windup
  (back-calculation/clamp), **deadband**, **valve-floor** (min. otwarcie w gotowości grzewczej).
  > **Zmienione 2026-07-09 (faza C; JAWNY rewers domyślnych nastaw — patrz `docs/DECISIONS.md` §8
  > z tabelą sweepa i BUILD_SPEC §6).** Stare domyślne `kp=8 / ki=0.02 / kt=6` dawały Ti≈7 min —
  > o rząd za agresywnie dla wylewki o τ=3-6 h (zmierzone +1,2 K przeregulowania i cykl graniczny
  > ±0,6 K). **Nowe domyślne, dobrane empirycznie sweepem na SKALIBROWANYM bliźniaku:
  > `kp=14 / ki=0.0015 (Ti≈2,6 h) / kt=12`** — przeregulowanie ≤ +0,2 K, ogon 24-48 h w 100 %
  > w ±0,3 K, ruch zaworu ~1 pp/h. **Trend jest FILTROWANY (S10):** surowa próbka dT/dt dopiero po
  > skumulowaniu ≥ 60 s (recompute po 2 s TRZYMA poprzednią wartość), potem EMA τ=15 min — dopiero
  > na tym sygnale działa kt=12. **Higiena integratora (S1/S2):** zamrożenie gdy dławienie rosy
  > S2 < 1 (dławienie mnoży zawór ZA regulatorem — bez tego godziny dławienia bankują windup);
  > reset przy zmianie GRZANIE↔CHŁODZENIE; wygaszenie po > 12 h bezczynności
  > (OFF/przejściowy/utrata czujnika). Stałe sprzężenia pogodowego (neutral/wzmocnienie/limit)
  > przeniesione do `ControllerConfig` jako knoby (control-F6).
- Brak sondy wylewki (`T_slab`). Zakładamy, że woda zasilająca jest zawsze odpowiednio ciepła/zimna
  (stronę wodną reguluje PC — poza zakresem). `T_slab` można w przyszłości *oszacować* z supply/return
  (nie cieplejszy niż zasilanie) — opcja, niekrytyczna dla v1.
- Feedforward od `T_out` (opcjonalny) jako korekta bazowego otwarcia/wzmocnienia — nie sterujemy
  temperaturą zasilania (to robi PC).
- **Świadomość strony wodnej:** ~~przy CWU/odszranianiu zamrażaj integrator~~ — **ODPADA (2026-07-08):
  właściciel ma bufor**, więc przerwy na CWU/odszranianie nie odcinają wody od podłogówki i nie są
  problemem. Plumbing pozostaje w kodzie jako uśpiona, opcjonalna funkcja (`CONF_ENTITY_HP_ACTIVE`,
  domyślnie wyłączona / `None`), ale nie jest wymagana ani używana w v1.
- **Cykl regulacji: co 5 min** (regulowalny); zapis pozycji przy zmianie ≥ próg.

### 8.4 Chłodzenie — floor cooling **w v1** (zmiana zakresu vs §6)
- v1 obsługuje **chłodzenie podłogą** (PI z odwróconym znakiem; zawory **regulowane**, nie zamknięte)
  **oraz** chłodzenie splitem.
- **Ochrona przeciw kondensacji (twarde zabezpieczenie), dwuwarstwowo:**
  - **Globalna (primary):** moduł liczy per-pokój punkt rosy (Magnus z `RH` + `T_room`), bierze
    **maksimum po pokojach chłodzonych** i **dodaje 2 K**, po czym **wystawia gotową bezpieczną
    wartość** (encja) — właściciel podaje ją do PC jako dolny limit temp. wody chłodzącej. Moduł
    **nie steruje wodą**, tylko dostarcza bezpieczną wartość.
  - **Lokalna (secondary, per-pokój):** jeśli **zmierzona temp. wody zasilającej** pokoju (najzimniejsza
    z jego pętli) spadnie do `T_dew_pokój + 2 K`, moduł **graduowanie przymyka, a przy przekroczeniu
    twardo zamyka** zawór chłodzenia tego pokoju — niezależnie od PC (defense-in-depth, S2), z histerezą.
- **Wymóg pomiarowy:** **`RH` per pokój chłodzony** (encja `sensor` humidity) — nowe wejście vs pierwotny PRD.

### 8.5 Szybkie źródło (split)
- Komenda: **`ON + tryb (grzanie/chłodzenie) + temperatura pokoju`** (cel = global + offset). Split sam
  się wysteruje (nie dotykamy mocy sprężarki). Realizacja: `climate.set_hvac_mode` + `climate.set_temperature`.
  > **Zmienione 2026-07-09 (faza B; patrz `docs/DECISIONS.md` §7 i BUILD_SPEC §5.2 kroki 3/14).**
  > **(S12) Cel splitu w grzaniu = `cel + 1,0 K`, w chłodzeniu = `cel − 1,0 K`** (stała, nie knob):
  > czujnik splitu przy suficie czyta cieplej, więc cel równy nastawie dławił jednostkę zanim boost
  > dowiózł — zwolnienie (release) nadal należy do NASZEGO czujnika pokojowego (histereza + min-ON).
  > W trybie przejściowym cel = nastawa (bez offsetu).
- **Załączenie:** gdy `|T_room − cel|` > **boost-offset** (regulowany; walidacja **boost-offset >
  deadband** — inaczej histereza załącz/zwolnij się odwraca, D2 2026-07-09). Ochrona sprężarki:
  **min. czas ON i OFF** (anti-short-cycle).
  > **Zmienione 2026-07-09 (C6, faza B):** kierunek splitu jest **stanem maszyny trójstanowej
  > OFF/GRZANIE/CHŁODZENIE** — zmiana kierunku WYŁĄCZNIE przez OFF z pełnym min-OFF; hold min-ON
  > re-emituje ZAPAMIĘTANY kierunek (nigdy świeżo policzony). Powód twardy: jednostki wewnętrzne
  > mogą dzielić wspólny agregat multisplit — mieszanie kierunków to konflikt trybów na agregacie.
  > Jedyny wyjątek: awaryjne S3/S4 może ustawić kierunek natychmiast (mróz > higiena sprężarki).
  > **(S4)** Maszyna konsumuje fizyczny stan splitu: pierwszy odczyt `fast_source_on` wygrywa nad
  > zimną maszyną, a timer dwell jest zasiewany konserwatywnie (pełny dwell po restarcie/reloadzie
  > — koniec z short-cyclingiem przy pętli restartów); późniejszy rozjazd komenda↔stan daje flagę
  > `fast_source_mismatch`. **(S3)** Adapter nie spamuje komendami: niezmieniona para
  > (tryb, cel) nie jest wysyłana co cykl, a co ~45 min następuje re-assert (samonaprawa po
  > ręcznej zmianie).
- **Koordynacja (anti priority-inversion):** podłoga zawsze bazą i **nie zamyka się** tylko dlatego, że
  split dogrzał/dochłodził; split dobija ponad próg i odpuszcza po wejściu w pasmo komfortu.

### 8.6 Tryby
- **Jeden globalny tryb domu**: `grzanie / przejściowy / chłodzenie / off` (encja wejściowa). Per-pokój
  tylko udział (od v0.3.0: 3-stan — §8.11) + udział-w-chłodzeniu + offset. Przejściowy: zawory
  zaparkowane, reguluje wyłącznie split dwukierunkowo. Off: pokój do spoczynku, brak komend.
  > **Zmienione 2026-07-09 (S12, faza B):** przejściowy BEZ biasu −0,65 K. Stara histereza
  > [cel−1,0; cel−0,3] trzymała pokój w całości poniżej nastawy. Teraz: załącz przy
  > `|błąd| > boost-offset`, split pracuje z **celem = nastawie** (jego własna regulacja trzyma
  > pokój NA nastawie), zwalnia dopiero gdy pokój przekroczy pasmo komfortu PO PRZECIWNEJ
  > stronie (darmowe zyski niosą pokój dalej), z poszanowaniem min-ON.

### 8.7 Bezpieczeństwo i degradacja
- **Utrata czujnika pokoju:** **zawór zamraża ostatnią pozycję**, **split → OFF**, flaga w raporcie.
  > **Zmienione 2026-07-09 (faza A hardeningu; JAWNY rewers części tej decyzji — patrz
  > `docs/DECISIONS.md` §6 i BUILD_SPEC §5.2 krok 1.** Freeze ostatniej pozycji obowiązuje
  > **tylko w GRZANIU**. W **CHŁODZENIU** utrata temperatury pokoju ⇒ **zawór 0** (nigdy
  > freeze-open): bez `T_room` pokój wypada z globalnego maksimum punktu rosy ORAZ lokalne S2
  > nie może się policzyć — zamrożony otwarty zawór puszczałby niechronioną zimną wodę bez
  > ograniczeń czasowych. Dodatkowo: hold pamięta ostatnią pozycję **zdrowej regulacji**
  > (nigdy awaryjne 0/100 z nadpisania safety), a zamknięcie zaworu przez S1/S2 **nie gasi**
  > źródła powietrznego, gdy aktywne jest S3/S4 (strona wodna i powietrzna decydowane
  > niezależnie). Nowość: **komenda pożegnalna** — przejście pokoju `live → shadow/off` oraz
  > unload wpisu parkują aktuatory jednorazowo (split OFF zawsze; zawór 0 w chłodzeniu,
  > w grzaniu pozycja zostaje — woda grzewcza jest ograniczona krzywą PC). Wejścia adaptera są
  > plauzybilizowane (zakres −10..50 °C, bramka skoku 4 K/cykl z potwierdzeniem 2 próbek,
  > wiek stanu: temperatura 45 min / wilgotność 60 min ⇒ jak brak odczytu), a globalny tryb
  > jest persystowany w Store (restart w lipcu nie wraca do logiki grzewczej).
- **Watchdog:** brak świeżych danych > 15 min → stan awaryjny/alarm w raporcie (recovery po 5 min).
  > **Zmienione 2026-07-09 (S6, faza E; patrz `docs/DECISIONS.md` §8).** Watchdog per-pokój (S5)
  > jest ŻYWY: adapter podaje rdzeniowi realny wiek danych pokoju
  > (`RoomInputs.last_update_age_minutes`), a akcja S5 to **pozycja neutralna** — `valve_floor`
  > w grzaniu (dom trzymany krzywą PC), 0 w chłodzeniu — zamiast twardego 0. Drabinka eskalacji
  > milczącego pokoju: freeze/hold (utrata czujnika po ~45 min stęchłego stanu) → pozycja
  > neutralna (S5, ~15 min później). Watchdog budynkowy adaptera pozostaje report-only; dodatkowo
  > raport budynku niesie licznik `sensor_lost_rooms` (bez nowej encji).
- Ochrona posadzki bez sondy podłogi: temp. wody zasilającej + ostrożne zakresy pozycji.
- Moduł jest **jedynym właścicielem** zaworów i splitów pokoi z udziałem. Na zewnątrz: globalny tryb,
  kill-switch (off = brak komend), właściciel strony wodnej (PC/CWU).
  > **Zmienione w v2 (v0.3.0, 2026-07-09) — patrz §8.11.** Kill-switch oraz per-pokojowe boole
  > udziału/live-control scalono w jeden kanoniczny 3-stan pokoju (`off` / `shadow` / `live`).
  > „Wszystko stop" = wszystkie pokoje w `off`/`shadow`.

### 8.8 Trzy wyjścia + raport (kontrakt)
1. **Per-pokój:** pozycja zaworów 0–100% (jedna na strefę).
2. **Per-pokój:** komenda szybkiego źródła (`ON + tryb + temp. pokoju`).
3. **Globalne:** bezpieczny punkt rosy `max_i(T_dew_i) + 2 K` (encja `sensor`, °C) do PC.
4. **Raport „pod maskę"** per pokój: błąd, trend, składowe decyzji, flagi (nasycenie, brak czujnika,
   zabezpieczenie), tekstowe „co i dlaczego" — **czytelny dla człowieka i agenta AI**.

### 8.9 Wdrożenie i jakość
- **Shadow / dry-run** przełącznikiem: liczy i loguje pełny raport, **nie wysyła komend**; potem LIVE
  (per-pokój przejmowanie). Panel daje dobry podgląd (człowiek + agent AI).
  > **v2 (v0.3.0):** „shadow/live" per pokój to teraz stany kanonicznego 3-stanu
  > `RoomControlState` (§8.11), a nie osobny przełącznik obok flagi udziału.
- **Symulator (cyfrowy bliźniak)** wzorowany na `pump-ahead` (RC 3R3C ZOH przez `expm`,
  `BuildingSimulator`/`SimulatedRoom`, `SyntheticWeather`, `SensorNoise`, `SimMetrics` + asercje) — do
  strojenia PID offline i testów scenariuszowych. `T_slab` istnieje jako ground-truth w symulatorze, ale
  **regulator go nie dostaje**.
- Testy dwuwarstwowe (unit TDD + symulacja), `mypy --strict`, `ruff`, `filterwarnings=error`.

### 8.10 Jawnie poza v1 (bez zmian)
- MPC / optymalizacja horyzontu / taryfy, model-learning / identyfikacja RC online, sterowanie pompą
  ciepła i stroną wodną, sonda podłogi, rekuperator/CO₂/free-cooling. (Floor cooling **przeniesione DO
  v1** — §8.4.)

### 8.11 Rewizja (v2) — kanoniczny 3-stan pokoju zamiast kill-switcha
> **To JAWNY rewers zamrożonej decyzji z §8.7/§8.9.** Odnotowane jako świadoma, datowana zmiana
> kontraktu (nie dryf): **2026-07-09, wydana w v0.3.0** (zmiana łamiąca; migracja config entry
> v1→v2). Szczegóły i tabela stanów: `docs/DECISIONS.md` §4.

Pierwotny wywiad zamroził **trzy** oddzielne sterowania udziałem: per-pokojową **flagę udziału**
(§8.2), per-pokojowy **przełącznik shadow/live** (§8.9) oraz **globalny kill-switch** (§8.7). W praktyce
kodowały jedno pytanie na pokój — *ile autorytetu ma tu Tortoise-UFH?* — ze stanami nadmiarowymi i
zachodzącymi na siebie, a kill-switch był słabszy niż „wszystkie pokoje w shadow".

**Decyzja (v2):** scalić je w **jeden `RoomControlState` na pokój**:
- `off` — pokój poza sterowaniem (rdzeń dostaje `Mode.OFF`: raportowany zawór 0 % — spoczynek,
  szybkie źródło off); liczy i raportuje, **nie pisze** — fizyczny aktuator pozostaje nietknięty.
- `shadow` — liczy i raportuje, **nie pisze** (dry-run). **Domyślny** dla nowego pokoju.
- `live` — liczy, raportuje **i pisze** do sprzętu.

Wyprowadzenia: `udział := stan != off`, `pisz := stan == live`. Źródło prawdy:
`entry.options[CONF_ROOM_STATE] = {pokój: stan}`. „Wszystko stop" = wszystkie pokoje `off`/`shadow`.
Powierzchnie: encja `select` (`control_state`), panel, komenda WS `tortoise_ufh/set_room_state`, krok
`settings` w options-flow (awaryjne UI). Zmiana samego stanu kanoniczną ścieżką (select/panel/WS)
**nie przeładowuje** wpisu (integrator PID zachowany); zapis mapy stanów wprost z options-flow,
niezgodny ze stanem w pamięci, nadal wymusza przeładowanie. **Migracja v1→v2** (jednorazowa): precedencja bezpieczeństwa — `udział == false` ⇒ `off`
(wygrywa nad `live_control == true`); inaczej `live_control` decyduje `live`/`shadow`; klucze
`participates`/`live_control`/`kill_switch` oraz encje `switch.*_kill_switch` / `switch.*_live_control`
zostają usunięte (zastąpione przez `select.*_control_state`). Bez zmian: trzy wyjścia (§8.8), degradacja
przy utracie czujnika (§8.7), warstwy punktu rosy (§8.4), prawo sterowania (§8.3).
