# Tortoise-UFH — instrukcja użytkownika

> Dotyczy wersji **0.7.0**. Instrukcja opisuje integrację po polsku; interfejs
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
11. [Flagi i alarmy — słownik](#11-flagi-i-alarmy--słownik)
12. [Rozwiązywanie problemów](#12-rozwiązywanie-problemów)
13. [Ograniczenia, FAQ i historia wersji](#13-ograniczenia-faq-i-historia-wersji)

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
mówiące, które zabezpieczenie lub ograniczenie zadziałało — słownik w §11.

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
   §10) oraz udział w chłodzeniu podłogowym. Zaznacz „Dodaj kolejne pomieszczenie",
   aby wprowadzić następny pokój.
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
encję `number`).

## 5. Panel boczny

U góry panelu **hero**: status algorytmu (Działa / Nieaktualne / Błąd) z wiekiem
ostatniego cyklu, licznik **Steruje x/y** (ile pokoi pisze do sprzętu), liczba
aktywnych **flag**, w chłodzeniu **bezpieczny punkt rosy** (§9), stepper
**temperatury domu** i przełącznik **trybu**.

Zakładki:

- **Pokoje** — tabela: przełącznik stanu **Wyłączony/Steruje** (pierwsza kolumna),
  pomiar z trendem, zadana (z korektą), uchyb, zawór %, zasilanie/powrót pierwszej
  pętli, tryb i temperatura wspomagania. Kliknięcie wiersza otwiera **szczegóły
  pokoju**: kafle (pomiar, zadana ze stepperem korekty, zawór, szybkie źródło),
  sekcję **Decyzja regulatora** (uchyb, punkt rosy, wkład członów P/I/Trend/FF,
  zawór surowy i końcowy, dławienie chłodzenia, integrator, nasycenie, minimalne
  otwarcie, słowne wyjaśnienie decyzji), wykresy **historii** (6 h / 24 h / 7 dni), sekcję
  **Okablowanie** (przypisane encje z podglądem na żywo) i **surowe dane** raportu.
- **Strojenie** — nastawy globalne + rzadkie nadpisania per pokój (§8). Przy każdym
  parametrze ikona **„i"** z objaśnieniem (najedź myszą, kliknij lub tapnij).
- **Zawory** — per pokój: Komenda, Surowy, Podłoga (minimalne otwarcie), Saturacja,
  Dławienie S2, Feedback (pozycja raportowana przez siłownik); rozwijane pętle
  z zasilaniem/powrotem/ΔT. Nagłówki kolumn też mają ikony „i".
- **Wspomaganie** — per pokój z szybkim źródłem: rodzaj, grupa (multisplit),
  komenda, stan rzeczywisty encji `climate`, timer dwell, flagi, link do encji
  i skrót „dostrój".

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
- binarne: utrata czujnika, nasycenie wyjścia, aktywna ochrona przed kondensacją.

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
„nadpisane" i przycisk **„Wróć do globalnej"**. **Uwaga: zapis strojenia przeładowuje
regulator — integrator PID jest zerowany.**

<!-- STRAŻNIK: opisy poniżej są 1:1 kopią tooltipów panelu
     (STR.tip_knob_* w frontend/tortoise-ufh-panel.js) — zmieniaj OBA miejsca razem. -->

| Parametr | Jednostka | Domyślnie | Co robi i kiedy ruszać |
|---|---|---|---|
| **Wzmocnienie P** (`kp`) | %/K | 14 | Wzmocnienie proporcjonalne: ile procent otwarcia zaworu dodaje każdy 1 K uchybu (zadana − zmierzona). Wyższe = szybsza reakcja, ale większe ryzyko przeregulowania bezwładnej podłogi. Domyślnie 14; typowo 8–20. Ruszaj małymi krokami i obserwuj dobę. |
| **Wzmocnienie I** (`ki`) | %/(K·s) | 0,0015 | Wzmocnienie całkujące: jak szybko regulator dokłada otwarcie, gdy mały uchyb utrzymuje się godzinami — usuwa trwały niedogrzew. Wartości są celowo bardzo małe (sekundy w mianowniku). Domyślnie 0,0015. Za duże ki = powolne bujanie temperatury wokół zadanej. |
| **Człon trendu** (`kt`) | %/(K/h) | 12 | Człon trendu (tłumienie): patrzy na tempo zmian temperatury. Gdy pokój szybko zmierza do zadanej, przymyka zawór zawczasu i ogranicza przestrzelenie bezwładnej podłogi. Domyślnie 12. Zwykle nie wymaga zmian. |
| **Strefa nieczułości** (`deadband_c`) | K | 0,3 | Strefa nieczułości wokół zadanej (±): wewnątrz niej regulator nie goni drobnych odchyłek, co zmniejsza ruchy zaworów i taktowanie. Domyślnie 0,3 K; typowo 0,2–0,5 K. |
| **Minimalne otwarcie zaworu** (`valve_floor_pct`) | % | 15 | Minimalne otwarcie zaworu, gdy pokój wzywa ciepło: nie schodzimy poniżej tej wartości, żeby w pętli był sensowny przepływ. Domyślnie 15 %. 0 wyłącza minimum. |
| **Próg dogrzewu** (`boost_offset_c`) | K | 1,0 | Próg dogrzewu: gdy uchyb przekroczy tę wartość, włącza się szybkie źródło (split/grzałka). Split puszcza z powrotem wewnątrz pasma komfortu; podłoga zawsze zostaje źródłem bazowym. Musi być większy niż strefa nieczułości. Domyślnie 1,0 K. |
| **Min. czas pracy wspomagania** (`fast_min_on_minutes`) | min | 10 | Minimalny czas pracy szybkiego źródła po włączeniu. Chroni sprężarkę splita przed taktowaniem i uspokaja zmiany kierunku w grupie multisplit. Domyślnie 10 min; minimum 3. |
| **Min. czas postoju wspomagania** (`fast_min_off_minutes`) | min | 10 | Minimalny czas postoju szybkiego źródła po wyłączeniu, zanim wolno je włączyć ponownie. Domyślnie 10 min; minimum 3. |
| **Margines punktu rosy** (`dew_margin_k`) | K | 2 | Chłodzenie: gdy zasilanie jest co najmniej o tyle K powyżej punktu rosy, zawór działa bez ograniczeń; bliżej rosy zaczyna się dławienie przepływu (ochrona przed kondensacją na podłodze). Domyślnie 2 K; zwiększ, jeśli widzisz wilgoć. |
| **Rampa dławienia rosy** (`dew_ramp_k`) | K | 2 | Szerokość rampy dławienia poniżej marginesu rosy: na tym odcinku przepływ maleje liniowo od 100 % do 0. Mniejsza wartość = ostrzejsze odcinanie. Domyślnie 2 K. |
| **Sprzężenie pogodowe** (`outdoor_ff_enabled`) | — | wył. | Sprzężenie pogodowe: dodaje bazowe otwarcie zaworu zależne od temperatury zewnętrznej, zanim pokój zdąży się wychłodzić. Przydatne przy dużych przeszkleniach i mrozach. Domyślnie wyłączone. |
| **FF: temp. neutralna** (`ff_neutral_c`) | °C | 15 | Temperatura zewnętrzna, przy której człon pogodowy wynosi zero; poniżej niej każdy 1 K chłodniej dodaje otwarcie według wzmocnienia FF. Domyślnie 15 °C. |
| **FF: wzmocnienie** (`ff_gain_pct_per_k`) | %/K | 1 | Ile procent otwarcia dodaje człon pogodowy za każdy 1 K poniżej temperatury neutralnej. Domyślnie 1 %/K. |
| **FF: limit** (`ff_max_pct`) | % | 20 | Górny limit członu pogodowego — sprzężenie nigdy nie doda więcej niż tyle procent otwarcia. Domyślnie 20 %. |

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
  to zawsze „włącz + kierunek + temperatura zadana pokoju" — split reguluje się sam;
  nigdy nie zastępuje podłogi, a jego decyzja nigdy nie przymyka zaworu.
- **Kiedy puszcza:** wewnątrz pasma komfortu (strefy nieczułości).
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

## 11. Flagi i alarmy — słownik

<!-- Słownik zgodny z FLAG_LABELS w frontend/tortoise-ufh-panel.js — zmieniaj razem. -->

| Flaga | Znaczenie | Co zrobić |
|---|---|---|
| `sensor_lost` | Utrata czujnika temperatury (brak odczytu, odczyt nieprawdopodobny albo starszy niż 45 min). Zawór: w grzaniu trzyma ostatnią zdrową pozycję, w chłodzeniu parkuje na 0; split OFF. | Sprawdź czujnik/baterię/sieć. Pokój wróci sam po świeżym, wiarygodnym odczycie. |
| `s2_throttle` | Chłodzenie dławione — zasilanie zbliża się do punktu rosy (przepływ < 100 %). | Stan normalny w wilgotne dni. Jeśli ciągły: podnieś limit wody na pompie (globalny punkt rosy) albo osusz powietrze. |
| `s2_condensation` | Ochrona przed kondensacją: zasilanie osiągnęło punkt rosy — zawór zamknięty. | Sprawdź wilgotność i temperaturę wody chłodzącej; upewnij się, że pompa respektuje globalny bezpieczny punkt rosy. |
| `rh_stale_gated` | Wilgotność nieświeża (60–120 min) — punkt rosy liczony z zapasem do +1 K. | Sprawdź czujnik wilgotności. Powyżej 120 min pokój wypada z ochron — patrz §9. |
| `valve_mismatch` | Siłownik od ≥3 cykli raportuje pozycję inną niż komenda („zawór nie słucha"). | Sprawdź siłownik/przekaźnik/encję; porównaj kolumny Komenda i Feedback w zakładce Zawory. |
| `fast_source_mismatch` | Split jest w innym stanie niż komenda (np. zmieniony pilotem). | Nic — zostanie nadpisany przy ponownym wymuszeniu (~45 min); na stałe ręczne sterowanie przełącz pokój na Wyłączony. |
| `fast_source_min_runtime` | Blokada minimalnego czasu pracy/postoju — wspomaganie chwilowo nie może zmienić stanu. | Nic — ochrona sprężarki; timer w zakładce Wspomaganie. |
| `fast_source_group_conflict` | Pokój przegrał arbitraż kierunku na wspólnym agregacie — jego split przymusowo OFF. | Nic — wróci po zmianie kierunku grupy. Jeśli częste: przemyśl korekty pokoi w grupie. |
| `fast_source_cannot_cool` | Pokój chce chłodzić, ale jego szybkie źródło nie umie (grzałka). | Informacyjne. |
| `cooling_disabled` | Pokój nie bierze udziału w chłodzeniu (opcja konfiguracji) — w trybie chłodzenia jest pomijany. | Zamierzone; zmień w opcjach pokoju, jeśli chcesz chłodzić. |
| `s1_floor_overheat` | Zabezpieczenie posadzki: zbyt gorące zasilanie — zawór zamknięty do ostygnięcia. | Sprawdź krzywą grzewczą/temperaturę zasilania pompy ciepła. |
| `s3_emergency_heat` | Awaryjne grzanie: pokój mocno wychłodzony — wymuszone grzanie wszystkim, co jest. | Sprawdź, czemu pokój tak wystygł (okno? awaria?). |
| `s4_emergency_cool` | Awaryjne chłodzenie: pokój mocno przegrzany — wymuszone chłodzenie. | Sprawdź źródło przegrzewu (słońce? awaria?). |
| `s5_watchdog` | Brak świeżych danych pokoju > 15 min — pozycja neutralna, alarm w raporcie. | Sprawdź czujniki/sieć; kasuje się po 5 min ciągłych świeżych danych. |
| `unknown_room` | Pokój bez konfiguracji regulatora (stan przejściowy po zmianach konfiguracji). | Przeładuj integrację; jeśli trwa — usuń i dodaj pokój. |
| `controller_error` | Wyjątek w regulatorze pokoju — bezpieczna degradacja (w grzaniu zawór trzyma pozycję, w chłodzeniu 0; split OFF). | Zajrzyj do logów HA i zgłoś problem (patrz §12). |

## 12. Rozwiązywanie problemów

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
  zamierzona zmiana raportowania — patrz §13. Przełącz pokój na Steruje, jeśli ma
  sterować.
- **Logi i diagnostyka** — Ustawienia → System → Logi (źródło
  `custom_components.tortoise_ufh`); pełny raport pokoju: szczegóły pokoju →
  „Pokaż surowe dane". Problemy zgłaszaj na GitHubie repozytorium z logiem
  i surowym raportem.

## 13. Ograniczenia, FAQ i historia wersji

**Ograniczenia (świadome, v1):** brak MPC/optymalizacji taryf, brak uczenia modelu
online, integracja nie steruje pompą ciepła ani stroną wodną (tylko publikuje
bezpieczny punkt rosy), brak czujnika posadzki (ochrona przez temperaturę zasilania),
jeden globalny tryb dla całego domu.

**FAQ**

- *Czy mogę używać tylko części pokoi?* Tak — pokoje w stanie Wyłączony są w pełni
  ignorowane przy zapisie; sterują tylko te przełączone na Steruje.
- *Czy zmiana temperatury domu działa od razu?* Tak — poza natychmiastową
  aktualizacją nastaw, w ciągu kilku sekund przeliczany jest pełny cykl sterowania.
- *Czy restart HA gubi nastawy?* Nie — temperatura domu, korekty i tryb są
  utrwalane i wracają po restarcie.

**Historia wersji / migracje**

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
