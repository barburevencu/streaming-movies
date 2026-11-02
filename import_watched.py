#!/usr/bin/env python3
"""Turn a Letterboxd data export into watched.json, for the site's Watched pill.

Export from letterboxd.com → Settings → Data → Export your data, then point this
at the downloaded zip (or its watched.csv). Each film is matched to its TMDB id
by title and year through the TMDB search API; matches are kept in watched.json,
so a re-import only looks up films logged since.

With --rss it instead adds the films in the user's diary feed (the latest 50
entries, which carry their TMDB ids) to watched.json. The weekly refresh does
this, so the export is only needed once, for the backlog. Films marked watched
without a diary entry never reach the feed; a fresh export picks those up.

Films listed in not_seen.csv (by Letterboxd URI) are left out of an import.

Usage: python3 import_watched.py ~/Downloads/letterboxd-USER-....zip
       python3 import_watched.py --rss [username]
"""

import csv
import datetime as dt
import io
import json
import re
import sys
import unicodedata
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fetch

HERE = Path(__file__).resolve().parent
USER = "barburevencu"


def fold(s):
    s = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in s if c.isalnum()).lower()


def read_rows(path):
    p = Path(path).expanduser()
    if p.suffix == ".zip":
        with zipfile.ZipFile(p) as z:
            text = z.read("watched.csv").decode("utf-8-sig")
    else:
        text = (p / "watched.csv" if p.is_dir() else p).read_text(encoding="utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def match(row):
    """TMDB id for a Letterboxd title and year: an exact title wins over the top hit."""
    name, year = row["Name"], row.get("Year") or ""
    search = lambda query, **params: (fetch.tmdb_get("/search/movie", query=query, include_adult="false", **params) or {}).get("results", [])
    near = lambda m: year and (m.get("release_date") or "")[:4] and abs(int(m["release_date"][:4]) - int(year)) <= 1
    try:
        # Letterboxd and TMDB often date a film a year apart, so look a year either side; among films
        # with the exact title from then, the most voted wins (an obscure namesake can sit in the exact year).
        found = {}
        for params in ([{"primary_release_year": int(year) + d} for d in (0, -1, 1)] if year else []) + [{}]:
            for m in search(name, **params):
                found.setdefault(m["id"], m)
        exact = [m for m in found.values() if fold(name) in (fold(m.get("title")), fold(m.get("original_title")))]
        if best := [m for m in exact if near(m)] or (exact if not year else []):
            return max(best, key=lambda m: m.get("vote_count") or 0)["id"]
        for params in ([{"primary_release_year": year}] if year else []) + [{}]:
            if res := search(name, **params):
                return res[0]["id"]
        # Long titles are punctuated differently across sites, so the part before a comma or colon is a last
        # try; only a title that starts with it, from within a year (sites disagree on those too), counts.
        if not (re.search(r"[,:]", name) and year):
            return None
        short = re.split(r"[,:]", name)[0]
        for m in search(short):
            y = (m.get("release_date") or "")[:4]
            if y and abs(int(y) - int(year)) <= 1 and any(fold(t).startswith(fold(short)) for t in (m.get("title"), m.get("original_title"))):
                return m["id"]
    except RuntimeError:
        pass
    return None


def save(films):
    fetch.write_json("watched.json", {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "films": films}, indent=0)


def from_rss(user):
    """Add the diary feed's films to watched.json, keyed rss:<slug>."""
    req = urllib.request.Request(f"https://letterboxd.com/{user}/rss/",
                                 headers={"User-Agent": "streaming-movies (github.com/barburevencu/streaming-movies)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        xml = resp.read().decode("utf-8", "replace")
    out = HERE / "watched.json"
    films = json.loads(out.read_text())["films"] if out.exists() else {}
    ids = set(films.values())
    new = 0
    for item in re.findall(r"<item>(.*?)</item>", xml, re.S):
        slug = re.search(r"<link>[^<]*/film/([^/<]+)/", item)
        mid = re.search(r"<tmdb:movieId>(\d+)</tmdb:movieId>", item)
        if slug and mid:  # list entries have neither
            new += int(mid.group(1)) not in ids
            ids.add(int(mid.group(1)))
            films.setdefault("rss:" + slug.group(1), int(mid.group(1)))
    save(films)
    print(f"{new} films added to watched.json from {user}'s diary feed; {len(ids - {None})} watched in all")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    if sys.argv[1] == "--rss":
        return from_rss(sys.argv[2] if len(sys.argv) > 2 else USER)
    rows = read_rows(sys.argv[1])
    # not_seen.csv: films marked watched on Letterboxd that the site should still count as unseen.
    skip = HERE / "not_seen.csv"
    if skip.exists():
        skip = {r["Letterboxd URI"] for r in csv.DictReader(skip.open(encoding="utf-8"))}
        rows = [r for r in rows if r["Letterboxd URI"] not in skip]
    if not rows:
        sys.exit("No films in watched.csv; not overwriting watched.json")
    fetch.KEY = fetch.load_key()

    out = HERE / "watched.json"
    known = json.loads(out.read_text())["films"] if out.exists() else {}
    todo = [r for r in rows if known.get(r["Letterboxd URI"]) is None]
    print(f"  {len(todo)} new of {len(rows)} watched films")
    with ThreadPoolExecutor(4) as pool:
        for r, mid in zip(todo, pool.map(match, todo)):
            known[r["Letterboxd URI"]] = mid

    films = {r["Letterboxd URI"]: known.get(r["Letterboxd URI"]) for r in rows}
    save(films)
    missing = [r["Name"] for r in rows if films[r["Letterboxd URI"]] is None]
    print(f"{len(films)} watched films written to watched.json; {len(missing)} without a TMDB match: {missing[:20]}")


if __name__ == "__main__":
    main()
