# Temporär: Formel-1-Quellen prüfen (wird wieder entfernt)
import json, urllib.request
from datetime import datetime, timezone
import update_newspaper as u

FEEDS = [
    "https://de.motorsport.com/rss/f1/news/",
    "https://www.motorsport-total.com/rss/rss_formel1.xml",
    "https://www.motorsport-total.com/formel-1",
    "https://www.formel1.de",
    "https://www.motorsport-magazin.com/rss/formel1.xml",
    "https://www.motorsport-magazin.com/formel1/",
    "https://www.auto-motor-und-sport.de/formel-1/",
    "https://newsfeed.kicker.de/news/formel1",
    "https://www.kicker.de/formel1/startseite",
    "https://www.laola1.at/de/red/motorsport/formel-1/",
    "https://www.sport1.de/motorsport/formel1",
    "https://www.spox.com/de/sport/formel1/",
    "https://www.sportschau.de/motorsport/formel-eins/index~rss2.xml",
    "https://www.sportschau.de/motorsport/formel1/",
    "https://news.google.com/rss/search?q=%22Formel+1%22&hl=de-AT&gl=AT&ceid=AT:de",
    "https://www.formula1.com/en/latest/all.xml",
    "https://www.the-race.com/feed/",
    "https://www.racefans.net/feed/",
    "https://www.autosport.com/rss/f1/news/",
    "https://www.planetf1.com/feed",
]

now = datetime.now(timezone.utc)
for url in FEEDS:
    parsed, err = u.fetch_feed({"url": url})
    if err:
        print(f"✗ {url}: {err[:120]}")
        continue
    entries = parsed.entries
    dates = [d for d in (u.entry_date(e) for e in entries) if d]
    newest = max(dates).isoformat()[:16] if dates else "-"
    fresh = sum(1 for d in dates if (now - d).days < 4)
    lens = sorted(len(u.to_text(u.entry_html(e))) for e in entries)
    med = lens[len(lens) // 2] if lens else 0
    title = parsed.feed.get("title", "")
    print(f"✓ {url}  [{title[:40]}] {len(entries)} Einträge, {fresh} < 4 Tage, neuester {newest}, Textlänge Median {med}")
    for e in entries[:2]:
        print("     ·", u.to_text(e.get("title", ""))[:90], "|", e.get("link", "")[:90])

def show(url, n=700):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Morgenblatt"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read().decode("utf-8", "replace")
        print(f"✓ {url} ({len(data)} B): {data[:n]}")
        return json.loads(data)
    except Exception as e:
        print(f"✗ {url}: {e}")

base = "https://api.jolpi.ca/ergast/f1/"
cal = show(base + "current.json", 300)
if cal:
    races = cal["MRData"]["RaceTable"]["Races"]
    print("Rennen:", len(races))
    for r in races:
        if r["date"] >= now.date().isoformat():
            print(json.dumps(r, ensure_ascii=False)[:1500]); break
    print("Namen:", [r["raceName"] for r in races])
ds = show(base + "current/driverstandings.json", 200)
if ds:
    lst = ds["MRData"]["StandingsTable"]["StandingsLists"]
    print("Fahrer-WM nach Runde", lst[0]["round"] if lst else None, json.dumps(lst[0]["DriverStandings"][:2], ensure_ascii=False)[:900] if lst else "leer")
cs = show(base + "current/constructorstandings.json", 200)
if cs:
    lst = cs["MRData"]["StandingsTable"]["StandingsLists"]
    print("Team-WM:", json.dumps(lst[0]["ConstructorStandings"][:2], ensure_ascii=False)[:600] if lst else "leer")
lr = show(base + "current/last/results.json", 200)
if lr:
    race = lr["MRData"]["RaceTable"]["Races"][0]
    print("Letztes Rennen:", race["raceName"], race["date"], json.dumps(race["Results"][:2], ensure_ascii=False)[:1200])
sp = show(base + "current/last/sprint.json", 300)
show("https://site.api.espn.com/apis/site/v2/sports/racing/f1/scoreboard", 400)
show("https://api.openf1.org/v1/meetings?year=2026", 300)
