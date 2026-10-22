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
                                             ├─ Gemini: Zusammenfassungen, Titelseite, Leitartikel, Briefing
                                             ├─ Briefing sprechen (Gemini-TTS, sonst edge-tts) → MP3 + Podcast-Feed
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

Danach läuft alles von selbst: jeden Morgen eine neue Ausgabe, Änderungen an `index.html` gehen beim Push sofort live.

## Feeds & Einstellungen

Alles steht in `feeds.json`: Ressorts, Reihenfolge, Icons, welche Ressorts im „Telegramm“ landen,
wie viele Artikel pro Feed, wie lange das Archiv reicht, Podcast-Stimmen.
Feeds dürfen als URL oder als `{"url": "...", "name": "...", "max_alter_tage": 60, "max_artikel": 3}`
eingetragen sein – praktisch für Fachjournale, die nur alle paar Wochen ein neues Heft bringen.
Liefert eine Adresse statt des Feeds eine Webseite, sucht die Action dort nach dem verlinkten Feed.

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

Jede Ausgabe wird als MP3 gesprochen (Gemini-TTS; fällt das aus, eine österreichische edge-tts-Stimme)
und mit Kapitelmarken als privater Podcast-Feed ausgeliefert. Die Feed-Adresse steht in der App unter
**Quellen & App** – einfach in Apple Podcasts, Overcast oder Pocket Casts hinzufügen.
In der App selbst spielt der Player die MP3 mit Kapiteln, auch über Sperrbildschirm und Kopfhörer-Tasten.

## Ansichten

- **Zeitung** – Titelseite mit Leitartikel, „Das Wichtigste heute“, Telegramm, darunter alle Ressorts.
  Oben rechts lässt sich jede Ausgabe der letzten 14 Tage aus dem Archiv aufschlagen.
- **Nano** – iPod-Menü mit Click Wheel: Rad drehen zum Blättern, Mitte = auswählen, MENU = zurück.
  Auch per Tastatur (↑ ↓ ↵ Esc) und Antippen. Im Abenddruck wird's der schwarze nano.

## Als App

Die Seite ist eine PWA: iPhone → Safari → Teilen → „Zum Home-Bildschirm“, Android → „App installieren“.
Die Ausgabe wird beim Öffnen gespeichert und ist danach offline lesbar.

## Lokal ausprobieren

```bash
pip install -r requirements.txt
MORGENBLATT_PASSWORT=test python update_newspaper.py   # optional mit GEMINI_API_KEY=...
python -m http.server 8000                             # dann http://localhost:8000 öffnen
```
