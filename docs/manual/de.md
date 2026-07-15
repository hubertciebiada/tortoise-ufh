# Tortoise-UFH — Benutzerhandbuch

> **Sprache / Język / Language:** [Polski](pl.md) · **Deutsch** · [English (README)](../../README.md)

> Bezieht sich auf Version **0.12.0**. Dieses Handbuch beschreibt die Integration
> auf Deutsch; die Oberfläche (Panel, Entitäten, Konfigurationsassistent, Dienste)
> ist auf Deutsch, Polnisch und Englisch verfügbar und folgt der in Ihrem
> Home-Assistant-Profil eingestellten Sprache.

## Inhaltsverzeichnis

1. [Was ist Tortoise-UFH](#1-was-ist-tortoise-ufh)
2. [Wie es funktioniert — Begriffe verständlich erklärt](#2-wie-es-funktioniert--begriffe-verständlich-erklärt)
3. [Installation (HACS)](#3-installation-hacs)
4. [Konfiguration Schritt für Schritt](#4-konfiguration-schritt-für-schritt)
5. [Seitenleisten-Panel](#5-seitenleisten-panel)
6. [Entitäten und Dienste](#6-entitäten-und-dienste)
7. [Regelungszustand: Aus / Aktiv](#7-regelungszustand-aus--aktiv)
8. [Abstimmungsparameter — vollständige Liste](#8-abstimmungsparameter--vollständige-liste)
9. [Flächenkühlung und Kondensation](#9-flächenkühlung-und-kondensation)
10. [Zusatzquelle und Multisplit](#10-zusatzquelle-und-multisplit)
11. [Wärmepumpe (optionale Anbindung)](#11-wärmepumpe-optionale-anbindung)
12. [Meldungen und Alarme — Verzeichnis](#12-meldungen-und-alarme--verzeichnis)
13. [Fehlerbehebung](#13-fehlerbehebung)
14. [Einschränkungen, FAQ und Versionsverlauf](#14-einschränkungen-faq-und-versionsverlauf)

---

## 1. Was ist Tortoise-UFH

Tortoise-UFH ist ein Klimaregler **je Raum** für ein Haus mit **träger
Fußbodenheizung** (Heizen **und Kühlen** über den Boden), mit optionaler
Unterstützung durch eine Zusatzquelle (Split-Klimagerät oder Heizgerät). Ein
gewöhnlicher Thermostat schaltet den Kreis ein und aus, sobald die Temperatur eine
Schwelle überschreitet — bei einem Boden, der mit mehreren Stunden Verzögerung
reagiert, endet das in einem Pendeln: mal zu kalt, mal zu warm. Tortoise-UFH stellt
stattdessen **stufenlos die Ventilöffnung (0–100 %)** anhand der Regelabweichung,
ihres Verlaufs und der Änderungsrate ein, sodass der träge Boden den Sollwert ohne
Überschwingen erreicht. Jeder Raum hat seinen eigenen, unabhängigen Regler, seinen
eigenen Temperatur-Offset und seinen eigenen Bericht „was ich getan habe und warum“.

## 2. Wie es funktioniert — Begriffe verständlich erklärt

**Regelabweichung** ist die Differenz: Sollwert minus gemessen. Positiv = zu kalt.

**PI-+-Trend-Regler** setzt die Ventilöffnung aus drei Gliedern zusammen:

- **P (proportional)** — reagiert auf die aktuelle Regelabweichung: je kälter, desto
  weiter öffnet es.
- **I (integral)** — betrachtet die über die Zeit anhaltende Regelabweichung. Wenn es
  stundenlang ein wenig zu kalt ist, fügt das I-Glied langsam Öffnung hinzu, bis die
  Unterversorgung verschwindet. Dieses „Gedächtnis“ nennen wir **Integral**
  (Integrator); es wird bisweilen bewusst **eingefroren** (z. B. wenn die Wärmepumpe
  Warmwasser bereitet), damit es nicht fälschlich aufläuft.
- **Trend (Dämpfung)** — betrachtet, wie schnell sich die Temperatur ändert. Wenn
  sich der Raum dem Sollwert schnell nähert, schließt es das Ventil **frühzeitig**
  etwas — das ist die wichtigste Bremse gegen das Überschwingen des trägen Bodens.

Hinzu kommt die optionale **Wettervorsteuerung (FF)** — eine Grundöffnung abhängig
von der Außentemperatur — sowie Begrenzungen: Mindestöffnung beim Heizen, Begrenzung
auf 0–100 % und Sicherheitsregeln.

**Globale Modi** (einer für das ganze Haus): **Heizung**, **Kühlung**, **Übergang**
(Ventile geparkt, der Split darf heizen oder kühlen — Frühling/Herbst) und **Aus**.
Der **Raumzustand** ist etwas anderes: je Raum wählen Sie **Aus** oder **Aktiv** —
siehe §7.

**Taupunkt** ist die Temperatur, unter der der Wasserdampf aus der Luft an einer
kalten Oberfläche kondensiert. Bei der Kühlung über den Boden ist das das
Hauptrisiko (nasser Boden), daher wirken zwei unabhängige Schutzmechanismen — siehe
§9.

**Split-Unterstützung**: Sobald die Regelabweichung die Zuschaltschwelle
überschreitet, schaltet die Integration das Klimagerät mit dem Befehl „ein + Richtung
+ Sollwert“ ein — der Split regelt sich danach selbst. Der Boden bleibt stets die
Basisquelle. Timer für Mindestlaufzeit und -stillstand (**Dwell**) schützen den
Kompressor vor Takten. Räume an einem gemeinsamen Außengerät (**Multisplit**) müssen
gemeinsam heizen oder kühlen — siehe §10.

**Meldungen** sind kurze Diagnosecodes im Raumbericht (z. B. `sensor_lost`), die
angeben, welcher Schutz oder welche Begrenzung gewirkt hat — Verzeichnis in §12.

## 3. Installation (HACS)

1. Öffnen Sie in Home Assistant **HACS → Menü (drei Punkte) → Benutzerdefinierte
   Repositories**.
2. Fügen Sie das Repository `https://github.com/hubertciebiada/tortoise-ufh`
   mit der Kategorie **Integration** hinzu.
3. Suchen Sie **Tortoise-UFH** und installieren Sie es.
4. **Starten Sie Home Assistant neu.**
5. Gehen Sie zu **Einstellungen → Geräte & Dienste → Integration hinzufügen** und
   wählen Sie **Tortoise-UFH**.

Voraussetzungen: Home Assistant **2024.1** oder neuer (Python 3.12+). Die
Bibliotheken `numpy` und `scipy` (aus dem Manifest der Integration) installiert Home
Assistant selbst. Nach der Konfiguration erscheint im Seitenmenü der Eintrag
**Tortoise-UFH** (das Panel ist für Administratoren sichtbar).

## 4. Konfiguration Schritt für Schritt

Der Assistent führt durch fünf Schritte:

1. **Standort** — Breiten- und Längengrad des Gebäudes (Wetterkompensation und
   Sonneneinstrahlung).
2. **Räume** — je Raum: Name, Bodenfläche (m²), ob eine Zusatzquelle vorhanden ist
   (Split / Heizgerät), optionale **Gruppe des gemeinsamen Außengeräts** (Multisplit,
   §10), optionale **erlaubte Zeiten der Zusatzquelle** (Ruhezeiten, §10 — beide
   Felder zusammen oder keines; gilt nur für Räume mit Zusatzquelle) sowie die
   Teilnahme an der Flächenkühlung. Markieren Sie „Weiteren Raum hinzufügen“, um den
   nächsten Raum einzugeben.
3. **Entitätszuordnung** — je Raum: Temperatursensor, Luftfeuchtigkeitssensor
   (**erforderlich**, wenn der Raum an der Kühlung teilnimmt), Ventilantriebe
   (`number`- oder `valve`-Entitäten; ein Raum kann mehrere Heizkreise haben — ein
   Antrieb pro Heizkreis), Vorlauf- und Rücklauftemperatursensoren (einer pro
   Heizkreis), `climate`-Entität der Zusatzquelle. Beim ersten Raum zusätzlich
   globale Entitäten: Außentemperatursensor und Modus-Entität (select /
   input_select).
4. **Algorithmus-Parameter** — Haus-Sollwert und Reglerparameter (Sie können die
   Standardwerte belassen; alles lässt sich später im Panel ändern — §8).
5. **Bestätigung** — Zusammenfassung. Räume starten im Zustand **Aus**: Es wird
   nichts an die Hardware geschrieben, bis Sie einen Raum auf **Aktiv** schalten.

Typische Validierungsfehler: `Es muss mindestens ein Ventilantrieb ausgewählt werden`
(Raum ohne Ventil), `Für Räume, die an der Flächenkühlung teilnehmen, ist ein
Luftfeuchtigkeitssensor erforderlich` (gekühlter Raum ohne Luftfeuchtigkeit), `Die
Entität hat eine falsche Maßeinheit` (die Integration prüft ausschließlich Einheiten:
°C, %, W — keine Marken), `Ein Raum mit diesem Namen existiert bereits` (auch Namen,
die sich nur in Groß-/Kleinschreibung/Leerzeichen unterscheiden — sie würden in der
Registry kollidieren), `Diese valve-Entität unterstützt das Setzen einer Position
nicht` (wählen Sie ein Ventil mit SET_POSITION oder eine `number`-Entität), `Legen
Sie beide Fensterzeiten fest…` (Ruhezeiten erfordern ein Paar Anfang/Ende,
voneinander verschieden — §10).

Nach dem Erstellen des Eintrags lässt sich alles in den **Integrationsoptionen**
ändern (Einstellungen → Geräte & Dienste → Tortoise-UFH → Konfigurieren): Räume
hinzufügen/bearbeiten/entfernen, Regelungs- und Abstimmungseinstellungen sowie der
optionale Abschnitt **Wärmepumpe** (§11).

## 5. Seitenleisten-Panel

Oben im Panel der **Hero-Bereich** — seit v0.10.3 kompakt und in **einer Zeile** (auf
schmalen Bildschirmen bricht er sauber um): Algorithmus-Status (Läuft / Veraltet /
Fehler) mit dem Alter des letzten Zyklus, der Zähler **Aktiv x/y** (wie viele Räume in
die Hardware schreiben), die Anzahl aktiver **Meldungen** (Farbe nach der
schwerwiegendsten; **ein Klick führt zum Reiter Meldungen** mit der vollständigen
Aufschlüsselung), beim Kühlen der **sichere Taupunkt** (§9), der Stepper der
**Haustemperatur** und der **Modus**-Schalter.

Das Umschalten des Raumzustands (Aus↔Aktiv) und des Hausmodus erfordert seit v0.8.0
eine **leichte Bestätigung**: Beim angeklickten Knopf erscheint eine Sprechblase
„Sind Sie sicher?“ mit den Schaltflächen **Ja, ändern / Abbrechen** (Escape oder ein
Klick daneben = abbrechen) — diese Schalter waren zu leicht versehentlich zu treffen.

Reiter:

- **Räume** — Tabelle: Umschalter des Zustands
  **Aus/Aktiv** (erste Spalte),
  Messwert mit Trend, Sollwert (mit Offset), Regelabweichung, Ventil %,
  Vorlauf/Rücklauf des ersten Heizkreises sowie — unter der gemeinsamen Überschrift
  **Zusatzquelle** — Modus und Sollwert des Splits/Heizgeräts („—“ = Raum ohne
  Zusatzquelle, „aus“ = Zusatzquelle ruht; das „i“-Symbol bei „Temp.“ erklärt die
  bewusste ±1 K — §10). Ein Klick auf die Zeile öffnet die **Raumdetails**: Kacheln
  (Messwert, Sollwert mit Offset-Stepper, Ventil, Zusatzquelle — mit der Zeile
  „erlaubt HH:MM–HH:MM“, wenn der Raum Ruhezeiten hat), den Abschnitt
  **Reglerentscheidung** (Regelabweichung, Taupunkt, Anteile der Glieder
  P/I/Trend/FF, Roh- und Endwert des Ventils, Kühldrosselung, Integrator, Sättigung,
  Mindestöffnung, verbale Erläuterung der Entscheidung), **Verlaufs**-Diagramme (6 h /
  24 h / 7 Tage), den Abschnitt **Sensoren und Signale** (im Vordergrund der AKTUELLE
  WERT jedes angeschlossenen Signals; die Quellentität zeigt das „i“-Symbol, ein Klick
  auf die Zeile öffnet den nativen Entitätsdialog), den standardmäßig eingeklappten
  Abschnitt **Diagnose-Entitäten** und die **Rohdaten** des Berichts.
- **Meldungen** — der **Meldungs-Annunciator** (seit v0.10.3 ein eigener Reiter;
  früher oben in Räume): eine einklappbare, gruppierte **Liste** (Stil wie
  „Abstimmung“) aller bekannten Meldungen in den Abschnitten **Sicherheit (S1–S6) /
  Zusatzquelle / Konfiguration** — Zeilen **abgeblendet**, wenn inaktiv, und
  **leuchtend** (linker Farbbalken der Schwere + Status „aktiv ×N Räume“, Raumliste in
  der Sprechblase), wenn sie auslösen; Sicherheitsregeln tragen den Code **S1–S6** auf
  der Plakette, und unter dem **„i“**-Symbol bei jeder Meldung steht die vollständige
  Erklärung (§12). Die Kopfzeile klappt die Liste ein/aus und zeigt selbst
  eingeklappt eine Zusammenfassungsplakette.
- **Abstimmung** — globale Einstellungen + vereinzelte Übersteuerungen je Raum (§8),
  seit v0.8.0 als **Liste von Parametern in thematischen Gruppen** (PI-Regler und
  Trend / Komfortband und Ventil / Wetterglied / Zusatzquelle / Kondensationsschutz /
  Wärmepumpe — Wasser) mit vollständigen Namen. Die Auswahl des Bereichs (**Global**
  oder ein konkreter Raum) erfolgt über **Reiter** oben (seit v0.10.1 — früher Chips).
  Bei jedem Parameter ein **„i“**-Symbol mit Erläuterung (mit der Maus überfahren,
  klicken oder tippen). Die Gruppe „Wärmepumpe — Wasser“ ist nur im Bereich Global
  sichtbar (§8).
- **Ventile** — je Raum: Befehl, Rohwert, Minimum (Mindestöffnung), Sättigung,
  S2-Drosselung, Rückmeldung (vom Aktor gemeldete Position); ausklappbare Heizkreise
  mit Vorlauf/Rücklauf/ΔT. Auch die Spaltenköpfe haben „i“-Symbole.
- **Zusatzquelle** — je Raum mit Zusatzquelle: Art, Gruppe (Multisplit), Befehl,
  Ist-Zustand der `climate`-Entität, Dwell-Timer, **erlaubte Zeiten** (Ruhezeiten-
  Fenster oder „immer“ — §10), Meldungen, Link zur Entität und die Verknüpfung
  „abstimmen“.
- **Wärmepumpe** — Status der optionalen Anbindung an die Pumpe (§11): Tortoise-Modus
  vs. Pumpenmodus (synchron/abweichend), Schalter des WW-Flags, Wasser-Sollwerte mit
  ihren Bestandteilen und das Signal „Pumpe für Fußbodenheizung verfügbar“. Ohne
  Konfiguration wird eine Anleitung angezeigt, wo man sie aktiviert.

Schwierigere Felder haben ein **„i“ im Kreis** — beim Überfahren (Computer) oder
Tippen (Telefon) erscheint eine Sprechblase mit Erläuterung; Escape oder eine
Berührung daneben schließt sie.

## 6. Entitäten und Dienste

Globale Entitäten (Gerät „UFH controller“):

- `number.*_home_temperature` — Haustemperatur (5–30 °C, Schritt 0,5),
- `sensor.*_global_safe_dew_point` — globaler sicherer Taupunkt (§9),
- `sensor.*_algorithm_status`, `sensor.*_last_update`, `sensor.*_watchdog_status`.

Entitäten je Raum (jeder Raum ist ein Gerät, kann einem HA-Bereich zugewiesen
werden):

- `select.<raum>_control_state` — Regelungszustand **Aus/Aktiv** (§7),
- `number.<raum>_setpoint_offset` — Sollwert-Offset (−5…+5 K),
- Diagnosesensoren: empfohlene Ventilstellung, Regelabweichung, Trend, Taupunkt,
  Integralanteil, Trendanteil, Modus der Zusatzquelle, Erläuterung der Entscheidung,
- binäre: Sensor verloren, Ausgang gesättigt, Kondensationsschutz aktiv,
  Durchflussstörung (`flow_fault`, Watchdog S6 — §8/§12).

Dienste (Domäne `tortoise_ufh`):

```yaml
# Haustemperatur einstellen
service: tortoise_ufh.set_home_temperature
data:
  temperature: 21.5

# Raum-Offset einstellen (Raumname aus der Konfiguration)
service: tortoise_ufh.set_room_offset
data:
  room: Salon
  offset: -0.5

# Globalen Modus einstellen (heating / transitional / cooling / off)
service: tortoise_ufh.set_mode
data:
  mode: cooling
```

Eine fertige Lovelace-Dashboard-Vorlage finden Sie in der Datei
`custom_components/tortoise_ufh/dashboard_tortoise_ufh.yaml`.

## 7. Regelungszustand: Aus / Aktiv

Jeder Raum hat genau zwei Zustände (Entität `select.<raum>_control_state`, Panel,
WS-Befehl `tortoise_ufh/set_room_state`):

- **Aus (`off`)** — der Raum ist von der Regelung ausgenommen. Der Kern berechnet ihn
  im Ruhemodus (Bericht: Ventil 0 %, Zusatzquelle aus) und **schreibt nichts an die
  Hardware** — die physischen Aktoren und der Split bleiben, wie Sie sie verlassen
  haben. Beim Umschalten von „Aktiv“ auf „Aus“ sendet die Integration einen
  einmaligen **Abschiedsbefehl**: Split immer AUS; das Ventil wird bei der Kühlung auf
  0 geschlossen (ein verwaistes offenes Ventil würde kaltes Wasser ohne
  Kondensationsschutz durchlassen), beim Heizen bleibt es in der letzten Stellung
  (warmes Wasser ist durch die Kurve der Wärmepumpe begrenzt).
- **Aktiv (`live`)** — der Raum wird berechnet, berichtet **und** seine Befehle werden
  geschrieben: Ventile (nur wenn die Änderung die Schreibschwelle überschreitet — kein
  Klappern der Aktoren) sowie Modus und Temperatur des Splits.

Praktische Regeln:

- **Ein neuer Raum startet Aus** — bewusst: Die Integration berührt die Hardware
  nicht, bis Sie es selbst entscheiden.
- **Manueller Betrieb** = Raum auf Aus schalten und die Geräte direkt steuern. (Im
  Zustand Aktiv ist die Integration Eigentümerin des Splits und erzwingt etwa alle 45
  Minuten erneut ihren Befehl — eine manuelle Änderung per Fernbedienung wird
  überschrieben.)
- **Kompletter Stopp** = alle Räume Aus.
- Die Zustandsänderung wirkt sofort und **setzt den PID-Integrator nicht zurück** (ein
  Neuladen erfolgt nur nach einer Änderung der Abstimmung).

## 8. Abstimmungsparameter — vollständige Liste

Der Reiter **Abstimmung** zeigt die **globalen** Einstellungen; wenn Sie einen Raum
wählen (Reiter oben), übersteuern Sie einzelne Parameter nur für diesen — ein
übersteuerter Parameter erhält die Markierung „übersteuert“ und die Schaltfläche
**„Auf global zurücksetzen“**. Seit v0.8.0 sind die Parameter in **thematische
Gruppen** geordnet (eine Liste unter der anderen), und die Namen sind vollständig,
ohne Abkürzungen. Die Gruppe **„Wärmepumpe — Wasser“** ist ausschließlich global — die
Wasser-Sollwerte betreffen das gesamte Gebäude, daher hat eine Übersteuerung je Raum
keinen physikalischen Sinn und wird abgelehnt. **Hinweis: Das Speichern der Abstimmung
lädt den Regler neu — der PID-Integrator wird auf null gesetzt.**

<!-- WÄCHTER: Die Beschreibungen unten sind eine 1:1-Kopie der Panel-Tooltips
     (STR.tip_knob_* in frontend/tortoise-ufh-panel.js) — BEIDE Stellen zusammen ändern. -->

**PI-Regler und Trend**

| Parameter | Einheit | Standard | Was er bewirkt und wann man ihn ändert |
|---|---|---|---|
| **Proportionalverstärkung (P)** (`kp`) | %/K | 14 | Proportionalverstärkung: wie viel Prozent Ventilöffnung jede 1 K Regelabweichung (Sollwert − gemessen) hinzufügt. Höher reagiert schneller, riskiert aber Überschwingen des trägen Bodens mit hoher Masse. Standard 14; typisch 8–20. In kleinen Schritten ändern und einen ganzen Tag beobachten. |
| **Integralverstärkung (I)** (`ki`) | %/(K·s) | 0,0015 | Integralverstärkung: wie schnell der Regler Öffnung hinzufügt, während eine kleine Regelabweichung über Stunden anhält — beseitigt die bleibende Regelabweichung. Die Werte sind bewusst sehr klein (Sekunden im Nenner). Standard 0,0015. Zu hoch = langsame Temperaturschwingung um den Sollwert. |
| **Dämpfung des Temperaturtrends** (`kt`) | %/(K/h) | 12 | Trenddämpfung: beobachtet, wie schnell sich die Temperatur bewegt. Wenn sich der Raum dem Sollwert schnell nähert, schließt sie das Ventil frühzeitig etwas und zähmt das Überschwingen des trägen Bodens. Standard 12. Muss selten geändert werden. |

**Komfortband und Ventil**

| Parameter | Einheit | Standard | Was er bewirkt und wann man ihn ändert |
|---|---|---|---|
| **Totband um den Sollwert** (`deadband_c`) | K | 0,3 | Totband um den Sollwert (±): innerhalb davon jagt der Regler keine winzigen Abweichungen, was Ventilbewegungen und Takten reduziert. Standard 0,3 K; typisch 0,2–0,5 K. |
| **Minimale Ventilöffnung beim Heizen** (`valve_floor_pct`) | % | 15 | Minimale Ventilöffnung, solange der Raum Wärme anfordert, um einen sinnvollen Durchfluss im Heizkreis zu halten. Standard 15 %. 0 deaktiviert die Mindestöffnung. |

**Wetterglied (Ventilöffnung)**

| Parameter | Einheit | Standard | Was er bewirkt und wann man ihn ändert |
|---|---|---|---|
| **Wettervorsteuerung aktiviert** (`outdoor_ff_enabled`) | — | aus | Wettervorsteuerung: fügt eine Grundöffnung des Ventils basierend auf der Außentemperatur hinzu, bevor der Raum Zeit hat auszukühlen. Nützlich bei großen Glasflächen und Kälteeinbrüchen. Standardmäßig aus. |
| **Neutrale Außentemperatur der Vorsteuerung** (`ff_neutral_c`) | °C | 15 | Außentemperatur, bei der das Wetterglied null ist; darunter fügt jede 1 K Kälte Öffnung gemäß der FF-Verstärkung hinzu. Standard 15 °C. |
| **Verstärkung der Vorsteuerung** (`ff_gain_pct_per_k`) | %/K | 1 | Wie viel Prozent Öffnung das Wetterglied je 1 K unterhalb der neutralen Temperatur hinzufügt. Standard 1 %/K. |
| **Obergrenze der Vorsteuerung** (`ff_max_pct`) | % | 20 | Obergrenze für das Wetterglied — die Vorsteuerung fügt nie mehr als diesen Prozentsatz an Öffnung hinzu. Standard 20 %. |

**Zusatzquelle (Split/Heizgerät)**

| Parameter | Einheit | Standard | Was er bewirkt und wann man ihn ändert |
|---|---|---|---|
| **Zuschaltschwelle der Zusatzquelle** (`boost_offset_c`) | K | 1,0 | Zuschaltschwelle: sobald die Regelabweichung diesen Wert überschreitet, schaltet sich die Zusatzquelle (Split/Heizgerät) zu. Sie gibt innerhalb des Komfortbands wieder frei; der Boden bleibt stets die Basisquelle. Muss größer als das Totband sein. Standard 1,0 K. |
| **Mindestlaufzeit der Zusatzquelle** (`fast_min_on_minutes`) | min | 10 | Mindestlaufzeit der Zusatzquelle nach dem Einschalten. Schützt den Split-Kompressor vor Takten und beruhigt Richtungswechsel in einer Multisplit-Gruppe. Standard 10 min; Minimum 3. |
| **Mindeststillstandszeit der Zusatzquelle** (`fast_min_off_minutes`) | min | 10 | Mindeststillstandszeit der Zusatzquelle, bevor sie wieder starten darf. Standard 10 min; Minimum 3. |
| **Sollwert-Überhöhung der Zusatzquelle** (`fast_target_offset_k`) | K | 1,0 | Um wie viel Kelvin der an den Split gesendete Sollwert während des Nachheizens/Nachkühlens über den Raum-Sollwert hinaus angehoben wird (Heizung: Sollwert + Wert, Kühlung: Sollwert − Wert). Der deckenmontierte Sensor des Splits misst wärmer als der Raumsensor — ohne die Überhöhung drosselt sich der Split, bevor das Nachheizen geliefert wird; über das Abschalten entscheidet ohnehin der Raumsensor (§10). 0 = keine Überhöhung. Standard 1,0 K. |

**Kondensationsschutz (Kühlung)**

| Parameter | Einheit | Standard | Was er bewirkt und wann man ihn ändert |
|---|---|---|---|
| **Sicherheitsmarge über dem Taupunkt** (`dew_margin_k`) | K | 2 | Kühlung: solange der Vorlauf mindestens so viele K über dem Taupunkt liegt, läuft das Ventil uneingeschränkt; näher am Taupunkt wird der Durchfluss gedrosselt (Kondensationsschutz am Boden). Standard 2 K; erhöhen Sie ihn, falls Sie jemals Feuchtigkeit sehen. |
| **Rampenbreite der Kühldrosselung** (`dew_ramp_k`) | K | 2 | Breite der Drosselrampe unterhalb der Taupunkt-Marge: über diese Spanne fällt der Durchfluss linear von 100 % auf 0. Kleiner = schärferes Abschneiden. Standard 2 K. |

**Wärmepumpe — Wasser (nur global; wirkt ausschließlich mit konfigurierter Anbindung —
§11)**

| Parameter | Einheit | Standard | Was er bewirkt und wann man ihn ändert |
|---|---|---|---|
| **Basistemperatur Kühlwasser** (`cooling_supply_base_c`) | °C | 18 | Der an die Wärmepumpe geschriebene Kühlwasser-Sollwert, während das Haus kühlt: Der geschriebene Wert ist das Maximum aus dieser Basis und dem globalen sicheren Taupunkt — nie unter dem Taupunkt. Nur aktiv, wenn die Kühl-Sollwert-Entität konfiguriert ist (Optionen → Wärmepumpe). Globaler Parameter — kann nicht je Raum übersteuert werden. Standard 18 °C. |
| **Basistemperatur Heizwasser** (`heating_supply_base_c`) | °C | 26 | Heizungsvorlauf bei der neutralen Außentemperatur (Parameter „Neutrale Außentemperatur der Vorsteuerung“, Standard 15 °C). Darunter steigt der Sollwert entlang der Kurvensteigung; das Ergebnis wird auf 20–40 °C begrenzt. Nur an die Pumpe geschrieben, wenn die Heiz-Sollwert-Entität konfiguriert ist — standardmäßig folgt die Pumpe ihrer eigenen Kurve. Globaler Parameter. Standard 26 °C. |
| **Kurvensteigung Heizwasser** (`heating_supply_slope`) | K/K | 0,5 | Um wie viele Kelvin der Heizwasser-Sollwert je 1 K Außentemperatur unterhalb des Neutralpunkts steigt. Standard 0,5 K/K; typisch 0,3–0,8 für ein gut gedämmtes Fußbodenheizungshaus. |

**Durchfluss-Watchdog (S6; wirkt nur mit Vorlauf- und Rücklaufsensoren des
Heizkreises)**

| Parameter | Einheit | Standard | Was er bewirkt und wann man ihn ändert |
|---|---|---|---|
| **Minimale ΔT, die als Durchfluss zählt** (`flow_epsilon_k`) | K | 0,3 | Kleinste Vorlauf−Rücklauf-Differenz des Heizkreises, die der Watchdog als Nachweis dafür wertet, dass tatsächlich Wasser fließt. Darunter (und ohne Sondenbewegung zur Quelle hin) löst ein Öffnungsbefehl ohne hydraulische Antwort die Meldung „kein Durchfluss“ aus. Bei verrauschten Sensoren erhöhen. |
| **Öffnungsbefehl-Schwelle des Watchdogs** (`flow_open_threshold_pct`) | % | 15 | Ab welchem Ventilbefehl ein Heizkreis eine hydraulische Antwort zeigen muss. Unterhalb der Schwelle wird der Heizkreis nicht auf fehlenden Durchfluss geprüft. |
| **Hydraulisches Antwortfenster** (`flow_response_window_min`) | min | 45 | Wie viele Minuten durchgehend fehlender Durchflusssignatur (bei geöffnetem Befehl und plausibler Zirkulation) die Meldung auslösen; dasselbe Fenster überwacht ein Ventil, das nicht schließt. Die Platte ist träge — Minimum 30 min, um Flattern zu vermeiden; 1440 deaktiviert den Watchdog praktisch. |

Der Watchdog ist ein unabhängiger, **physischer** Zeuge der Ventilarbeit: Er stützt
sich ausschließlich auf die Wassersensoren des Heizkreises und **vertraut nicht** der
von der Ventil-Entität gemeldeten Position (dieser Kanal kann den Befehl nach einem
Reglerneustart „nachplappern“ — eine reale Störung aus dem Sommer 2026, als die
Ventile stillstanden, aber Gehorsam meldeten). Die Reaktion ist passiv: Meldung +
`binary_sensor`-Entität „Durchflussstörung“ + Einfrieren des Integrators; der Watchdog
bewegt selbst kein Ventil. Im Reiter Ventile gibt es einen Chip zur
Durchfluss-Gesundheit (ok / kein Durchfluss? / schließt nicht?) sowie die Schaltfläche
für den **manuellen Aktuierungstest** — das Ventil wird bewusst für 20–30 min auf
100 % geöffnet, und über das Ergebnis entscheidet die Antwort der Sonden (zur
Ausführung nach Wartung oder einem Stromereignis; wird durch Sicherheitsregeln
abgebrochen).

## 9. Flächenkühlung und Kondensation

Die Kühlung über den Boden hat ein hartes Risiko: **Kondenswasser auf dem Boden**,
wenn das kalte Wasser unter den Taupunkt des Raums fällt. Der Schutz ist
zweischichtig:

1. **Globaler sicherer Taupunkt** — die Integration berechnet den Taupunkt jedes
   gekühlten Raums (aus Temperatur und Luftfeuchtigkeit), nimmt das **Maximum** und
   addiert **2 K**. Das Ergebnis veröffentlicht sie als
   `sensor.*_global_safe_dew_point`. **Verbinden Sie diesen Wert als untere Grenze der
   Kühlwassertemperatur mit der Wärmepumpe** (z. B. über Modbus/ESPHome/eine
   Automatisierung, die die Grenze im Pumpencontroller schreibt) — die Integration
   steuert das Wasser nicht selbst, sondern liefert nur den fertigen sicheren Wert.
2. **Lokale S2-Drosselung (je Raum)** — wenn die gemessene Vorlauftemperatur des
   Heizkreises sich dem Taupunkt des Raums nähert, wird der Durchfluss linear
   gedrosselt (Meldung `s2_throttle`), und am Taupunkt selbst schließt das Ventil
   vollständig (Meldung `s2_condensation`). Wirkt unabhängig von der Wärmepumpe.

Die Luftfeuchtigkeit ist hier der wichtigste Messwert, daher wird sie gesondert
überwacht: Ein Messwert, der älter als 60 min ist, wird noch verwendet, aber mit
wachsendem Sicherheitszuschlag (+ bis zu 1 K auf den Taupunkt, Meldung
`rh_stale_gated`); über 120 min ist er unbrauchbar — der Raum fällt aus dem globalen
Maximum, und der lokale Schutz stoppt die Kühlung vorsorglich. Ein Raum ohne
Luftfeuchtigkeitssensor kann nicht an der Kühlung teilnehmen (der Assistent erzwingt
das).

## 10. Zusatzquelle und Multisplit

- **Wann sich der Split einschaltet:** wenn die Regelabweichung die
  **Zuschaltschwelle** (§8) überschreitet. Der Befehl lautet stets „ein + Richtung +
  Sollwert“ — der Split regelt sich selbst; er ersetzt nie den Boden, und seine
  Entscheidung schließt nie das Ventil.
- **Wann er freigibt:** innerhalb des Komfortbands (des Totbands).
- **Was mit dem Boden während des Nachkühlens geschieht (Kühlung):** Wenn der Split
  kühlt, kühlt er die **Luft**, sodass die gemessene Regelabweichung schnell sinkt und
  der PI-Regler selbst das Bodenventil auf null schließen wollte — der Split würde also
  den Boden indirekt genau dann aushungern, wenn der warme Beton entladen werden muss.
  Daher wird bei der Kühlung **das Bodenventil gehalten**, solange der Split
  eingeschaltet ist, auf dem Niveau vom Moment des Zuschaltens (und wenn sich der Raum
  weiter erwärmt, kann der PI es **noch weiter** öffnen — das Halten ist eine
  Untergrenze, keine Obergrenze). Dadurch entlädt der Boden weiter den Beton, und der
  Split taktet seltener. Das Halten **schwächt den Kondensationsschutz nicht**: Die
  Taupunkt-Drosselung (§9) schließt das gehaltene Ventil weiterhin, und der Verlust des
  Raumsensors schließt es weiterhin auf 0. Beim Heizen ändert sich nichts. Es gibt
  keine neuen Parameter einzustellen — es funktioniert automatisch.
- **Warum der Sollwert des Splits vom Raum-Sollwert abweicht:** Der Sollwert des
  Splits wird bewusst um den Parameter **Sollwert-Überhöhung der Zusatzquelle**
  angehoben (§8; Standard 1 K — Heizung: +1 K, Kühlung: −1 K). Der Sensor des Splits
  hängt unter der Decke und misst wärmer als unser Raumsensor — ein Sollwert gleich dem
  Ziel würde den Split abschalten, bevor das Nachheizen/Nachkühlen den Raum tatsächlich
  erreicht. Über das Abschalten entscheidet ohnehin UNSER Raumsensor mit Hysterese,
  sodass der Raum nicht „davonläuft“. Wenn Sie möchten, dass der Split genau den
  Sollwert erhält, setzen Sie den Parameter auf 0. Im Übergangsmodus gilt stets
  Sollwert = Ziel.
- **Dwell-Timer:** Mindestlaufzeit und -stillstand (§8) — ein Richtungswechsel geht
  stets über AUS mit voller Stillstandszeit.
- **Erneutes Erzwingen:** Ein unveränderter Befehl wird nicht in jedem Zyklus gesendet
  (kein Piepen, Ihre Lüftungseinstellungen überstehen es), aber etwa **alle 45
  Minuten** wird das Paar Modus+Temperatur erneut erzwungen; bis dahin meldet die
  Meldung `fast_source_mismatch` eine Abweichung.
- **Multisplit:** Räume, deren Inneneinheiten an einem gemeinsamen Außengerät hängen,
  kennzeichnen Sie mit derselben **Gruppe** in der Raumkonfiguration. Das Außengerät
  kann physikalisch nicht gleichzeitig heizen und kühlen, daher entscheidet die
  Integration **eine Richtung je Gruppe**: Es gewinnt der Raum, der am weitesten
  außerhalb seines Komfortbands liegt, und die **bestehende Richtung erhält einen Bonus
  von 0,5 K** (eine neue Richtung muss deutlich stärker benötigt sein — kein Flattern).
  Der Verlierer wird zwangsweise abgeschaltet (Meldung `fast_source_group_conflict`)
  und kehrt erst nach einem Richtungswechsel der Gruppe zurück, wobei er die volle
  Stillstandszeit durchläuft.

### Ruhezeiten der Zusatzquelle

Je Raum können Sie **erlaubte Zeiten der Zusatzquelle** festlegen (Raumkonfiguration,
Felder „Zusatzquelle erlaubt ab/bis“): das Fenster, in dem der Split/das Heizgerät
arbeiten **darf**, z. B. 07:00–22:00. Außerhalb des Fensters herrschen Ruhezeiten:

- **Fenster-Semantik:** beide Felder zusammen oder keines (leer = immer erlaubt). Das
  Fenster darf **über Mitternacht reichen** (z. B. 22:00–07:00 erlaubt den Betrieb nur
  nachts). Die Fensterkante wird mit der Genauigkeit des Regelzyklus (5 min) beachtet.
- **Außerhalb des Fensters** schaltet sich der Split nicht ein, selbst wenn die
  Regelabweichung die Zuschaltschwelle überschreitet — der Raumbericht trägt dann die
  (informative) Meldung `fast_source_quiet_hours`, und der Boden heizt/kühlt weiterhin
  normal.
- **Fensterende bei laufendem Split:** Die Einheit schaltet sich über die NORMALEN
  Regeln der Mindestlaufzeit (Dwell) ab — der Kompressorschutz ist wichtiger als
  Pünktlichkeit auf die Minute.
- **Übergangsmodus:** Die Ruhezeiten gelten auch dort, obwohl der Split dann die
  einzige Quelle des Raums ist — Ruhe ist Ruhe (eine bewusste Entscheidung; in einem
  kühlen Frühling kann der Raum bis zum Morgen leicht vom Sollwert abdriften).
- **Sicherheitsausnahme:** Notheizen/-kühlen (S3/S4 — Raum extrem
  unterkühlt/überhitzt) darf die Ruhe durchbrechen. Die Sicherheit des Raums steht über
  dem akustischen Komfort.

Das konfigurierte Fenster sehen Sie im Reiter **Zusatzquelle** (Spalte „Erlaubte
Zeiten“) und in den Raumdetails (Kachel Zusatzquelle).

## 11. Wärmepumpe (optionale Anbindung)

<!-- Inhalt in Übereinstimmung mit den Tooltips tip_hp_* in frontend/tortoise-ufh-panel.js
     und mit prd-control-brain.md §8.13 — zusammen ändern. -->

Seit v0.8.0 kann Tortoise-UFH **optional** mit einer Wärmepumpe zusammenarbeiten (z. B.
Panasonic Aquarea über HeishaMon). Alles ist **opt-in**: Ohne Konfiguration des
Abschnitts **Einstellungen → Geräte & Dienste → Tortoise-UFH → Konfigurieren →
Wärmepumpe** berührt die Integration die Pumpe nicht, wie bisher. Tortoise **steuert
weiterhin nicht den Kompressor, die Leistung oder die Wetterkurve der Pumpe** — es
synchronisiert höchstens die RICHTUNG des Modus und gibt begrenzte Wasser-Sollwerte
vor.

**Konfiguration (alle Felder optional):**

- **Modus-Entität der Pumpe** (`select`, HeishaMon-Stil: „Heat only“ / „Cool only“ /
  „Auto“ / „DHW only“ / „Heat+DHW“ / „Cool+DHW“ / „Auto+DHW“; beispielsweise
  `select.heat_pump_mode`),
- **Entität für den Heizwasser-Sollwert** (`number`, optional — standardmäßig folgt die
  Pumpe ihrer eigenen Kurve),
- **Entität für den Kühlwasser-Sollwert** (`number`, z. B.
  `number.z1_cool_request_temp`),
- **Entität „Pumpe für Fußbodenheizung verfügbar“** — wenn ausgeschaltet
  (WW-Bereitung/Abtauung), frieren die Raumregler den Integralanteil ein.

**Synchronisierung der Modusrichtung:** Tortoise-Modus „Heizung“ → Variante „Heat“,
„Kühlung“ → Variante „Cool“, stets **unter Beibehaltung des Teils „+DHW“** — das
WW-Flag gehört der externen WW-Automatik, und Tortoise entfernt es nie und schreibt
auch nie „DHW only“ allein. Wenn die Pumpe aktuell im „DHW only“ IST, schreibt Tortoise
gar nicht (die WW-Automatik ist mitten im Zyklus und stellt die Richtung selbst wieder
her — das Panel zeigt dann eine Abweichung mit dem Hinweis „DHW only“). In den Modi
Übergang und Aus wird die Richtung nicht erzwungen. Der Schreibvorgang ist selten: bei
einem Tortoise-Moduswechsel oder wenn eine Abweichung ≥2 Zyklen anhält, höchstens alle
15 Minuten.

**Kühlwasser-Sollwert:** Im Kühlmodus schreibt Tortoise an die Pumpe
`max(Basistemperatur Kühlwasser, globaler sicherer Taupunkt)` (§8, §9) — das Wasser
fällt nie in die Kondensationszone. Der Wert wird **auf den Schritt (`step`) der
number-Entität der Pumpe mit gewöhnlicher mathematischer Rundung gerundet** (auf die
nächste volle Teilung, die Hälfte aufwärts), um genau das Raster der Pumpe zu treffen —
ohne dies konnte eine Pumpe mit 1-°C-Schritt z. B. 16,56 → 16 selbst abschneiden, unter
den Taupunkt (issue #5). Bewusst gewöhnliche Rundung, nicht „immer aufwärts“: Die
2-K-Taupunkt-Marge deckt mit Reserve höchstens eine halbe Teilung Spiel ab. Schreiben
bei einer Änderung ≥ 0,5 K, alle ~45 min erneut erzwungen.

**Heizwasser-Sollwert (optional):** eine einfache Wetterkurve — Basis + Steigung ×
Unterschreitung der neutralen Temperatur, begrenzt auf 20–40 °C (§8). Ohne einen
Außentemperatur-Messwert im jeweiligen Zyklus wird nichts geschrieben (die Pumpe bleibt
beim letzten Sollwert — ihre Firmware hat eigene Grenzen). Der Heiz-Sollwert wird mit
derselben gewöhnlichen Rundung wie beim Kühlen auf den Schritt der Entität gerundet.
Bevor Sie die Heiz-Entität angeben, vergleichen Sie die Tortoise-Kurve mit der Kurve
Ihrer eigenen Pumpe.

**WW-Schalter (Reiter Wärmepumpe):** fügt den Teil „+DHW“ der Modus-Entität hinzu oder
entfernt ihn (z. B. „Heat only“ ↔ „Heat+DHW“) — das ist eine manuelle Anforderung
„Wasser jetzt aufheizen / stopp“, keine Sperre. **Die externe WW-Automatik ist
Eigentümerin dieses Flags und darf es jederzeit überschreiben — das ist ihr Recht.**
Das Entfernen des Flags, wenn die Pumpe im „DHW only“ ist, ist unmöglich (es gibt keine
Richtung, zu der zurückgekehrt werden könnte).

**Kühlstart erzwingen (optional, Panasonic; ab v0.13.0):** Im Kühlbetrieb startet der
Verdichter einer Panasonic Aquarea erst, wenn der Rücklauf `Sollwert + 3 K` erreicht, und
stoppt bei `Sollwert` — diese 3-K-Hysterese ist im Firmware fest verdrahtet. In langen
Stillständen bleibt der Rücklauf daher hoch stehen und der Boden liefert zu wenig. Nach dem
Aktivieren senkt Tortoise — sobald es die Pumpe im Stillstand mit echtem Bedarf sieht — den
geschriebenen Sollwert für einen Zyklus auf den taupunktsicheren Taupunkt, um den Verdichter zu
lösen, und stellt danach sofort den normalen Sollwert wieder her. Ergebnis: kälteres
Durchschnittswasser bei weiterhin taupunktsicherem Rücklauf. **Aktivieren und einstellen** Sie es
im Reiter **Feineinstellung**, Gruppe **„Kühlstart erzwingen“**: der Ein/Aus-Schalter plus vier
globale Regler (angestrebtes Totband, Verzögerung vor dem Erzwingen, Mindestabstand zwischen Starts,
Obergrenze der Starts pro Stunde). Geben Sie zuvor die Entitäten für **Rücklauftemperatur** und
**Verdichterfrequenz** unter Optionen → **Wärmepumpe** an (die **Vorlauf**-Entität ist dort rein
diagnostisch). Standardmäßig **deaktiviert**; Panasonic-spezifisch. Den Zustand (Impuls / Ruhe /
Sperrzeit, Schwelle, Messwerte) sehen Sie im Reiter Wärmepumpe.

**Wann Tortoise an die Pumpe schreibt:** nur wenn mindestens ein Raum im Zustand
**Aktiv** ist (das globale Gerät darf nur bewegt werden, wenn irgendjemand die Regelung
übergeben hat) — sonst zeigt der Reiter „Schreibvorgänge pausiert“. Der Abschiedsbefehl
(Ausschalten eines Raums / Entfernen der Integration) **berührt die Pumpe nicht**.

## 12. Meldungen und Alarme — Verzeichnis

<!-- Verzeichnis in Übereinstimmung mit FLAG_LABELS in frontend/tortoise-ufh-panel.js — zusammen ändern. -->

Alle diese Meldungen sehen Sie auch als **Annunciator im eigenen Reiter „Meldungen“**
(seit v0.10.0; seit v0.10.3 ein eigener Reiter, früher oben in Räume) — eine
einklappbare, gruppierte **Liste** (Stil wie „Abstimmung“): Jede Meldung ist eine
Zeile — **abgeblendet**, wenn inaktiv, und **leuchtend** (linker Farbbalken + Status
„aktiv ×N Räume“), wenn sie auslöst. Sicherheitsregeln tragen den Code **S1–S6**, und
unter dem **„i“**-Symbol bei jeder Meldung steht ihre vollständige Erklärung. Die Liste
rendert aus einem einzigen Register — eine neue Meldung erscheint darin automatisch.
Das vollständige Verzeichnis unten:

| Meldung | Bedeutung | Was zu tun ist |
|---|---|---|
| `sensor_lost` | Verlust des Temperatursensors (kein Messwert, unplausibler Messwert oder älter als 45 min). Ventil: beim Heizen hält es die letzte gesunde Stellung, beim Kühlen parkt es auf 0; Split AUS. | Prüfen Sie Sensor/Batterie/Netzwerk. Der Raum kehrt bei einem frischen, glaubwürdigen Messwert von selbst zurück. |
| `s2_throttle` | Kühlung gedrosselt — der Vorlauf nähert sich dem Taupunkt (Durchfluss < 100 %). | An feuchten Tagen normal. Wenn dauerhaft: Erhöhen Sie die Wassergrenze an der Pumpe (globaler Taupunkt) oder entfeuchten Sie die Luft. |
| `s2_condensation` | Kondensationsschutz: Der Vorlauf hat den Taupunkt erreicht — Ventil geschlossen. | Prüfen Sie Luftfeuchtigkeit und Kühlwassertemperatur; stellen Sie sicher, dass die Pumpe den globalen sicheren Taupunkt respektiert. |
| `rh_stale_gated` | Luftfeuchtigkeit veraltet (60–120 min) — Taupunkt mit einem Zuschlag von bis zu +1 K berechnet. | Prüfen Sie den Luftfeuchtigkeitssensor. Über 120 min fällt der Raum aus den Schutzmechanismen — siehe §9. |
| `valve_mismatch` | Der Aktor meldet seit ≥3 Zyklen eine andere Position als der Befehl („das Ventil gehorcht nicht“). | Prüfen Sie Aktor/Relais/Entität; vergleichen Sie die Spalten Befehl und Rückmeldung im Reiter Ventile. |
| `fast_source_mismatch` | Der Split ist in einem anderen Zustand als der Befehl (z. B. per Fernbedienung geändert). | Nichts — er wird beim erneuten Erzwingen (~45 min) überschrieben; für dauerhaft manuelle Steuerung schalten Sie den Raum auf Aus. |
| `fast_source_min_runtime` | Sperre der Mindestlaufzeit/-stillstandszeit — die Zusatzquelle kann den Zustand vorübergehend nicht wechseln. | Nichts — Kompressorschutz; der Timer steht im Reiter Zusatzquelle. |
| `fast_source_quiet_hours` | Ruhezeiten der Zusatzquelle — der Raum ist außerhalb seines Fensters erlaubter Zeiten (§10), der Split schaltet sich nicht ein (ein laufender beendet seine Mindestlaufzeit). | Informativ. Das Fenster ändern Sie in der Raumkonfiguration. |
| `fast_source_group_conflict` | Der Raum hat die Richtungsabwägung am gemeinsamen Außengerät verloren — sein Split zwangsweise AUS. | Nichts — er kehrt nach einem Richtungswechsel der Gruppe zurück. Wenn häufig: Überdenken Sie die Offsets der Räume in der Gruppe. |
| `fast_source_cannot_cool` | Der Raum möchte kühlen, aber seine Zusatzquelle kann es nicht (Heizgerät). | Informativ. |
| `cooling_disabled` | Der Raum nimmt nicht an der Kühlung teil (Konfigurationsoption) — im Kühlmodus wird er übersprungen. | Beabsichtigt; ändern Sie es in den Raumoptionen, wenn Sie kühlen möchten. |
| `flicker_pulsing` | Kühl-Sollwert-Flicker (Panasonic, §11): Für diesen einen Zyklus wurde der Sollwert auf den taupunktsicheren Boden gesenkt, um den Verdichter aus seiner festen 3-K-Rücklaufhysterese zu lösen; der nächste Zyklus stellt den normalen Sollwert wieder her. | Informativ (beabsichtigt). |
| `flicker_dew_blocked` | Der Flicker wäre scharf, aber ein Impuls müsste den rohen Taupunkt überschreiten — kein Spielraum, den Sollwert zu senken; der Impuls wird zurückgehalten. | Informativ; an feuchten Tagen normal. |
| `flicker_no_sensor` | Der Kühl-Flicker ist aktiviert, aber der Rücklauf- oder Verdichterfrequenz-Messwert fehlt (Entität nicht gesetzt oder veraltet) — Impulse werden zurückgehalten. | Prüfen Sie die Entitäten für Rücklauftemperatur und Verdichterfrequenz (Optionen → Wärmepumpe). |
| `s1_floor_overheat` | Bodenschutz: zu heißer Vorlauf — Ventil bis zum Abkühlen geschlossen. | Prüfen Sie die Heizkurve/Vorlauftemperatur der Wärmepumpe. |
| `s3_emergency_heat` | Notheizen: Raum stark unterkühlt — erzwungenes Heizen mit allem, was da ist. | Prüfen Sie, warum der Raum so ausgekühlt ist (Fenster? Störung?). |
| `s4_emergency_cool` | Notkühlen: Raum stark überhitzt — erzwungenes Kühlen. | Prüfen Sie die Überhitzungsquelle (Sonne? Störung?). |
| `s5_watchdog` | Keine frischen Raumdaten seit > 15 min — neutrale Stellung, Alarm im Bericht. | Prüfen Sie Sensoren/Netzwerk; löscht sich nach 5 min durchgehend frischer Daten. |
| `unknown_room` | Raum ohne Reglerkonfiguration (Übergangszustand nach Konfigurationsänderungen). | Laden Sie die Integration neu; hält es an — entfernen Sie den Raum und fügen Sie ihn hinzu. |
| `controller_error` | Ausnahme im Raumregler — sichere Degradierung (beim Heizen hält das Ventil die Stellung, beim Kühlen 0; Split AUS). | Sehen Sie in die HA-Protokolle und melden Sie das Problem (siehe §13). |
| `loop_no_flow` | Durchfluss-Watchdog (S6): Das Ventil ist seit längerer Zeit offen, aber die Heizkreis-Sonden sehen keinen Wasserdurchfluss — Integrator eingefroren, Entität „Durchflussstörung“ eingeschaltet. | Prüfen Sie den Ventilcontroller/Aktor und den Durchfluss am Verteiler (Rotameter). Der Watchdog vertraut der Ventilrückmeldung nicht — siehe §8, „Durchfluss-Watchdog“. |
| `actuation_test_running` | Ein manueller Aktuierungstest dieses Raums läuft (Ventil bewusst auf 100 %). | Informativ. Warten Sie bis zum Ende oder brechen Sie im Panel ab. |
| `actuation_test_failed` | Aktuierungstest fehlgeschlagen — der Heizkreis reagierte hydraulisch nicht auf die Öffnung. | Prüfen Sie Controller/Aktor/Durchfluss dieses Heizkreises. |

## 13. Fehlerbehebung

- **Das Panel zeigt „Veraltet“ / Watchdog `stale`** — seit >15 min hat kein Raum
  frische Daten geliefert. Prüfen Sie die Sensoren und ihre Integrationen
  (Zigbee/ESPHome). Der Status kehrt nach 5 Minuten ununterbrochen frischer Daten
  zurück.
- **Ein Raum hat `sensor_lost`, obwohl der Sensor funktioniert** — der Messwert wurde
  möglicherweise vom Plausibilitäts-Gate abgelehnt (ein Sprung > 4 K/Zyklus erfordert
  die Bestätigung durch einen zweiten Messwert nach ~5 min; Bereich −10…50 °C) oder ist
  älter als 45 min. Sehen Sie in den Abschnitt Verkabelung in den Raumdetails — dort
  sehen Sie die Rohzustände der Entitäten.
- **Das Ventil gehorcht nicht (`valve_mismatch`)** — vergleichen Sie Befehl vs.
  Rückmeldung (Reiter Ventile). Typische Ursachen: Aktor ohne Stromversorgung,
  schreibgeschützte Entität, falsche Entität dem Heizkreis zugewiesen.
- **Der Split ist in einem anderen Zustand als der Befehl** — Meldung
  `fast_source_mismatch`; die Integration überschreibt ihn beim erneuten Erzwingen
  (~45 min). Möchten Sie manuell steuern — schalten Sie den Raum auf Aus (§7).
- **Kein globaler Taupunkt beim Kühlen** — fahren Sie mit der Maus über die Kachel im
  Hero: Sie sehen den Grund je Raum (keine Luftfeuchtigkeit, Raum kühlt nicht, Kühlung
  deaktiviert, keine Messung). Den Taupunkt berechnen nur Räume, die tatsächlich
  kühlen.
- **Das Panel lädt nicht** — aktualisieren Sie unter Umgehung des Caches (Strg+F5);
  prüfen Sie, ob die Integration geladen ist (Einstellungen → Geräte & Dienste). Das
  Panel erfordert Administratorrechte.
- **Nach dem Update auf 0.7.0 zeigt das Select noch die Option „Shadow“** — Home
  Assistant cacht Übersetzungen; ein Neustart von HA hilft. Die Auswahl der alten
  Option wird ohnehin von der Validierung abgelehnt.
- **Nach dem Update zeigen die Raumsensoren Ventil 0 % / Split aus** — der Raum ist im
  Zustand Aus (das frühere „Shadow“ wurde auf Aus migriert). Das ist eine beabsichtigte
  Änderung der Berichterstattung — siehe §14. Schalten Sie den Raum auf Aktiv, wenn er
  steuern soll.
- **Protokolle und Diagnose** — Einstellungen → System → Protokolle (Quelle
  `custom_components.tortoise_ufh`); vollständiger Raumbericht: Raumdetails →
  „Rohdaten anzeigen“. Melden Sie Probleme im GitHub-Repository mit Protokoll und
  Rohbericht.

## 14. Einschränkungen, FAQ und Versionsverlauf

**Einschränkungen (bewusst, v1):** kein MPC/keine Tarifoptimierung, kein
Online-Modelllernen, die Integration steuert nicht den Kompressor, die Leistung oder
die Wetterkurve der Wärmepumpe (die optionale Anbindung aus §11 synchronisiert
höchstens die Modusrichtung und gibt begrenzte Wasser-Sollwerte vor; standardmäßig
deaktiviert), kein Bodensensor (Schutz über die Vorlauftemperatur), ein globaler Modus
für das ganze Haus.

**FAQ**

- *Kann ich nur einen Teil der Räume nutzen?* Ja — Räume im Zustand Aus werden beim
  Schreiben vollständig ignoriert; es steuern nur die auf Aktiv geschalteten.
- *Wirkt eine Änderung der Haustemperatur sofort?* Ja — neben der sofortigen
  Aktualisierung der Sollwerte wird innerhalb weniger Sekunden ein vollständiger
  Regelzyklus neu berechnet.
- *Gehen bei einem HA-Neustart die Einstellungen verloren?* Nein — Haustemperatur,
  Offsets und Modus werden persistiert und kehren nach dem Neustart zurück.

**Versionsverlauf / Migrationen**

- **0.12.0 (2026-07-14)** — **deutsche (DE) Oberfläche und Dokumentation** plus
  Dokumentations-Aufräumen. Die App (Panel, Entitäten, Konfigurationsassistent, Dienste) ist
  jetzt vollständig auf Deutsch übersetzt, mit englischem Fallback für alle anderen Sprachen.
  Das Handbuch wanderte vom missverständlichen Namen `INSTRUKCJA.md` in den Ordner
  **`docs/manual/`** (`pl.md`, `de.md`); das README hat einen Sprachumschalter. Keine
  Änderung am Kern oder am I/O-Vertrag; keine Konfigurationsmigration.
- **0.11.1 (2026-07-14)** — Panel: Die Meldung **„Kühlung in diesem Raum deaktiviert“**
  (`cooling_disabled`) leuchtet nicht mehr gelb. Das ist ein beabsichtigter,
  dauerhafter Zustand (der Raum ist bewusst von der Kühlung ausgenommen), sodass sie die
  gesamte Kühlsaison über fälschlich wie eine Störung aussah. Eingeführt wurde die
  neutrale Schwere **„info“** (eine ruhige, gedämpfte Farbe, unterhalb von „Warnung“) —
  die Meldung ist weiterhin sichtbar und erklärt, hebt aber den Statuspunkt des Raums
  nicht auf Gelb und macht keinen Lärm. Dieselbe Tatsache sieht man auch unverändert in
  den Raumdetails („Ausschluss vom sicheren Taupunkt“). Ausschließlich Panel — keine
  Konfigurationsmigration, kein neuer Parameter, der I/O-Vertrag unangetastet.
- **0.11.0 (2026-07-14)** — drei Änderungen. **(1) Kühlung — der Boden hält die
  Stellung während des Nachheizens durch den Split:** Wenn der Split die Kühlung
  unterstützt, schließt die von ihm gekühlte Luft das Bodenventil nicht mehr auf null —
  der Boden hält die letzte Stellung und entlädt weiterhin die Kühle aus dem Beton (die
  Platte kann auskühlen, der Split hat es leichter, weniger kurze Zyklen). Wirkt nur bei
  der Kühlung, **fügt keinen Parameter hinzu** und schwächt den Schutz vor Kondenswasser
  nicht (die Taupunkt-Drosselung S2 sowie der Sensorverlust schließen das Ventil
  weiterhin). **(2) Entfernte Erkennung „Ventil schließt nicht“ (`loop_stuck_open`):**
  Sie erzeugte Fehlalarme, da sich aus der bloßen Lufttemperatur nicht hart bestätigen
  lässt, ob der Aktor Wasser durchlässt. Die Erkennung des **fehlenden Durchflusses**
  (`loop_no_flow`) und der manuelle **Aktuierungstest** funktionieren unverändert. **(3)
  Panel — vereinheitlichtes Aussehen der Bedienelemente im Hero** (einheitliche Höhe,
  Rahmen und Hintergrund; ein Fehler behoben, durch den die Metriken mit zu großer
  Schrift gerendert wurden). Keine Konfigurationsmigration; der I/O-Vertrag unangetastet.
- **0.10.3 (2026-07-13)** — Panel: **Hero in einer Zeile** (war in dreien — nahm viel
  Platz ein; die Höhe sinkt ~2×, auf schmalen Bildschirmen bricht er sauber um),
  Metriken als Inline-„Beschriftung + Wert“, vereinheitlichte Bedienelemente, die
  Metrik **Meldungen klickbar** → führt zum Reiter Meldungen, die Schaltfläche „Räume
  verwalten“ als Symbol. Der **Meldungs-Annunciator** vom oberen Rand des Reiters Räume
  in einen **eigenen Reiter „Meldungen“** verschoben (Reihenfolge: Räume / Meldungen /
  Abstimmung / Ventile / Zusatzquelle / Wärmepumpe). Ausschließlich Panel — keine
  Konfigurationsmigration, der I/O-Vertrag unangetastet.
- **0.10.2 (2026-07-13)** — Korrektur (issue #5): Der Kühl- und Heizwasser-Sollwert wird
  nun auf den **realen Schritt (`step`) der number-Entität der Pumpe** mit gewöhnlicher
  mathematischer Rundung (Hälfte aufwärts) gerundet, nicht auf ein fest verdrahtetes
  0,5-K-Raster. Zuvor wurde das taupunktsichere Ziel 16,56 °C als 16,5 geschrieben, und
  eine Pumpe mit 1-°C-Schritt schnitt es auf 16 ab — unter die Taupunktschwelle. Die
  gewöhnliche Rundung (nicht „immer aufwärts“) gibt bewusst höchstens eine halbe Teilung
  der 2-K-Marge ab, um keine Kühlleistung zu verlieren. Ausschließlich die
  Schreibschicht — keine Migration, der I/O-Vertrag unangetastet.
- **0.10.1 (2026-07-13)** — Panel-Feinschliff: Der Meldungs-Annunciator von Kacheln in
  eine **einklappbare, gruppierte Liste** im Stil der „Abstimmung“ umgebaut (eine Zeile
  pro Meldung, linker Farbbalken der Schwere + Status „aktiv ×N“, die Kopfzeile klappt
  ein/aus und zeigt nach dem Einklappen eine Zusammenfassung). Im Reiter „Abstimmung“
  wurde die Bereichsauswahl (Global / Raum) von Chips auf **Reiter** geändert.
  Ausschließlich Panel — keine Konfigurationsmigration.
- **0.10.0 (2026-07-13)** — **Meldungs-Annunciator** am oberen Rand des Reiters Räume:
  alle bekannten Meldungen als Kacheln (abgeblendet, wenn inaktiv; farbig leuchtend mit
  Zähler `×N`, wenn sie auslösen), gruppiert in Sicherheit (S1–S6) / Zusatzquelle /
  Konfiguration, jede mit „i“ und vollständiger Erklärung. Rendert aus **einem einzigen
  Meldungsregister** (eine neue Meldung erscheint automatisch). Die Metrik „Meldungen“
  im Hero zeigt nun die Anzahl aktiver Meldungen in der Farbe der schwerwiegendsten.
  Behebt die „tote“ Schwere `alarm` (S6-Meldungen leuchten nun rot, nicht grau).
  Änderung ausschließlich im Panel — keine Konfigurationsmigration, der Reglervertrag
  unangetastet.
- **0.9.0 (2026-07-13)** — **Durchfluss-Watchdog (S6)**: ein unabhängiger, physischer
  Zeuge der Ventilarbeit aus den Wassersonden des Heizkreises, der der Rückmeldung der
  Ventil-Entität nicht vertraut (Meldung `loop_no_flow`, Entität „Durchflussstörung“,
  Einfrieren des Integrators); **manueller Aktuierungstest** (Dienst
  `tortoise_ufh.test_actuation`), ein Chip zur Durchfluss-Gesundheit im Reiter Ventile,
  drei neue Abstimmungsparameter (§8). Korrekturen am Schreibpfad der Ventile: erneutes
  Erzwingen des Befehls (~45 min) und Schreiben bei einer Feedback-Abweichung — nach
  einem Ausfall des Ventilcontrollers „friert“ die Parkposition nicht mehr ein. Die
  Sollwert-Überhöhung des Splits (±1 K, S12) ist nun der Parameter
  `fast_target_offset_k` (0 = keine Überhöhung). Keine Konfigurationsmigration. Behebt
  Meldung #4.
- **0.8.0 (2026-07-12)** — **Ruhezeiten der Zusatzquelle** je Raum (Fenster erlaubter
  Zeiten des Splits, auch über Mitternacht — §10) und **optionale Anbindung an die
  Wärmepumpe** (Synchronisierung der Modusrichtung unter Beibehaltung des WW-Flags,
  Kühl-/Heizwasser-Sollwerte, WW-Schalter, ein neuer Panel-Reiter — §11; drei neue
  globale Abstimmungsparameter — §8). Eine UX-Runde im Panel: normale (nicht
  Großbuchstaben-)Tabellenköpfe, Bestätigungen beim Umschalten von Raumzustand und
  Hausmodus, Zusatzquellen-Spalten unter einer gemeinsamen Überschrift mit lesbaren
  Leerzuständen, Abstimmung als Liste in Gruppen mit vollständigen Namen, Abschnitt
  „Sensoren und Signale“ mit dem Wert im Vordergrund, standardmäßig eingeklappte
  Diagnose-Entitäten, Tooltip „±1 K“ des Split-Sollwerts. Keine Konfigurationsmigration.
- **0.7.0 (2026-07-12)** — **der Zustand „Shadow“ (Beobachtet) wurde entfernt**: Der
  Raumzustand ist fortan der Zweizustand Aus/Aktiv, und der Standardzustand eines neuen
  Raums ist Aus. Die Migration ist automatisch (Config-Entry v2→v3; alte v1-Einträge
  durchlaufen den ganzen Weg v1→v3): „Shadow“-Räume werden zu Aus — das Schreibverhalten
  ist identisch (keiner dieser Zustände schrieb etwas), es ändert sich nur die
  Berichterstattung (ein Raum im Zustand Aus meldet Ruhe statt „was er tun würde“). Neu:
  „i“-Symbole mit Erläuterungen im Panel, eine vollständige polnische Sprachversion
  (Panel, Entitäten, Assistent, Dienste) und dieses Handbuch.
- **0.5.0–0.6.x** — Geräte je Raum, übersetzte Entitätsnamen, Multisplit-Gruppen mit
  Richtungsabwägung, Sicherheitshärtungen (vollständige Liste: `docs/DECISIONS.md`).
- **0.3.0** — der dreistufige Raumzustand ersetzte den globalen Kill-Switch (Migration
  v1→v2).
