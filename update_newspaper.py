import feedparser
import json
import os
import time
from google import genai

FEEDS = {
    "geopolitik": [
        "https://sicherheitspod.de/feed/mp3/",
        "https://warontherocks.com/feed/",
        "https://www.swp-berlin.org/rss.xml",
        "https://zeitschrift-ip.dgap.org/de/rss.xml",
    ],
    "ai": [
        "https://anchor.fm/s/f7cac464/podcast/rss",
        "https://www.oneusefulthing.org/feed",
        "https://www.heise.de/thema/Kuenstliche-Intelligenz.xml"
    ],
    "med_ai": [
        "https://www.thelancet.com/rssfeed/landig_current.xml",
        "https://www.kma-online.de/rss/aktuelle-meldungen.xml",
        "https://www.e-health-com.de/rss.xml",
    ],
    "rapid": [
        "https://news.google.com/rss/search?q=%22SK+Rapid%22+OR+%22Rapid+Wien%22&hl=de-AT&gl=AT&ceid=AT:de"
    ],
    "darts": [
        "https://www.dartsnews.de/feed",
        "https://www.dartn.de/rss.xml"
    ]
}

def run():
    raw_data = {}
    for cat, urls in FEEDS.items():
        raw_data[cat] = []
        for url in urls:
            try:
                f = feedparser.parse(url)
                for entry in f.entries[:2]:
                    raw_data[cat].append({
                        "title": entry.get("title", ""),
                        "source": f.feed.get("title", url),
                        "link": entry.get("link", ""),
                        "summary": (entry.get("summary", "") or entry.get("description", ""))[:250]
                    })
            except Exception as e:
                print(f"Fehler bei {url}: {e}")

    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    
    prompt = f"""
    Du bist der Chefredakteur meiner persönlichen Tageszeitung. Wähle aus den Rohdaten die wertvollsten Stücke aus: max. 2 pro Kategorie. Wähle 1 herausragendes Stück als 'deep_dive' (Leitartikel).
    
    Antworte AUSSCHLIESSLICH im folgenden JSON-Format ohne Markdown-Blöcke oder zusätzlichen Text:
    {{
      "deep_dive": {{
        "title": "...",
        "summary": "...",
        "takeaway": "..."
      }},
      "geopolitik": [{{"title": "...", "source": "...", "link": "...", "summary": "...", "takeaway": "..."}}],
      "ai": [{{"title": "...", "source": "...", "link": "...", "summary": "...", "takeaway": "..."}}],
      "med_ai": [{{"title": "...", "source": "...", "link": "...", "summary": "...", "takeaway": "..."}}],
      "rapid": [{{"title": "...", "source": "...", "link": "...", "summary": "...", "takeaway": "..."}}],
      "darts": [{{"title": "...", "source": "...", "link": "...", "summary": "...", "takeaway": "..."}}]
    }}

    Rohdaten:
    {json.dumps(raw_data, ensure_ascii=False)}
    """
    
    # Warteschleife: Versuche es bis zu 3 Mal, falls der Server überlastet ist
    max_retries = 3
    for attempt in range(max_retries):
        try:
            print(f"Sende Anfrage an Gemini (Versuch {attempt + 1}/{max_retries})...")
            response = client.models.generate_content(
                model="gemini-3.8-flash",
                contents=prompt
            )
            break  # Erfolgreich! Schleife beenden.
        except Exception as e:
            if "503" in str(e) or "UNAVAILABLE" in str(e):
                if attempt < max_retries - 1:
                    print("Server ist beschäftigt. Warte 30 Sekunden...")
                    time.sleep(30)
                else:
                    print("Server dauerhaft überlastet. Abbruch.")
                    raise e
            else:
                raise e # Ein anderer Fehler, direkter Abbruch
    
    raw_text = response.text.strip()
    if raw_text.startswith("```json"):
        raw_text = raw_text[7:]
    elif raw_text.startswith("```"):
        raw_text = raw_text[3:]
    if raw_text.endswith("```"):
        raw_text = raw_text[:-3]
    raw_text = raw_text.strip()

    json.loads(raw_text)

    with open("edition.json", "w", encoding="utf-8") as f:
        f.write(raw_text)
    print("edition.json erfolgreich erstellt.")

if __name__ == "__main__":
    run()
