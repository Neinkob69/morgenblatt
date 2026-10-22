# morgenblatt
Mein eigener RSS Feed, der aussieht wie eine Tageszeitung – oder wie das Menü eines iPod nano.

Kein Ungelesen-Zähler, kein Postfach-Gefühl: Einmal am Tag erscheint eine Ausgabe mit allen Artikeln
aus meinen Feeds, KI-zusammengefasst, mit Leitartikel, Hörausgabe und Volltext zum Lesen direkt in der App.

## So funktioniert's

```
feeds.json ──► GitHub Action (täglich) ──► update_newspaper.py ──► edition.json ──► index.html (GitHub Pages)
                                             │
                                             ├─ Feeds holen, Volltext aus dem XML (content:encoded)
                                             ├─ Teaser-Feeds: Volltext von der Artikelseite nachladen (trafilatura)
                                             ├─ HTML bereinigen (nh3) – keine Skripte, kein Tracking
                                             └─ Gemini: Zusammenfassungen, Titelseite, Leitartikel, Briefing
```

- **Feeds ändern:** `feeds.json` bearbeiten. Ressorts, Reihenfolge, Icons und welche Ressorts im
  „Telegramm“-Kasten landen, stehen alle dort. Feeds dürfen als URL oder als `{"url": "...", "name": "..."}` eingetragen sein.
- **Ohne KI:** Fehlt `GEMINI_API_KEY` oder streikt Gemini, erscheint die Ausgabe trotzdem – nur ohne Zusammenfassungen.
- **Modell wechseln:** Umgebungsvariable `GEMINI_MODEL` im Workflow setzen.
- **Neue Ausgabe von Hand:** Actions → „Tägliche Zeitung generieren“ → *Run workflow*.

## Ansichten

- **Zeitung** – Titelseite mit Leitartikel, „Das Wichtigste heute“, Telegramm, darunter alle Ressorts.
- **Nano** – iPod-Menü mit Click Wheel: Rad drehen zum Blättern, Mitte = auswählen, MENU = zurück.
  Auch per Tastatur (↑ ↓ ↵ Esc) und Antippen. Im Abenddruck wird's der schwarze nano.

## Als App

Die Seite ist eine PWA: iPhone → Safari → Teilen → „Zum Home-Bildschirm“, Android → „App installieren“.
Die ganze Ausgabe inklusive Volltexten wird beim Öffnen gespeichert und ist danach offline lesbar.

## Lokal ausprobieren

```bash
pip install -r requirements.txt
python update_newspaper.py          # optional mit GEMINI_API_KEY=...
python -m http.server 8000          # dann http://localhost:8000 öffnen
```
