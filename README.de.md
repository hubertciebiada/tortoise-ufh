<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="custom_components/tortoise_ufh/brand/logo-dark.png">
    <img src="custom_components/tortoise_ufh/brand/logo.png" alt="Tortoise-UFH — Fußbodenheizung, vereinfacht" width="360">
  </picture>
</p>

# Tortoise-UFH

**Raumweise Klimaregelung im geschlossenen Regelkreis für Fußbodenheizungen mit hoher thermischer Masse in Home Assistant — die langsame Schildkröte trägt die Last, der schnelle Hase schließt die Lücke.**

[![HACS Custom][hacs-badge]][hacs-url]
[![Home Assistant][ha-badge]][ha-url]
[![License: MIT][license-badge]][license-url]

[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[hacs-url]: https://hacs.xyz
[ha-badge]: https://img.shields.io/badge/Home%20Assistant-2024.1+-blue.svg
[ha-url]: https://www.home-assistant.io
[license-badge]: https://img.shields.io/badge/License-MIT-green.svg
[license-url]: LICENSE

> **Sprache / Language / Język:** **Deutsch** (diese Seite) · [English](README.md) · [Polski](docs/manual/pl.md)
>
> Das vollständige Endbenutzer-Handbuch (Installation, Konfiguration, das Seitenleisten-Panel, die
> Abstimmung und das Meldungsverzeichnis) wird je Sprache in [`docs/manual/`](docs/manual/) gepflegt —
> [Deutsch](docs/manual/de.md) · [Polski](docs/manual/pl.md). Dieses README ist die deutsche Übersicht.

---

## Was es ist

Tortoise-UFH ist eine benutzerdefinierte Home-Assistant-Integration, die einen unabhängigen
Regelkreis je Raum für ein Fußbodenheizungshaus (UFH) mit einer schweren Betonplatte betreibt —
und optional eine Zusatzquelle (Split / Klimagerät) zur Unterstützung. Sie unterstützt sowohl
**Heizen** als auch **Flächenkühlung**.

Der Name ist die Fabel:

- **Die Schildkröte (żółw) = die langsame Fußbodenheizung.** Eine 60–80 mm dicke Betonplatte hat
  eine thermische Zeitkonstante von 4–6 Stunden. Sie ist gemächlich, erreicht aber stets den
  Sollwert und speichert Energie wie eine Batterie. Tortoise-UFH moduliert das Ventil jedes Raums,
  um diese Platte ohne Überschwingen zu steuern.
- **Der Hase (zając) = die Zusatzquelle.** Ein Split/Klimagerät reagiert in Minuten. Tortoise-UFH
  schaltet ihn nur zu, um *die Wartezeit auf Komfort zu verkürzen*, wenn der Raum deutlich außerhalb
  des Bands liegt, und lässt ihn nie stillschweigend zur primären Quelle werden (Schutz gegen
  Prioritätsumkehr). Der Split regelt sich selbst — Tortoise-UFH befiehlt Modus und Zieltemperatur,
  nie die Kompressorleistung.

Der Regler gehört zur **PID-Familie** (PI mit einem Trenddämpfungsglied plus optionaler
Wettervorsteuerung) — bewusst *kein* MPC. Es gibt keinen Kalman-Filter, keine
Online-Modellidentifikation und keine Warmwassersteuerung. Der Kern reagiert robust auf das, was er
messen kann, und degradiert sicher, wenn ein Sensor ausfällt.

Der reine Python-Regelkern (`custom_components/tortoise_ufh/core/`) importiert nie Home Assistant,
sodass der gesamte Algorithmus offline mit Unit- und Simulationstests prüfbar ist. Der Rest von
`custom_components/tortoise_ufh/` ist ein dünner Home-Assistant-Adapter darum herum.

---

## Die drei Ausgaben

In jedem 5-Minuten-Zyklus erzeugt Tortoise-UFH für das gesamte Gebäude genau drei Arten von
Befehlen:

1. **Je Raum — Ventilstellung, `0..100 %`.** Ein Wert je Raum/Zone; jeder Heizkreis der
   Fußbodenheizung in diesem Raum erhält ihn. Eine Ventil-Mindestöffnung hält die Platte beim
   Heizen sanft aktiv; eine Taupunkt-Drosselung je Raum schließt das Ventil, wenn sich das
   Vorlaufwasser beim Kühlen dem Taupunkt des Raums nähert.
2. **Je Raum — Befehl an die Zusatzquelle.** `ON` + Richtung (Heizen / Kühlen, je nach Modus) +
   die Zieltemperatur des Raums. Der Split regelt sich selbst um dieses Ziel; Mindestlauf-/
   Mindeststillstandszeiten werden erzwungen, damit er nicht taktet.
3. **Global — ein einzelner sicherer Taupunktwert (°C).** Berechnet als
   `max über gekühlte Räume( dew_point(T_room, humidity) ) + 2 K`. Er wird als eine Sensor-Entität
   bereitgestellt. **Sie führen diesen Wert Ihrer Wärmepumpe als untere Grenze des Kühlvorlaufs
   zu**, damit die Pumpe nie Wasser sendet, das kälter ist, als es irgendein Raum ohne Kondensation
   vertragen kann. Er meldet `unknown`, wenn kein Raum kühlt oder keine Luftfeuchtigkeit verfügbar
   ist — das Seitenleisten-Panel erklärt zusätzlich je Raum, *warum* kein Wert erzeugt wird.

   **Der `unknown`-Vertrag — der Verbraucher muss ausfallsicher sein.** `unknown` bedeutet „gerade
   kommt kein Raum für die Berechnung infrage“; es bedeutet **nicht** „kein Kondensationsrisiko“.
   Jede Automatisierung, die diesen Sensor in die Wärmepumpe leitet, muss `unknown` konservativ
   behandeln: eine feste sichere untere Grenze halten (z. B. 18–19 °C) oder die Flächenkühlung
   stoppen — niemals „keine Grenze“. Eine Referenzautomatisierung (generische Entitätsnamen):

   ```yaml
   alias: "UFH-Kühlung: sichere untere Vorlaufgrenze"
   triggers:
     - trigger: state
       entity_id: sensor.tortoise_ufh_global_safe_dew_point
   actions:
     - choose:
         - conditions:
             - condition: template
               value_template: >-
                 {{ states('sensor.tortoise_ufh_global_safe_dew_point')
                    not in ['unknown', 'unavailable'] }}
           sequence:
             - action: number.set_value
               target:
                 entity_id: number.heat_pump_cool_supply_min
               data:
                 value: "{{ states('sensor.tortoise_ufh_global_safe_dew_point') }}"
       default:
         # Ausfallsicher: kein infrage kommender Raum -> konservative feste Grenze.
         - action: number.set_value
           target:
             entity_id: number.heat_pump_cool_supply_min
           data:
             value: 19
   ```

Neben den Befehlen gibt jeder Raum einen umfangreichen **Bericht** aus („ein Fenster in die
Blackbox“): Regelabweichung, gemessener Trend (°C/h), die einzelnen PID-/Trend-/Vorsteuerungsglieder,
den Ventil-Rohwert vor Begrenzungen, Sättigungs- und Taupunkt-Drossel-Meldungen sowie eine kurze,
für Menschen und KI lesbare Erläuterung, was er getan hat und warum.

---

## Funktionen

- **PI-+-Trend-Regelung je Raum** — ein unabhängiger Regler pro Raum; Anti-Windup per Rückrechnung;
  ein Trenddämpfungsglied, das die Trägheit der Platte zähmt und Überschwingen verhindert.
- **Koordination langsam + schnell** — die Fußbodenheizung ist stets primär; der Split *fügt* nur
  Boost oberhalb eines konfigurierbaren Offsets hinzu und gibt innerhalb des Komfortbands frei. Er
  reduziert nie das Ventil (keine Prioritätsumkehr) und taktet nie (Mindest-Ein-/Ausschalt-Timer).
- **Heizen und Flächenkühlung** — mit Magnus-Taupunktberechnung je Raum und einer abgestuften
  Ventildrosselung Vorlauf-gegen-Taupunkt sowie der globalen sicheren Taupunktgrenze für die Pumpe.
- **Sichere Degradierung** — ein fehlender Raumtemperatursensor hält beim Heizen die letzte gesunde
  Ventilstellung (und parkt das Ventil beim Kühlen auf 0, wo ein eingefroren offenes Ventil beide
  Kondensationsabwehren umgehen würde), schaltet die Zusatzquelle aus und meldet den Raum im Bericht,
  statt auszufallen. Eingaben werden vor dem Regler auf Plausibilität geprüft (Bereich, Änderungsrate,
  Zustandsalter).
- **Integrator-Einfrieren** — wenn die Wärmepumpe für die Fußbodenheizung nicht verfügbar ist (z. B.
  Abtauung), wird der Integralanteil eingefroren, damit er nicht gegen einen toten Aktor aufläuft.
- **Optionale Wettervorsteuerung** — ein moderates Grundventilglied aus der Außentemperatur; den Rest
  erledigt der PI-Regelkreis.
- **Optionale Wärmepumpen-Anbindung & Ruhezeiten** — eine Opt-in-Anbindung kann den Modus der
  Wärmepumpe und die Kühl-/Heizwasser-Sollwerte steuern (stets unter Wahrung des
  Warmwasser-Vorrangs), und jeder Raum kann seine Zusatzquelle auf ein erlaubtes Zeitfenster
  beschränken.
- **Regelungszustand je Raum (Aus / Aktiv)** — ein Zwei-Zustands-Select pro Raum:
  *Aus* nimmt ihn von der Regelung aus (der Kern lässt ihn ruhen und es wird nie etwas geschrieben),
  *Aktiv* steuert seine Hardware. Ein „Hände weg“ für das ganze Haus ist einfach jeder Raum auf
  Aus (siehe unten).
- **Seitenleisten-Panel** — ein abhängigkeitsfreies Home-Assistant-Panel mit sechs Reitern (Räume,
  Meldungen, Abstimmung, Ventile, Zusatzquelle, Wärmepumpe): eine Live-Tabelle je Raum
  (Regelungszustand, gemessene Temperatur, Sollwert, Regelabweichung, Ventil %, Vorlauf-/
  Rücklaufwasser, Modus), ein Meldungs-Annunciator, die Reglerabstimmung (globale Verstärkungen plus
  vereinzelte Übersteuerungen je Raum), die optionale Wärmepumpen-Anbindung und der vollständige
  Bericht jedes Raums.
- **Hardwareunabhängig** — Sie ordnen bei der Einrichtung Home-Assistant-Entitäten den Rollen zu;
  Einheiten werden geprüft (°C, %, W), Marken nicht.
- **Eingebauter Gebäudesimulator** — ein digitaler Zwilling (3R3C-RC-Modell je Raum, ZOH über
  Matrixexponential), der denselben Reglercode wie Home Assistant antreibt, sodass das Verhalten in
  Tests dem Verhalten im Produktivbetrieb entspricht.

---

## Installation

### Über HACS (benutzerdefiniertes Repository)

1. Öffnen Sie in Home Assistant **HACS**.
2. Öffnen Sie das Drei-Punkte-Menü und wählen Sie **Benutzerdefinierte Repositories**.
3. Fügen Sie `https://github.com/hubertciebiada/tortoise-ufh` mit der Kategorie **Integration** hinzu.
4. Suchen Sie **Tortoise-UFH** und installieren Sie es.
5. **Starten Sie Home Assistant neu.**
6. Gehen Sie zu **Einstellungen → Geräte & Dienste → Integration hinzufügen** und suchen Sie
   **Tortoise-UFH**.

### Manuelle Installation

1. Laden Sie die neueste Version von GitHub herunter.
2. Kopieren Sie `custom_components/tortoise_ufh/` in Ihr Home-Assistant-Verzeichnis
   `config/custom_components/`.
3. Starten Sie Home Assistant neu und fügen Sie die Integration über die Benutzeroberfläche hinzu.

Erfordert Home Assistant 2024.1.0 oder neuer und Python 3.12+. Die Kern-Laufzeitabhängigkeiten
(`numpy`, `scipy`) werden von Home Assistant automatisch installiert.

### Hardware-Empfehlungen für die Flächenkühlung

Beide Software-Kondensationsschichten (der globale sichere Taupunkt und die Drosselung je Raum)
vertrauen letztlich Ihren **Luftfeuchtigkeitssensoren** — ein veralteter oder driftender RH-Messwert
ist ihr gemeinsamer Ausfallmodus. Bevor Sie die Flächenkühlung aktivieren:

- **Montieren Sie einen Hardware-Kondensationssensor (Rohr-Taupunkt) am Verteiler** — ein günstiger
  Öffner-Sensor, der am kältesten Vorlaufrohr befestigt und so verdrahtet ist, dass er die
  Kühlzirkulation direkt stoppt. Er ist eine unabhängige dritte Schutzschicht, die selbst dann wirkt,
  wenn jede Softwareschicht mit falschen Daten gespeist wird.
- **Dämmen Sie den Verteiler und alle freiliegenden Kaltwasserleitungen.** Teile mit geringer
  thermischer Masse (Verteilerbalken, Fittings) erreichen in Minuten die Wassertemperatur und sind
  der erste Ort, an dem sich bei einem Feuchtigkeitsanstieg Tau bildet — lange bevor die Platte
  gefährdet ist.
- **Montieren Sie die Vorlaufsonden der Heizkreise am Verteilerbalken** (vor den Heizkreisventilen),
  nicht dahinter: Eine Sonde stromabwärts eines geschlossenen Ventils misst stehendes,
  raumerwärmtes Wasser, was die Taupunkt-Drosselung gegen ihre eigene Messung auf/zu pendeln lassen
  kann.
- Geben Sie jedem gekühlten Raum einen echten Luftfeuchtigkeitssensor. Ein Raum ohne RH wird
  **blind** gekühlt, und der Regler weigert sich konservativ, sein Ventil zu öffnen (Drosselung 0).
- **Prüfen Sie die Meldefrequenz Ihrer RH-Sensoren** vor der Kühlsaison. Schwellenmeldende Sensoren
  (z. B. eine CO₂/RH-Kombi über Matter) können in einem stabilen Raum legitim mehrere zehn Minuten
  stumm bleiben. Der Regler toleriert dies mit einem zweistufigen Alters-Gate: Ein Messwert ist bis
  60 min frisch; zwischen 60 und 120 min wird der letzte Wert noch mit einer zusätzlichen
  **+1-K-Taupunkt-Marge** verwendet (Berichtsmeldung `rh_stale_gated`); nach 120 min stoppt die
  Kühlung des Raums konservativ. Wenn Sie `rh_stale_gated` regelmäßig sehen, verkürzen Sie das
  maximale Melde-Intervall des Sensors.

### Alarmierung bei degradierten Räumen

Die Gebäude-Nutzlast trägt `sensor_lost_rooms` — die Anzahl der Räume, die aktuell im degradierten
Zustand `sensor_lost` laufen (im Panel und über den Websocket `tortoise_ufh/get_live` sichtbar,
bewusst keine weitere Entität). Für Alarmierung je Raum beobachten Sie die `flags` im Bericht jedes
Raums (Websocket/Panel) oder alarmieren Sie einfach darauf, dass Ihre Quell-Temperatursensoren
`unavailable`/veraltet werden — der Regler degradiert einen Raum, sobald seine Temperatur nicht mehr
vertrauenswürdig ist (außerhalb des Bereichs, springend oder älter als ~45 min).

---

## Konfiguration

Der Einrichtungsassistent (Entity-/Number-/Select-/Boolean-Selektoren, hardwareunabhängige
Einheitenprüfung) fragt zuerst nach dem Gebäudestandort (verwendet für Wetterkompensation und
Sonneneinstrahlungs-Vorsteuerung) und ordnet dann Ihre Entitäten den Rollen zu: je Raum einen
Temperatursensor, einen oder mehrere Ventilantriebe (`number`- oder `valve`-Entitäten), optionale
Vorlauf-/Rücklaufwassersensoren, optionale Luftfeuchtigkeit (für gekühlte Räume erforderlich) und
eine optionale Split-`climate`-Entität; global einen Außentemperatursensor.

**Multisplit-Besitzer:** Wenn die Inneneinheiten mehrerer Räume ein physisches Außengerät teilen,
geben Sie ihnen im Raumschritt dasselbe Label **Gruppe des gemeinsamen Außengeräts** (ein beliebiger
Name, z. B. `outdoor_unit_a`). Der Regler bittet dann nie ein Aggregat, gleichzeitig zu heizen und zu
kühlen: Wenn die Anforderungen kollidieren (Routine in der Übergangssaison), gewinnt der Raum, der am
weitesten außerhalb seines Komfortbands liegt, die Richtung, eine Einheit innerhalb ihrer
Mindest-EIN-Zeit behält sie, und der verlierende Raum wartet seine Mindest-AUS-Zeit ab, wobei die
Meldung `fast_source_group_conflict` in seinem Bericht sichtbar ist. Lassen Sie das Feld für
unabhängige Einheiten leer.

Die tägliche Steuerung findet im **Tortoise-UFH-Seitenleisten-Panel** statt (automatisch
hinzugefügt, nur für Administratoren). Vom Panel aus können Sie:

- Die **globale Haustemperatur** einstellen und den **Modus** wählen (Heizung / Übergang /
  Kühlung / Aus).
- Einen **Offset je Raum** anpassen — der Sollwert jedes Raums ist `Haustemperatur + Raum-Offset`.
- Den **Regelungszustand** jedes Raums einstellen (Aus / Aktiv). Die Teilnahme eines Raums an der
  Kühlung wird in den Integrationsoptionen konfiguriert, nicht im Panel.
- Den **PI-+-Trend-Regler** vom Reiter Abstimmung aus abstimmen — globale Verstärkungen mit
  vereinzelten Übersteuerungen je Raum.
- Den **Live-Bericht** jedes Raums öffnen, um die vollständige Aufschlüsselung der Entscheidung zu
  lesen.

Die globale Haustemperatur und die Offsets je Raum sind auch als beschreibbare `number`-Entitäten
verfügbar, der globale Modus als beschreibbares `select` (`home_mode` — die einzige Quelle der
Wahrheit für den Modus; aus einem eigenen Helfer heraus über eine Automatisierung mit
`select.select_option` steuerbar), und dieselben Aktionen stehen als Dienste
`set_home_temperature`, `set_room_offset` und `set_mode` sowie über die Panel-WebSocket-API zur
Verfügung. Kein YAML-Editieren erforderlich. Eine
Lovelace-Vorlage `dashboard_tortoise_ufh.yaml` wird als Rückfalloption mitgeliefert.

### Geräte und Entitätsbenennung

Seit v0.5.0 ist jeder Raum ein **Gerät** in Home Assistant (Modell „Room zone“, verknüpft mit einem
Hub-Gerät „UFH controller“ je Eintrag, das die globalen Entitäten trägt), sodass Räume HA-**Bereichen**
zugewiesen und auf ihrer Geräteseite durchsucht werden können. Entitäten werden über das
Übersetzungssystem von Home Assistant benannt (`has_entity_name` + `translation_key`), sodass die
Entitätsnamen Ihrer HA-Sprache folgen (Englisch, Polnisch und Deutsch werden mit der Integration
ausgeliefert).

Aktualisierte Installationen behalten ihre bestehenden Entitäts-IDs — Entitäten werden über ihre
unveränderte `unique_id` in der Registry zugeordnet. **Neue** Installationen leiten Entitäts-IDs aus
der Geräte- + Übersetzte-Namen-Konvention ab, sodass einige IDs von Installationen vor 0.5.0
abweichen (z. B. `sensor.salon_dew_point` statt `sensor.salon_room_dew_point`); lösen Sie Entitäten
über die UI-Auswahl auf, statt IDs von vor 0.5.0 fest zu codieren.

---

## Regelungszustand: Aus / Aktiv (je Raum)

Ob Tortoise-UFH die Aktoren eines Raums berührt, wird durch den **Regelungszustand** dieses Raums
gesteuert, der als `select`-Entität je Raum (`select.<room>_control_state`), im Panel und über den
WebSocket-Befehl `tortoise_ufh/set_room_state` bereitgestellt wird. Ein neuer Raum startet im sicheren
Standard **Aus** — es wird nichts geschrieben, bis Sie ihn bewusst auf **Aktiv** schalten.

- **`off` (Aus).** Der Raum ist von der Regelung ausgenommen — der Kern wird mit `Mode.OFF` gespeist,
  sodass sein Bericht Ventil 0 % und die Zusatzquelle im Leerlauf zeigt, und **es wird nichts
  geschrieben**: Die physischen Aktoren bleiben genau so, wie Sie sie verlassen haben. Das Umschalten
  eines aktiven Raums auf Aus sendet zuerst einen einmaligen Abschied (Split AUS; das Ventil wird bei
  der Kühlung auf 0 gefahren und beim Heizen in Haltestellung belassen).
- **`live` (Aktiv).** Der Koordinator schreibt die Ventil-Entitäten des Raums (nur wenn der neue Wert
  um mindestens die Schreibschwelle vom letzten abweicht, um Aktor-Klappern zu vermeiden) sowie Modus
  und Zieltemperatur des Splits.

> **Manuelle Übersteuerungen & das 45-Minuten-Neuerzwingen.** Im Zustand `live` ist Tortoise-UFH der
> EIGENTÜMER des Split des Raums: Ein unveränderter Befehl wird nicht in jedem Zyklus erneut gesendet
> (kein Piepen, Ihre Ventilator- und Lamellen-Anpassungen überstehen es), aber das Paar Modus + Ziel
> wird **etwa alle 45 Minuten neu erzwungen**, sodass eine manuelle Änderung von Modus oder Ziel des
> Splits per Fernbedienung innerhalb dieses Fensters überschrieben wird (der Bericht meldet in der
> Zwischenzeit `fast_source_mismatch`). Wenn Sie die Hardware eines Raums eine Weile von Hand steuern
> möchten, **schalten Sie den Raum auf `off`** — das ist der unterstützte „manuelle Modus“: Der
> Regler schreibt nichts, und der Weg zurück nach `live` parkt die Aktoren sicher neu (der Split
> absolviert eine ehrliche Mindest-AUS-Zeit vor jedem Neustart).

Ein „Hände weg von der Hardware“ für das ganze Haus ist einfach **jeder Raum auf Aus** — der
Regelungszustand je Raum ersetzte den früheren globalen Kill-Switch. Eine Änderung des
Regelungszustands eines Raums wirkt sofort und **setzt den PID-Integrator nicht zurück** (nur
Abstimmungsänderungen laden den Regler neu).

Kurz gesagt: **ein Raum auf Aus → nur Bericht, keine Befehle; nur aktive Räume steuern die
Hardware.**

> **v0.7.0 entfernte den dritten Zustand `shadow`** (im echten Modus rechnen, aber nichts schreiben —
> eine Probelauf-Hilfe für die Einführung). Der Config-Eintrag migriert automatisch (v2 → v3): Räume
> im Zustand `shadow` werden zu `off`. Beachten Sie die bewusste Änderung der Berichterstattung: Ein
> Shadow-Raum zeigte früher „was er tun würde“ in seinen Sensoren und seiner Panel-Zeile; ein Raum im
> Zustand `off` meldet stattdessen den Leerlauf (Ventil 0 %, Zusatzquelle aus) — das ist das
> beabsichtigte Verhalten, keine defekten Sensoren. Siehe `docs/DECISIONS.md` §13.

> Aktualisieren Sie von einer älteren Installation? Der Config-Eintrag migriert automatisch (v1 → v3
> in einem Schritt): Das alte `participates`-Flag, die `live_control`-Schalter je Raum und der globale
> Kill-Switch werden in den Regelungszustand je Raum eingefaltet (`participates == false` wird zu
> `off`; ein Raum, der nicht `live` war, landet auf `off`). Die Entitäten
> `switch.tortoise_ufh_kill_switch` und `switch.*_live_control` werden entfernt und durch
> `select.*_control_state` ersetzt. v0.5.0 zieht zusätzlich das redundante
> `binary_sensor.*_live_control` zurück (es spiegelte lediglich `control_state == "live"` wider;
> verwaiste Registry-Einträge werden bei der Einrichtung bereinigt) und entfernt das Übergangsfeld
> `live_control_enabled` aus der Nutzlast des Websockets `tortoise_ufh/get_live` — lesen Sie
> stattdessen `control_state`.

---

## Für Entwickler

Die Integration ist aufgeteilt in einen reinen Regelkern — innerhalb der Integration mitgeliefert,
sodass eine HACS-Installation eigenständig ist — und einen dünnen Home-Assistant-Adapter darum herum:

```
tortoise-ufh/
└── custom_components/tortoise_ufh/  # HA-Adapter — importiert VOM Kern über .core
    ├── coordinator.py               # 5-Min-DataUpdateCoordinator: lesen → Kern ausführen → schreiben
    ├── config_flow.py  panel.py  websocket.py  sensor.py  number.py  select.py ...
    ├── frontend/tortoise-ufh-panel.js
    └── core/                        # reiner Kern — importiert nie homeassistant
        ├── models.py                # I/O-Dataclasses + RoomReport (der Blackbox-Vertrag)
        ├── config.py                # RoomConfig / BuildingConfig / ControllerConfig
        ├── pid.py                   # PIDController (PI + Anti-Windup)
        ├── controller.py            # RoomController + BuildingController
        ├── dew_point.py             # Magnus-Taupunkt + Kühldrosselung
        ├── rc_model.py              # 3R3C-RC-Modell (ZOH über scipy.linalg.expm)
        ├── simulator.py             # BuildingSimulator digitaler Zwilling
        └── ...                      # Wetter, Metriken, Szenarien, Profile, Sicherheit
```

Die eine harte Regel: **`custom_components/tortoise_ufh/core/` darf nie `homeassistant`
importieren.** Der Kern hängt nur von `numpy` und `scipy` ab (plus der Standardbibliothek), liefert
`py.typed` und ist ohne Home Assistant vollständig testbar.

### Setup

```bash
git clone https://github.com/hubertciebiada/tortoise-ufh.git
cd tortoise-ufh
pip install -e ".[dev]"
```

### Tests

Die Suite verwendet drei pytest-Marker: `unit` (schnell, isoliert), `simulation` (szenariobasiert,
End-to-End durch den digitalen Zwilling) und `slow`. Kerntests benötigen nur `numpy`, `scipy` und
`pytest` — keine Home-Assistant-Installation.

```bash
# Schnelle Unit-Tests
python -m pytest -m unit

# Simulationsszenarien
python -m pytest -m simulation

# Alles
python -m pytest
```

### Codequalität

```bash
ruff check .
ruff format --check .
mypy custom_components/tortoise_ufh/core
```

Der Kern wird `mypy --strict`-sauber gehalten; ruff läuft mit `line-length = 88` und den Regelsätzen
`E, F, I, UP, B, SIM`. Jeder Wert-, Config- und Ergebnistyp ist eine eingefrorene Dataclass, die sich
in `__post_init__` selbst validiert.

---

## Lizenz

Tortoise-UFH wird unter der [MIT-Lizenz](LICENSE) veröffentlicht.
