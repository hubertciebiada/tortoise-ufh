# Tortoise-UFH — instrukcja użytkownika

> **Język / Language / Sprache:** **Polski** · [Deutsch](de.md) · [English (README)](../../README.md)

> Dotyczy wersji **0.12.0**. Instrukcja opisuje integrację po polsku; interfejs
> (panel, encje, kreator konfiguracji, usługi) jest dostępny po polsku i po
> angielsku i podąża za językiem ustawionym w Twoim profilu Home Assistant.

## Spis treści

1. [Czym jest Tortoise-UFH](#1-czym-jest-tortoise-ufh)
2. [Jak to działa — pojęcia po ludzku](#2-jak-to-działa--pojęcia-po-ludzku)
3. [Instalacja (HACS)](#3-instalacja-hacs)
4. [Konfiguracja krok po kroku](#4-konfiguracja-krok-po-kroku)
5. [Panel boczny](#5-panel-boczny)
6. [Encje i usługi](#6-encje-i-usługi)
7. [Stan sterowania: Wyłączony / Steruje](#7-stan-sterowania-wyłączony--steruje)
8. [Parametry strojenia — pełna lista](#8-parametry-strojenia--pełna-lista)
9. [Chłodzenie podłogowe i kondensacja](#9-chłodzenie-podłogowe-i-kondensacja)
10. [Szybkie źródło i multisplit](#10-szybkie-źródło-i-multisplit)
11. [Pompa ciepła (opcjonalne sprzężenie)](#11-pompa-ciepła-opcjonalne-sprzężenie)
12. [Flagi i alarmy — słownik](#12-flagi-i-alarmy--słownik)
13. [Rozwiązywanie problemów](#13-rozwiązywanie-problemów)
14. [Ograniczenia, FAQ i historia wersji](#14-ograniczenia-faq-i-historia-wersji)

---

## 1. Czym jest Tortoise-UFH

Tortoise-UFH to regulator klimatu **per pokój** dla domu z **bezwładnym ogrzewaniem
podłogowym** (grzanie **i chłodzenie** podłogą), z opcjonalną asystą szybkiego źródła
(klimatyzator split lub grzałka). Zwykły termostat włącza i wyłącza obieg, gdy
temperatura przekroczy próg — przy podłodze, która reaguje z opóźnieniem kilku godzin,
kończy się to huśtawką: raz za zimno, raz za ciepło. Tortoise-UFH zamiast tego
**płynnie ustawia otwarcie zaworów (0–100 %)** na podstawie uchybu, jego historii
i tempa zmian, dzięki czemu bezwładna podłoga dochodzi do zadanej temperatury bez
przestrzeleń. Każdy pokój ma własny, niezależny regulator, własną korektę temperatury
i własny raport „co i dlaczego zrobiłem".

## 2. Jak to działa — pojęcia po ludzku

**Uchyb** to różnica: temperatura zadana minus zmierzona. Dodatni = za zimno.

**Regulator PI + trend** składa otwarcie zaworu z trzech członów:

- **P (proporcjonalny)** — reaguje na bieżący uchyb: im zimniej, tym szerzej otwiera.
- **I (całkujący)** — patrzy na uchyb utrzymujący się w czasie. Jeżeli przez godziny
  jest odrobinę za zimno, człon I powoli dokłada otwarcia, aż niedogrzew zniknie.
  Tę „pamięć" nazywamy **całką** (integratorem); bywa celowo **zamrażana** (np. gdy
  pompa ciepła robi CWU), żeby nie narosła fałszywie.
- **Trend (tłumienie)** — patrzy, jak szybko temperatura się zmienia. Gdy pokój
  szybko zmierza do zadanej, przymyka zawór **zawczasu** — to główny hamulec
  przestrzeleń bezwładnej podłogi.

Do tego dochodzi opcjonalne **sprzężenie pogodowe (FF)** — bazowe otwarcie zależne od
temperatury zewnętrznej — oraz limity: minimalne otwarcie przy grzaniu, obcięcie do
0–100 % i reguły bezpieczeństwa.

**Tryby globalne** (jeden dla całego domu): **grzanie**, **chłodzenie**,
**przejściowy** (zawory zaparkowane, split może grzać lub chłodzić — wiosna/jesień)
i **wyłączony**. **Stan pokoju** to co innego: per pokój wybierasz **Wyłączony** albo
**Steruje** — patrz §7.

**Punkt rosy** to temperatura, poniżej której para wodna z powietrza skrapla się na
zimnej powierzchni. Przy chłodzeniu podłogą to główne ryzyko (mokra podłoga), dlatego
działają dwie niezależne ochrony — patrz §9.

**Asysta splita**: gdy uchyb przekroczy próg dogrzewu, integracja włącza klimatyzator
komendą „włącz + kierunek + temperatura zadana" — split reguluje się dalej sam.
Podłoga zawsze zostaje źródłem bazowym. Timery minimalnego czasu pracy i postoju
(**dwell**) chronią sprężarkę przed taktowaniem. Pokoje na wspólnym agregacie
(**multisplit**) muszą grzać albo chłodzić razem — patrz §10.

**Flagi** to krótkie kody diagnostyczne w raporcie pokoju (np. `sensor_lost`),
mówiące, które zabezpieczenie lub ograniczenie zadziałało — słownik w §12.

## 3. Instalacja (HACS)

1. W Home Assistant otwórz **HACS → menu (trzy kropki) → Custom repositories**.
2. Dodaj repozytorium `https://github.com/hubertciebiada/tortoise-ufh`
   z kategorią **Integration**.
3. Wyszukaj **Tortoise-UFH** i zainstaluj.
4. **Zrestartuj Home Assistant.**
5. Wejdź w **Ustawienia → Urządzenia i usługi → Dodaj integrację** i wybierz
   **Tortoise-UFH**.

Wymagania: Home Assistant **2024.1** lub nowszy (Python 3.12+). Biblioteki `numpy`
i `scipy` (z manifestu integracji) Home Assistant doinstaluje sam. Po skonfigurowaniu
w menu bocznym pojawia się pozycja **Tortoise-UFH** (panel widoczny dla
administratorów).

## 4. Konfiguracja krok po kroku

Kreator prowadzi przez pięć kroków:

1. **Lokalizacja** — szerokość i długość geograficzna budynku (kompensacja pogodowa
   i nasłonecznienie).
2. **Pokoje** — dla każdego pokoju: nazwa, powierzchnia podłogi (m²), czy ma szybkie
   źródło (split / grzałka), opcjonalna **grupa wspólnego agregatu** (multisplit,
   §10), opcjonalne **dozwolone godziny wspomagania** (ciche godziny, §10 — oba pola
   razem albo żadne; dotyczy tylko pokoi ze wspomaganiem) oraz udział w chłodzeniu
   podłogowym. Zaznacz „Dodaj kolejne pomieszczenie", aby wprowadzić następny pokój.
3. **Przypisanie encji** — per pokój: czujnik temperatury, czujnik wilgotności
   (**wymagany**, gdy pokój bierze udział w chłodzeniu), siłowniki zaworów (encje
   `number` lub `valve`; pokój może mieć kilka pętli — po jednym siłowniku na
   pętlę), czujniki temperatury zasilania i powrotu (po jednym na pętlę), encja
   `climate` szybkiego źródła. Przy pierwszym pokoju dodatkowo encje globalne:
   czujnik temperatury zewnętrznej i encja trybu (select / input_select).
4. **Parametry algorytmu** — nastawa domu i knoby regulatora (możesz zostawić
   domyślne; wszystko zmienisz później w panelu — §8).
5. **Potwierdzenie** — podsumowanie. Pokoje startują w stanie **Wyłączony**: nic
   nie jest zapisywane do sprzętu, dopóki nie przełączysz pokoju na **Steruje**.

Typowe błędy walidacji: `Należy wybrać co najmniej jeden siłownik zaworu` (pokój bez
zaworu), `Czujnik wilgotności jest wymagany…` (pokój chłodzony bez wilgotności),
`Encja ma nieprawidłową jednostkę miary` (integracja sprawdza wyłącznie jednostki:
°C, %, W — nie marki), `Pomieszczenie o tej nazwie już istnieje` (także nazwy
różniące się tylko wielkością liter/spacjami — kolidowałyby w rejestrze),
`Ta encja valve nie obsługuje ustawiania pozycji` (wybierz zawór z SET_POSITION albo
encję `number`), `Podaj obie godziny okna…` (ciche godziny wymagają pary od/do,
różnych od siebie — §10).

Po utworzeniu wpisu wszystko zmienisz w **opcjach integracji** (Ustawienia →
Urządzenia i usługi → Tortoise-UFH → Konfiguruj): dodawanie/edycja/usuwanie pokoi,
ustawienia sterowania i strojenia oraz opcjonalna sekcja **Pompa ciepła** (§11).

## 5. Panel boczny

U góry panelu **hero** — od v0.10.3 kompaktowy, mieszczący się w **jednym wierszu**
(na wąskim ekranie zawija się czytelnie): status algorytmu (Działa / Nieaktualne /
Błąd) z wiekiem ostatniego cyklu, licznik **Steruje x/y** (ile pokoi pisze do
sprzętu), liczba aktywnych **flag** (barwa wg najgroźniejszej; **kliknięcie przenosi
do zakładki Flagi** z pełnym rozkładem), w chłodzeniu **bezpieczny punkt rosy** (§9),
stepper **temperatury domu** i przełącznik **trybu**.

Przełączenie stanu pokoju (Wyłączony↔Steruje) oraz trybu domu wymaga od v0.8.0
**lekkiego potwierdzenia**: przy klikniętym przycisku pojawia się dymek „na pewno?"
z przyciskami **Tak, zmień / Anuluj** (Escape lub kliknięcie obok = anuluj) — te
przełączniki były zbyt łatwe do trafienia przypadkiem.

Zakładki:

- **Pokoje** — tabela: przełącznik stanu
  **Wyłączony/Steruje** (pierwsza kolumna),
  pomiar z trendem, zadana (z korektą), uchyb, zawór %, zasilanie/powrót pierwszej
  pętli oraz — pod wspólnym nagłówkiem **Wspomaganie** — tryb i temperatura zadana
  splita/grzałki („—" = pokój bez wspomagania, „wył." = wspomaganie stoi; ikona „i"
  przy „Temp." tłumaczy celowe ±1 K — §10). Kliknięcie wiersza otwiera **szczegóły
  pokoju**: kafle (pomiar, zadana ze stepperem korekty, zawór, szybkie źródło —
  z linijką „dozwolone HH:MM–HH:MM", gdy pokój ma ciche godziny), sekcję
  **Decyzja regulatora** (uchyb, punkt rosy, wkład członów P/I/Trend/FF,
  zawór surowy i końcowy, dławienie chłodzenia, integrator, nasycenie, minimalne
  otwarcie, słowne wyjaśnienie decyzji), wykresy **historii** (6 h / 24 h / 7 dni),
  sekcję **Czujniki i sygnały** (na pierwszym planie AKTUALNA WARTOŚĆ każdego
  podłączonego sygnału; źródłową encję pokazuje ikona „i", a kliknięcie wiersza
  otwiera natywne okno encji), domyślnie zwiniętą sekcję **Encje diagnostyczne**
  i **surowe dane** raportu.
- **Flagi** — **annunciator flag** (od v0.10.3 osobna zakładka; wcześniej u góry Pokoje):
  zwijana, pogrupowana **lista** (styl jak „Strojenie") wszystkich znanych flag w sekcjach
  **Bezpieczeństwo (S1–S6) / Wspomaganie / Konfiguracja** — wiersze **wyszarzone**, gdy
  nieaktywne, i **zapalone** (lewy pasek koloru severity + status „aktywna ×N pokoi", lista
  pokoi w dymku), gdy zadziałają; zabezpieczenia noszą kod **S1–S6** na plakietce, a pod
  ikoną **„i"** przy każdej fladze jest pełne objaśnienie (§12). Nagłówek zwija/rozwija
  listę, a nawet po zwinięciu pokazuje plakietkę podsumowania.
- **Strojenie** — nastawy globalne + rzadkie nadpisania per pokój (§8), od v0.8.0
  jako **lista parametrów w grupach tematycznych** (Regulator PI i trend / Pasmo
  komfortu i zawór / Człon pogodowy / Szybkie źródło / Ochrona przed kondensacją /
  Pompa ciepła — woda) z pełnymi nazwami. Wybór zakresu (**Globalne** lub konkretny
  pokój) to **zakładki** u góry (od v0.10.1 — wcześniej chipy). Przy każdym parametrze
  ikona **„i"** z objaśnieniem (najedź myszą, kliknij lub tapnij). Grupa „Pompa ciepła —
  woda" jest widoczna tylko w zakresie Globalne (§8).
- **Zawory** — per pokój: Komenda, Surowy, Podłoga (minimalne otwarcie), Saturacja,
  Dławienie S2, Feedback (pozycja raportowana przez siłownik); rozwijane pętle
  z zasilaniem/powrotem/ΔT. Nagłówki kolumn też mają ikony „i".
- **Wspomaganie** — per pokój z szybkim źródłem: rodzaj, grupa (multisplit),
  komenda, stan rzeczywisty encji `climate`, timer dwell, **dozwolone godziny**
  (okno cichych godzin albo „zawsze" — §10), flagi, link do encji i skrót „dostrój".
- **Pompa ciepła** — stan opcjonalnego sprzężenia z pompą (§11): tryb Tortoise vs
  tryb pompy (zgodny/rozjazd), przełącznik flagi CWU, nastawy wody ze składnikami
  i sygnał „pompa dostępna dla podłogówki". Bez konfiguracji pokazuje instrukcję,
  gdzie ją włączyć.

Trudniejsze pola mają ikonę **„i" w kółku** — po najechaniu (komputer) albo
tapnięciu (telefon) pojawia się dymek z objaśnieniem; Escape lub dotknięcie obok
zamyka.

## 6. Encje i usługi

Encje globalne (urządzenie „UFH controller"):

- `number.*_home_temperature` — temperatura domu (5–30 °C, krok 0,5),
- `sensor.*_global_safe_dew_point` — globalny bezpieczny punkt rosy (§9),
- `sensor.*_algorithm_status`, `sensor.*_last_update`, `sensor.*_watchdog_status`.

Encje per pokój (każdy pokój jest urządzeniem, można przypisać do obszaru HA):

- `select.<pokój>_control_state` — stan sterowania **Wyłączony/Steruje** (§7),
- `number.<pokój>_setpoint_offset` — korekta nastawy (−5…+5 K),
- sensory diagnostyczne: zalecane otwarcie zaworu, uchyb, trend, punkt rosy,
  człon całkujący, człon trendu, tryb szybkiego źródła, wyjaśnienie decyzji,
- binarne: utrata czujnika, nasycenie wyjścia, aktywna ochrona przed kondensacją,
  usterka przepływu (`flow_fault`, watchdog S6 — §8/§12).

Usługi (domena `tortoise_ufh`):

```yaml
# Ustaw temperaturę domu
service: tortoise_ufh.set_home_temperature
data:
  temperature: 21.5

# Ustaw korektę pokoju (nazwa pokoju z konfiguracji)
service: tortoise_ufh.set_room_offset
data:
  room: Salon
  offset: -0.5

# Ustaw tryb globalny (heating / transitional / cooling / off)
service: tortoise_ufh.set_mode
data:
  mode: cooling
```

Gotowy szablon dashboardu Lovelace znajdziesz w pliku
`custom_components/tortoise_ufh/dashboard_tortoise_ufh.yaml`.

## 7. Stan sterowania: Wyłączony / Steruje

Każdy pokój ma dokładnie dwa stany (encja `select.<pokój>_control_state`, panel,
komenda WS `tortoise_ufh/set_room_state`):

- **Wyłączony (`off`)** — pokój poza sterowaniem. Rdzeń liczy go w trybie spoczynku
  (raport: zawór 0 %, szybkie źródło wyłączone) i **nic nie zapisuje do sprzętu** —
  fizyczne siłowniki i split pozostają, jak je zostawiłeś. Przy przełączeniu
  z „Steruje" na „Wyłączony" integracja wysyła jednorazową **komendę pożegnalną**:
  split zawsze OFF; zawór w chłodzeniu jest zamykany do 0 (osierocony otwarty zawór
  puszczałby zimną wodę bez ochron przeciwkondensacyjnych), w grzaniu zostaje na
  ostatniej pozycji (ciepła woda jest ograniczona krzywą pompy ciepła).
- **Steruje (`live`)** — pokój jest liczony, raportowany **i** jego komendy są
  zapisywane: zawory (tylko gdy zmiana przekracza próg zapisu — bez terkotania
  siłowników) oraz tryb i temperatura splita.

Zasady praktyczne:

- **Nowy pokój startuje Wyłączony** — świadomie: integracja nie dotknie sprzętu,
  dopóki sam nie zdecydujesz.
- **Tryb ręczny** = przełącz pokój na Wyłączony i steruj urządzeniami bezpośrednio.
  (W stanie Steruje integracja jest właścicielem splita i mniej więcej co 45 minut
  ponownie wymusza swoją komendę — ręczna zmiana z pilota zostanie nadpisana.)
- **Całkowity stop** = wszystkie pokoje Wyłączone.
- Zmiana stanu działa natychmiast i **nie resetuje integratora PID** (przeładowanie
  następuje tylko po zmianie strojenia).

## 8. Parametry strojenia — pełna lista

Zakładka **Strojenie** pokazuje nastawy **globalne**; wybierając pokój (chips u góry)
nadpisujesz pojedyncze parametry tylko dla niego — nadpisany knob dostaje znaczek
„nadpisane" i przycisk **„Wróć do globalnej"**. Od v0.8.0 parametry są ułożone
w **grupy tematyczne** (jedna lista pod drugą), a nazwy są pełne, bez skrótowców.
Grupa **„Pompa ciepła — woda"** jest wyłącznie globalna — nastawy wody dotyczą całego
budynku, więc nadpisanie per pokój nie ma fizycznego sensu i jest odrzucane.
**Uwaga: zapis strojenia przeładowuje regulator — integrator PID jest zerowany.**

<!-- STRAŻNIK: opisy poniżej są 1:1 kopią tooltipów panelu
     (STR.tip_knob_* w frontend/tortoise-ufh-panel.js) — zmieniaj OBA miejsca razem. -->

**Regulator PI i trend**

| Parametr | Jednostka | Domyślnie | Co robi i kiedy ruszać |
|---|---|---|---|
| **Wzmocnienie części proporcjonalnej (P)** (`kp`) | %/K | 14 | Wzmocnienie proporcjonalne: ile procent otwarcia zaworu dodaje każdy 1 K uchybu (zadana − zmierzona). Wyższe = szybsza reakcja, ale większe ryzyko przeregulowania bezwładnej podłogi. Domyślnie 14; typowo 8–20. Ruszaj małymi krokami i obserwuj dobę. |
| **Wzmocnienie części całkującej (I)** (`ki`) | %/(K·s) | 0,0015 | Wzmocnienie całkujące: jak szybko regulator dokłada otwarcie, gdy mały uchyb utrzymuje się godzinami — usuwa trwały niedogrzew. Wartości są celowo bardzo małe (sekundy w mianowniku). Domyślnie 0,0015. Za duże ki = powolne bujanie temperatury wokół zadanej. |
| **Tłumienie trendu temperatury** (`kt`) | %/(K/h) | 12 | Człon trendu (tłumienie): patrzy na tempo zmian temperatury. Gdy pokój szybko zmierza do zadanej, przymyka zawór zawczasu i ogranicza przestrzelenie bezwładnej podłogi. Domyślnie 12. Zwykle nie wymaga zmian. |

**Pasmo komfortu i zawór**

| Parametr | Jednostka | Domyślnie | Co robi i kiedy ruszać |
|---|---|---|---|
| **Strefa nieczułości wokół zadanej** (`deadband_c`) | K | 0,3 | Strefa nieczułości wokół zadanej (±): wewnątrz niej regulator nie goni drobnych odchyłek, co zmniejsza ruchy zaworów i taktowanie. Domyślnie 0,3 K; typowo 0,2–0,5 K. |
| **Minimalne otwarcie zaworu przy grzaniu** (`valve_floor_pct`) | % | 15 | Minimalne otwarcie zaworu, gdy pokój wzywa ciepło: nie schodzimy poniżej tej wartości, żeby w pętli był sensowny przepływ. Domyślnie 15 %. 0 wyłącza minimum. |

**Człon pogodowy (otwarcie zaworu)**

| Parametr | Jednostka | Domyślnie | Co robi i kiedy ruszać |
|---|---|---|---|
| **Człon pogodowy włączony** (`outdoor_ff_enabled`) | — | wył. | Sprzężenie pogodowe: dodaje bazowe otwarcie zaworu zależne od temperatury zewnętrznej, zanim pokój zdąży się wychłodzić. Przydatne przy dużych przeszkleniach i mrozach. Domyślnie wyłączone. |
| **Temperatura neutralna członu pogodowego** (`ff_neutral_c`) | °C | 15 | Temperatura zewnętrzna, przy której człon pogodowy wynosi zero; poniżej niej każdy 1 K chłodniej dodaje otwarcie według wzmocnienia FF. Domyślnie 15 °C. |
| **Wzmocnienie członu pogodowego** (`ff_gain_pct_per_k`) | %/K | 1 | Ile procent otwarcia dodaje człon pogodowy za każdy 1 K poniżej temperatury neutralnej. Domyślnie 1 %/K. |
| **Górny limit członu pogodowego** (`ff_max_pct`) | % | 20 | Górny limit członu pogodowego — sprzężenie nigdy nie doda więcej niż tyle procent otwarcia. Domyślnie 20 %. |

**Szybkie źródło (split/grzałka)**

| Parametr | Jednostka | Domyślnie | Co robi i kiedy ruszać |
|---|---|---|---|
| **Próg włączenia wspomagania** (`boost_offset_c`) | K | 1,0 | Próg dogrzewu: gdy uchyb przekroczy tę wartość, włącza się szybkie źródło (split/grzałka). Split puszcza z powrotem wewnątrz pasma komfortu; podłoga zawsze zostaje źródłem bazowym. Musi być większy niż strefa nieczułości. Domyślnie 1,0 K. |
| **Minimalny czas pracy wspomagania** (`fast_min_on_minutes`) | min | 10 | Minimalny czas pracy szybkiego źródła po włączeniu. Chroni sprężarkę splita przed taktowaniem i uspokaja zmiany kierunku w grupie multisplit. Domyślnie 10 min; minimum 3. |
| **Minimalny czas postoju wspomagania** (`fast_min_off_minutes`) | min | 10 | Minimalny czas postoju szybkiego źródła po wyłączeniu, zanim wolno je włączyć ponownie. Domyślnie 10 min; minimum 3. |
| **Podbicie nastawy wspomagania** (`fast_target_offset_k`) | K | 1,0 | O ile kelwinów nastawa wysyłana do splita jest podbijana poza zadaną pokoju podczas dogrzewu/dochładzania (grzanie: zadana + wartość, chłodzenie: zadana − wartość). Czujnik splita pod sufitem czyta cieplej niż czujnik pokojowy — bez podbicia split dławi się przed dostarczeniem dogrzewu; o wyłączeniu i tak decyduje czujnik pokojowy (§10). 0 = bez podbicia. Domyślnie 1,0 K. |

**Ochrona przed kondensacją (chłodzenie)**

| Parametr | Jednostka | Domyślnie | Co robi i kiedy ruszać |
|---|---|---|---|
| **Margines bezpieczeństwa nad punktem rosy** (`dew_margin_k`) | K | 2 | Chłodzenie: gdy zasilanie jest co najmniej o tyle K powyżej punktu rosy, zawór działa bez ograniczeń; bliżej rosy zaczyna się dławienie przepływu (ochrona przed kondensacją na podłodze). Domyślnie 2 K; zwiększ, jeśli widzisz wilgoć. |
| **Szerokość rampy dławienia chłodzenia** (`dew_ramp_k`) | K | 2 | Szerokość rampy dławienia poniżej marginesu rosy: na tym odcinku przepływ maleje liniowo od 100 % do 0. Mniejsza wartość = ostrzejsze odcinanie. Domyślnie 2 K. |

**Pompa ciepła — woda (tylko globalnie; działa wyłącznie ze skonfigurowanym
sprzężeniem — §11)**

| Parametr | Jednostka | Domyślnie | Co robi i kiedy ruszać |
|---|---|---|---|
| **Bazowa temperatura wody chłodzenia** (`cooling_supply_base_c`) | °C | 18 | Nastawa wody chłodzenia pisana do pompy, gdy dom chłodzi: zapisywana wartość to maksimum z tej bazy i globalnego bezpiecznego punktu rosy — nigdy poniżej punktu rosy. Działa tylko przy skonfigurowanej encji nastawy chłodzenia (opcje → Pompa ciepła). Parametr globalny — nie da się go nadpisać per pokój. Domyślnie 18 °C. |
| **Bazowa temperatura wody grzania** (`heating_supply_base_c`) | °C | 26 | Zasilanie wody grzewczej przy neutralnej temperaturze zewnętrznej (parametr „Temperatura neutralna członu pogodowego", domyślnie 15 °C). Poniżej niej nastawa rośnie według nachylenia krzywej; całość obcinana do 20–40 °C. Pisana do pompy tylko przy skonfigurowanej encji nastawy grzania — domyślnie pompa jedzie na własnej krzywej. Parametr globalny. Domyślnie 26 °C. |
| **Nachylenie krzywej wody grzania** (`heating_supply_slope`) | K/K | 0,5 | O ile kelwinów rośnie nastawa wody grzania na każdy 1 K temperatury zewnętrznej poniżej temperatury neutralnej. Domyślnie 0,5 K/K; typowo 0,3–0,8 dla dobrze ocieplonego domu z podłogówką. |

**Watchdog przepływu (S6; działa tylko z czujnikami zasilania i powrotu pętli)**

| Parametr | Jednostka | Domyślnie | Co robi i kiedy ruszać |
|---|---|---|---|
| **Minimalna ΔT uznawana za przepływ** (`flow_epsilon_k`) | K | 0,3 | Najmniejsza różnica zasilanie−powrót pętli, którą watchdog uznaje za dowód, że woda faktycznie płynie. Poniżej niej (i bez ruchu sond ku źródłu) otwarta komenda bez odpowiedzi hydraulicznej podnosi flagę „brak przepływu". Podnieś przy szumiących czujnikach. |
| **Próg komendy otwarcia watchdoga** (`flow_open_threshold_pct`) | % | 15 | Od jakiej komendy zaworu pętla ma obowiązek pokazać odpowiedź hydrauliczną. Poniżej progu pętla nie jest oceniana pod kątem braku przepływu. |
| **Okno odpowiedzi hydraulicznej** (`flow_response_window_min`) | min | 45 | Ile minut ciągłego braku sygnatury przepływu (przy otwartej komendzie i wiarygodnej cyrkulacji) podnosi flagę; to samo okno pilnuje zaworu, który nie domyka. Płyta jest wolna — minimum 30 min, żeby uniknąć trzepotania; 1440 praktycznie wyłącza watchdoga. |

Watchdog to niezależny, **fizyczny** świadek pracy zaworów: opiera się wyłącznie na
czujnikach wody pętli i **nie ufa** pozycji zgłaszanej przez encję zaworu (ten kanał
potrafi „echować" komendę po restarcie kontrolera — realna awaria z lata 2026, gdy
zawory stały, a raportowały posłuszeństwo). Reakcja jest bierna: flaga + encja
`binary_sensor` „usterka przepływu" + zamrożenie integratora; watchdog sam nie rusza
zaworem. W zakładce Zawory jest chip zdrowia przepływu (ok / brak przepływu? / nie
domyka?) oraz przycisk **ręcznego testu aktuacji** — zawór jest celowo otwierany na
100 % na 20–30 min, a o wyniku decyduje odpowiedź sond (do uruchamiania po serwisie lub
zdarzeniu zasilania; przerywany przez reguły bezpieczeństwa).

## 9. Chłodzenie podłogowe i kondensacja

Chłodzenie podłogą ma jedno twarde ryzyko: **skropliny na posadzce**, gdy zimna woda
zejdzie poniżej punktu rosy pomieszczenia. Ochrona jest dwuwarstwowa:

1. **Globalny bezpieczny punkt rosy** — integracja liczy punkt rosy każdego
   chłodzonego pokoju (z temperatury i wilgotności), bierze **maksimum** i dodaje
   **2 K**. Wynik publikuje jako `sensor.*_global_safe_dew_point`. **Podłącz tę
   wartość do pompy ciepła jako dolny limit temperatury wody chłodzącej** (np.
   przez modbus/ESPHome/automatyzację zapisującą limit w sterowniku pompy) —
   integracja sama nie steruje wodą, tylko dostarcza gotową bezpieczną wartość.
2. **Lokalne dławienie S2 (per pokój)** — gdy zmierzona temperatura zasilania pętli
   zbliża się do punktu rosy pokoju, przepływ jest liniowo dławiony (flaga
   `s2_throttle`), a przy samym punkcie rosy zawór zamyka się całkowicie (flaga
   `s2_condensation`). Działa niezależnie od pompy ciepła.

Wilgotność jest tu najważniejszym pomiarem, więc jest pilnowana osobno:
odczyt starszy niż 60 min jest jeszcze używany, ale z rosnącym zapasem
bezpieczeństwa (+ do 1 K na punkcie rosy, flaga `rh_stale_gated`); powyżej 120 min
jest bezużyteczny — pokój wypada z globalnego maksimum, a lokalna ochrona
zatrzymuje chłodzenie zachowawczo. Pokój bez czujnika wilgotności nie może brać
udziału w chłodzeniu (kreator to wymusza).

## 10. Szybkie źródło i multisplit

- **Kiedy split się włącza:** gdy uchyb przekroczy **próg dogrzewu** (§8). Komenda
  to zawsze „włącz + kierunek + temperatura zadana" — split reguluje się sam;
  nigdy nie zastępuje podłogi, a jego decyzja nigdy nie przymyka zaworu.
- **Kiedy puszcza:** wewnątrz pasma komfortu (strefy nieczułości).
- **Co się dzieje z podłogą podczas dogrzewu (chłodzenie):** gdy split chłodzi,
  schładza **powietrze**, więc zmierzony uchyb szybko maleje i sam regulator PI
  chciałby przymknąć zawór podłogi do zera — czyli split pośrednio zagłodziłby
  podłogę dokładnie wtedy, gdy trzeba rozładować ciepły beton. Dlatego w chłodzeniu,
  **dopóki split jest włączony, zawór podłogi jest trzymany** na poziomie z chwili
  załączenia splita (a jeśli pokój dalej się grzeje, PI może go otworzyć **jeszcze
  bardziej** — trzymanie to dolna granica, nie sufit). Dzięki temu podłoga dalej
  rozładowuje beton, a split rzadziej taktuje. Trzymanie **nie osłabia ochrony przed
  kondensacją**: dławienie punktu rosy (§9) nadal przymyka trzymany zawór, a utrata
  czujnika pokoju nadal zamyka go do 0. W grzaniu nic się nie zmienia. Nowych
  parametrów do ustawienia nie ma — działa automatycznie.
- **Dlaczego nastawa splita różni się od zadanej:** nastawa splita jest celowo
  podbijana o parametr **podbicie nastawy wspomagania** (§8; domyślnie 1 K —
  grzanie: +1 K, chłodzenie: −1 K). Czujnik splita wisi pod sufitem i czyta
  cieplej niż nasz czujnik pokojowy — nastawa równa zadanej gasiłaby split, zanim
  dogrzanie/dochłodzenie faktycznie dotrze do pokoju. O wyłączeniu i tak decyduje
  NASZ czujnik pokojowy z histerezą, więc pokój nie „ucieknie". Jeśli wolisz, by
  split dostawał dokładnie zadaną, ustaw parametr na 0. W trybie przejściowym
  nastawa = zadana zawsze.
- **Timery dwell:** minimalny czas pracy i postoju (§8) — zmiana kierunku zawsze
  przechodzi przez OFF z pełnym czasem postoju.
- **Ponowne wymuszanie:** niezmieniona komenda nie jest wysyłana co cykl (bez
  pikania, Twoje ustawienia nawiewu przetrwają), ale mniej więcej **co 45 minut**
  para tryb+temperatura jest wymuszana ponownie; do tego czasu rozjazd zgłasza
  flaga `fast_source_mismatch`.
- **Multisplit:** pokoje, których jednostki wewnętrzne wiszą na wspólnym agregacie,
  oznacz tą samą **grupą** w konfiguracji pokoju. Agregat fizycznie nie umie grzać
  i chłodzić naraz, więc integracja rozstrzyga **jeden kierunek na grupę**: wygrywa
  pokój najdalej poza swoim pasmem komfortu, a **urzędujący kierunek ma bonus
  0,5 K** (nowy kierunek musi być wyraźnie bardziej potrzebny — bez trzepotania).
  Przegrany jest przymusowo wyłączony (flaga `fast_source_group_conflict`) i wraca
  dopiero po zmianie kierunku grupy, przechodząc pełny czas postoju.

### Ciche godziny wspomagania

Per pokój możesz ustawić **dozwolone godziny wspomagania** (konfiguracja pokoju,
pola „Wspomaganie dozwolone od/do"): okno, w którym split/grzałka **może** pracować,
np. 07:00–22:00. Poza oknem panują ciche godziny:

- **Semantyka okna:** oba pola razem albo żadne (puste = zawsze wolno). Okno może
  **przechodzić przez północ** (np. 22:00–07:00 pozwala pracować tylko nocą).
  Krawędź okna jest honorowana z dokładnością cyklu sterowania (5 min).
- **Poza oknem** split się nie załącza, choćby uchyb przekraczał próg dogrzewu —
  raport pokoju nosi wtedy flagę `fast_source_quiet_hours` (informacyjną), a podłoga
  dalej normalnie grzeje/chłodzi.
- **Koniec okna przy pracującym splicie:** jednostka wyłącza się przez NORMALNE
  reguły minimalnego czasu pracy (dwell) — ochrona sprężarki jest ważniejsza niż
  punktualność co do minuty.
- **Tryb przejściowy:** ciche godziny obowiązują też tam, mimo że split jest wtedy
  jedynym źródłem pokoju — cisza to cisza (świadoma decyzja; chłodną wiosną pokój
  może do rana lekko odpłynąć od zadanej).
- **Wyjątek bezpieczeństwa:** awaryjne grzanie/chłodzenie (S3/S4 — pokój skrajnie
  wychłodzony/przegrzany) ma prawo złamać ciszę. Bezpieczeństwo pokoju stoi wyżej
  niż komfort akustyczny.

Skonfigurowane okno widać w zakładce **Wspomaganie** (kolumna „Dozwolone godziny")
i w szczegółach pokoju (kafelek Szybkie źródło).

## 11. Pompa ciepła (opcjonalne sprzężenie)

<!-- Treść zgodna z tooltipami tip_hp_* w frontend/tortoise-ufh-panel.js
     i z prd-control-brain.md §8.13 — zmieniaj razem. -->

Od v0.8.0 Tortoise-UFH może **opcjonalnie** współpracować z pompą ciepła
(np. Panasonic Aquarea przez HeishaMon). Wszystko jest **opt-in**: bez konfiguracji
sekcji **Ustawienia → Urządzenia i usługi → Tortoise-UFH → Konfiguruj → Pompa
ciepła** integracja pompy nie dotyka, tak jak dotąd. Tortoise **nadal nie steruje
sprężarką, mocą ani krzywą pogodową pompy** — synchronizuje co najwyżej KIERUNEK
trybu i podaje ograniczone nastawy wody.

**Konfiguracja (wszystkie pola opcjonalne):**

- **Encja trybu pompy** (`select`, styl HeishaMon: „Heat only" / „Cool only" /
  „Auto" / „DHW only" / „Heat+DHW" / „Cool+DHW" / „Auto+DHW"; przykładowo
  `select.heat_pump_mode`),
- **encja nastawy wody grzania** (`number`, opcjonalna — domyślnie pompa jedzie na
  własnej krzywej),
- **encja nastawy wody chłodzenia** (`number`, np. `number.z1_cool_request_temp`),
- **encja „pompa dostępna dla podłogówki"** — gdy wyłączona (CWU/odszranianie),
  regulatory pokojów zamrażają człon całkujący.

**Synchronizacja kierunku trybu:** tryb Tortoise „grzanie" → wariant „Heat",
„chłodzenie" → wariant „Cool", zawsze **z zachowaniem członu „+DHW"** — flaga CWU
należy do zewnętrznej automatyki CWU i Tortoise nigdy jej nie zdejmuje ani nie pisze
samego „DHW only". Gdy pompa aktualnie JEST w „DHW only", Tortoise nie pisze wcale
(automatyka CWU jest w środku cyklu i sama przywróci kierunek — panel pokazuje wtedy
rozjazd z notką „DHW only"). W trybach Przejściowy i Wył. kierunek nie jest
wymuszany. Zapis jest rzadki: przy zmianie trybu Tortoise albo gdy rozjazd utrzymuje
się przez ≥2 cykle, nie częściej niż co 15 minut.

**Nastawa wody chłodzenia:** w trybie chłodzenia Tortoise pisze do pompy
`max(bazowa temperatura wody chłodzenia, globalny bezpieczny punkt rosy)` (§8, §9) —
woda nigdy nie zejdzie do strefy kondensacji. Wartość jest **zaokrąglana do kroku
(`step`) encji number pompy zwykłym zaokrągleniem matematycznym** (do najbliższej
pełnej działki, połówka w górę), żeby trafić dokładnie w siatkę pompy — bez tego pompa
o kroku 1 °C potrafiła sama ściąć np. 16,56 → 16, poniżej progu rosy (issue #5).
Świadomie zwykłe zaokrąglenie, nie „zawsze w górę": 2 K marginesu punktu rosy z
zapasem pokrywa najwyżej pół działki luzu. Zapis przy zmianie ≥ 0,5 K, wymuszany
ponownie co ~45 min.

**Nastawa wody grzania (opcjonalna):** prosta krzywa pogodowa — baza + nachylenie ×
niedobór względem temperatury neutralnej, obcięta do 20–40 °C (§8). Bez odczytu
temperatury zewnętrznej w danym cyklu nic nie jest pisane (pompa zostaje na
ostatniej nastawie — jej firmware ma własne limity). Nastawa grzania jest
zaokrąglana do kroku encji tym samym zwykłym zaokrągleniem co chłodzenie. Zanim
wskażesz encję grzania, porównaj krzywą Tortoise z krzywą własnej pompy.

**Przełącznik CWU (zakładka Pompa ciepła):** dokłada/zdejmuje człon „+DHW" w encji
trybu (np. „Heat only" ↔ „Heat+DHW") — to ręczna prośba „dogrzej wodę teraz /
przestań", nie blokada. **Zewnętrzna automatyka CWU jest właścicielem tej flagi
i może ją w każdej chwili nadpisać — to jej prawo.** Zdjęcie flagi, gdy pompa jest
w „DHW only", jest niemożliwe (nie ma kierunku, do którego można wrócić).

**Wymuszanie startu chłodzenia (opcjonalny, Panasonic; od v0.13.0):** w chłodzeniu sprężarka
Panasonic Aquarea rusza dopiero, gdy powrót osiągnie `nastawa + 3 K`, a zatrzymuje się przy
`nastawa` — ta histereza 3 K jest zaszyta w firmware. W długich postojach powrót stoi więc
wysoko, a podłoga niedodostarcza. Po włączeniu Tortoise — widząc pompę w postoju z realnym
zapotrzebowaniem — na jeden cykl obniża zapisaną nastawę do bezpiecznego dla rosy punktu rosy,
żeby wytrącić sprężarkę, po czym natychmiast przywraca normalną nastawę. Efekt: chłodniejsza
średnia woda przy wciąż bezpiecznym dla rosy powrocie. **Włączasz i stroisz** to na zakładce
**Strojenie**, w grupie **„Wymuszanie startu chłodzenia"**: przełącznik on/off plus cztery
globalne pokrętła (docelowa histereza, zwłoka przed wymuszeniem, min. przerwa między startami,
limit startów na godzinę). Wcześniej wskaż encje **temperatury powrotu** i **częstotliwości
sprężarki** w opcjach → **Pompa ciepła** (encja **wylotu** jest tam tylko diagnostyczna). Domyślnie
**wyłączone**; rozwiązanie specyficzne dla Panasonica. Stan (impuls / czuwanie / przerwa, próg,
odczyty) widać w zakładce Pompa ciepła.

**Kiedy Tortoise pisze do pompy:** tylko gdy przynajmniej jeden pokój jest w stanie
**Steruje** (globalne urządzenie wolno ruszać tylko, gdy ktokolwiek oddał sterowanie)
— inaczej zakładka pokazuje „Zapisy wstrzymane". Komenda pożegnalna (wyłączenie
pokoju / usunięcie integracji) **nie dotyka pompy**.

## 12. Flagi i alarmy — słownik

<!-- Słownik zgodny z FLAG_LABELS w frontend/tortoise-ufh-panel.js — zmieniaj razem. -->

Wszystkie te flagi widzisz też jako **annunciator w osobnej zakładce „Flagi"** (od v0.10.0;
od v0.10.3 własna zakładka, wcześniej u góry Pokoje) —
zwijaną, pogrupowaną **listę** (styl jak „Strojenie"): każda flaga to wiersz — **wyszarzony**,
gdy nieaktywna, i **zapalony** (lewy pasek koloru + status „aktywna ×N pokoi"), gdy zadziała.
Zabezpieczenia noszą kod **S1–S6**, a pod ikoną **„i"** przy każdej fladze jest jej pełne
objaśnienie. Lista renderuje się z jednego rejestru — nowa flaga pojawia się w niej
automatycznie. Pełny słownik poniżej:

| Flaga | Znaczenie | Co zrobić |
|---|---|---|
| `sensor_lost` | Utrata czujnika temperatury (brak odczytu, odczyt nieprawdopodobny albo starszy niż 45 min). Zawór: w grzaniu trzyma ostatnią zdrową pozycję, w chłodzeniu parkuje na 0; split OFF. | Sprawdź czujnik/baterię/sieć. Pokój wróci sam po świeżym, wiarygodnym odczycie. |
| `s2_throttle` | Chłodzenie dławione — zasilanie zbliża się do punktu rosy (przepływ < 100 %). | Stan normalny w wilgotne dni. Jeśli ciągły: podnieś limit wody na pompie (globalny punkt rosy) albo osusz powietrze. |
| `s2_condensation` | Ochrona przed kondensacją: zasilanie osiągnęło punkt rosy — zawór zamknięty. | Sprawdź wilgotność i temperaturę wody chłodzącej; upewnij się, że pompa respektuje globalny bezpieczny punkt rosy. |
| `rh_stale_gated` | Wilgotność nieświeża (60–120 min) — punkt rosy liczony z zapasem do +1 K. | Sprawdź czujnik wilgotności. Powyżej 120 min pokój wypada z ochron — patrz §9. |
| `valve_mismatch` | Siłownik od ≥3 cykli raportuje pozycję inną niż komenda („zawór nie słucha"). | Sprawdź siłownik/przekaźnik/encję; porównaj kolumny Komenda i Feedback w zakładce Zawory. |
| `fast_source_mismatch` | Split jest w innym stanie niż komenda (np. zmieniony pilotem). | Nic — zostanie nadpisany przy ponownym wymuszeniu (~45 min); na stałe ręczne sterowanie przełącz pokój na Wyłączony. |
| `fast_source_min_runtime` | Blokada minimalnego czasu pracy/postoju — wspomaganie chwilowo nie może zmienić stanu. | Nic — ochrona sprężarki; timer w zakładce Wspomaganie. |
| `fast_source_quiet_hours` | Ciche godziny wspomagania — pokój jest poza swoim oknem dozwolonych godzin (§10), split się nie załącza (pracujący kończy przez min. czas pracy). | Informacyjne. Okno zmienisz w konfiguracji pokoju. |
| `fast_source_group_conflict` | Pokój przegrał arbitraż kierunku na wspólnym agregacie — jego split przymusowo OFF. | Nic — wróci po zmianie kierunku grupy. Jeśli częste: przemyśl korekty pokoi w grupie. |
| `fast_source_cannot_cool` | Pokój chce chłodzić, ale jego szybkie źródło nie umie (grzałka). | Informacyjne. |
| `cooling_disabled` | Pokój nie bierze udziału w chłodzeniu (opcja konfiguracji) — w trybie chłodzenia jest pomijany. | Zamierzone; zmień w opcjach pokoju, jeśli chcesz chłodzić. |
| `flicker_pulsing` | Wymuszanie startu chłodzenia (Panasonic, §11): na ten jeden cykl nastawę obniżono do bezpiecznego dla rosy progu, by wytrącić sprężarkę z jej stałej histerezy 3 K powrotu; następny cykl przywraca normalną nastawę. | Informacyjne (zamierzone). |
| `flicker_dew_blocked` | Wymuszenie startu byłoby uzbrojone, ale impuls musiałby przekroczyć surowy punkt rosy — brak zapasu na obniżenie nastawy; impuls wstrzymany. | Informacyjne; normalne w wilgotne dni. |
| `flicker_no_sensor` | Wymuszanie startu chłodzenia włączone, ale brak odczytu powrotu lub częstotliwości sprężarki (encja nieustawiona lub nieświeża) — impulsy wstrzymane. | Sprawdź encje temperatury powrotu i częstotliwości sprężarki (opcje → Pompa ciepła). |
| `s1_floor_overheat` | Zabezpieczenie posadzki: zbyt gorące zasilanie — zawór zamknięty do ostygnięcia. | Sprawdź krzywą grzewczą/temperaturę zasilania pompy ciepła. |
| `s3_emergency_heat` | Awaryjne grzanie: pokój mocno wychłodzony — wymuszone grzanie wszystkim, co jest. | Sprawdź, czemu pokój tak wystygł (okno? awaria?). |
| `s4_emergency_cool` | Awaryjne chłodzenie: pokój mocno przegrzany — wymuszone chłodzenie. | Sprawdź źródło przegrzewu (słońce? awaria?). |
| `s5_watchdog` | Brak świeżych danych pokoju > 15 min — pozycja neutralna, alarm w raporcie. | Sprawdź czujniki/sieć; kasuje się po 5 min ciągłych świeżych danych. |
| `unknown_room` | Pokój bez konfiguracji regulatora (stan przejściowy po zmianach konfiguracji). | Przeładuj integrację; jeśli trwa — usuń i dodaj pokój. |
| `controller_error` | Wyjątek w regulatorze pokoju — bezpieczna degradacja (w grzaniu zawór trzyma pozycję, w chłodzeniu 0; split OFF). | Zajrzyj do logów HA i zgłoś problem (patrz §13). |
| `loop_no_flow` | Watchdog przepływu (S6): zawór jest otwarty od dłuższego czasu, ale sondy pętli nie widzą przepływu wody — integrator zamrożony, encja „usterka przepływu" włączona. | Sprawdź kontroler zaworów/siłownik i przepływ na rozdzielaczu (rotametry). Watchdog nie ufa feedbackowi zaworu — patrz §8, „Watchdog przepływu". |
| `actuation_test_running` | Trwa ręczny test aktuacji tego pokoju (zawór celowo na 100 %). | Informacyjne. Zaczekaj do końca albo anuluj w panelu. |
| `actuation_test_failed` | Test aktuacji zakończony niepowodzeniem — pętla nie odpowiedziała hydraulicznie na otwarcie. | Sprawdź kontroler/siłownik/przepływ tej pętli. |

## 13. Rozwiązywanie problemów

- **Panel pokazuje „Nieaktualne" / watchdog `stale`** — od >15 min żaden pokój nie
  dostarczył świeżych danych. Sprawdź czujniki i ich integracje (Zigbee/ESPHome).
  Status wraca po 5 minutach nieprzerwanie świeżych danych.
- **Pokój ma `sensor_lost`, choć czujnik działa** — odczyt mógł zostać odrzucony
  przez bramkę wiarygodności (skok > 4 K/cykl wymaga potwierdzenia drugim odczytem
  po ~5 min; zakres −10…50 °C) albo jest starszy niż 45 min. Zobacz sekcję
  Okablowanie w szczegółach pokoju — tam widać surowe stany encji.
- **Zawór nie słucha (`valve_mismatch`)** — porównaj Komenda vs Feedback
  (zakładka Zawory). Typowe przyczyny: siłownik bez zasilania, encja tylko do
  odczytu, zła encja przypisana do pętli.
- **Split w innym stanie niż komenda** — flaga `fast_source_mismatch`; integracja
  nadpisze go przy ponownym wymuszeniu (~45 min). Chcesz sterować ręcznie —
  przełącz pokój na Wyłączony (§7).
- **Brak globalnego punktu rosy w chłodzeniu** — najedź na kafelek w hero: zobaczysz
  powód per pokój (brak wilgotności, pokój nie chłodzi, chłodzenie wyłączone, brak
  pomiaru). Punkt rosy liczą tylko pokoje realnie chłodzące.
- **Panel się nie ładuje** — odśwież z pominięciem cache (Ctrl+F5); sprawdź, czy
  integracja jest załadowana (Ustawienia → Urządzenia i usługi). Panel wymaga
  uprawnień administratora.
- **Po aktualizacji do 0.7.0 select pokazuje jeszcze opcję „Obserwuje"** —
  Home Assistant cache'uje tłumaczenia; pomaga restart HA. Wybór starej opcji i tak
  zostanie odrzucony przez walidację.
- **Po aktualizacji sensory pokoju pokazują zawór 0 % / split off** — pokój jest
  w stanie Wyłączony (dawne „Obserwuje" zostało zmigrowane na Wyłączony). To
  zamierzona zmiana raportowania — patrz §14. Przełącz pokój na Steruje, jeśli ma
  sterować.
- **Logi i diagnostyka** — Ustawienia → System → Logi (źródło
  `custom_components.tortoise_ufh`); pełny raport pokoju: szczegóły pokoju →
  „Pokaż surowe dane". Problemy zgłaszaj na GitHubie repozytorium z logiem
  i surowym raportem.

## 14. Ograniczenia, FAQ i historia wersji

**Ograniczenia (świadome, v1):** brak MPC/optymalizacji taryf, brak uczenia modelu
online, integracja nie steruje sprężarką, mocą ani krzywą pogodową pompy ciepła
(opcjonalne sprzężenie z §11 synchronizuje co najwyżej kierunek trybu i podaje
ograniczone nastawy wody; domyślnie wyłączone), brak czujnika posadzki (ochrona
przez temperaturę zasilania), jeden globalny tryb dla całego domu.

**FAQ**

- *Czy mogę używać tylko części pokoi?* Tak — pokoje w stanie Wyłączony są w pełni
  ignorowane przy zapisie; sterują tylko te przełączone na Steruje.
- *Czy zmiana temperatury domu działa od razu?* Tak — poza natychmiastową
  aktualizacją nastaw, w ciągu kilku sekund przeliczany jest pełny cykl sterowania.
- *Czy restart HA gubi nastawy?* Nie — temperatura domu, korekty i tryb są
  utrwalane i wracają po restarcie.

**Historia wersji / migracje**

- **0.12.0 (2026-07-14)** — **niemiecki (DE) interfejs i dokumentacja** + porządki w
  dokumentach. Aplikacja (panel, encje, kreator konfiguracji, usługi) ma teraz pełne
  tłumaczenie niemieckie, z fallbackiem do angielskiego dla pozostałych języków. Manual
  wyszedł z mylącej nazwy `INSTRUKCJA.md` do folderu **`docs/manual/`** (`pl.md`, `de.md`);
  README ma przełącznik języka. Bez zmian w rdzeniu ani w kontrakcie I/O; bez migracji
  konfiguracji.
- **0.11.1 (2026-07-14)** — panel: flaga **„Chłodzenie wyłączone w tym pokoju"**
  (`cooling_disabled`) przestała świecić na żółto. To zamierzony, stały stan (pokój celowo
  wypisany z chłodzenia), więc przez cały sezon chłodniczy fałszywie wyglądał jak usterka.
  Wprowadzony neutralny poziom severity **„info"** (spokojny, stonowany kolor, poniżej
  „ostrzeżenia") — flaga jest dalej widoczna i wyjaśniona, ale nie podbija kropki statusu
  pokoju do żółtej i nie robi szumu. Ten sam fakt widać też bez zmian w szczegółach pokoju
  („Powód braku punktu rosy"). Wyłącznie panel — bez migracji konfiguracji, bez nowego
  parametru, kontrakt I/O nietknięty.
- **0.11.0 (2026-07-14)** — trzy zmiany. **(1) Chłodzenie — podłoga trzyma pozycję podczas
  dogrzewu splitem:** gdy split wspomaga chłodzenie, schłodzone przez niego powietrze nie
  zamyka już zaworu podłogi do zera — podłoga utrzymuje ostatnią pozycję i dalej rozładowuje
  chłód z betonu (płyta zdąża się wychłodzić, splitowi jest łatwiej, mniej krótkich cykli).
  Działa tylko w chłodzeniu, **nie dokłada żadnego parametru** i nie osłabia zabezpieczeń przed
  skroplinami (throttle punktu rosy S2 oraz utrata czujnika nadal domykają zawór).
  **(2) Usunięte wykrywanie „zawór nie domyka" (`loop_stuck_open`):** dawało fałszywe alarmy,
  bo z samej temperatury powietrza nie da się twardo potwierdzić, czy siłownik przepuszcza wodę.
  Wykrywanie **braku przepływu** (`loop_no_flow`) i ręczny **test aktuacji** działają bez zmian.
  **(3) Panel — ujednolicony wygląd kontrolek w hero** (spójna wysokość, obramowanie i tło;
  naprawiony błąd, przez który metryki renderowały się zbyt dużą czcionką). Bez migracji
  konfiguracji; kontrakt I/O nietknięty.
- **0.10.3 (2026-07-13)** — panel: **hero w jednym wierszu** (był w trzech — zajmował dużo
  miejsca; wysokość spada ~2×, na wąskim ekranie zawija się czytelnie), metryki jako inline
  „podpis + wartość", ujednolicone kontrolki, metryka **Flagi klikalna** → przenosi do zakładki
  Flagi, przycisk „Zarządzaj pokojami" jako ikona. **Annunciator flag** przeniesiony z góry
  zakładki Pokoje do **własnej zakładki „Flagi"** (kolejność: Pokoje / Flagi / Strojenie /
  Zawory / Wspomaganie / Pompa). Wyłącznie panel — bez migracji konfiguracji, kontrakt I/O
  nietknięty.
- **0.10.2 (2026-07-13)** — poprawka (issue #5): nastawa wody chłodzenia i grzania jest teraz
  zaokrąglana do **realnego kroku (`step`) encji number pompy** zwykłym zaokrągleniem
  matematycznym (połówka w górę), a nie do zaszytej siatki 0,5 K. Wcześniej dew-bezpieczny cel
  16,56 °C był pisany jako 16,5, a pompa o kroku 1 °C ścinała go do 16 — poniżej progu punktu
  rosy. Zwykłe zaokrąglenie (nie „zawsze w górę") świadomie oddaje najwyżej pół działki z 2 K
  marginesu, żeby nie tracić mocy chłodzenia. Wyłącznie warstwa zapisu — bez migracji, kontrakt
  I/O nietknięty.
- **0.10.1 (2026-07-13)** — dopieszczenie panelu: annunciator flag przerobiony z kafelków na
  **zwijaną, pogrupowaną listę** w stylu „Strojenia" (wiersz na flagę, lewy pasek koloru severity
  + status „aktywna ×N", nagłówek zwija/rozwija i pokazuje podsumowanie po zwinięciu). W zakładce
  „Strojenie" wybór zakresu (Globalne / pokój) zmieniony z chipów na **zakładki**. Wyłącznie panel —
  bez migracji konfiguracji.
- **0.10.0 (2026-07-13)** — **annunciator flag** na górze zakładki Pokoje: wszystkie znane
  flagi jako kafelki (wyszarzone, gdy nieaktywne; zapalone w kolorze z licznikiem `×N`, gdy
  zadziałają), pogrupowane w Bezpieczeństwo (S1–S6) / Wspomaganie / Konfiguracja, każda z „i"
  i pełnym objaśnieniem. Renderuje się z **jednego rejestru flag** (nowa flaga pojawia się
  automatycznie). Metryka „Flagi" w hero pokazuje teraz liczbę aktywnych flag w barwie wg
  najgroźniejszej. Naprawia „martwą" wagę `alarm` (flagi S6 świecą teraz czerwono, nie szaro).
  Zmiana wyłącznie w panelu — bez migracji konfiguracji, kontrakt sterownika nietknięty.
- **0.9.0 (2026-07-13)** — **watchdog przepływu (S6)**: niezależny, fizyczny świadek
  pracy zaworów z sond wody pętli, który nie ufa feedbackowi encji zaworu (flaga
  `loop_no_flow`, encja „usterka przepływu", zamrożenie
  integratora); **ręczny test aktuacji** (usługa `tortoise_ufh.test_actuation`), chip
  zdrowia przepływu w zakładce Zawory, trzy nowe parametry strojenia (§8). Poprawki
  ścieżki zapisu zaworów: ponowne wymuszanie komendy (~45 min) i zapis przy rozjeździe
  feedbacku — po awarii kontrolera zaworów pozycja parkingowa już nie „zamarza".
  Podbicie nastawy splita (±1 K, S12) jest teraz parametrem `fast_target_offset_k`
  (0 = bez podbicia). Bez migracji konfiguracji. Naprawia zgłoszenie #4.
- **0.8.0 (2026-07-12)** — **ciche godziny wspomagania** per pokój (okno dozwolonych
  godzin splita, także przez północ — §10) i **opcjonalne sprzężenie z pompą ciepła**
  (synchronizacja kierunku trybu z zachowaniem flagi CWU, nastawy wody chłodzenia /
  grzania, przełącznik CWU, nowa zakładka panelu — §11; trzy nowe globalne parametry
  strojenia — §8). Runda UX panelu: normalne (nie wersalikowe) nagłówki tabel,
  potwierdzenia przełączeń stanu pokoju i trybu domu, kolumny wspomagania pod
  wspólnym nagłówkiem z czytelnymi stanami pustymi, strojenie jako lista w grupach
  z pełnymi nazwami, sekcja „Czujniki i sygnały" z wartością na pierwszym planie,
  domyślnie zwinięte encje diagnostyczne, tooltip „±1 K" nastawy splita. Bez migracji
  konfiguracji.
- **0.7.0 (2026-07-12)** — **usunięto stan „Obserwuje" (shadow)**: stan pokoju to
  odtąd dwustan Wyłączony/Steruje, a domyślny stan nowego pokoju to Wyłączony.
  Migracja jest automatyczna (config entry v2→v3; stare wpisy v1 przechodzą całą
  drogę v1→v3): pokoje „Obserwuje" stają się Wyłączone — zachowanie zapisu jest
  identyczne (żaden z tych stanów nic nie pisał), zmienia się tylko raportowanie
  (pokój Wyłączony raportuje spoczynek zamiast „co by zrobił"). Nowość: ikony „i"
  z objaśnieniami w panelu, pełna polska wersja językowa (panel, encje, kreator,
  usługi) i ta instrukcja.
- **0.5.0–0.6.x** — urządzenia per pokój, tłumaczone nazwy encji, grupy multisplit
  z arbitrażem kierunku, utwardzenia bezpieczeństwa (pełna lista:
  `docs/DECISIONS.md`).
- **0.3.0** — trzy-stanowy stan pokoju zastąpił globalny kill-switch (migracja
  v1→v2).
