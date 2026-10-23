# Temporär: Quellen für einen Themenkatalog prüfen (wird wieder entfernt)
import json, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import update_newspaper as u

FEEDS = """
https://rss.orf.at/news.xml
https://www.derstandard.at/rss/inland
https://www.diepresse.com
https://www.kleinezeitung.at
https://kurier.at
https://www.profil.at
https://www.derstandard.at/rss/international
https://www.tagesschau.de/ausland/index~rss2.xml
https://rss.dw.com/xml/rss-de-all
https://newsfeed.zeit.de/politik/ausland/index
https://www.tagesschau.de/inland/index~rss2.xml
https://www.spiegel.de/schlagzeilen/index.rss
https://newsfeed.zeit.de/index
https://www.deutschlandfunk.de/nachrichten-100.rss
https://www.srf.ch/news/bnf/rss/1646
https://www.nzz.ch/recent.rss
https://www.derstandard.at/rss/wirtschaft
https://www.tagesschau.de/wirtschaft/index~rss2.xml
https://www.handelsblatt.com/contentexport/feed/schlagzeilen
https://www.heise.de/rss/heise-atom.xml
https://rss.golem.de/rss.php?feed=RSS2.0
https://t3n.de/rss.xml
https://www.derstandard.at/rss/web
https://the-decoder.de/feed/
https://www.scinexx.de/feed/
https://www.derstandard.at/rss/gesundheit
https://www.aerzteblatt.de/rss/news.asp
https://www.klimareporter.de
https://www.derstandard.at/rss/klimaschutz
https://www.tagesschau.de/wissen/klima/index~rss2.xml
https://www.derstandard.at/rss/kultur
https://www.deutschlandfunkkultur.de/kulturnachrichten-100.rss
https://www.perlentaucher.de
https://rss.orf.at/sport.xml
https://newsfeed.kicker.de/news/aktuell
https://newsfeed.kicker.de/news/fussball
https://www.kicker.de
https://www.sportschau.de
https://www.gamestar.de/news/rss/news.rss
https://www.derstandard.at/rss/games
https://www.pcgames.de
https://www.filmstarts.de
https://www.derstandard.at/rss/etat
https://rss.orf.at/noe.xml
https://rss.orf.at/ooe.xml
https://rss.orf.at/steiermark.xml
https://rss.orf.at/kaernten.xml
https://rss.orf.at/salzburg.xml
https://rss.orf.at/tirol.xml
https://rss.orf.at/vorarlberg.xml
https://rss.orf.at/burgenland.xml
https://dietagespresse.com/feed/
https://www.der-postillon.com/feeds/posts/default?alt=rss
https://www.derstandard.at/rss/lifestyle
https://www.derstandard.at/rss/reisen
https://www.derstandard.at/rss/immobilien
https://www.derstandard.at/rss/wohnen
https://www.derstandard.at/rss/karriere
https://www.derstandard.at/rss/panorama
https://www.derstandard.at/rss/sport
https://www.derstandard.at/rss/wien
https://www.watson.ch/api/1.0/rss/index.xml
https://www.heise.de/thema/Gesundheit.xml
""".split()

now = datetime.now(timezone.utc)

def check(url):
    parsed, err = u.fetch_feed({"url": url})
    if err:
        return f"✗ {url}: {err[:100]}"
    entries = parsed.entries
    dates = [d for d in (u.entry_date(e) for e in entries) if d]
    fresh = sum(1 for d in dates if (now - d).days < 4)
    lens = sorted(len(u.to_text(u.entry_html(e))) for e in entries)
    med = lens[len(lens) // 2] if lens else 0
    link = parsed.get("href", "")
    return f"{'✓' if fresh else '~'} {url}  [{parsed.feed.get('title', '')[:45]}] {len(entries)} Einträge, {fresh} < 4 Tage, Text-Median {med}, Feed {link[:90]}"

with ThreadPoolExecutor(max_workers=12) as pool:
    for line in pool.map(check, FEEDS):
        print(line)

def cors(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Origin": "https://example.github.io"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
            print(f"CORS {url}: ACAO={r.headers.get('Access-Control-Allow-Origin')} ({len(body)} B)")
            return json.loads(body)
    except Exception as e:
        print(f"CORS {url}: ✗ {e}")

geo = cors("https://geocoding-api.open-meteo.com/v1/search?name=Graz&count=3&language=de&format=json")
if geo:
    print("  Geo:", [(r["name"], r.get("admin1"), r.get("country"), r["latitude"], r["longitude"]) for r in geo.get("results", [])])
for league in ["aut.1", "ger.1", "ger.2", "eng.1", "esp.1", "ita.1", "fra.1", "sui.1", "ned.1", "aut.2"]:
    data = cors(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/teams")
    if data:
        lg = data["sports"][0]["leagues"][0]
        teams = [(t["team"]["id"], t["team"]["displayName"]) for t in lg["teams"]]
        print(f"  {league} {lg.get('name')}: {len(teams)} Teams: {teams}")
st = cors("https://site.web.api.espn.com/apis/v2/sports/soccer/ger.1/standings")
if st:
    groups = st.get("children") or [st]
    print("  ger.1 Tabelle:", [(g.get("name"), len(g["standings"]["entries"])) for g in groups])
cors("https://api.jolpi.ca/ergast/f1/current.json")
