#!/usr/bin/env python3
"""Snapshot the TSPDT "1,000 Greatest Films" Letterboxd list into tspdt.json.

The list changes once a year, so this is run by hand (not by the weekly
refresh). Each film's Letterboxd page gives its TMDB id, which is how the site
matches streaming titles to the list; a film's rank is its position in "tmdb"
plus one (null where Letterboxd has no TMDB id).

Usage: python3 fetch_tspdt.py [list URL]
"""

import json
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

LIST_URL = "https://letterboxd.com/tspdtfanaccount/list/the-1000-greatest-films-21st-edition-2026/"
EDITION = "21st edition (2026)"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
TMDB_ID_RE = re.compile(r'data-tmdb-id="(\d+)"')


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def tmdb_id(slug):
    for _ in range(3):
        try:
            m = TMDB_ID_RE.search(fetch(f"https://letterboxd.com/film/{slug}/"))
            return int(m.group(1)) if m else None
        except Exception:
            continue
    return None


def main():
    base = (sys.argv[1] if len(sys.argv) > 1 else LIST_URL).rstrip("/") + "/"
    slugs, page = [], 1
    while True:
        html = fetch(base if page == 1 else f"{base}page/{page}/")
        found = re.findall(r'data-item-slug="([^"]+)"', html)
        if not found:
            break
        slugs.extend(s for s in found if s not in slugs)
        page += 1
    if len(slugs) < 1000:
        sys.exit(f"Only found {len(slugs)} films; not overwriting tspdt.json")
    with ThreadPoolExecutor(6) as pool:
        ids = list(pool.map(tmdb_id, slugs))
    missing = [s for s, i in zip(slugs, ids) if i is None]
    if len(missing) > 50:
        sys.exit(f"{len(missing)} films without a TMDB id; not overwriting tspdt.json")
    with open("tspdt.json", "w", encoding="utf-8") as f:
        json.dump({"edition": EDITION, "source": base, "tmdb": ids, "slugs": slugs}, f, indent=0)
    print(len(slugs), "films written to tspdt.json;", len(missing), "without a TMDB id:", missing)


if __name__ == "__main__":
    main()
