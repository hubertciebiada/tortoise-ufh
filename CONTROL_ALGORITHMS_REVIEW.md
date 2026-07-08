# Przegląd algorytmów sterowania — HVAC o dużej bezwładności termicznej

> **Czym jest ten dokument.** Wydestylowany, przenośny **przegląd algorytmów** sterowania
> ogrzewaniem/chłodzeniem podłogowym (UFH), oderwany od konkretnej implementacji. Zawiera:
> ogół problemu matematycznego, spektrum metod (od termostatu przez PID po MPC), rzeczywiste
> wzory (zweryfikowane z dojrzałym kodem, ~15k LOC, ~2100 testów), wskazania na biblioteki
> i metodykę. Cel: **zasiać nowe repo od czystej karty**, bez nalotów i dryfu.
>
> **Kierunek nowej aplikacji: PID.** Dlatego rodzina PID (kaskada, anti-windup, feedforward /
> outdoor reset, TPI) jest tu potraktowana jako **pierwszoplanowa i rekomendowana** dla
> podłogówki. MPC i predykcja są opisane jako warstwa wyższa — do świadomej decyzji „czy warto",
> nie jako domyślny wybór. Sekcja [§14](#14-pid-vs-mpc--kiedy-co) porównuje oba podejścia wprost.
>
> **Czego tu nie ma (celowo).** Kodu integracji z Home Assistant, roadmapy produktowej,
> biblioteki scenariuszy, wielu profili budynków. To naloty konkretnego repo, nie wiedza.
>
> **Konwencja jednostek:** temperatury °C, moce W, opory R [K/W], pojemności C [J/K], czas
> w sekundach (dyskretyzacja) lub minutach (symulacja). Pozycja zaworu 0–1.

---

## Spis treści

1. [Problem sterowania — ogół](#1-problem-sterowania--ogół)
2. [Przegląd i spektrum algorytmów](#2-przegląd-i-spektrum-algorytmów)
3. [Sterowanie klasyczne — PID i pochodne](#3-sterowanie-klasyczne--pid-i-pochodne)  ← rekomendowane
4. [Model matematyczny RC (wspólny fundament)](#4-model-matematyczny-rc-wspólny-fundament)
5. [MPC — warstwa predykcyjna](#5-mpc--warstwa-predykcyjna)
6. [Estymacja stanu — filtr Kalmana](#6-estymacja-stanu--filtr-kalmana)
7. [Identyfikacja parametrów RC (grey-box)](#7-identyfikacja-parametrów-rc-grey-box)
8. [Podmodele fizyczne (feedforward)](#8-podmodele-fizyczne-feedforward)
9. [Warstwa bezpieczeństwa](#9-warstwa-bezpieczeństwa-niezależna-od-algorytmu)
10. [Wejścia i wymagania danych](#10-wejścia-i-wymagania-danych)
11. [Biblioteki — wskazania](#11-biblioteki--wskazania)
12. [Metodyka implementacji i testów](#12-metodyka-implementacji-i-testów)
13. [Ściąga: stałe i wartości typowe](#13-ściąga-stałe-fizyczne-i-wartości-typowe)
14. [PID vs MPC — kiedy co](#14-pid-vs-mpc--kiedy-co)
15. [Źródła: papery, repozytoria, standardy](#15-źródła-papery-repozytoria-standardy)

---

## 1. Problem sterowania — ogół

### 1.1 Istota

Sterowanie źródłem ciepła/chłodu o **dużej bezwładności termicznej** (woda w wylewce
betonowej — UFH) tak, aby utrzymać temperaturę zadaną w każdym pomieszczeniu, zimą (grzanie)
i latem (chłodzenie). Opcjonalnie: wspomaganie **szybkim źródłem konwekcyjnym**
(split / fan-coil) w wybranych pomieszczeniach.

| Źródło | Nośnik | τ (stała czasowa) | Rola | Obowiązkowe |
|--------|--------|-------------------|------|-------------|
| UFH (podłogówka) | woda w wylewce | **4–6 h** | baza — wolne, ciągłe, energooszczędne | zawsze |
| Split (powietrze-powietrze) | konwekcja | **5–15 min** | boost — szybka korekta, droższa | opcjonalnie per pokój |

### 1.2 Matematyczna sygnatura problemu

To, co czyni problem interesującym, to **rozdział skal czasowych** i **różnica rzędu**
funkcji przenoszenia obu źródeł:

```
Podłogówka → T_room:  G_floor(s) ≈ K_f · e^(−θ·s) / [(τ₁s+1)(τ₂s+1)]   (2. rząd + opóźnienie)
  τ₁ ≈ 3–6 h, τ₂ ≈ 0.5–2 h, θ ≈ 15–45 min, K_f ≈ 0.005–0.02 °C/W

Split → T_room:        G_conv(s) ≈ K_c / (τ_c·s + 1)                    (1. rząd)
  τ_c ≈ 5–15 min, K_c ≈ 0.003–0.01 °C/W
```

Stosunek pojemności `C_slab / C_air ≈ 54:1` jest fizycznym źródłem tej separacji.

**Dwie twarze tej sygnatury:**
- **Utrudnienie dla reaktywnego sterowania:** zanim wylewka zareaguje (θ + τ), zaburzenie
  minęło. Czysty termostat cykluje i przeregulowuje.
- **Ułatwienie dla PID + feedforward:** ta sama bezwładność czyni układ **quasi-stacjonarnym
  i samostabilizującym**. Dobrze dobrany strumień ciepła (outdoor reset) pokrywa większość
  strat; pętla PID koryguje tylko resztę. Duża masa wybacza wolnym regulatorom.

### 1.3 Cel i ograniczenia

- **Cel (miękki):** minimalizuj dyskomfort `(T_room − T_set)²` i (opcjonalnie) koszt energii.
- **Komfort (twardy):** `T_room` nie może spaść poniżej `T_min` — nigdy, nawet dla oszczędności.
- **Ograniczenia fizyczne (twarde):**
  - `T_floor ≤ 34 °C` — ochrona wylewki/wykładziny.
  - `T_floor ≥ T_dew + 2 °C` — w trybie cooling, ochrona przed kondensacją.

> **Uwaga o limicie podłogi.** EN 1264 podaje 29 °C dla stref przebywania w *normalnych*
> warunkach. Przy ekstremalnie niskiej temperaturze zewnętrznej limit operacyjny **34 °C**
> jest akceptowalny i celowo wyższy — decyzja projektowa, nie błąd.

### 1.4 Patologia do uniknięcia — priority inversion (tylko przy 2 źródłach)

Jeśli szybkie źródło (split) jest obecne: grzeje pokój w minuty → termostat zadowolony →
zawór podłogówki się zamyka → wylewka stygnie → split pracuje non-stop jako *primary*.
COP spada, koszty rosną, bezwładna baza jest martwa. **Reguła:** wolne źródło jest zawsze
bazą; szybkie tylko skraca czas dojścia do komfortu, nigdy nie przejmuje roli.
(Opis patologii i rozwiązanie przez PWM na wolnym źródle: Tekmar Essay E006, Watts.)

> **Dla systemu UFH-only** cała ta sekcja odpada — problem redukuje się do SISO. To zresztą
> najczęstszy przypadek i naturalne środowisko dla dobrze zrobionego PID + outdoor reset.

---

## 2. Przegląd i spektrum algorytmów

Metody układają się na osi **reaktywne → predykcyjne**, równolegle do osi **bez modelu →
z modelem**. Im niżej w tabeli, tym więcej wiedzy i infrastruktury wymaga algorytm — i tym
więcej potencjalnie zyskuje, o ile są cele, które to uzasadniają (taryfy, solar, 2 źródła).

| Algorytm | Model budynku? | Prognoza? | Złożoność | Typowe zastosowanie |
|----------|:--------------:|:---------:|:---------:|---------------------|
| **On/off (histereza / bang-bang)** | nie | nie | trywialna | prosty termostat, baseline odniesienia |
| **P / PI / PID** | nie | nie | niska | pojedyncza pętla temperatury |
| **PID kaskadowy + anti-windup** | nie | nie | niska–śr. | `T_room → T_slab → zawór` |
| **PID + outdoor reset (feedforward)** | luźny (krzywa) | opcjonalnie | **średnia** | **← podłogówka w praktyce** |
| **Rule-based / heurystyka na modelu** | tak (luźny) | tak | średnia | ~80% optimum tanim kosztem |
| **LQR / dekompozycja perturbacji osobliwej** | tak | feedback | śr.–wys. | akademickie; wolny/szybki podsystem |
| **MPC (QP, liniowy)** | tak | tak | wysoka | pre-ładowanie masy, taryfy, 2 źródła, twarde ograniczenia optymalnie |
| **Stochastyczny MPC / NMPC** | tak | tak | b. wysoka | niepewność prognozy, nieliniowości |

**Wnioski z literatury (skrót):**
1. Model 2. rzędu (2R2C) wystarcza dla MPC — 4R4C nie daje proporcjonalnej poprawy
   (Sourbron & Verhelst 2013).
2. Proste reguły heurystyczne przybliżają ~80% optimum (Sharifi et al. 2022) — warto mieć je
   jako baseline i sanity-check.
3. Horyzont predykcji ≥ 24 h jest konieczny dla wolnego źródła (krótszy traci zdolność
   pre-ładowania masy termicznej).
4. Night setback bywa **kontrproduktywny** przy dużej bezwładności — rozładowanie i ponowne
   naładowanie wylewki kosztuje więcej, niż daje.
5. Sterowanie optymalne jest z natury **bang-bang** (zasada minimum Pontryagina): ładuj masę,
   gdy przyszły koszt braku ciepła > koszt energii teraz; szybkie źródło tylko przy zagrożeniu
   komfortu.

**Rekomendacja dla nowej aplikacji:** zacznij od **PID kaskadowego + outdoor reset +
anti-windup + deadband** (§3). To pokrywa większość wartości. MPC (§5) dokładaj tylko, gdy
pojawi się konkretny cel, którego PID nie osiąga (§14).

---

## 3. Sterowanie klasyczne — PID i pochodne

> Rekomendowany rdzeń nowej aplikacji. Cała sekcja nie wymaga solvera QP ani estymatora —
> wystarczy `numpy`/czysty Python. Model RC (§4) i identyfikacja (§7) przydają się tu **tylko**
> do offline'owego strojenia na symulatorze, nie w pętli sterowania.

### 3.1 Spektrum — od termostatu do PID+FF

- **On/off (histereza):** termostat z martwą strefą. Baseline. Działa, ale cykluje i nie
  wykorzystuje masy termicznej.
- **P / PI / PID:** dla podłogówki **człon D zwykle szkodzi** — opóźnienie `θ` i szum czujnika
  wzmacniają różniczkowanie. **PI wystarcza** w niemal wszystkich przypadkach.
- **PID + feedforward + kaskada:** poziom praktyczny (poniżej).

### 3.2 Kaskada i jej odwrócenie

```
Pętla zewnętrzna:  T_room  → setpoint T_slab (lub T_supply)      [wolna korekta na błędzie pokoju]
Pętla wewnętrzna:  T_slab  → pozycja zaworu 0–100%               [utrzymanie zadanej wylewki]
```

**Trudność strukturalna:** w klasycznej kaskadzie pętla wewnętrzna jest *szybsza* od
zewnętrznej. Tu jest **odwrotnie** — wylewka (wewnętrzna) jest wolniejsza niż powietrze
(zewnętrzna). Dlatego czysta, agresywna kaskada bywa niestabilna. **Praktyczne rozwiązanie:**
oprzeć setpoint wylewki głównie na **feedforwardzie (outdoor reset, §3.4)**, a pętli PI dać
tylko rolę powolnego korektora resztowego błędu `T_room`.

### 3.3 Anti-windup (konieczny)

Zawór nasyca się w `[0, 1]`, a wylewka reaguje wolno → integrator „ładuje się" podczas
nasycenia → duże przeregulowanie po dojściu do celu. Minimalny, sprawdzony wzorzec:

```
error = T_set − T_air
integral += error · dt
integral = clip(integral, −I_max, +I_max)      ← clamp (albo back-calculation)
output = Kp · error + Ki · integral
output = clip(output, 0, 1)                     ← zawór 0–100%
```

Lepsza wersja to **back-calculation**: gdy `output` nasycony, „cofnij" integrator o różnicę
`(output_przed_clip − output_po_clip)/Kb`. Clamp jest prostszy i zwykle wystarcza.

### 3.4 Feedforward — SEDNO sterowania podłogówką

To feedforward, nie sprzężenie zwrotne, wykonuje większość pracy — bo podłogówka jest
quasi-stacjonarna:

- **Outdoor reset / krzywa kompensacji pogodowej:** setpoint wylewki (lub `T_supply`) jako
  funkcja `T_out` (pełne wzory w [§8.2](#82-krzywa-kompensacji-pogodowej-weather-compensation)).
  Dobór strumienia ciepła do bieżących strat = ~80% sterowania. PI tylko dostraja.
- **Feedforward solarny:** wyprzedzająca redukcja mocy przed nasłonecznieniem
  ([§8.4](#84-zyski-solarne--pełen-gti-erbs--liu-jordan)). Wymaga prognozy GHI — i tu PID
  zaczyna „podglądać przyszłość", zbliżając się do predykcji. Można poprzestać na reakcji na
  bieżące `Q_sol` (mierzone/liczone) bez prognozy.

### 3.5 Realizacja aktuatora — TPI / PWM

Siłowniki termoelektryczne zaworów są często **on/off** i wolne. „Pozycję 0–100%" realizuje
się jako **duty cycle (PWM)** w oknie np. 15–30 min → **TPI (Time-Proportional & Integral)**.
Dobierz okno PWM znacznie krótsze od `τ_slab` (minuty vs godziny), by pulsowanie nie przebijało
się na temperaturę pokoju. (Wzorzec spopularyzowany m.in. przez HA „Versatile Thermostat".)

### 3.6 Dodatki praktyczne

- **Deadband** (np. ±0.5 °C): brak reakcji w paśmie → mniej cyklowania i zużycia aktuatora.
- **Valve floor minimum** (15–20% w sezonie grzewczym): utrzymuje wylewkę „ciepłą w gotowości",
  skraca czas odpowiedzi po spadku.
- **Gain scheduling:** inne nastawy `Kp, Ki` dla rozruchu (duży błąd) vs steady-state.
- **Deadband przełączania heat/cool** z histerezą na `T_out` (tryb auto: zimna noc → ciepły dzień).

### 3.7 Strojenie

- **Lambda / IMC tuning** — dla modeli 1. rzędu z opóźnieniem (FOPDT). Stabilne, nieagresywne,
  jedna „ręczna gałka" (`λ` = pożądana stała czasowa zamkniętej pętli). **Najlepiej pasuje do
  podłogówki.**
- **Relay (Åström–Hägglund) auto-tuning** — automatyczne wzbudzenie oscylacji przekaźnikiem →
  nastawy. Używane np. w HA „SAT".
- **Ziegler–Nichols** — szybki, ale agresywny; ostrożnie przy dużym opóźnieniu `θ`.
- **Strojenie na symulatorze (rekomendowane):** zidentyfikuj RC (§7) → zbuduj cyfrowego
  bliźniaka (§12) → parametric sweep po `Kp, Ki` z metrykami komfortu/energii. Spina PID
  z resztą wiedzy i pozwala stroić offline, bez ryzyka na żywym domu.

### 3.8 Dlaczego PID zwykle wystarcza dla podłogówki

Wylewka jest samostabilizująca i quasi-stacjonarna. **Outdoor reset (FF) + korektor PI na
`T_room` + anti-windup + deadband** daje solidny, przewidywalny wynik — bez solvera, bez
identyfikacji online, bez prognozy, łatwy do zaufania i debugowania. MPC wygrywa dopiero przy
konkretnych celach z [§14](#14-pid-vs-mpc--kiedy-co); bez nich prostszy regulator jest właściwym
wyborem inżynierskim.

---

## 4. Model matematyczny RC (wspólny fundament)

> Potrzebny do **symulatora** (strojenie PID i MPC offline), do **MPC** (predykcja) oraz do
> **feedforwardu**. W samej pętli PID nie jest wymagany.

### 4.1 Analogia elektryczna

Temperatura ↔ napięcie, ciepło ↔ prąd, `R [K/W]` ↔ rezystancja, `C [J/K]` ↔ kondensator.
Równanie węzła: `C_j · dT_j/dt = Σ_h (T_h − T_j)/R_{h,j} + Q_j`.

### 4.2 Struktura 3R3C (i 2R2C)

```
x = [T_air, T_slab, T_wall]ᵀ          (2R2C: [T_air, T_slab])
u = [Q_conv, Q_floor]ᵀ  (MIMO, split)   lub  [Q_floor] (SISO, UFH-only)
d = [T_out, Q_sol, Q_int]ᵀ            (2R2C: [T_out, Q_sol])
```

```
C_air  · dT_air/dt  = (T_slab−T_air)/R_sf + (T_wall−T_air)/R_wi + (T_out−T_air)/R_ve
                      + Q_conv + Q_int + f_conv·Q_sol
C_slab · dT_slab/dt = (T_air−T_slab)/R_sf + (T_ground−T_slab)/R_ins + Q_floor
C_wall · dT_wall/dt = (T_air−T_wall)/R_wi + (T_out−T_wall)/R_wo + f_rad·Q_sol
```

`f_conv + f_rad ≤ 1` (reszta zysku solarnego odbita). Domyślnie `f_conv=0.6, f_rad=0.4`.

### 4.3 Postać macierzowa: ẋ = A·x + B·u + E·d + b

**A (3×3):**
```
A[0,0] = −(1/(R_sf·C_air) + 1/(R_wi·C_air) + 1/(R_ve·C_air))
A[0,1] = 1/(R_sf·C_air)          A[0,2] = 1/(R_wi·C_air)
A[1,0] = 1/(R_sf·C_slab)         A[1,1] = −(1/(R_sf·C_slab) + 1/(R_ins·C_slab))
A[2,0] = 1/(R_wi·C_wall)         A[2,2] = −(1/(R_wi·C_wall) + 1/(R_wo·C_wall))
```

**B** — dwa wejścia trafiają w strukturalnie różne węzły:
```
MIMO:  B = [1/C_air   0     ]   ← Q_conv TYLKO do powietrza
           [0         1/C_slab]  ← Q_floor TYLKO do wylewki
           [0         0      ]
SISO:  B = [0; 1/C_slab; 0]      ← jedyne wejście
```

**E (3×3)** i **b**:
```
E[0,0]=1/(R_ve·C_air)  E[0,1]=f_conv/C_air  E[0,2]=1/C_air
E[2,0]=1/(R_wo·C_wall) E[2,1]=f_rad/C_wall
b[1] = T_ground/(R_ins·C_slab)
```

### 4.4 Dyskretyzacja ZOH przez macierz augmentowaną

Stabilna numerycznie nawet dla sztywnych układów (duży `C_slab/C_air`):
```
        [ A_c  B_c  E_c  b_c ]
M   =   [  0    0    0    0  ]      Ms = expm(M · dt)  (scipy.linalg.expm)
        [  0    0    0    0  ]      → A_d,B_d,E_d,b_d z bloków Ms
        [  0    0    0    0  ]
```
Krok: `x[k+1] = A_d·x[k] + B_d·u[k] + E_d·d[k] + b_d`.
Steady-state: `x_ss = −A_c⁻¹·(B_c·u + E_c·d + b_c)`.

### 4.5 Rozdział skal czasowych

`ε = C_air/C_slab ≪ 1` → wolny podsystem (wylewka/powłoka) i szybki (powietrze). Kontroler
złożony `u = u_slow + u_fast` przybliża pełne optimum do `O(ε)` (Gupta et al. 2017). Daje
intuicję, dlaczego dwa źródła da się rozprząc — i dlaczego feedforward na wolnej dynamice
załatwia większość pracy.

---

## 5. MPC — warstwa predykcyjna

> Warstwa wyższa niż PID. Włączaj świadomie (patrz [§14](#14-pid-vs-mpc--kiedy-co)).

### 5.1 Funkcja kosztu

```
J = w_comfort · Σ_{k=1..N} (T_air[k] − T_set)²      ← komfort
  + w_energy  · Σ ‖u[k]‖²                            ← wysiłek aktuatora / energia
  + w_smooth  · Σ ‖u[k] − u[k−1]‖²                   ← move-suppression
  + w_slack   · Σ slack[k]                           ← kara za naruszenie miękkiego pasma
```
Domyślne wagi: `w_comfort=1.0, w_energy=0.1, w_smooth=0.01, w_slack=1000`.

**Rozszerzenie taryfowe:** zamień/dodaj człon energii na `Σ c_elec(t[k]) · P_elec[k] · Δt`,
gdzie `P_elec = Q/COP(T_out,T_supply)` a `c_elec(t)` to cena spot. Komfort pozostaje twardym
ograniczeniem (slack z dużą karą) — taryfa nigdy nie wypycha `T_room < T_min`.

### 5.2 Ograniczenia

```
x[0] = x0
x[k+1] = A_d·x[k] + B_d·u[k] + w[k],   w[k] = E_d·d[k] + b_d
T_slab[k] ≤ T_floor_max                (34 °C)
T_slab[k] ≥ T_dew[k] + margin          (2 K; cooling)
0 ≤ u_floor ≤ 1
split: heating 0≤u_conv≤1,  cooling −1≤u_conv≤0
T_set−band−slack ≤ T_air[k] ≤ T_set+band+slack,   slack ≥ 0
```
Horyzont `N=96` (24 h × 15 min). 2 wejścia → 192 zmienne: **mały QP**.

### 5.3 Konstrukcja QP i DPP (klucz do wydajności)

Buduj problem **raz** jako sparametryzowany QP zgodny z **DPP** (re-solve bez rekompilacji).
Zasada: **nie mnóż parametru przez parametr.** Iloczyn `E_d @ d` (oba parametry) łamie DPP —
policz zakłócenie poza solverem jako jeden parametr:
```
w[k] = E_d · d[k] + b_d   ← policz w numpy PRZED solve, wstrzyknij jako jeden Parameter
```
`A_d, B_d` jako parametry są OK (mnożą zmienne). Granice splitu (zależne od trybu) też jako parametry.

### 5.4 Receding horizon, warm start, fallback

- **Receding horizon:** rozwiąż cały horyzont, zastosuj tylko `u[0]`, powtórz co 5–15 min.
- **Warm start:** zasiej solver poprzednim rozwiązaniem przesuniętym o krok (`np.roll(prev,−1)`).
- **Fallback:** infeasible / timeout / wyjątek → zejdź na minimalny PI (ten sam z §3.3).
  Sterowanie nie może zniknąć, gdy QP zawiedzie. Timeout solvera ~`0.1 s`.

### 5.5 Intuicja optymalności

Bang-bang (Pontryagin): podłogówka ładuje masę, gdy przyszły koszt braku ciepła > koszt energii
teraz; split włącza się tylko przy zagrożeniu komfortu. Proste reguły na modelu ≈ 80% optimum.

---

## 6. Estymacja stanu — filtr Kalmana

> Potrzebny głównie dla MPC (pełny `x0`). PID zwykle działa na samym `T_room`; Kalman może i tam
> wygładzać pomiar / rekonstruować `T_slab`, ale nie jest wymagany.

`T_slab` (i `T_wall`) zwykle **nie są mierzone**. Liniowy Kalman rekonstruuje je z
`T_room` (opcjonalnie `T_floor_surface`):
```
C = [1,0,0]                 (tylko T_room)
C = [[1,0,0],[0,1,0]]       (T_room + T_floor)
```

Cykl:
```
PREDICT: x⁻ = A_d·x + B_d·u + E_d·d + b_d ;  P⁻ = A_d·P·A_dᵀ + Q
UPDATE:  y = z − C·x⁻ ;  S = C·P⁻·Cᵀ + R ;  K = P⁻·Cᵀ·S⁻¹
         x = x⁻ + K·y ;  P = (I−K·C)·P⁻·(I−K·C)ᵀ + K·R·Kᵀ   (forma Josepha)
```
**Detale stabilności:** licz `K` przez `solve`, nie inwersję; symetryzuj `P=(P+Pᵀ)/2`; używaj
formy Josepha; brakujący pomiar (`z=None`) → sam predict, `P` rośnie, filtr się skoryguje.

Startowe kowariancje (dla `dt` rzędu minut):
`Q=diag([0.01,0.001,0.01])`, `R=diag([0.1])` lub `diag([0.1,0.2])`, `P0=diag([5,10,5])`
(duża niepewność `T_slab`).

---

## 7. Identyfikacja parametrów RC (grey-box)

> Przydatna i dla PID (offline strojenie na symulatorze, dobór outdoor reset), i dla MPC.

**Grey-box:** znana struktura (równania RC), nieznane `R,C`. Minimalizuj MSE między
predykowanym `T_air` a mierzonym `T_room`:
```
θ* = argmin_θ  mean( (T_air_pred(θ) − T_room_measured)² )
```
- 2R2C: `[R_sf, R_env, C_air, C_slab]` (4 param.); 3R3C: `[R_sf,R_wi,R_wo,R_ve,C_air,C_slab,C_wall]` (7).

**Log-przestrzeń + multi-start** (bo `R~0.01` vs `C~10⁶` — wiele rzędów wielkości):
```
method="L-BFGS-B" (box bounds),  options={maxiter:500, ftol:1e-12, gtol:1e-8}
φ = log(θ),  sampling log-uniform w [log(lo),log(hi)],  n_starts≈10,  wybór: najlepszy fun()
```
**Granice (2R2C, przykład):** `R_sf∈(0.001,0.1)`, `R_env∈(0.005,0.2)`, `C_air∈(1e4,5e5)`, `C_slab∈(5e5,2e7)`.

**Burn-in i walidacja:**
- Zainicjuj wszystkie węzły pierwszym `T_room`, wyłącz pierwsze ~6 h z MSE (nieznany `T_slab`).
- Walidacja out-of-sample: predykcja 6/12/24 h; cel **RMSE(12 h) < 0.5 °C**.
- Cross-validation czasowa (fold po dniach/tyg.) — stabilność parametrów = brak przeuczenia.
- Dane muszą mieć **pobudzenie** (zmienność) — 2–4 tyg. naturalnej pogody i sterowania.

---

## 8. Podmodele fizyczne (feedforward)

Czyste funkcje: dostarczają `d` i przeliczają aktuatory na moc. Wspólne dla PID (feedforward)
i MPC. **Dla PID-owej aplikacji §8.2 (outdoor reset) i §8.3 (rosa) są najważniejsze.**

### 8.1 Moc pętli UFH — wzór zredukowany EN 1264

```
Q = U · A · ΔT_log
U_pipe_per_m = 2π·k_pex / ln(d_outer/d_inner)          k_pex = 0.35 W/(m·K)
f_spacing    = 1 / (1 + spacing/(π·d_outer))
U            = U_pipe_per_m · f_spacing · L_pipe / A
ΔT_log = (ΔT_in − ΔT_out)/ln(ΔT_in/ΔT_out)
  heating: ΔT_in=T_supply−T_slab, ΔT_out=T_return−T_slab   (domyślny spadek 5 °C)
  cooling: ΔT_in=T_slab−T_supply, ΔT_out=T_slab−T_return   (domyślny 3 °C), zwraca Q<0
```
Gdy `|ΔT_in−ΔT_out|<ε` → średnia arytmetyczna; gdy `ΔT≤0` → `Q=0`. Strażnik kierunku: heating
i `T_supply≤T_slab` → 0; cooling i `T_supply≥T_slab` → 0. **Asymetria:** cooling ~30–40 W/m²
vs heating ~50–80 W/m².

### 8.2 Krzywa kompensacji pogodowej (weather compensation)

Rdzeń outdoor reset dla PID:
```
heating: T_supply = clip(base + slope·max(0, T_neutral − T_out), min, max)
cooling: T_supply = clip(base + slope·max(0, T_out − T_neutral), min, max)
```
Domyślny dolny clamp: heating 20 °C, cooling 7 °C. Parametry (`base, slope, T_neutral, max`) to
zwykle 2–3 nastawy widoczne w pompie (Aquarea/Panasonic, Daikin, Vaillant, Ecodan…).

### 8.3 Punkt rosy — Magnus (ochrona przed kondensacją)

Magnus (Alduchov & Eskridge 1996), błąd < 0.1 °C dla `T∈[−40,60]`:
```
a=17.625, b=243.04
γ = a·T_air/(b+T_air) + ln(RH/100) ;   T_dew = b·γ/(a−γ)
```
Uproszczenie legacy: `T_dew ≈ T_air − (100−RH)/5`. **Graduowane dławienie** zaworu chłodzenia:
```
gap = T_floor − T_dew
throttle = 0                 gdy gap ≤ margin
         = 1                 gdy gap ≥ margin+ramp_width
         = (gap−margin)/ramp_width   pomiędzy       (domyślnie margin=2, ramp_width=2)
```
RH jest krytycznym pomiarem **tylko dla cooling** — czujnik w każdym pokoju z floor cooling.

### 8.4 Zyski solarne — pełen GTI (Erbs + Liu-Jordan)

```
I0h = 1361·(1 + 0.033·cos(2π·doy/365))·sin(elev)
kt  = GHI/I0h  →  kd (Erbs):
   kt≤0.22:  kd = 1 − 0.09·kt
   0.22<kt≤0.80: kd = 0.9511 − 0.1604kt + 4.388kt² − 16.638kt³ + 12.336kt⁴
   kt>0.80:  kd = 0.165
I_diffuse = kd·GHI ;  I_beam = GHI − I_diffuse
DNI = I_beam/sin(elev)  (elev≥~1°)
cosθ = cos(elev)·cos(az_słońca − az_okna)   (clamp ≥0)
GTI = DNI·cosθ + I_diffuse·0.5 + GHI·albedo·0.5      (albedo≈0.2)
Q_sol = Σ_okna GTI·A_okno·g_value                    (g≈0.5–0.7)
```
> **Alternatywa produkcyjna:** `pvlib` (Erbs, Liu-Jordan/Perez, pozycja słońca) — rygorystyczniej
> niż wzory pisane ręcznie.

### 8.5 Model COP (do członu kosztu/energii)

`P_elec = Q_thermal / COP`. Trzy tryby: **AUTO_LEARNED** (regresja biliniowa
`COP = a0 + a1·T_out + a2·T_supply`, min. 48 h danych), **LOOKUP_TABLE** (`numpy.interp` po
`T_out`), **CONSTANT** (domyślnie 3.5). Wynik clampowany do `[1.0, 8.0]`.

---

## 9. Warstwa bezpieczeństwa (niezależna od algorytmu)

Bezpieczeństwo musi być **niezależne od algorytmu** — awaria procesu sterującego nie może
wyłączyć zabezpieczeń. Realizuj jako reguły natywne poza algorytmem (np. automatyzacje YAML),
priorytet: `Safety > algorytm > natywna krzywa pompy`.

Reguły z histerezą (on/off), sortowane po priorytecie:

| Reguła | on / off | Akcja | Prio |
|--------|----------|-------|------|
| **S1** przegrzanie podłogi | `T_floor > 34` / `< 33` | zamknij zawór | 1 |
| **S2** kondensacja | `T_floor − (T_dew+2) < 0` / `> 1` | zamknij zawór | 1 |
| **S3** awaryjne grzanie | `T_room < 5` / `> 6` | wymuś grzanie | 2 |
| **S4** awaryjne chłodzenie | `T_room > 35` / `< 34` | wymuś chłodzenie | 2 |
| **S5** watchdog | brak update `> 15 min` / `< 5` | fallback na krzywą pompy | 3 |

Osobne progi on/off = brak oscylacji na granicy. Reguły to **dane** (dataclass), nie hierarchia klas.

---

## 10. Wejścia i wymagania danych

**Pomiary:** `T_room` per pokój (30–60 s, minimum krytyczne); `RH_room` (tylko cooling);
`T_floor_surface` (opcjonalnie — ochrona + lepsza identyfikacja); `T_supply/T_return`, pozycja
zaworu, `T_outdoor`, stan pompy (heat/DHW/idle/defrost), `P_electric`.

**API:** pogoda (np. Open-Meteo, 48 h, godzinowo): `temperature_2m`, GHI/`global_tilted_irradiance`,
`relative_humidity_2m`, `wind_speed_10m`; historyczne: `archive-api.open-meteo.com/v1/archive`.
Taryfa (opcjonalnie): cena spot godzinowo.

**Konfiguracja:** per pokój `T_set`, okna (orientacja, powierzchnia, `g`), powierzchnia podłogi,
geometria pętli, czy split. Budynek: grubość wylewki. Sprzęt: moc splitu, COP/tabela, zasobnik CWU.

**Nie potrzeba:** CO2, obecność domowników (nice-to-have).

---

## 11. Biblioteki — wskazania

### 11.1 Dla aplikacji PID-owej (lekki rdzeń)

| Biblioteka | Wersja | Do czego |
|-----------|--------|----------|
| **numpy** | ≥ 1.26 | wektory/macierze; regulator da się napisać nawet w czystym Pythonie |
| **scipy** | ≥ 1.12 | `linalg.expm` (symulator), `optimize.minimize` (strojenie/identyfikacja offline) |

Sam PID **nie wymaga** cvxpy/osqp. Python ≥ 3.12, type hints wszędzie.

### 11.2 Gdy dokładasz MPC

| Biblioteka | Wersja | Do czego |
|-----------|--------|----------|
| **cvxpy** | ≥ 1.5 | modelowanie QP (DPP — kompilacja raz, re-solve wiele) |
| **osqp** | ≥ 0.6 | solver QP (operator splitting, warm-start) |

### 11.3 Wizualizacja / dev

`matplotlib ≥ 3.8`, `pandas ≥ 2.0`, `plotly ≥ 5.18` + `dash ≥ 2.14` (replay).
`pytest ≥ 8` + `pytest-cov`, `ruff ≥ 0.3` (lint **i** format — sprawdzaj oba), `mypy ≥ 1.9`
(`strict`), `pre-commit ≥ 4`.

### 11.4 Alternatywy zamiast pisania od zera

| Zamiast | Rozważ | Kiedy |
|---------|--------|-------|
| ręczny PID | `simple-pid` | typowy PID z anti-windup, jako baza |
| ręczny wrapper QP | **osqp** bezpośrednio | ustalona struktura QP, bez narzutu cvxpy |
| cvxpy+OSQP | **do-mpc**, **acados** | nieliniowość, twardy real-time |
| ręczny Kalman | **filterpy** | prototypowanie |
| ręczne wzory solarne | **pvlib** | rygorystyczna geometria słońca |
| ręczna identyfikacja | **DarkGreyBox** | grey-box RC fitting jako start |

---

## 12. Metodyka implementacji i testów

### 12.1 Symulator jako cyfrowy bliźniak

Fundament: **symulator budynku** (model RC z *known-ground-truth* parametrami + pogoda + szum).
Kontroler (PID lub MPC) steruje symulatorem zamiast prawdziwym domem. To test harness dla
wszystkiego — i **stanowisko do strojenia PID** offline.
```
for t in range(duration_minutes):
    measurements = sim.get_measurements(noise)
    actions = ctrl.step(measurements, t)      # PID albo MPC — ten sam interfejs
    sim.apply(actions)
    log.append(t, measurements, actions)
```
Wydajność: tydzień @ 1 min ≈ 10 080 kroków < 1 s; rok z MPC co 15 min ≈ 1 min. Symulacja jest
tania — używaj hojnie (parametric sweep nastaw PID, A/B PID vs MPC).

### 12.2 Dwie warstwy testów

- **Unit (TDD):** model vs rozwiązanie analityczne; steady-state; **PID: anti-windup nie
  przeregulowuje, deadband tłumi cyklowanie, valve floor respektowany**; MPC: constraint
  `T_floor≤34` nigdy przekroczony, `w_energy→∞`⇒0 mocy; identyfikacja odzyskuje known `R,C ±10%`;
  fizyka: EN 1264 i Magnus vs referencje.
- **Symulacja (scenariusze):** całe przebiegi z asercjami — `comfort_pct` (% w paśmie ±0.5 °C),
  brak przekroczeń podłogi/rosy, energia ≤ baseline, A/B PID vs MPC na pełnym roku.

### 12.3 Ścieżka do produkcji

```
1. RED→test  2. GREEN  3. REFACTOR  4. SIMULATE  5. SHADOW (read-only: licz, loguj, NIE steruj)
6. LIVE (jeden pokój)  7. SCALE (kolejne)
```
**Shadow mode** daje pewność przed przejęciem sterowania. **Zasada nadrzędna:** żadnych zmian na
żywym systemie grzewczym bez jawnej, zatwierdzonej decyzji — development i testy lokalnie, w symulacji.

---

## 13. Ściąga: stałe fizyczne i wartości typowe

| Wielkość | Wartość | Uwaga |
|----------|---------|-------|
| `C_air` | ~60 kJ/K (20 m²) | `ρ·c·V` powietrza |
| `C_slab` | ~3250 kJ/K (80 mm) | ≈ 10 kWh — "bateria termiczna" |
| `C_wall` | 500–5000 kJ/K | zależnie od konstrukcji |
| `C_slab/C_air` | **~54:1** | źródło rozdziału skal czasowych |
| `R_sf` | ~0.01 K/W | podłoga→powietrze |
| `R_ins` | 0.005–0.02 K/W | izolacja pod wylewką |
| `R_ve` | 0.01–0.05 K/W | wentylacja/infiltracja |
| `τ_slab` | 4–6 h | (τ₆₃ ≈ 4.4–4.7 h) |
| `τ_split` | 5–15 min | źródło konwekcyjne |
| limit `T_floor` | 34 °C (op.) / 29 °C (EN 1264) | |
| margines rosy | `T_floor ≥ T_dew + 2 °C` | |
| moc heating / cooling | 50–80 / 30–40 W/m² | asymetria |
| `k_pex` | 0.35 W/(m·K) | rura PE-X |
| `g_value` okna | 0.5–0.7 | |
| albedo | ~0.2 | trawa/gleba |
| stała słoneczna | 1361 W/m² | |
| okno PWM (TPI) | 15–30 min | ≪ τ_slab |
| cykl sterowania | 5–15 min | |
| horyzont MPC | 24 h (≥1×τ_slab), np. 96×15 min | |

**Punkt odniesienia (realny dom):** parterowy, ~13 pętli UFH, pompa powietrze-woda ~4.9 kW,
wylewka ~7 cm, ściany ~30 cm wełny, strop ~20 cm, `Q_total≈4610 W`, `L_rur≈411 m`,
płd. Polska (`lat≈50.5, lon≈19.5`).

---

## 14. PID vs MPC — kiedy co

| Kryterium | **PID + outdoor reset** | **MPC (QP)** |
|-----------|-------------------------|--------------|
| Złożoność implementacji | niska | wysoka (solver + estymator + model) |
| Zależności runtime | numpy (lub czysty Python) | + cvxpy + osqp |
| Model budynku | niepotrzebny (krzywa wystarcza) | wymagany (identyfikacja RC) |
| Prognoza pogody | opcjonalna | wymagana |
| Dane do startu | brak — działa od razu | 2–4 tyg. do identyfikacji |
| Pre-ładowanie masy termicznej | nie (reaktywny + FF) | tak, natywnie |
| Taryfy dynamiczne (koszt) | trudne / ad-hoc | naturalne (człon `c_elec·P`) |
| Antycypacja zysków solarnych | tylko reakcja bieżąca | wyprzedzająca redukcja |
| Koordynacja 2 źródeł | reguły + histereza | w funkcji kosztu |
| Twarde ograniczenia (34 °C, rosa) | przez warstwę safety | w QP + safety |
| Zaufanie i debugowanie | łatwe, przewidywalne | trudniejsze (czarna skrzynka QP) |
| Ryzyko | niskie | wyższe (infeasibility, timeouts) |

**Reguła decyzyjna:**
- **Zacznij od PID** (kaskada + outdoor reset + anti-windup + deadband). Dla UFH-only bez
  optymalizacji taryf to zwykle wystarcza i jest właściwym wyborem inżynierskim.
- **Dokładaj MPC tylko** gdy pojawi się konkretny cel, którego PID nie osiąga: (a) pre-ładowanie
  wylewki pod taryfy dynamiczne, (b) wielogodzinna antycypacja pogody/solar, (c) koordynacja
  dwóch źródeł bez priority inversion, (d) optymalne trzymanie twardych ograniczeń.
- **Ścieżka hybrydowa:** ten sam interfejs `ctrl.step(measurements) → actions` dla PID i MPC.
  Zbuduj symulator (§12), strój PID na nim, a MPC dołóż później za flagą — bez przepisywania reszty.

---

## 15. Źródła: papery, repozytoria, standardy

### Papery
- **Drgoňa, Picard, Helsen (2020)** — Cloud MPC for GEOTABS, *J. Process Control* (53.5% energii,
  36.9% komfortu vs rule-based).
- **Atienza-Márquez et al. (2017)** — Radiant floor + fan-coil, *Energy & Buildings* (τ₆₃ 4.4–4.7 h).
- **Killian & Kozek (2018)** — Hierarchiczny MIMO MPC, *Applied Energy*.
- **Rastegarpour et al. (2019)** — MPC dla A2W HP + radiant floor.
- **Oldewurtel et al. (2012)** — Stochastyczny MPC z prognozą, *E&B*.
- **Gupta et al. (2017)** — Perturbacja osobliwa (wolny/szybki podsystem).
- **Liu et al. (2016)** — Star-RC model wylewki, błąd < 5.5% vs FEM.
- **Li et al. (2021)** — MPC radiant floor: −56% czasu odpowiedzi, +24.5% COP vs PID.
- **Sourbron & Verhelst (2013)** — 2R2C wystarcza dla MPC.
- **Sharifi et al. (2022)** — proste reguły ≈ 80% optimum.
- **Åström & Hägglund** — *PID Controllers: Theory, Design, and Tuning* (relay auto-tuning, anti-windup).
- **Alduchov & Eskridge (1996)** — Magnus; **Erbs et al. (1982)** — dekompozycja dyfuzu;
  **Liu & Jordan (1960)** — izotropowy dyfuz.

### Repozytoria / narzędzia
- `simple-pid` — PID z anti-windup. `pvlib` — modele solarne. `DarkGreyBox` — grey-box RC.
- `do-mpc`, `acados` — MPC (nieliniowy / real-time). `filterpy` — filtry Kalmana.
- HA (odniesienie problemowe): `versatile_thermostat` (TPI + auto-learning), `SAT`
  (PID + outdoor reset + relay autotune), `ha-dual-smart-thermostat`, `haos_mpc`, `roommind` (MPC+EKF).
- Komercyjne wzorce: Tekmar 557 (PWM na wolnym źródle), Ekinex KNX (offset setpointów).

### Standardy
- **EN 1264** — ogrzewanie podłogowe, max temp. powierzchni (29 °C strefy przebywania w normalnych
  warunkach; limit operacyjny może być wyższy w ekstremalnych warunkach).
- **ISO 13790 / EN ISO 52016-1** — model budynku 5R1C (rozszerzalny do 5R2C).

---

*Dokument wydestylowany z dojrzałej implementacji (model 3R3C, PID+fallback, MPC cvxpy/OSQP,
Kalman, identyfikacja L-BFGS-B, EN 1264, Magnus, GTI Erbs/Liu-Jordan, safety S1–S5). Wszystkie
wzory zweryfikowane z kodem. Przeznaczenie: fundament nowego, czystego repozytorium sterowania —
z rdzeniem PID i predykcją (MPC) jako opcjonalną warstwą wyższą.*
