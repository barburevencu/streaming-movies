#!/usr/bin/env python3
"""Collect every movie streaming (by subscription) on our services into data.json.

Availability comes from TMDB's watch providers (powered by JustWatch); film
details (director, country, runtime, genres) from TMDB and are cached in
cache.json, so a refresh only looks up films it hasn't seen before.
state.json remembers when each film first appeared on each service and
region, which gives the site its "New" view and the list of titles that left.

The TMDB key (v3 api key or v4 read token) comes from the TMDB_API_KEY
environment variable or a .tmdb_key file next to this script.

Usage: python3 fetch.py [--only Criterion ...]
"""

import argparse
import http.client
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
SIX = ["RO", "US", "GB", "IT", "DE", "FR"]

# Service -> (names TMDB may list it under, regions to check). Order is the
# order of the site's Services pills.
SERVICES = {
    "Criterion": (["Criterion Channel"], ["US"]),
    "Mubi": (["MUBI"], SIX),
    "Kanopy": (["Kanopy"], ["US"]),
    "HBO Max": (["HBO Max", "Max"], ["RO"]),
    "Apple TV": (["Apple TV", "Apple TV Plus", "Apple TV+"], ["US"]),  # the subscription; the store is "Apple TV Store"
    "Netflix": (["Netflix"], SIX),
    "Disney": (["Disney Plus", "Disney+"], SIX),
}

TMDB_API = "https://api.themoviedb.org/3"
POSTER = "https://image.tmdb.org/t/p/w185"
COUNTRY_SHORT = {"United States of America": "USA", "United Kingdom": "UK",
                 "Soviet Union": "USSR", "Union of Soviet Socialist Republics": "USSR"}


def load_key():
    key = os.environ.get("TMDB_API_KEY")
    if not key and (HERE / ".tmdb_key").exists():
        key = (HERE / ".tmdb_key").read_text().strip()
    if not key:
        sys.exit("No TMDB key: set TMDB_API_KEY or create .tmdb_key next to this script.")
    return key


KEY = ""


def tmdb_get(path, **params):
    """GET a TMDB endpoint, retrying rate limits and network errors."""
    headers = {"Accept": "application/json"}
    if len(KEY) > 40:
        headers["Authorization"] = f"Bearer {KEY}"
    else:
        params["api_key"] = KEY
    url = f"{TMDB_API}{path}?{urllib.parse.urlencode(params)}"
    for attempt in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code != 429 and e.code < 500:
                raise
        except (OSError, http.client.HTTPException):  # dropped connections, timeouts
            pass
        time.sleep(2 ** attempt)
    raise RuntimeError(f"TMDB request kept failing: {path}")


def provider_id(region, names):
    wanted = {n.lower() for n in names}
    for p in tmdb_get("/watch/providers/movie", watch_region=region)["results"]:
        if p["provider_name"].lower() in wanted:
            return p["provider_id"]
    return None


def discover(pid, region):
    """TMDB ids of every movie on a provider's subscription tier in a region."""
    params = dict(watch_region=region, with_watch_providers=pid,
                  with_watch_monetization_types="flatrate", sort_by="popularity.desc",
                  include_adult="false")
    first = tmdb_get("/discover/movie", page=1, **params)
    pages = min(first["total_pages"], 500)
    results = list(first["results"])
    with ThreadPoolExecutor(8) as pool:
        for data in pool.map(lambda p: tmdb_get("/discover/movie", page=p, **params), range(2, pages + 1)):
            results.extend(data["results"])
    if first["total_pages"] > 500:
        print(f"  ! only the first 10,000 of {first['total_results']} titles", file=sys.stderr)
    return {m["id"]: m.get("popularity") or 0 for m in results}


def details(mid):
    """The film fields the site shows, from one TMDB call."""
    try:
        d = tmdb_get(f"/movie/{mid}", append_to_response="credits")
    except RuntimeError:
        return None  # left out of the cache, so the next run tries again
    if not d:
        return None
    directors = [c["name"] for c in (d.get("credits") or {}).get("crew", []) if c.get("job") == "Director"]
    countries = [COUNTRY_SHORT.get(c["name"], c["name"]) for c in d.get("production_countries", [])]
    return {
        "title": d.get("title") or "",
        "orig": d.get("original_title") or "",
        "lang": d.get("original_language") or "",
        "year": (d.get("release_date") or "")[:4],
        "director": ", ".join(directors[:3]),
        "country": countries[0] if countries else "",
        "runtime": d.get("runtime") or 0,
        "genres": ", ".join(g["name"] for g in d.get("genres", [])),
        "poster": POSTER + d["poster_path"] if d.get("poster_path") else "",
        "rating": round(d.get("vote_average") or 0, 1),
        "votes": d.get("vote_count") or 0,
    }


def read_json(name, default):
    p = HERE / name
    return json.loads(p.read_text()) if p.exists() else default


def write_json(name, obj, **kw):
    tmp = HERE / (name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, **kw))
    tmp.replace(HERE / name)


def main():
    global KEY
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append", choices=list(SERVICES), help="limit to these services")
    args = ap.parse_args()
    KEY = load_key()
    services = {k: v for k, v in SERVICES.items() if not args.only or k in args.only}
    today = dt.date.today().isoformat()

    # Availability: film id -> {service: [regions]}, plus popularity.
    where, pop, checked = {}, {}, []
    for service, (names, regions) in services.items():
        for region in regions:
            pid = provider_id(region, names)
            if pid is None:
                print(f"  {service} {region}: not on TMDB, skipped")
                continue
            found = discover(pid, region)
            checked.append(f"{service}|{region}")
            for mid, p in found.items():
                where.setdefault(mid, {}).setdefault(service, []).append(region)
                pop[mid] = max(pop.get(mid, 0), p)
            print(f"  {service} {region}: {len(found)} films")
    if not where:
        sys.exit("No films found; not overwriting data.json")

    # Details, cached by TMDB id.
    cache = read_json("cache.json", {})
    todo = [mid for mid in where if str(mid) not in cache]
    print(f"  details: {len(todo)} new of {len(where)}")
    with ThreadPoolExecutor(4) as pool:
        for i, (mid, d) in enumerate(zip(todo, pool.map(details, todo)), 1):
            if d:
                cache[str(mid)] = d
            if i % 500 == 0:  # keep progress if a long first run fails
                write_json("cache.json", cache)
    write_json("cache.json", cache)
    if missing := sum(str(mid) not in cache for mid in todo):
        print(f"  ! {missing} films without details this time; they're left out until the next run")

    # First/last seen per film, service and region. Only the services checked
    # in this run are compared, so --only never marks the others as gone.
    state = read_json("state.json", {})
    # A service and region seen for the first time is a baseline, not a week of new arrivals.
    known = {k.split("|", 1)[1] for k in state}
    current = set()
    for mid, svcs in where.items():
        for service, regions in svcs.items():
            for r in regions:
                k = f"{mid}|{service}|{r}"
                current.add(k)
                e = state.setdefault(k, {"first": today if f"{service}|{r}" in known else ""})
                e["last"] = today
    write_json("state.json", state, indent=0)

    films = []
    for mid, svcs in where.items():
        d = cache.get(str(mid))
        if not d:
            continue
        added = max(state[f"{mid}|{s}|{r}"]["first"] for s, rs in svcs.items() for r in rs)
        films.append({"id": mid, **d, "pop": round(pop[mid], 2), "added": added,
                      "lb": f"https://letterboxd.com/tmdb/{mid}/",
                      "where": {s: sorted(rs, key=lambda r: SIX.index(r) if r in SIX else 99)
                                for s, rs in svcs.items()}})

    # Titles that left a service in the past 30 days.
    month_ago = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    gone = []
    for k, e in state.items():
        mid, service, region = k.split("|")
        if f"{service}|{region}" in checked and k not in current and e["last"] >= month_ago:
            d = cache.get(mid)
            if d:
                gone.append({"id": int(mid), "title": d["title"], "year": d["year"], "service": service,
                             "region": region, "last": e["last"]})

    write_json("data.json", {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "services": [{"name": s, "regions": r} for s, (_, r) in services.items()],
        "films": films, "gone": gone,
    })
    print(f"{len(films)} films written to data.json")


if __name__ == "__main__":
    main()
