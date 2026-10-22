"""Morgenblatt – baut die tägliche Ausgabe aus den RSS-Feeds in feeds.json.

Ablauf:
  1. Alle Feeds holen, Artikel normalisieren (Volltext aus dem XML, Bild, Audio)
  2. Reine Teaser-Feeds: Volltext von der Artikelseite nachladen
  3. Gemini fasst jeden Artikel zusammen und stellt die Titelseite zusammen
  4. edition.json + feeds.opml schreiben

Ohne GEMINI_API_KEY (oder wenn Gemini streikt) erscheint die Ausgabe trotzdem,
nur ohne KI-Redaktion.
"""
import hashlib
import html
import json
import os
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import quoteattr

import feedparser
import nh3

CONFIG_FILE = "feeds.json"
EDITION_FILE = "edition.json"
OPML_FILE = "feeds.opml"

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
SUMMARY_BATCH = 40          # Artikel pro Zusammenfassungs-Anfrage
TEASER_MAX_CHARS = 900      # weniger Text im Feed = nur Teaser → Volltext nachladen
MAX_CONTENT_CHARS = 40000   # Obergrenze pro Artikel, hält edition.json schlank

socket.setdefaulttimeout(20)

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

def fetch_feed(feed_cfg):
    url = feed_cfg["url"]
    try:
        parsed = feedparser.parse(url, agent="Mozilla/5.0 (compatible; Morgenblatt/2.0)")
        if parsed.bozo and not parsed.entries:
            raise ValueError(parsed.get("bozo_exception", "kein gültiger Feed"))
        return parsed, None
    except Exception as e:
        return None, str(e)


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
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.get("max_alter_tage", 4))

    jobs = [(section, feed) for section in config["ressorts"] for feed in feed_list(section)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda job: fetch_feed(job[1]), jobs))

    articles, seen, feed_status = [], set(), {}
    for (section, feed_cfg), (parsed, error) in zip(jobs, results):
        url = feed_cfg["url"]
        if error:
            print(f"  ✗ {url}: {error}")
            feed_status[url] = {"url": url, "name": feed_cfg.get("name") or url, "ok": False, "count": 0}
            continue

        feed_name = feed_cfg.get("name") or to_text(parsed.feed.get("title", "")) or url
        count = 0
        for entry in parsed.entries:
            if count >= per_feed:
                break
            art = normalize_entry(entry, section["id"], feed_name, parsed.feed.get("link", ""))
            published = art["published"] and datetime.fromisoformat(art["published"])
            title_key = (section["id"], art["title"].lower())
            if (published and published < cutoff) or art["id"] in seen or title_key in seen:
                continue
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

def ask_gemini(client, prompt, retries=3):
    for attempt in range(retries):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config={"response_mime_type": "application/json", "temperature": 0.4},
            )
            text = response.text.strip()
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
            return json.loads(text)
        except Exception as e:
            print(f"  Gemini-Fehler (Versuch {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                time.sleep(15 * (attempt + 1))
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


def edit_front_page(client, articles, sections):
    """Gemini wählt nur IDs aus und schreibt Texte – Links und Titel kommen nie aus der KI."""
    telegram = {s["id"] for s in sections if s.get("telegramm")}
    overview = [
        {"id": a["id"], "ressort": a["section"], "quelle": a["source"], "titel": a["headline"], "zusammenfassung": a["summary"]}
        for a in articles
    ]
    ressorts = [{"id": s["id"], "name": s["name"]} for s in sections]
    prompt = f"""Du bist Chefredakteur meiner persönlichen Tageszeitung aus Wien.
Hier sind alle Meldungen des Tages. Stelle die Titelseite zusammen:

- "lead_id": die ID des wichtigsten, gehaltvollsten Stücks (wird der Leitartikel; nicht aus {sorted(telegram)})
- "top_ids": 3 weitere wichtige IDs für "Das Wichtigste heute" (nicht der Leitartikel, nicht aus {sorted(telegram)})
- "briefing": Sprechtext für meine Hörausgabe, wird per Sprachausgabe vorgelesen.
  Natürlich und locker formuliert, keine Aufzählungszeichen, keine URLs, keine Emojis.
  "intro": kurze Begrüßung mit dem Tagesüberblick (2–3 Sätze).
  "chapters": je Ressort mit Meldungen ein Kapitel, die 2–3 wichtigsten Meldungen in 4–7 Sätzen.
  "outro": ein Satz zum Abschluss.

Ressorts: {json.dumps(ressorts, ensure_ascii=False)}

Antworte als JSON:
{{"lead_id": "...", "top_ids": ["...", "...", "..."],
  "briefing": {{"intro": "...", "chapters": [{{"section": "<ressort-id>", "text": "..."}}], "outro": "..."}}}}

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


# ───────────────────────── 4. Ausgabe schreiben ─────────────────────────

def build_briefing(sections, articles, ai_briefing):
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

    chapters = []
    for s in sections:
        items = [a for a in articles if a["section"] == s["id"]][:2]
        if items:
            text = " ".join(f"{a['headline']}. {a['summary']}" for a in items)
            chapters.append({"id": s["id"], "title": names[s["id"]], "text": f"{names[s['id']]}: {text}"})
    return chapters


def write_opml(config, feed_status):
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        f"  <head><title>{html.escape(config.get('titel', 'Morgenblatt'))} – Quellen</title></head>",
        "  <body>",
    ]
    for section in config["ressorts"]:
        lines.append(f"    <outline text={quoteattr(section['name'])}>")
        for feed in feed_list(section):
            name = feed_status[feed["url"]]["name"]
            lines.append(f"      <outline type=\"rss\" text={quoteattr(name)} xmlUrl={quoteattr(feed['url'])}/>")
        lines.append("    </outline>")
    lines += ["  </body>", "</opml>", ""]
    with open(OPML_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def run():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        config = json.load(f)
    sections = config["ressorts"]
    telegram = {s["id"] for s in sections if s.get("telegramm")}

    print("Hole Feeds …")
    articles, feed_status = collect_articles(config)
    if not articles:
        raise SystemExit("Keine Artikel gefunden – alte Ausgabe bleibt stehen.")
    enrich_fulltext(articles, config.get("einstellungen", {}).get("volltext_nachladen", True))

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
        print(f"KI-Redaktion mit {GEMINI_MODEL} …")
        summarize(client, articles)
        front = edit_front_page(client, articles, sections)
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
        "text": shorten(to_text(lead["content_html"], paragraphs=True), 1400),
        "takeaway": lead["takeaway"],
    }

    edition = {
        "version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ai": bool(front),
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
                "feeds": [feed_status[f["url"]] for f in feed_list(s)],
            }
            for s in sections
        ],
        "lead": {"article": lead["id"], **{k: lead_text.get(k, "") for k in ("headline", "subline", "text", "takeaway")}},
        "top": top,
        "briefing": build_briefing(sections, articles, front and front.get("briefing")),
        "articles": articles,
    }

    with open(EDITION_FILE, "w", encoding="utf-8") as f:
        json.dump(edition, f, ensure_ascii=False, indent=1)
    write_opml(config, feed_status)
    full = sum(a["full_text"] for a in articles)
    print(f"{EDITION_FILE} geschrieben: {len(articles)} Artikel, davon {full} mit Volltext.")


if __name__ == "__main__":
    run()
