"""Morgenblatt – baut die tägliche Ausgabe aus den RSS-Feeds in feeds.json.

Ablauf:
  1. Alle Feeds holen, Artikel normalisieren (Volltext aus dem XML, Bild, Audio)
  2. Reine Teaser-Feeds: Volltext von der Artikelseite nachladen
  3. Gemini fasst jeden Artikel zusammen und stellt die Titelseite zusammen
  4. Hörausgabe als MP3 sprechen und als Podcast-Feed ausliefern
  5. Ausgabe (verschlüsselt) + Archiv nach OUT_DIR schreiben – von dort deployt die Action

Ohne GEMINI_API_KEY (oder wenn Gemini streikt) erscheint die Ausgabe trotzdem,
nur ohne KI-Redaktion. Ohne MORGENBLATT_PASSWORT ist die Seite öffentlich – dann
enthält sie nur Anrisse und keine fremden Volltexte.

Umgebungsvariablen:
  GEMINI_API_KEY        KI-Redaktion und Sprachausgabe
  MORGENBLATT_PASSWORT  verschlüsselt die Ausgabe, schaltet Volltexte frei
  OUT_DIR               Zielordner (Standard: aktueller Ordner)
  SITE_URL              Adresse der live-Seite – von dort kommen Archiv und alte Folgen zurück
  NUR_FRONTEND=true     keine neue Ausgabe bauen, nur die vorhandene neu veröffentlichen
"""
import base64
import gzip
import hashlib
import html
import json
import os
import re
import socket
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape, quoteattr
from zoneinfo import ZoneInfo

import feedparser
import nh3

CONFIG_FILE = "feeds.json"
EDITION_FILE = "edition.json"

OUT_DIR = os.environ.get("OUT_DIR", ".")
SITE_URL = os.environ.get("SITE_URL", "")
PASSWORD = os.environ.get("MORGENBLATT_PASSWORT", "")
TZ = ZoneInfo("Europe/Vienna")

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
# Ausweichmodelle, falls das Hauptmodell überlastet ist (503) – kommagetrennt überschreibbar
GEMINI_FALLBACK_MODELS = os.environ.get("GEMINI_FALLBACK_MODELS", "gemini-3.7-flash,gemini-3.5-flash-lite").split(",")
GEMINI_TTS_MODEL = os.environ.get("GEMINI_TTS_MODEL", "gemini-3.8-flash-tts")
KDF_ITERATIONS = 310_000
SUMMARY_BATCH = 40          # Artikel pro Zusammenfassungs-Anfrage
TEASER_MAX_CHARS = 900      # weniger Text im Feed = nur Teaser → Volltext nachladen
MAX_CONTENT_CHARS = 40000   # Obergrenze pro Artikel, hält edition.json schlank

FEED_TIMEOUT = 45  # manche Server (SWP, IP) antworten langsam
socket.setdefaulttimeout(FEED_TIMEOUT)

# Manche Feeds liefern Bots eine HTML-Seite statt RSS – mit vollständigen Browser-Headern klappt's
FEED_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
    "Accept-Language": "de-AT,de;q=0.9,en;q=0.8",
}

# Erlaubtes HTML im Lesemodus – alles andere (Skripte, Styles, iframes, Tracking) fliegt raus
ALLOWED_TAGS = {
    "p", "br", "hr", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "blockquote",
    "pre", "code", "em", "strong", "b", "i", "u", "s", "sub", "sup", "a", "img",
    "figure", "figcaption", "table", "thead", "tbody", "tr", "th", "td",
}
ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title"},
    "th": {"colspan", "rowspan"},
    "td": {"colspan", "rowspan"},
}


# ───────────────────────── Hilfsfunktionen ─────────────────────────

def clean_html(raw):
    return nh3.clean(
        raw or "",
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        url_schemes={"http", "https", "mailto"},
        set_tag_attribute_values={"a": {"target": "_blank"}, "img": {"loading": "lazy"}},
    ).strip()


def to_text(raw, paragraphs=False):
    """HTML → Klartext. Mit paragraphs=True bleiben Absätze als Leerzeilen erhalten."""
    raw = re.sub(r"</(p|h\d|li|blockquote|figcaption|tr)>|<br\s*/?>", "\n\n", raw or "", flags=re.I)
    text = html.unescape(nh3.clean(raw, tags=set()))
    blocks = (re.sub(r"\s+", " ", b).strip() for b in re.split(r"\n\s*\n", text))
    return ("\n\n" if paragraphs else " ").join(b for b in blocks if b)


def shorten(text, limit):
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",;:–-") + " …"


def safe_url(url):
    return url if (url or "").startswith(("https://", "http://")) else None


def article_id(link, title):
    return hashlib.sha1((link or title).encode("utf-8")).hexdigest()[:10]


def entry_date(entry):
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def entry_html(entry):
    """content:encoded bzw. Atom-Content ist meist der Volltext, summary oft nur der Teaser."""
    best = max((c.get("value", "") for c in entry.get("content", []) or []), key=len, default="")
    summary = entry.get("summary", "") or entry.get("description", "") or ""
    return best if len(best) >= len(summary) else summary


def entry_audio(entry):
    for enc in entry.get("enclosures", []) or []:
        if (enc.get("type") or "").startswith("audio/") or (enc.get("href") or "").endswith(".mp3"):
            return enc.get("href")
    return None


def entry_image(entry, raw_html):
    for media in (entry.get("media_thumbnail") or []) + (entry.get("media_content") or []):
        url = media.get("url") or ""
        kind = (media.get("medium") or media.get("type") or "")
        if url and ("image" in kind or re.search(r"\.(jpe?g|png|webp|gif)(\?|$)", url, re.I)):
            return url
    for enc in entry.get("enclosures", []) or []:
        if (enc.get("type") or "").startswith("image/"):
            return enc.get("href")
    if (entry.get("image") or {}).get("href"):  # itunes:image bei Podcasts
        return entry["image"]["href"]
    match = re.search(r'<img[^>]+src=["\'](https?://[^"\']+)', raw_html or "")
    return match.group(1) if match else None


def feed_list(section):
    """Feeds dürfen in feeds.json als URL-String oder als {"url": ..., "name": ...} stehen."""
    return [f if isinstance(f, dict) else {"url": f} for f in section.get("feeds", [])]


# ───────────────────────── 1. Feeds holen ─────────────────────────

def download_feed(url):
    request = urllib.request.Request(url, headers=FEED_HEADERS)
    with urllib.request.urlopen(request, timeout=FEED_TIMEOUT) as response:
        data, final_url = response.read(), response.geturl()
    # content-location sorgt dafür, dass relative Links im Feed richtig aufgelöst werden
    return feedparser.parse(data, response_headers={"content-location": final_url}), data, final_url


def discover_feed(html_bytes, base_url):
    """Liefert die Adresse eine Webseite statt eines Feeds, steht der Feed meist im <link rel="alternate">."""
    page = html_bytes.decode("utf-8", "replace")
    for tag in re.findall(r"<link\b[^>]*>", page, re.I):
        if re.search(r"rel=[\"']?alternate", tag, re.I) and re.search(r"application/(rss|atom)\+xml", tag, re.I):
            href = re.search(r"href=[\"']([^\"']+)", tag, re.I)
            if href:
                return urllib.parse.urljoin(base_url, html.unescape(href.group(1)))
    return None


def fetch_feed(feed_cfg):
    url = feed_cfg["url"]
    try:
        if not url.startswith(("http://", "https://")):
            parsed = feedparser.parse(url)  # lokale Datei, z. B. zum Testen
        else:
            parsed, data, final_url = download_feed(url)
            alternative = not parsed.entries and discover_feed(data, final_url)
            if alternative and alternative != url:
                print(f"  → {url} ist eine Webseite, Feed gefunden unter {alternative}")
                parsed, _, _ = download_feed(alternative)
        if parsed.bozo and not parsed.entries:
            raise ValueError(parsed.get("bozo_exception", "kein gültiger Feed"))
        return parsed, None
    except Exception as e:
        return None, str(e)


def short_error(message):
    """Fehlergrund fürs Quellen-Fenster: kurz und verständlich."""
    code = re.search(r"HTTP Error (\d+)", message)
    if code:
        return f"HTTP {code.group(1)}"
    if "timed out" in message:
        return "Zeitüberschreitung"
    return "kein gültiger Feed"


def normalize_entry(entry, section_id, feed_name, link_fallback):
    raw = entry_html(entry)
    title = to_text(entry.get("title", "")) or "Ohne Titel"
    link = safe_url(entry.get("link")) or safe_url(link_fallback) or ""
    source = feed_name

    # Google News: echter Verlag steckt in <source>, Titel endet auf " - Verlag"
    news_source = entry.get("source") or {}
    if news_source.get("title"):
        source = news_source["title"]
        suffix = " - " + source
        if title.endswith(suffix):
            title = title[: -len(suffix)]

    date = entry_date(entry)
    return {
        "id": article_id(link, title),
        "section": section_id,
        "source": source,
        "title": title,
        "link": link,
        "author": to_text(entry.get("author", "")),
        "published": date.isoformat() if date else None,
        "image": safe_url(entry_image(entry, raw)),
        "audio": safe_url(entry_audio(entry)),
        "content_html": clean_html(raw),
    }


def collect_articles(config):
    settings = config.get("einstellungen", {})
    per_feed = settings.get("max_artikel_pro_feed", 8)
    now = datetime.now(timezone.utc)

    jobs = [(section, feed) for section in config["ressorts"] for feed in feed_list(section)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda job: fetch_feed(job[1]), jobs))

    articles, seen, feed_status = [], set(), {}
    for (section, feed_cfg), (parsed, error) in zip(jobs, results):
        url = feed_cfg["url"]
        if error:
            print(f"  ✗ {url}: {error}")
            feed_status[url] = {"url": url, "name": feed_cfg.get("name") or url, "ok": False, "count": 0, "error": short_error(error)}
            continue

        feed_name = feed_cfg.get("name") or to_text(parsed.feed.get("title", "")) or url
        # Fachjournale erscheinen seltener – pro Feed dürfen "max_alter_tage" und "max_artikel" abweichen
        cutoff = now - timedelta(days=feed_cfg.get("max_alter_tage", settings.get("max_alter_tage", 4)))
        count = 0
        for entry in parsed.entries:
            if count >= feed_cfg.get("max_artikel", per_feed):
                break
            art = normalize_entry(entry, section["id"], feed_name, parsed.feed.get("link", ""))
            published = art["published"] and datetime.fromisoformat(art["published"])
            title_key = (section["id"], art["title"].lower())
            if (published and published < cutoff) or art["id"] in seen or title_key in seen:
                continue
            if any(word.lower() in art["title"].lower() for word in feed_cfg.get("ohne", [])):
                continue  # z. B. Bezahlartikel: "ohne": ["[premium]"]
            seen.update({art["id"], title_key})
            articles.append(art)
            count += 1

        print(f"  ✓ {feed_name}: {count} Artikel")
        feed_status[url] = {"url": url, "name": feed_name, "ok": True, "count": count}

    return articles, feed_status


# ───────────────────────── 2. Volltext nachladen ─────────────────────────

def enrich_fulltext(articles, enabled):
    import trafilatura
    from trafilatura.settings import use_config

    cfg = use_config()
    cfg.set("DEFAULT", "DOWNLOAD_TIMEOUT", "15")

    def fetch_fulltext(url):
        downloaded = trafilatura.fetch_url(url, config=cfg)
        if not downloaded:
            return None
        return trafilatura.extract(
            downloaded, url=url, output_format="html", config=cfg,
            include_images=True, include_links=True, include_comments=False, favor_precision=True,
        )

    def needs_fulltext(art):
        return (
            enabled
            and art["link"].startswith("http")
            and not art["audio"]                     # Podcast: Shownotes sind der Inhalt
            and "news.google.com" not in art["link"]  # Google-News-Links sind JS-Weiterleitungen
            and len(to_text(art["content_html"])) < TEASER_MAX_CHARS
        )

    def work(art):
        try:
            extracted = fetch_fulltext(art["link"]) or ""
            # Die Schlagzeile zeigen wir selbst, also die erste <h1> der Seite weglassen
            extracted = clean_html(re.sub(r"<h1[^>]*>.*?</h1>", "", extracted, count=1, flags=re.S | re.I))
        except Exception as e:
            print(f"  ✗ Volltext {art['link']}: {e}")
            return
        if len(to_text(extracted)) > max(TEASER_MAX_CHARS, len(to_text(art["content_html"])) * 1.5):
            art["content_html"] = extracted

    todo = [a for a in articles if needs_fulltext(a)]
    print(f"Lade Volltext für {len(todo)} Teaser-Artikel nach …")
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(work, todo))

    for art in articles:
        if len(art["content_html"]) > MAX_CONTENT_CHARS:
            art["content_html"] = clean_html(art["content_html"][:MAX_CONTENT_CHARS])
        text = to_text(art["content_html"])
        art["full_text"] = len(text) >= TEASER_MAX_CHARS
        art["reading_minutes"] = max(1, round(len(text.split()) / 200))


# ───────────────────────── 3. KI-Redaktion ─────────────────────────

_failed_models = set()


def ask_gemini(client, prompt, retries=3):
    """Bei Überlast erst mit wachsendem Abstand neu versuchen, dann aufs nächste Modell ausweichen.
    Ein Modell, das in diesem Lauf komplett ausgefallen ist, wird danach übersprungen."""
    models = [m.strip() for m in [GEMINI_MODEL, *GEMINI_FALLBACK_MODELS] if m.strip()]
    for model in dict.fromkeys(models):
        if model in _failed_models:
            continue
        for attempt in range(retries):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config={"response_mime_type": "application/json", "temperature": 0.4},
                )
                text = response.text.strip()
                text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
                return json.loads(text)
            except Exception as e:
                print(f"  Gemini-Fehler mit {model} (Versuch {attempt + 1}/{retries}): {str(e)[:160]}")
                if "404" in str(e) or "NOT_FOUND" in str(e):
                    break  # Modell gibt's nicht (mehr) – gleich weiter
                if attempt < retries - 1:
                    time.sleep(20 * 2 ** attempt)
        _failed_models.add(model)
    return None


def summarize(client, articles):
    for start in range(0, len(articles), SUMMARY_BATCH):
        batch = articles[start:start + SUMMARY_BATCH]
        payload = [
            {"id": a["id"], "quelle": a["source"], "titel": a["title"], "text": shorten(to_text(a["content_html"]), 1500)}
            for a in batch
        ]
        prompt = f"""Du bist Redakteur einer deutschsprachigen Tageszeitung aus Wien.
Bearbeite JEDEN der folgenden Artikel, auch englische – deine Antwort ist immer auf Deutsch.

- "headline": prägnante deutsche Schlagzeile (deutsche Originaltitel übernehmen, Clickbait entschärfen)
- "summary": 2–3 sachliche Sätze: Was steht drin?
- "takeaway": 1 Satz: Warum ist das relevant?

Erfinde nichts, was nicht im Text steht. Ist der Text zu dünn, fasse nur zusammen, was da ist.
Antworte als JSON: {{"articles": [{{"id": "...", "headline": "...", "summary": "...", "takeaway": "..."}}]}}

Artikel:
{json.dumps(payload, ensure_ascii=False)}"""
        print(f"  Zusammenfassungen {start + 1}–{start + len(batch)} …")
        result = ask_gemini(client, prompt) or {}
        by_id = {a["id"]: a for a in batch}
        for item in result.get("articles", []):
            art = by_id.get(item.get("id"))
            if art:
                for key in ("headline", "summary", "takeaway"):
                    art[key] = (item.get(key) or "").strip() or art[key]


def describe_extras(extras, date):
    """Wetter, Börse und Der Tag als Stichworte für die KI."""
    lines = [f"Datum: {german_date(date.isoformat())}"]
    if w := extras.get("weather"):
        lines.append(f"Wetter in {w['place']}: {w['text']}, {w['min']} bis {w['max']} Grad, Regenrisiko {w['rain_chance']} %, Wind bis {w['wind']} km/h")
    if markets := extras.get("markets"):
        lines.append("Börse (Schluss Vortag): " + ", ".join(f"{m['name']} {m['value']} ({m['change']:+.1f} %)" for m in markets))
    today = extras.get("today") or {}
    if today.get("observances"):
        lines.append("Aktionstage heute: " + "; ".join(today["observances"]))
    if today.get("name_days"):
        lines.append("Namenstage: " + ", ".join(today["name_days"]))
    events = "\n".join(f"  {i}: {e['year']} – {e['text']}" for i, e in enumerate(today.get("events", [])))
    return "\n".join(lines), events


def edit_front_page(client, articles, sections, extras, date):
    """Gemini wählt nur IDs aus und schreibt Texte – Links und Titel kommen nie aus der KI."""
    telegram = {s["id"] for s in sections if s.get("telegramm")}
    context, events = describe_extras(extras, date)
    overview = [
        {"id": a["id"], "ressort": a["section"], "quelle": a["source"], "titel": a["headline"], "zusammenfassung": a["summary"]}
        for a in articles
    ]
    ressorts = [{"id": s["id"], "name": s["name"]} for s in sections]
    prompt = f"""Du bist Chefredakteur meiner persönlichen Tageszeitung aus Wien.
Hier sind alle Meldungen des Tages. Stelle die Titelseite zusammen:

- "lead_id": die ID des wichtigsten, gehaltvollsten Stücks (wird der Leitartikel; nicht aus {sorted(telegram)})
- "top_ids": 3 weitere wichtige IDs für "Das Wichtigste heute" (nicht der Leitartikel, nicht aus {sorted(telegram)})
- "briefing": Sprechtext für meinen täglichen Podcast, wird per Sprachausgabe vorgelesen.
  Wie eine entspannte Wiener Morgensendung: natürlich, mit Übergängen zwischen den Meldungen.
  Keine Aufzählungszeichen, keine URLs, keine Emojis. Abkürzungen und Zahlen so schreiben,
  wie man sie spricht ("zum Beispiel" statt "z. B.").
  "intro": Begrüßung mit Datum, kurz dem Wetter, gern einem Aktionstag, dann der Tagesüberblick (4–5 Sätze).
  "chapters": je Ressort mit Meldungen ein Kapitel: die 2–4 wichtigsten Meldungen in 5–9 Sätzen,
  Sport und Lokales kürzer. Im Finanz-Kapitel darfst du die Börsenkurse nennen.
  "outro": 1–2 Sätze zum Abschluss.
  Insgesamt etwa 900–1200 Wörter.
- "fact": Fakt des Tages. Wähle aus den Ereignissen unten das überraschendste und formuliere es in einem
  lebendigen Satz, der mit „Heute vor … Jahren“ beginnt. Nur was im Ereignis steht, nichts dazuerfinden.

Ressorts: {json.dumps(ressorts, ensure_ascii=False)}

{context}
Ereignisse an diesem Tag (Index: Jahr – Text):
{events or "  (keine)"}

Antworte als JSON:
{{"lead_id": "...", "top_ids": ["...", "...", "..."],
  "briefing": {{"intro": "...", "chapters": [{{"section": "<ressort-id>", "text": "..."}}], "outro": "..."}},
  "fact": {{"index": 0, "text": "..."}}}}

Meldungen:
{json.dumps(overview, ensure_ascii=False)}"""
    return ask_gemini(client, prompt)


def write_lead(client, art):
    prompt = f"""Du bist Chefredakteur einer Wiener Tageszeitung. Schreibe auf Basis des folgenden Artikels
den Leitartikel der heutigen Ausgabe – auf Deutsch, auch wenn die Quelle englisch ist.

- "headline": starke Schlagzeile
- "subline": eine Unterzeile, die neugierig macht
- "text": 180–250 Wörter, sachlich mit Einordnung: Worum geht es, was ist neu, was bedeutet es? Absätze mit \\n\\n trennen.
- "takeaway": 1 Satz Redaktions-Notiz

Erfinde keine Fakten, die nicht im Text stehen.
Antworte als JSON: {{"headline": "...", "subline": "...", "text": "...", "takeaway": "..."}}

Quelle: {art["source"]}
Titel: {art["title"]}
Text:
{shorten(to_text(art["content_html"]), 12000)}"""
    return ask_gemini(client, prompt)


def pick_front_page_without_ai(articles, telegram):
    """Fallback ohne KI: das längste Stück wird Leitartikel, die neuesten drei kommen nach oben."""
    candidates = [a for a in articles if a["section"] not in telegram] or articles
    lead = max(candidates, key=lambda a: len(a["content_html"]))
    newest = sorted(candidates, key=lambda a: a["published"] or "", reverse=True)
    top = [a["id"] for a in newest if a["id"] != lead["id"]][:3]
    return lead, top


# ───────────────────────── 4. Rund um die Zeitung: Wetter, Börse, Der Tag ─────────────────────────

# WMO-Wettercodes von Open-Meteo → Symbol und Beschreibung
WEATHER_CODES = {
    0: ("☀️", "sonnig"), 1: ("🌤️", "überwiegend sonnig"), 2: ("⛅", "teils bewölkt"), 3: ("☁️", "bedeckt"),
    45: ("🌫️", "Nebel"), 48: ("🌫️", "Reifnebel"), 51: ("🌦️", "leichter Nieselregen"), 53: ("🌦️", "Nieselregen"),
    55: ("🌧️", "starker Nieselregen"), 56: ("🌧️", "gefrierender Niesel"), 57: ("🌧️", "gefrierender Niesel"),
    61: ("🌧️", "leichter Regen"), 63: ("🌧️", "Regen"), 65: ("🌧️", "starker Regen"), 66: ("🌧️", "gefrierender Regen"),
    67: ("🌧️", "gefrierender Regen"), 71: ("🌨️", "leichter Schneefall"), 73: ("🌨️", "Schneefall"), 75: ("❄️", "starker Schneefall"),
    77: ("🌨️", "Schneegriesel"), 80: ("🌦️", "Regenschauer"), 81: ("🌧️", "kräftige Schauer"), 82: ("⛈️", "heftige Schauer"),
    85: ("🌨️", "Schneeschauer"), 86: ("🌨️", "Schneeschauer"), 95: ("⛈️", "Gewitter"), 96: ("⛈️", "Gewitter mit Hagel"),
    99: ("⛈️", "Gewitter mit Hagel"),
}


def get_bytes(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Morgenblatt; persönlicher RSS-Reader)"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def get_json(url):
    return json.loads(get_bytes(url))


def fetch_weather(cfg):
    """Tagesprognose von Open-Meteo (frei, ohne Key). Tagsüber zählt, was zwischen 8 und 20 Uhr passiert."""
    query = urllib.parse.urlencode({
        "latitude": cfg.get("lat", 48.21), "longitude": cfg.get("lon", 16.37), "timezone": "Europe/Vienna", "forecast_days": 1,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max,sunrise,sunset,uv_index_max",
        "hourly": "temperature_2m,precipitation_probability,weather_code",
    })
    data = get_json(f"https://api.open-meteo.com/v1/forecast?{query}")
    daily, hourly = data["daily"], data["hourly"]
    daytime = range(8, 21)
    codes = [hourly["weather_code"][h] for h in daytime]
    code = max(set(codes), key=lambda c: (codes.count(c), c))  # häufigstes Wetter, bei Gleichstand das „schlechtere“
    icon, text = WEATHER_CODES.get(code, ("🌡️", "wechselhaft"))
    slot = lambda h: {
        "hour": h, "temp": round(hourly["temperature_2m"][h]), "rain": hourly["precipitation_probability"][h],
        "icon": WEATHER_CODES.get(hourly["weather_code"][h], ("🌡️", ""))[0],
    }
    return {
        "place": cfg.get("ort", "Wien"), "icon": icon, "text": text,
        "min": round(daily["temperature_2m_min"][0]), "max": round(daily["temperature_2m_max"][0]),
        "rain_chance": max(hourly["precipitation_probability"][h] for h in daytime),
        "rain_mm": daily["precipitation_sum"][0], "wind": round(daily["wind_speed_10m_max"][0]),
        "uv": daily["uv_index_max"][0], "sunrise": daily["sunrise"][0][11:16], "sunset": daily["sunset"][0][11:16],
        "hours": [slot(h) for h in (8, 12, 16, 20)],
    }


def fetch_quote(item):
    """Schlusskurs vom Vortag samt Veränderung (Yahoo Finance)."""
    data = get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(item['symbol'])}?range=5d&interval=1d")
    result = data["chart"]["result"][0]
    closes = [c for c in result["indicators"]["quote"][0]["close"] if c is not None]
    if len(closes) < 2:
        raise ValueError("zu wenige Kurse")
    return {"name": item["name"], "value": round(closes[-1], 2), "change": round((closes[-1] / closes[-2] - 1) * 100, 2),
            "currency": result["meta"].get("currency", "")}


def fetch_eurusd():
    """EZB-Referenzkurs, verglichen mit dem Vortag."""
    import xml.etree.ElementTree as ET

    ns = {"e": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"}
    root = ET.fromstring(get_bytes("https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"))
    rates = [float(day.find("e:Cube[@currency='USD']", ns).get("rate")) for day in root.findall(".//e:Cube[@time]", ns)[:2]]
    return {"name": "EUR/USD", "value": rates[0], "change": round((rates[0] / rates[1] - 1) * 100, 2), "currency": ""}


def fetch_markets(cfg):
    jobs = [(item, "index") for item in cfg.get("indizes", [])] + [(item, "aktie") for item in cfg.get("aktien", [])]

    def work(job):
        item, kind = job
        try:
            return {**fetch_quote(item), "kind": kind}
        except Exception as e:
            print(f"  ✗ Kurs {item['name']}: {e}")
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        quotes = [q for q in pool.map(work, jobs) if q]
    if cfg.get("eur_usd", True):
        try:
            quotes.insert(min(1, len(quotes)), {**fetch_eurusd(), "kind": "devisen"})
        except Exception as e:
            print(f"  ✗ EUR/USD: {e}")
    return quotes


def wiki_text(text):
    return re.sub(r"\s+", " ", text.replace("­", "")).strip()


def fetch_today(date):
    """Namenstage, Gedenk- und Aktionstage, „Heute vor … Jahren“ (Wikipedia) und kuriose Feiertage."""
    today = {"name_days": [], "observances": [], "events": []}
    path = f"{date.month:02d}/{date.day:02d}"
    try:
        for item in get_json(f"https://api.wikimedia.org/feed/v1/wikipedia/de/onthisday/holidays/{path}")["holidays"]:
            header, _, rest = wiki_text(item["text"].replace("\n", " | ")).partition(" | ")
            entries = [e.strip() for e in rest.split(" | ") if e.strip()]
            if header.startswith("Namenstag"):
                today["name_days"] += [n.strip() for e in entries for n in e.split(",") if n.strip()]
            elif "Aktionstag" in header or "Gedenk- und" in header or ("Staatlich" in header and any("Österreich" in e for e in entries)):
                today["observances"] += [re.sub(r"\s*\(.*?\)$", "", e) for e in entries]
    except Exception as e:
        print(f"  ✗ Wikipedia-Gedenktage: {e}")
    try:
        events = get_json(f"https://api.wikimedia.org/feed/v1/wikipedia/de/onthisday/selected/{path}")["selected"]
        today["events"] = [{"year": e["year"], "ago": date.year - e["year"], "text": wiki_text(e["text"])} for e in events if e.get("year")][:10]
    except Exception as e:
        print(f"  ✗ Wikipedia-Ereignisse: {e}")
    parsed, error = fetch_feed({"url": "https://www.kuriose-feiertage.de/feed/"})
    for entry in (parsed.entries if parsed else []):
        published = entry_date(entry)
        if published and published.astimezone(TZ).strftime("%m-%d") == date.strftime("%m-%d"):
            today["observances"].append(to_text(entry.get("title", "")).split(" – ")[0].strip())
    if error:
        print(f"  ✗ Kuriose Feiertage: {error}")
    today["observances"] = list(dict.fromkeys(o for o in today["observances"] if o))[:6]
    return today


def pick_anniversary(events):
    """Ohne KI: das Ereignis mit dem rundesten Jahrestag."""
    if not events:
        return None
    roundness = lambda e: next((score for score, step in ((3, 100), (2, 50), (1, 25), (0.5, 10)) if e["ago"] % step == 0), 0)
    return max(events, key=roundness)


def collect_extras(config, date):
    extras = {}
    if config.get("wetter"):
        try:
            extras["weather"] = fetch_weather(config["wetter"])
            w = extras["weather"]
            print(f"  ✓ Wetter {w['place']}: {w['text']}, {w['min']}–{w['max']} °C")
        except Exception as e:
            print(f"  ✗ Wetter: {e}")
    if config.get("boerse"):
        extras["markets"] = fetch_markets(config["boerse"])
        print(f"  ✓ Börse: {len(extras['markets'])} Kurse")
    if config.get("der_tag", True):
        extras["today"] = fetch_today(date)
        t = extras["today"]
        print(f"  ✓ Der Tag: {len(t['observances'])} Aktionstage, {len(t['name_days'])} Namenstage, {len(t['events'])} Ereignisse")
    return extras


# ───────────────────────── 4b. Ausgabe schreiben ─────────────────────────

def greeting_without_ai(extras, date):
    parts = [f"Guten Morgen! Heute ist {german_date(date.isoformat())}."]
    if w := extras.get("weather"):
        parts.append(f"In {w['place']} wird es {w['text']} bei {w['min']} bis {w['max']} Grad.")
    if observances := (extras.get("today") or {}).get("observances"):
        parts.append(f"Und heute ist {observances[0]}.")
    return " ".join(parts)


def build_briefing(sections, articles, ai_briefing, greeting=""):
    names = {s["id"]: s.get("kurz") or s["name"] for s in sections}
    if ai_briefing and ai_briefing.get("chapters"):
        chapters = [{"id": "intro", "title": "Begrüßung", "text": ai_briefing.get("intro", "")}]
        chapters += [
            {"id": c["section"], "title": names[c["section"]], "text": c.get("text", "")}
            for c in ai_briefing["chapters"] if c.get("section") in names and c.get("text")
        ]
        if ai_briefing.get("outro"):
            chapters.append({"id": "outro", "title": "Ausklang", "text": ai_briefing["outro"]})
        return [c for c in chapters if c["text"]]

    chapters = [{"id": "intro", "title": "Begrüßung", "text": greeting}] if greeting else []
    for s in sections:
        items = [a for a in articles if a["section"] == s["id"]][:2]
        if items:
            text = " ".join(f"{a['headline']}. {a['summary']}" for a in items)
            chapters.append({"id": s["id"], "title": names[s["id"]], "text": f"{names[s['id']]}: {text}"})
    return chapters


def strip_fulltext(articles):
    """Ohne Passwort ist die Seite öffentlich – dann nur Anrisse, keine fremden Volltexte."""
    for art in articles:
        teaser = shorten(to_text(art["content_html"]), 400)
        art["content_html"] = f"<p>{html.escape(teaser)}</p>" if teaser else ""
        art["full_text"] = False


def build_edition(config):
    sections = config["ressorts"]
    telegram = {s["id"] for s in sections if s.get("telegramm")}

    date = datetime.now(TZ).date()
    print("Hole Wetter, Börse und Der Tag …")
    extras = collect_extras(config, date)
    print("Hole Feeds …")
    articles, feed_status = collect_articles(config)
    if not articles:
        raise SystemExit("Keine Artikel gefunden – alte Ausgabe bleibt stehen.")
    enrich_fulltext(articles, config.get("einstellungen", {}).get("volltext_nachladen", True) and bool(PASSWORD))

    order = {s["id"]: i for i, s in enumerate(sections)}
    articles.sort(key=lambda a: a["published"] or "", reverse=True)
    articles.sort(key=lambda a: order[a["section"]])

    for art in articles:
        art.update(headline=art["title"], summary=shorten(to_text(art["content_html"]), 280), takeaway="")

    api_key = os.environ.get("GEMINI_API_KEY")
    client, front, lead_text = None, None, None
    if api_key:
        from google import genai
        client = genai.Client(api_key=api_key)
        print(f"KI-Redaktion mit {GEMINI_MODEL} (Ausweichmodelle: {', '.join(GEMINI_FALLBACK_MODELS)}) …")
        summarize(client, articles)
        front = edit_front_page(client, articles, sections, extras, date)
    else:
        print("Kein GEMINI_API_KEY – Ausgabe erscheint ohne KI-Redaktion.")

    by_id = {a["id"]: a for a in articles}
    if front and front.get("lead_id") in by_id:
        lead = by_id[front["lead_id"]]
        top = [
            i for i in front.get("top_ids", [])
            if i in by_id and i != lead["id"] and by_id[i]["section"] not in telegram
        ][:3]
        lead_text = write_lead(client, lead)
    else:
        lead, top = pick_front_page_without_ai(articles, telegram)

    lead_text = lead_text or {
        "headline": lead["headline"],
        "subline": lead["summary"],
        "text": shorten(to_text(lead["content_html"], paragraphs=True), 1400 if PASSWORD else 400),
        "takeaway": lead["takeaway"],
    }
    if not PASSWORD:
        strip_fulltext(articles)

    today = extras.get("today")
    if today is not None:
        events = today.get("events", [])
        fact = (front or {}).get("fact") or {}
        index = fact.get("index")
        if isinstance(index, int) and 0 <= index < len(events) and fact.get("text"):
            today["fact"] = {"text": fact["text"].strip(), "year": events[index]["year"]}
        elif best := pick_anniversary(events):
            today["fact"] = {"text": f"Heute vor {best['ago']} Jahren: {best['text']}", "year": best["year"]}

    edition = {
        "version": 2,
        "date": date.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ai": bool(front),
        "private": bool(PASSWORD),
        "title": config.get("titel", "MORGENBLATT"),
        "subtitle": config.get("unterzeile", ""),
        "telegram": config.get("telegramm", {}),
        "sections": [
            {
                "id": s["id"],
                "name": s["name"],
                "short": s.get("kurz") or s["name"],
                "icon": s.get("icon", "📰"),
                "telegram": bool(s.get("telegramm")),
                "color": s.get("farbe", ""),
                "markets": bool(s.get("kurszettel")),
                "feeds": [feed_status[f["url"]] for f in feed_list(s)],
            }
            for s in sections
        ],
        "lead": {"article": lead["id"], **{k: lead_text.get(k, "") for k in ("headline", "subline", "text", "takeaway")}},
        "top": top,
        "briefing": build_briefing(sections, articles, front and front.get("briefing"), greeting_without_ai(extras, date)),
        **{key: extras[key] for key in ("weather", "markets", "today") if extras.get(key) is not None},
        "articles": articles,
    }
    return edition, client


# ───────────────────────── 5. Hörausgabe als Podcast ─────────────────────────

TTS_STYLE = ("Lies den folgenden Text als ruhige, freundliche Moderation einer Wiener "
             "Morgensendung vor – natürlich, warm und nicht zu schnell:")
SILENCE_SECONDS = 0.6
EDGE_BITRATE = 48000  # edge-tts liefert MP3 mit 24 kHz / 48 kbit/s
WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONTHS = ["Jänner", "Februar", "März", "April", "Mai", "Juni", "Juli",
          "August", "September", "Oktober", "November", "Dezember"]


def german_date(iso_date):
    d = datetime.fromisoformat(iso_date)
    return f"{WEEKDAYS[d.weekday()]}, {d.day}. {MONTHS[d.month - 1]} {d.year}"


def timestamp(seconds, hours=False):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if hours or h else f"{m:02d}:{s:02d}"


def synthesize_gemini(client, text, voice):
    """Gemini-TTS liefert rohes PCM (16 Bit, mono). Gibt (pcm, samplerate) zurück."""
    from google.genai import types

    response = client.models.generate_content(
        model=GEMINI_TTS_MODEL,
        contents=f"{TTS_STYLE}\n\n{text}",
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))
            ),
        ),
    )
    blob = next(part.inline_data for part in response.candidates[0].content.parts if part.inline_data)
    rate = re.search(r"rate=(\d+)", blob.mime_type or "")
    return blob.data, int(rate.group(1)) if rate else 24000


def podcast_gemini(client, chapters, voice):
    import lameenc

    pcm, starts, rate = bytearray(), [], 24000
    for chapter in chapters:
        for attempt in range(3):
            try:
                data, rate = synthesize_gemini(client, chapter["text"], voice)
                break
            except Exception as e:
                print(f"  TTS-Fehler bei „{chapter['title']}“ (Versuch {attempt + 1}/3): {e}")
                if attempt == 2:
                    raise
                time.sleep(30)  # meist ein Rate-Limit
        starts.append(len(pcm) / 2 / rate)
        pcm += data + bytes(int(rate * SILENCE_SECONDS) * 2)

    encoder = lameenc.Encoder()
    encoder.set_bit_rate(64)
    encoder.set_in_sample_rate(rate)
    encoder.set_channels(1)
    encoder.set_quality(2)
    return bytes(encoder.encode(bytes(pcm)) + encoder.flush()), starts, len(pcm) / 2 / rate


def podcast_edge(chapters, voice):
    """Ersatzstimme ohne API-Key, falls Gemini-TTS nicht will (Microsoft-Edge-Vorlesestimmen)."""
    import asyncio
    import edge_tts

    async def speak(text):
        audio = bytearray()
        async for chunk in edge_tts.Communicate(text, voice).stream():
            if chunk["type"] == "audio":
                audio += chunk["data"]
        return bytes(audio)

    mp3, starts = bytearray(), []
    for chapter in chapters:
        starts.append(len(mp3) * 8 / EDGE_BITRATE)
        mp3 += asyncio.run(speak(chapter["text"]))
    return bytes(mp3), starts, len(mp3) * 8 / EDGE_BITRATE


def make_podcast(edition, settings, client, podcast_dir):
    """Spricht das Briefing als MP3, trägt Kapitelmarken ein und gibt die Folge für den Feed zurück."""
    chapters = edition["briefing"]
    if not settings.get("aktiv", True) or not chapters:
        return None

    audio, engine = None, None
    if client:
        try:
            print(f"Spreche Hörausgabe mit {GEMINI_TTS_MODEL} …")
            audio, engine = podcast_gemini(client, chapters, settings.get("stimme_gemini", "Charon")), "gemini"
        except Exception as e:
            print(f"  Gemini-TTS fehlgeschlagen, weiter mit edge-tts: {e}")
    if not audio:
        try:
            print("Spreche Hörausgabe mit edge-tts …")
            audio, engine = podcast_edge(chapters, settings.get("stimme_edge", "de-AT-JonasNeural")), "edge"
        except Exception as e:
            print(f"  Heute keine Hörausgabe: {e}")
            return None

    mp3, starts, duration = audio
    if len(mp3) < 10_000:
        print("  Heute keine Hörausgabe: Audio leer.")
        return None
    filename = f"{edition['date']}.mp3"
    os.makedirs(out(podcast_dir), exist_ok=True)
    with open(out(podcast_dir, filename), "wb") as f:
        f.write(mp3)
    for chapter, start in zip(chapters, starts):
        chapter["start"] = round(start, 1)
    edition["podcast"] = {
        "url": f"{podcast_dir}/{filename}",
        "feed": f"{podcast_dir}/feed.xml",
        "duration": round(duration),
        "engine": engine,
    }
    print(f"  {filename}: {duration / 60:.1f} Min. ({engine})")
    return {
        "date": edition["date"],
        "file": filename,
        "bytes": len(mp3),
        "duration": round(duration),
        "lead": edition["lead"]["headline"],
        "chapters": [{"title": c["title"], "start": c["start"]} for c in chapters],
    }


def write_podcast_feed(episodes, podcast_dir, paper_title):
    """Privater Podcast-Feed: itunes:block hält ihn aus Verzeichnissen, der Pfad ist geheim."""
    base = site_base()
    items = []
    for ep in episodes:
        chapters = ep.get("chapters", [])
        description = "\n".join([f"Schwerpunkt: {ep.get('lead', '')}", ""] + [f"{timestamp(c['start'])} {c['title']}" for c in chapters])
        psc = "".join(f'<psc:chapter start="{timestamp(c["start"], True)}" title={quoteattr(c["title"])}/>' for c in chapters)
        published = datetime.fromisoformat(ep["date"]).replace(hour=6, tzinfo=TZ)
        items.append(f"""    <item>
      <title>{escape(german_date(ep["date"]))}</title>
      <description>{escape(description)}</description>
      <enclosure url={quoteattr(base + podcast_dir + "/" + ep["file"])} length="{ep["bytes"]}" type="audio/mpeg"/>
      <guid isPermaLink="false">morgenblatt-{ep["date"]}</guid>
      <pubDate>{format_datetime(published)}</pubDate>
      <itunes:duration>{ep["duration"]}</itunes:duration>
      <psc:chapters version="1.2">{psc}</psc:chapters>
    </item>""")

    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:podcast="https://podcastindex.org/namespace/1.0" xmlns:psc="http://podlove.org/simple-chapters">
  <channel>
    <title>{escape(paper_title.title())} – Hörausgabe</title>
    <link>{escape(base or "./")}</link>
    <description>Meine tägliche Zeitung zum Hören.</description>
    <language>de-at</language>
    <itunes:author>{escape(paper_title.title())}</itunes:author>
    <itunes:image href={quoteattr(base + "icons/podcast-cover.png")}/>
    <itunes:explicit>false</itunes:explicit>
    <itunes:block>Yes</itunes:block>
    <podcast:locked>yes</podcast:locked>
{chr(10).join(items)}
  </channel>
</rss>
"""
    with open(out(podcast_dir, "feed.xml"), "w", encoding="utf-8") as f:
        f.write(feed)


def publish_podcast(episode, podcast_dir, keep, paper_title):
    episodes = [e for e in read_json(out(podcast_dir, "episodes.json"), []) if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.mp3", e.get("file", ""))]
    if episode:
        episodes = [e for e in episodes if e["date"] != episode["date"]] + [episode]
    episodes.sort(key=lambda e: e["date"], reverse=True)
    for old in episodes[keep:]:
        with suppress(OSError):
            os.remove(out(podcast_dir, old["file"]))
    episodes = [e for e in episodes[:keep] if os.path.exists(out(podcast_dir, e["file"]))]
    if episodes:
        write_json(out(podcast_dir, "episodes.json"), episodes, indent=1)
        write_podcast_feed(episodes, podcast_dir, paper_title)


# ───────────────────────── 6. Veröffentlichen ─────────────────────────

def out(*parts):
    return os.path.join(OUT_DIR, *parts)


def site_base():
    return SITE_URL.rstrip("/") + "/" if SITE_URL else ""


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data, **kwargs):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, **kwargs)


def podcast_path():
    """Mit Passwort liegt der Podcast unter einem geheimen Pfad, den nur die entschlüsselte Ausgabe kennt."""
    if not PASSWORD:
        return "podcast"
    return "podcast/" + hashlib.sha256(f"morgenblatt-podcast:{PASSWORD}".encode()).hexdigest()[:24]


def encrypt(edition):
    """AES-256-GCM mit PBKDF2-Schlüssel – entschlüsselt wird im Browser per WebCrypto."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt, iv = os.urandom(16), os.urandom(12)
    key = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode("utf-8"), salt, KDF_ITERATIONS, dklen=32)
    data = AESGCM(key).encrypt(iv, gzip.compress(json.dumps(edition, ensure_ascii=False).encode("utf-8")), None)
    b64 = lambda b: base64.b64encode(b).decode("ascii")
    return {
        "version": 2, "encrypted": True, "date": edition["date"], "generated_at": edition["generated_at"],
        "kdf": "PBKDF2-SHA256", "iterations": KDF_ITERATIONS, "salt": b64(salt), "iv": b64(iv), "data": b64(data),
    }


def decrypt(payload):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    raw = lambda k: base64.b64decode(payload[k])
    key = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode("utf-8"), raw("salt"), payload["iterations"], dklen=32)
    return json.loads(gzip.decompress(AESGCM(key).decrypt(raw("iv"), raw("data"), None)))


def readable(payload):
    """Passt eine (alte) Ausgabe zum aktuellen Modus? Öffentlich bleibt öffentlich, privat nur mit diesem Passwort."""
    if not isinstance(payload, dict) or payload.get("version") != 2:
        return False
    if not PASSWORD:
        return not payload.get("encrypted")
    if not payload.get("encrypted"):
        return False
    try:
        decrypt(payload)
        return True
    except Exception:
        return False


def restore_from_site(podcast_dir, with_current=False):
    """Pages ersetzt bei jedem Deploy die ganze Seite. Archiv und alte Podcast-Folgen holen wir
    deshalb von der live-Seite zurück, bevor die neue Ausgabe dazukommt."""
    base = site_base()
    if not base:
        return

    def grab(rel):
        target = out(rel)
        if os.path.exists(target):
            return
        try:
            request = urllib.request.Request(base + rel, headers={"User-Agent": "Morgenblatt"})
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read()
        except Exception:
            return
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(data)

    for rel in ["archiv/index.json", f"{podcast_dir}/episodes.json", f"{podcast_dir}/feed.xml"] + ([EDITION_FILE] if with_current else []):
        grab(rel)
    for entry in read_json(out("archiv", "index.json"), {}).get("editions", []):
        if re.fullmatch(r"archiv/\d{4}-\d{2}-\d{2}\.json", entry.get("file", "")):
            grab(entry["file"])
    for ep in read_json(out(podcast_dir, "episodes.json"), []):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.mp3", ep.get("file", "")):
            grab(f"{podcast_dir}/{ep['file']}")


def publish_edition(edition, keep_days):
    payload = encrypt(edition) if PASSWORD else edition
    write_json(out(EDITION_FILE), payload)

    # Archiv: eine Datei pro Tag; ältere und nicht mehr lesbare (z. B. altes Passwort) fliegen raus
    rel = f"archiv/{edition['date']}.json"
    write_json(out(rel), payload)
    previous = [
        e for e in read_json(out("archiv", "index.json"), {}).get("editions", [])
        if e.get("date") != edition["date"] and re.fullmatch(r"archiv/\d{4}-\d{2}-\d{2}\.json", e.get("file", ""))
    ]
    still_readable = [e for e in previous if readable(read_json(out(e["file"]), None))]
    index = sorted(still_readable + [{"date": edition["date"], "file": rel}], key=lambda e: e["date"], reverse=True)
    for old in [e for e in previous if e not in still_readable] + index[keep_days:]:
        with suppress(OSError):
            os.remove(out(old["file"]))
    write_json(out("archiv", "index.json"), {"editions": index[:keep_days]}, indent=1)


def run():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        config = json.load(f)
    settings = config.get("einstellungen", {})
    podcast_settings = config.get("podcast", {})
    podcast_dir = podcast_path()
    os.makedirs(OUT_DIR, exist_ok=True)

    if os.environ.get("NUR_FRONTEND") == "true":
        restore_from_site(podcast_dir, with_current=True)
        if readable(read_json(out(EDITION_FILE), None)):
            print("Nur Oberfläche neu veröffentlicht – Ausgabe, Archiv und Podcast bleiben.")
            return
        print("Keine passende Ausgabe online – baue eine neue.")
        with suppress(OSError):
            os.remove(out(EDITION_FILE))
    else:
        restore_from_site(podcast_dir)

    if not PASSWORD:
        print("Hinweis: Kein MORGENBLATT_PASSWORT gesetzt – die Seite ist öffentlich, daher ohne Volltexte.")

    edition, client = build_edition(config)
    episode = make_podcast(edition, podcast_settings, client, podcast_dir)
    publish_podcast(episode, podcast_dir, podcast_settings.get("folgen_behalten", 7), edition["title"])
    publish_edition(edition, settings.get("archiv_tage", 14))

    full = sum(a["full_text"] for a in edition["articles"])
    mode = "verschlüsselt" if PASSWORD else "öffentlich"
    print(f"{EDITION_FILE} geschrieben ({mode}): {len(edition['articles'])} Artikel, davon {full} mit Volltext.")


if __name__ == "__main__":
    run()
