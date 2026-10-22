import feedparser
import json
import os
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
                for entry in f.entries[:5]:
                    raw_data[cat].append({
                        "title": entry.get("title", ""),
                        "source": f.feed.get("title", url),
                        "link": entry.get("link", ""),
                        "summary": (entry.get("summary", "") or entry.get("description", ""))[:400]
                    })
            except Exception as e:
                print(f"Fehler bei {url}: {e}")

    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    
    prompt = f"""
    Du bist der Chefredakteur meiner persönlichen Tageszeitung. 
    Wähle aus den Rohdaten die wertvollsten Stücke aus: max. 2 für Geopolitik, 2 für AI, 2 für Medical AI, je 2 für Sport.
    Wähle außerdem 1 herausragendes Stück als 'deep_dive' (Leitartikel mit starkem Lern- und Analysewert).
    
    Formatiere die Antwort EXAKT als JSON mit folgendem Schema:
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
    
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config={"response_mime_type": "application/json"}
    )
    
    with open("edition.json", "w", encoding="utf-8") as f:
        f.write(response.text)
    print("edition.json erfolgreich erstellt.")

if __name__ == "__main__":
    run()
