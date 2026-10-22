# morgenblatt
Mein eigener RSS Feed, der aussieht wie eine Tageszeitung – oder wie das Menü eines iPod nano.

Kein Ungelesen-Zähler, kein Postfach-Gefühl: Einmal am Tag erscheint eine Ausgabe mit allen Artikeln
aus meinen Feeds, KI-zusammengefasst, mit Leitartikel, Volltext zum Lesen direkt in der App
und einer gesprochenen Hörausgabe als echtem Podcast.

## So funktioniert's

```
feeds.json ──► GitHub Action (täglich) ──► update_newspaper.py ──► _site/ ──► GitHub Pages
                                             │
                                             ├─ Feeds holen, Volltext aus dem XML bzw. von der Artikelseite
                                             ├─ HTML bereinigen (nh3) – keine Skripte, kein Tracking
                                             ├─ Wetter (Open-Meteo), Kurszettel (Yahoo Finance, EZB), Der Tag (Wikipedia, kuriose-feiertage.de)
                                             ├─ Rapid: Spielplan, Ergebnisse & Tabelle (ESPN)
                                             ├─ Gemini: Zusammenfassungen, Doppelte bündeln, Titelseite, Leitartikel, Fakt, Quiz, Briefing
                                             ├─ Sudoku des Tages, am Wochenende Lesestücke, sonntags „Die Woche“
                                             ├─ Briefing als Zwiegespräch sprechen (Gemini-TTS, sonst edge-tts) → MP3 + Podcast-Feed
                                             └─ Ausgabe verschlüsseln, Archiv der letzten 14 Tage pflegen
```

Die Action veröffentlicht direkt auf Pages – es gibt keine täglichen Commits mehr. Archiv und alte
Podcast-Folgen holt sie sich bei jedem Lauf von der live-Seite zurück.

## Einrichten

1. **Settings → Pages → Source: „GitHub Actions“** (einmalig).
2. **Settings → Secrets and variables → Actions → Tab „Secrets“ → „New repository secret“:**
   - `GEMINI_API_KEY` – KI-Redaktion und Sprachausgabe
   - `MORGENBLATT_PASSWORT` – macht die Ausgabe privat (siehe unten)

   Wichtig: *Repository secret*, nicht „Variable“ und nicht als Secret der Umgebung `github-pages`.
3. Actions → „Tägliche Zeitung generieren“ → *Run workflow*.

Danach läuft alles von selbst: jeden Morgen eine neue Ausgabe. Ein Push auf `main` stellt Änderungen sofort live –
neue Feeds in `feeds.json` bringen gleich eine neue Ausgabe, Änderungen an der Oberfläche nur die neue Oberfläche.

## Feeds & Einstellungen

Alles steht in `feeds.json`: Ressorts, Reihenfolge, Icons, welche Ressorts im „Telegramm“ landen,
wie viele Artikel pro Feed, wie lange das Archiv reicht, Podcast-Stimmen.
Feeds dürfen als URL oder als `{"url": "...", "name": "...", "max_alter_tage": 60, "max_artikel": 3}`
eingetragen sein – praktisch für Fachjournale, die nur alle paar Wochen ein neues Heft bringen.
Liefert eine Adresse statt des Feeds eine Webseite, sucht die Action dort nach dem verlinkten Feed.
Mit `"ohne": ["[premium]"]` fliegen Artikel mit diesen Wörtern im Titel raus (z. B. Bezahlartikel).

Außerdem in `feeds.json`:

- `wetter` – Ort und Koordinaten für die Tagesprognose im Zeitungskopf
- `boerse` – Indizes und Aktien für den Kurszettel (Yahoo-Finance-Kürzel, z. B. `EBS.VI` für die Erste Group);
  das Ressort mit `"kurszettel": true` zeigt die Aktien als Tabelle
- `der_tag` – Kasten mit Aktionstagen, Namenstagen und Fakt des Tages
- `fussball` – Spielplan, Ergebnisse und Tabelle eines Vereins im Ressort `ressort`. `espn_id` ist die Vereins-ID bei ESPN
  (Rapid = 519, steht in der Adresse der Vereinsseite auf espn.com), `liga` die Liga für die Tabelle,
  `wettbewerbe` alle Bewerbe, deren Spiele im Spielplan auftauchen sollen. Braucht keinen API-Schlüssel.
- `raetsel` – Rätselecke mit Sudoku des Tages (Mo leicht → Sa sehr schwer) und Quiz zur Ausgabe
- `wochenende` – samstags und sonntags „Fürs Wochenende“ mit langen Lesestücken, sonntags „Die Woche“ als Rückblick
- `podcast.sprecher` – die beiden Stimmen der Hörausgabe: Name, Gemini-Stimme und edge-tts-Stimme als Ersatz.
  Mit nur einem Eintrag wird's wieder ein Monolog.
- `farbe` je Ressort – Bühnenlicht im Karussell (`violett`, `blau`, `tuerkis`, `gold`, `petrol`, `rot`, `gruen`, `orange`, `pink`, `grau`)

- **Ohne KI:** Fehlt `GEMINI_API_KEY` oder streikt Gemini, erscheint die Ausgabe trotzdem – nur ohne Zusammenfassungen.
  Ist das Hauptmodell überlastet, weicht die Action auf Ersatzmodelle aus.
- **Modelle wechseln:** `GEMINI_MODEL`, `GEMINI_FALLBACK_MODELS` bzw. `GEMINI_TTS_MODEL` im Workflow setzen.

## Private Ausgabe & Urheberrecht

Ein RSS-Reader wie Feedly darf ganze Artikel anzeigen, weil er sie nur *dir* zeigt – hinter deinem Login.
GitHub Pages ist dagegen öffentlich: Ohne Schutz wären fremde Artikel für alle lesbar, und das wäre eine
Veröffentlichung, keine private Nutzung.

Deshalb gilt:

- **Mit `MORGENBLATT_PASSWORT`:** Die Ausgabe wird in der Action mit AES-256-GCM verschlüsselt und erst
  in deinem Browser entschlüsselt. Auf dem Server liegt nur Datensalat – wie ein Login, nur ohne Server.
  Volltexte, Archiv und Podcast-Feed (unter einem geheimen Pfad) sind privat.
- **Ohne Passwort:** Die Seite ist öffentlich und enthält deshalb nur Anrisse und KI-Zusammenfassungen,
  keine fremden Volltexte.
- Volltexte landen nie im Git-Repository, nur im verschlüsselten Pages-Deploy.
- Bezahlschranken werden nicht umgangen: Nachgeladen wird nur, was die Seite ohnehin frei ausliefert.

## Hörausgabe als Podcast

Jede Ausgabe wird als MP3 gesprochen – als Gespräch zwischen Anna und Max, die sich die Meldungen
zuspielen (Gemini-TTS mit zwei Stimmen; fällt das aus, zwei österreichische edge-tts-Stimmen) –
und mit Kapitelmarken als privater Podcast-Feed ausgeliefert. Sonntags gibt's ein eigenes Kapitel „Die Woche“. Die Feed-Adresse steht in der App unter
**Quellen & App** – einfach in Apple Podcasts, Overcast oder Pocket Casts hinzufügen.
In der App selbst spielt der Player die MP3 mit Kapiteln, auch über Sperrbildschirm und Kopfhörer-Tasten.

## Ansichten

- **Zeitung** – Titelseite mit Wetter, Kurszettel, Leitartikel, „Das Wichtigste heute“, Telegramm (mit Rapids
  nächstem Spiel) und „Der Tag“, darunter alle Ressorts. Oben rechts lässt sich jede Ausgabe der letzten 14 Tage
  aus dem Archiv aufschlagen.
- **Karussell** – wie die Song-Auswahl bei SingStar: Die Artikel drehen sich als Cover in einer Endlosschleife.
  Wischen, Pfeiltasten oder Mausrad blättern, ↑ ↓ wechselt das Ressort, 🎲 rollt zu einem zufälligen Artikel,
  „Hörprobe“ liest die Zusammenfassung vor (mit „Auto“ bei jedem Cover).
- **Nano** – iPod-Menü mit Click Wheel: Rad drehen zum Blättern, Mitte = auswählen, MENU = zurück.
  Auch per Tastatur (↑ ↓ ↵ Esc) und Antippen. Im Abenddruck wird's der schwarze nano.

## Was sonst noch drin ist

- **Merkliste** – ☆ im Artikel (oder Taste M im Karussell) merkt ihn vor. Gemerkte Artikel samt Volltext bleiben
  auf dem Gerät gespeichert, auch wenn ihre Ausgabe längst aus dem Archiv gefallen ist.
- **Doppelte bündeln** – Berichten mehrere Feeds über dieselbe Geschichte, erscheint sie nur einmal (mit dem
  ausführlichsten Text) und darunter „Auch berichtet:“ mit den anderen Quellen.
- **Rätselecke** – Sudoku des Tages (eindeutig lösbar, jeden Tag ein anderes, mit Prüfen, Notlösung und Uhr)
  und fünf Quizfragen zu den Artikeln der Ausgabe. Der Spielstand bleibt auf dem Gerät gespeichert.
- **Wochenende** – „Fürs Wochenende“ sammelt die langen Stücke der Woche, sonntags blickt „Die Woche“ auf die
  großen Geschichten und Aufmacher zurück.

## Als App

Die Seite ist eine PWA: iPhone → Safari → Teilen → „Zum Home-Bildschirm“, Android → „App installieren“.
Die Ausgabe wird beim Öffnen gespeichert und ist danach offline lesbar.

## Lokal ausprobieren

```bash
pip install -r requirements.txt
MORGENBLATT_PASSWORT=test python update_newspaper.py   # optional mit GEMINI_API_KEY=...
python -m http.server 8000                             # dann http://localhost:8000 öffnen
```
