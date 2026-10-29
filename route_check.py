#!/usr/bin/env python3
"""Route check: the best drive between two saved addresses (the hospital and home) given live
traffic, with every accident, breakdown and closure on the corridor listed.  Data: TomTom Routing API v1 + TomTom Traffic Incident Details API v5, plus
511NJ's official NJDOT / Turnpike Authority feed (no key needed).

    python route_check.py                hospital -> home; asks "leaving now or at a time?"
    python route_check.py back           home -> hospital
    python route_check.py --now          skip the question, leave now
    python route_check.py --at "tomorrow 7am"
    python route_check.py --pdf          also write reports\\route_...pdf and open it
    python route_check.py --map          also write reports\\route_...html (interactive map), open it
    python route_check.py --alts 3 --no-tolls --json

Needs TOMTOM_API_KEY and the two addresses (HOSPITAL_ADDRESS, HOME_ADDRESS, optional
HOSPITAL_LABEL, HOME_LABEL) in the .env next to this file or in the environment;
.env.example shows the lines and README.md explains the free key.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import textwrap
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, quote_plus

import requests
from dotenv import dotenv_values

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"
CACHE_DIR = HERE / "cache"
GEOCACHE = CACHE_DIR / "geocode.json"
LAST_RUN = CACHE_DIR / "last_run.json"
REPORT_DIR = HERE / "reports"

# ---- the trip: addresses come from .env / the environment / Streamlit secrets, never the code --
PLACE_VARS = {"hospital": ("HOSPITAL_LABEL", "HOSPITAL_ADDRESS", "Hospital"),
              "home": ("HOME_LABEL", "HOME_ADDRESS", "Home")}
DEFAULT_TRIP = ("hospital", "home")   # `back` on the command line reverses it
# ---------------------------------------------------------------------------------------------

BASE = "https://api.tomtom.com"
TIMEOUT = 25
ON_ROUTE_METERS = 120        # an incident this close to a route line counts as "on" that route
NEARBY_METERS = 1500         # "nearby" = within this of one of the routes (a road you might detour onto)
BBOX_PAD_DEG = 0.02          # ~2 km of padding around the routes when asking for incidents
MAX_BBOX_KM2 = 10_000        # TomTom's limit for the incidents bounding box
PER_ROUTE_MAX = 8            # TomTom items listed under each route before "... and N more"
M_TO_MI = 1 / 1609.344

ICON = {0: "Unknown", 1: "Accident", 2: "Fog", 3: "Dangerous conditions", 4: "Rain", 5: "Ice",
        6: "Jam", 7: "Lane closed", 8: "Road closed", 9: "Road works", 10: "Wind",
        11: "Flooding", 14: "Broken-down vehicle"}
ACCIDENT_ISSUES = {1, 3, 7, 8, 14}          # listed in their own section
CATEGORY_FILTER = "1,3,6,7,8,9,14"          # accidents, hazards, jams, closures, works, breakdowns
MAGNITUDE = {0: "unknown", 1: "minor", 2: "moderate", 3: "major", 4: "closure"}
SEVERITY = {"closure": 4, "major": 3, "moderate": 2, "minor": 1}
PROBABILITY = {"certain": "certain", "probable": "probable", "risk_of": "risk of",
               "improbable": "unlikely"}
INCIDENT_FIELDS = ("{incidents{type,geometry{type,coordinates},properties{id,iconCategory,"
                   "magnitudeOfDelay,events{description,code},startTime,endTime,from,to,length,"
                   "delay,roadNumbers,timeValidity,probabilityOfOccurrence,numberOfReports,"
                   "lastReportTime}}}")
CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"

NJ511_FEED = "https://511nj.org/client/rest/rss/RSSAllNJActiveEvents"
NJ511_ISSUE_WORDS = ("crash", "accident", "disabled", "fire", "overturn", "jackknif", "debris",
                     "pedestrian", "police", "emergency", "obstruction", "hazard", "closure",
                     "closed", "assist", "incident", "spill", "animal")
DIR_WORDS = {"northbound": "N", "southbound": "S", "eastbound": "E", "westbound": "W"}
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july",
                                      "august", "september", "october", "november", "december"], 1)}
DAYNAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_DAY = r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
_MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
DATE_RE = re.compile(rf"\b({_MONTH})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})", re.I)
WINDOW_RE = re.compile(rf"(\d{{1,2}}):(\d{{2}})\s*([AP]M)\s+thru\s+(?:{_DAY}\s+)?"
                       rf"(?:{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}},?\s+)?"
                       r"(\d{1,2}):(\d{2})\s*([AP]M)", re.I)
DAYLIST_RE = re.compile(rf"({_DAY}(?:/{_DAY})+)", re.I)
SCHED_START_RE = re.compile(rf",?\s*(?:{_DAY}\s+)?{_MONTH}\s+\d{{1,2}}", re.I)


class ApiError(Exception):
    pass


class ConfigError(Exception):
    """A required setting (an address) is missing."""


# ---- small helpers ---------------------------------------------------------------------------

def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def api_get(url: str, params: dict) -> dict:
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT,
                         headers={"Accept-Encoding": "gzip"})
    except requests.ConnectionError:
        raise ApiError("No internet connection, or api.tomtom.com is unreachable.")
    except requests.Timeout:
        raise ApiError("TomTom did not answer within %d seconds. Try again." % TIMEOUT)
    if r.status_code == 403:
        raise ApiError("TomTom refused the API key (HTTP 403). Either the key in .env is wrong, "
                       "a brand-new key is not active yet (give it a few minutes), the key is "
                       "missing the Routing / Search / Traffic products, or this month's free "
                       "allowance is used up.")
    if r.status_code == 429:
        raise ApiError("TomTom rate limit hit (HTTP 429). Wait a minute and try again.")
    if r.status_code >= 400:
        detail = ""
        try:
            body = r.json()
            err = body.get("detailedError") or body.get("error") or {}
            detail = err.get("message") if isinstance(err, dict) else str(err)
        except ValueError:
            detail = r.text[:300]
        raise ApiError(f"TomTom returned HTTP {r.status_code} for {url.split(BASE)[-1]}: {detail}")
    try:
        return r.json()
    except ValueError:
        raise ApiError("TomTom returned something that is not JSON.")


def clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def day_and_clock(dt: datetime) -> str:
    return dt.strftime("%a %b %d").replace(" 0", " ") + "  " + clock(dt)


def minutes(seconds) -> str:
    m = int(round((seconds or 0) / 60))
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60:02d} min"


def plus_minutes(seconds) -> str:
    m = int(round((seconds or 0) / 60))
    return f"+{m} min"


def parse_iso(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---- departure time ---------------------------------------------------------------------------

DAY_WORDS = {"today": 0, "tod": 0, "tonight": 0, "tomorrow": 1, "tmrw": 1, "tmr": 1, "tom": 1}
WEEKDAYS = {"mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "weds": 2,
            "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "fri": 4, "friday": 4,
            "sat": 5, "saturday": 5, "sun": 6, "sunday": 6}
TIME_RE = re.compile(r"^(?P<h>\d{1,2})(?:[:.](?P<m>\d{2}))?(?P<ampm>am|pm|a|p)?$")


def parse_departure(text: str, now: datetime) -> datetime | None:
    """Turn 'now', '5:30pm', '17:30', 'tomorrow 7am', '7 am tomorrow', 'fri 6:15am' or an ISO
    stamp into a naive local datetime. `now` is naive local time. Returns None if unreadable."""
    t = " ".join(text.strip().lower().split())
    if t in ("", "now"):
        return now
    try:
        iso = datetime.fromisoformat(text.strip())
        return iso.astimezone().replace(tzinfo=None) if iso.tzinfo else iso
    except ValueError:
        pass
    tokens = [tok for tok in t.split() if tok not in ("at", "on")]
    day_word = None
    if tokens and (tokens[0] in DAY_WORDS or tokens[0] in WEEKDAYS):
        day_word = tokens.pop(0)
    elif tokens and (tokens[-1] in DAY_WORDS or tokens[-1] in WEEKDAYS):
        day_word = tokens.pop()
    m = TIME_RE.match("".join(tokens))          # "6:15 am" -> "6:15am"
    if not m:
        return None
    h, mi = int(m.group("h")), int(m.group("m") or 0)
    ampm = (m.group("ampm") or "")[:1]
    if ampm:
        if not 1 <= h <= 12 or mi > 59:
            return None
        h = h % 12 + (12 if ampm == "p" else 0)
    elif h > 23 or mi > 59:
        return None

    day_offset = None
    if day_word in DAY_WORDS:
        day_offset = DAY_WORDS[day_word]
    elif day_word in WEEKDAYS:
        day_offset = (WEEKDAYS[day_word] - now.weekday()) % 7

    cand = now.replace(hour=h, minute=mi, second=0, microsecond=0)
    if day_offset:
        return cand + timedelta(days=day_offset)
    if cand > now:
        return cand
    # The time has already passed today.
    if not ampm and h < 12 and cand + timedelta(hours=12) > now:
        return cand + timedelta(hours=12)            # "5:30" typed at 2 PM means 5:30 PM
    if day_word in WEEKDAYS:
        return cand + timedelta(days=7)              # "tue 7am" on a Tuesday evening = next week
    return cand + timedelta(days=1)                  # a bare past time rolls to tomorrow


def ask_departure(now: datetime) -> datetime:
    prompt = ("Leaving now?  Press Enter for now, or type a time "
              "(5:30pm, 17:30, tomorrow 7am, fri 6:15am): ")
    for _ in range(2):
        try:
            answer = input(prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            return now
        dep = parse_departure(answer, now)
        if dep is not None:
            return dep
        print(f"  Could not read '{answer.strip()}'. Try something like 5:30pm or tomorrow 7am.")
    print("  Going with: now.")
    return now


# ---- geocoding -------------------------------------------------------------------------------

def geocode(key: str, query: str) -> dict:
    """Address -> lat/lon, cached forever in cache/geocode.json. Tries TomTom's geocode endpoint,
    then TomTom's search endpoint (a key may have only one of the two products), then the free
    US Census geocoder (no key at all)."""
    cache = load_json(GEOCACHE, {})
    if query in cache:
        return cache[query]
    entry, source, errors = None, "", []
    for name, url in (("TomTom geocode", f"{BASE}/search/2/geocode/{quote(query)}.json"),
                      ("TomTom search", f"{BASE}/search/2/search/{quote(query)}.json")):
        try:
            data = api_get(url, {"key": key, "limit": 1, "countrySet": "US"})
        except ApiError as e:
            errors.append(f"{name}: {str(e).split('.')[0]}")
            continue
        results = data.get("results") or []
        if results:
            best = results[0]
            entry = {"lat": best["position"]["lat"], "lon": best["position"]["lon"],
                     "address": (best.get("address") or {}).get("freeformAddress", query)}
            source = name
            break
        errors.append(f"{name}: no match")
    if entry is None:
        try:
            r = requests.get(CENSUS_URL, params={"address": query, "benchmark": "Public_AR_Current",
                                                 "format": "json"}, timeout=30)
            match = ((r.json().get("result") or {}).get("addressMatches") or [None])[0]
            if match:
                entry = {"lat": match["coordinates"]["y"], "lon": match["coordinates"]["x"],
                         "address": match["matchedAddress"].title()}
                source = "US Census geocoder"
            else:
                errors.append("US Census: no match")
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            errors.append(f"US Census: {e}")
    if entry is None:
        raise ApiError(f"Could not find coordinates for '{query}'. Check HOSPITAL_ADDRESS / HOME_ADDRESS in .env. "
                       + "; ".join(errors))
    cache[query] = entry
    save_json(GEOCACHE, cache)
    print(f"  Address check ({source}): '{query}' -> {entry['address']}  "
          f"({entry['lat']:.5f}, {entry['lon']:.5f})")
    return entry


# ---- geometry --------------------------------------------------------------------------------

def _seg_dist2(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return (px - ax) ** 2 + (py - ay) ** 2
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    return (px - (ax + t * dx)) ** 2 + (py - (ay + t * dy)) ** 2


class RouteIndex:
    """Grid-hashed polyline so 'how far is this point from the route?' is cheap."""

    def __init__(self, pts: list[tuple[float, float]], cell: float = 1500.0):
        self.cell = cell
        self.cl = math.cos(math.radians(pts[0][0])) * 111_320.0
        self.cy = 110_540.0
        self.xy = [(lon * self.cl, lat * self.cy) for lat, lon in pts]
        self.grid: dict[tuple[int, int], list[int]] = {}
        for i in range(len(self.xy) - 1):
            (x1, y1), (x2, y2) = self.xy[i], self.xy[i + 1]
            for cx in range(int(min(x1, x2) // cell), int(max(x1, x2) // cell) + 1):
                for cy in range(int(min(y1, y2) // cell), int(max(y1, y2) // cell) + 1):
                    self.grid.setdefault((cx, cy), []).append(i)

    def nearest(self, lat: float, lon: float) -> tuple[float, int]:
        """(distance in metres, index of the nearest route segment); inf if beyond ~1 cell."""
        x, y = lon * self.cl, lat * self.cy
        cx, cy = int(x // self.cell), int(y // self.cell)
        best, best_i = math.inf, 0
        for ox in (-1, 0, 1):
            for oy in (-1, 0, 1):
                for i in self.grid.get((cx + ox, cy + oy), ()):
                    d2 = _seg_dist2(x, y, *self.xy[i], *self.xy[i + 1])
                    if d2 < best:
                        best, best_i = d2, i
        return math.sqrt(best), best_i

    def distance_m(self, lat: float, lon: float) -> float:
        return self.nearest(lat, lon)[0]

    def bearing(self, i: int) -> float:
        (x1, y1), (x2, y2) = self.xy[i], self.xy[min(i + 1, len(self.xy) - 1)]
        return math.degrees(math.atan2(x2 - x1, y2 - y1)) % 360


def cumulative_m(pts: list[tuple[float, float]]) -> list[float]:
    cum = [0.0]
    cl = math.cos(math.radians(pts[0][0])) * 111_320.0
    for (lat1, lon1), (lat2, lon2) in zip(pts, pts[1:]):
        dx, dy = (lon2 - lon1) * cl, (lat2 - lat1) * 110_540.0
        cum.append(cum[-1] + math.hypot(dx, dy))
    return cum


def _bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    (lat1, lon1), (lat2, lon2) = a, b
    return math.degrees(math.atan2((lon2 - lon1) * math.cos(math.radians(lat1)), lat2 - lat1)) % 360


def _dir_suffix(name: str) -> str:
    m = re.search(r"\b([NSEW])\s*$", name or "")
    return m.group(1) if m else ""


# ---- routes ----------------------------------------------------------------------------------

def fetch_routes(key: str, o: dict, d: dict, depart_param: str, alts: int, no_tolls: bool) -> dict:
    params = {"key": key, "traffic": "true", "routeType": "fastest", "travelMode": "car",
              "maxAlternatives": alts, "computeTravelTimeFor": "all", "sectionType": "traffic",
              "instructionsType": "text", "departAt": depart_param}
    if no_tolls:
        params["avoid"] = "tollRoads"
    url = f"{BASE}/routing/1/calculateRoute/{o['lat']},{o['lon']}:{d['lat']},{d['lon']}/json"
    return api_get(url, params)


def road_name(ins: dict) -> str:
    nums = [str(n) for n in (ins.get("roadNumbers") or [])]
    if nums:                                           # "New Jersey Tpke S" beats "I-95 S"
        return next((n for n in nums if re.search(r"Tpke|Pkwy|Expy", n)), nums[0])
    return (ins.get("street") or "").strip()


def parse_route(raw: dict) -> dict:
    s = raw.get("summary") or {}
    pts = [(p["latitude"], p["longitude"])
           for leg in raw.get("legs", []) for p in leg.get("points", [])]
    if len(pts) < 2:
        raise ApiError("TomTom returned a route without geometry.")
    sections = [sec for sec in raw.get("sections", [])
                if str(sec.get("sectionType", "")).lower() == "traffic" or "delayInSeconds" in sec]
    instructions = (raw.get("guidance") or {}).get("instructions") or []
    r = {
        "length_m": s.get("lengthInMeters", 0),
        "travel_s": s.get("travelTimeInSeconds", 0),
        "delay_s": s.get("trafficDelayInSeconds", 0),
        "free_s": s.get("noTrafficTravelTimeInSeconds"),
        "pts": pts,
        "cum": cumulative_m(pts),
        "sections": sections,
        "instructions": instructions,
    }
    r["index"] = RouteIndex(pts)
    r["via"] = via_label(r)
    return r


def via_label(route: dict) -> str:
    instr = route["instructions"]
    total = route["length_m"] or route["cum"][-1]
    dist: dict[str, float] = {}
    order: list[str] = []
    for i, ins in enumerate(instr):
        start = ins.get("routeOffsetInMeters", 0)
        end = instr[i + 1].get("routeOffsetInMeters", total) if i + 1 < len(instr) else total
        name = road_name(ins)
        if not name:
            continue
        if name not in dist:
            order.append(name)
        dist[name] = dist.get(name, 0.0) + max(0.0, end - start)
    if not dist:
        return "route"
    top = sorted(dist, key=dist.get, reverse=True)[:3]
    keep = [n for n in order if n in top and dist[n] >= 0.08 * total] or [top[0]]
    return "via " + ", ".join(keep)


def road_at(route: dict, offset_m: float) -> str:
    name = ""
    for ins in route["instructions"]:
        if ins.get("routeOffsetInMeters", 0) <= offset_m:
            n = road_name(ins)
            if n:
                name = n
        else:
            break
    return name or "route"


def slow_stretches(route: dict) -> list[dict]:
    out = []
    for sec in route["sections"]:
        delay = sec.get("delayInSeconds") or 0
        if delay < 60:
            continue
        s, e = sec.get("startPointIndex", 0), sec.get("endPointIndex", 0)
        cum = route["cum"]
        start_m = cum[min(s, len(cum) - 1)]
        length_m = max(0.0, cum[min(e, len(cum) - 1)] - start_m)
        cat = (sec.get("simpleCategory") or "").replace("_", " ").lower()
        speed = sec.get("effectiveSpeedInKmh")
        extra = ", ".join(x for x in (cat, f"{round(speed * 0.621)} mph" if speed else "") if x)
        out.append({"road": road_at(route, start_m), "length_m": length_m, "delay_s": delay,
                    "mag": MAGNITUDE.get(sec.get("magnitudeOfDelay"), ""), "extra": extra,
                    "i0": s, "i1": e})
    return out


def stretch_line(s: dict) -> str:
    where = f"{s['road']}, {s['length_m'] * M_TO_MI:.1f} mi"
    extra = f"({s['extra']})" if s["extra"] else ""
    return f"{'Slow stretch':<22}{where:<46.44}{plus_minutes(s['delay_s']):>8}   {s['mag']:<9}{extra}"


# ---- incidents (TomTom) ----------------------------------------------------------------------

def fetch_incidents(key: str, routes: list[dict]) -> list[dict] | None:
    lats = [lat for r in routes for lat, _ in r["pts"]]
    lons = [lon for r in routes for _, lon in r["pts"]]
    min_lat, max_lat = min(lats) - BBOX_PAD_DEG, max(lats) + BBOX_PAD_DEG
    min_lon, max_lon = min(lons) - BBOX_PAD_DEG, max(lons) + BBOX_PAD_DEG
    mid = math.radians((min_lat + max_lat) / 2)
    area_km2 = ((max_lon - min_lon) * 111.32 * math.cos(mid)) * ((max_lat - min_lat) * 110.54)
    if area_km2 > MAX_BBOX_KM2:
        print(f"  (Skipping the incident list: the corridor is {area_km2:,.0f} km2, over TomTom's "
              f"{MAX_BBOX_KM2:,} km2 limit for one request.)")
        return None
    params = {"key": key, "bbox": f"{min_lon:.5f},{min_lat:.5f},{max_lon:.5f},{max_lat:.5f}",
              "fields": INCIDENT_FIELDS, "language": "en-US",
              "categoryFilter": CATEGORY_FILTER, "timeValidityFilter": "present,future"}
    data = api_get(f"{BASE}/traffic/services/5/incidentDetails", params)
    return data.get("incidents") or []


def incident_coords(inc: dict) -> list[tuple[float, float]]:
    g = inc.get("geometry") or {}
    coords = g.get("coordinates") or []
    if g.get("type") == "Point":
        coords = [coords]
    pts = [(c[1], c[0]) for c in coords if isinstance(c, (list, tuple)) and len(c) >= 2]
    if len(pts) > 40:                                  # long jams: sample, keep both ends
        step = len(pts) // 40 + 1
        pts = pts[::step] + [pts[-1]]
    return pts


def relation(pts: list[tuple[float, float]], roads: list[str], route: dict) -> tuple[str, float, int]:
    """How an incident sits relative to one route: 'on' (along it, same direction), 'opposite'
    (the other carriageway), 'cross' (a road that merely crosses or touches it), 'near'
    (within NEARBY_METERS) or 'far'. Returns (kind, nearest distance in m, nearest segment)."""
    near = [route["index"].nearest(lat, lon) for lat, lon in pts]
    k = min(range(len(pts)), key=lambda i: near[i][0])
    dmin, seg = near[k]
    if dmin > NEARBY_METERS:
        return "far", dmin, seg
    if dmin > ON_ROUTE_METERS:
        return "near", dmin, seg
    route_dir = _dir_suffix(road_at(route, route["cum"][seg]))
    inc_dirs = {_dir_suffix(r) for r in roads} - {""}
    if route_dir and inc_dirs and route_dir not in inc_dirs:
        return "opposite", dmin, seg                   # text says "US-9 N", route is going S
    if len(pts) == 1:
        return "on", dmin, seg
    frac = sum(d <= ON_ROUTE_METERS for d, _ in near) / len(near)
    a, b = pts[max(0, k - 1)], pts[min(len(pts) - 1, k + 1)]
    if frac < 0.5:
        return "cross", dmin, seg                      # most of it is somewhere else
    if a == b:
        return "on", dmin, seg
    diff = abs((_bearing(a, b) - route["index"].bearing(seg) + 180) % 360 - 180)
    if diff <= 60:
        return "on", dmin, seg
    if diff >= 120:
        return "opposite", dmin, seg
    return "cross", dmin, seg


def annotate(inc: dict, routes: list[dict], depart_utc: datetime, horizon: timedelta) -> dict | None:
    p = inc.get("properties") or {}
    start, end = parse_iso(p.get("startTime")), parse_iso(p.get("endTime"))
    if end and end < depart_utc:
        return None                                    # over before you leave
    if start and start > depart_utc + horizon:
        return None                                    # starts after you'd be there
    pts = incident_coords(inc)
    if not pts:
        return None
    roads_list = [str(x) for x in (p.get("roadNumbers") or [])]
    rels = {idx: relation(pts, roads_list, r) for idx, r in enumerate(routes)}
    on = {idx: d for idx, (kind, d, _) in rels.items() if kind == "on"}
    near_m = min(d for _, d, _ in rels.values())
    opposite = not on and any(kind == "opposite" for kind, _, _ in rels.values())
    cat = p.get("iconCategory", 0)
    events = p.get("events") or []
    desc = next((e.get("description") for e in events if e.get("description")), "")
    kind = ICON.get(cat, "Incident")
    note = desc.strip() if desc and desc.strip().lower() != kind.lower() else ""
    roads = "/".join(roads_list)
    if not roads and on:                               # borrow the road name from the route
        idx = min(on, key=on.get)
        roads = road_at(routes[idx], routes[idx]["cum"][rels[idx][2]])
        roads = "" if roads == "route" else roads
    frm, to = (p.get("from") or "").strip(), (p.get("to") or "").strip()
    if frm and to and frm != to:
        where = f"{frm} to {to}"
    else:
        where = frm or to or ""
    where = f"{roads}: {where}" if roads and where else (roads or where or "location unknown")
    return {
        "kind": kind, "note": note, "cat": cat, "where": where, "on": on,
        "near_m": near_m, "opposite": opposite,
        "delay_s": p.get("delay"), "mag": MAGNITUDE.get(p.get("magnitudeOfDelay"), ""),
        "prob": p.get("probabilityOfOccurrence") or "", "reports": p.get("numberOfReports"),
        "last": parse_iso(p.get("lastReportTime")), "start": start, "end": end,
        "future": (p.get("timeValidity") == "future") or bool(start and start > depart_utc),
        "length_m": p.get("length"), "accident_issue": cat in ACCIDENT_ISSUES,
        "pts": [[round(lat, 5), round(lon, 5)] for lat, lon in pts],
    }


def dedupe(items: list[dict]) -> list[dict]:
    """TomTom reports one incident per road segment, so the same lane closure can come back
    eight times. Merge entries that read the same, keeping the worst numbers."""
    seen: dict[tuple, dict] = {}
    for a in items:
        key = (a["cat"], a["kind"], a["where"], a["note"])
        prev = seen.get(key)
        if prev is None:
            seen[key] = a
            continue
        for idx, d in a["on"].items():
            prev["on"][idx] = min(d, prev["on"].get(idx, d))
        prev["near_m"] = min(prev["near_m"], a["near_m"])
        prev["opposite"] = prev["opposite"] and a["opposite"]
        prev["delay_s"] = max(prev["delay_s"] or 0, a["delay_s"] or 0) or None
        prev["reports"] = max(prev["reports"] or 0, a["reports"] or 0) or None
        if SEVERITY.get(a["mag"], 0) > SEVERITY.get(prev["mag"], 0):
            prev["mag"] = a["mag"]
        if a["last"] and (not prev["last"] or a["last"] > prev["last"]):
            prev["last"] = a["last"]
    return list(seen.values())


def incident_delay(a: dict) -> str:
    if a["cat"] == 8 and not a["delay_s"]:
        return "closed"
    return plus_minutes(a["delay_s"]) if a["delay_s"] else "-"


def incident_confidence(a: dict) -> str:
    prob = PROBABILITY.get(a["prob"], a["prob"])
    return prob + ("  POTENTIAL" if a["prob"] and a["prob"] != "certain" else "")


def incident_line(a: dict, tag: str) -> str:
    prob = PROBABILITY.get(a["prob"], a["prob"])
    potential = "  POTENTIAL" if a["prob"] and a["prob"] != "certain" else ""
    reports = f"{a['reports']} rpt" if a["reports"] else ""
    last = clock(a["last"].astimezone()) if a["last"] else ""
    sched = f"  starts {clock(a['start'].astimezone())}" if a["future"] and a["start"] else ""
    note = f"  [{a['note']}]" if a.get("note") else ""
    return (f"{tag:<10}{a['kind']:<22.20}{a['where']:<46.44}{incident_delay(a):>8}   {a['mag']:<9}"
            f"{prob:<9}{reports:<7}{last}{potential}{sched}{note}")


def route_tag(a: dict, best_idx: int) -> str:
    if not a["on"]:
        return "opp. dir" if a.get("opposite") else "nearby"
    idx = min(a["on"], key=a["on"].get)
    return "ON BEST" if idx == best_idx else f"ON ALT {idx}"


# ---- 511NJ (NJDOT's official feed: no key, RSS with coordinates) -----------------------------

def fetch_nj511() -> list[dict] | None:
    try:
        r = requests.get(NJ511_FEED, timeout=TIMEOUT, headers={"User-Agent": "route_check/1.0"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except (requests.RequestException, ET.ParseError) as e:
        print(f"  (511NJ feed unavailable right now: {str(e)[:80]})")
        return None
    out = []
    for it in root.iter("item"):
        pt = it.find("{http://www.georss.org/georss}point")
        if pt is None or not pt.text:
            continue
        try:
            lat, lon = (float(x) for x in pt.text.split())
        except ValueError:
            continue
        title = " ".join((it.findtext("title") or "").split())
        road, _, kind = title.partition(" : ")
        try:
            when = parsedate_to_datetime(it.findtext("pubDate") or "")
        except (TypeError, ValueError):
            when = None
        out.append({"road": road.strip() or title, "kind": kind.strip() or "Event", "lat": lat,
                    "lon": lon, "when": when,
                    "desc": " ".join((it.findtext("description") or "").split())})
    return out


def nj511_class(kind: str) -> str:
    k = kind.lower()
    if any(w in k for w in NJ511_ISSUE_WORDS):
        return "incident"
    if "delay" in k or "congestion" in k or "volume" in k:
        return "delay"
    return "work"                                      # construction, milling, striping, ...


def nj511_active(desc: str, depart: datetime) -> bool:
    """Best-effort read of the schedule NJDOT writes into the description ("Tuesday October 27th,
    2026 thru Friday October 30th, 2026, Tuesday/Wednesday/Thursday, 09:00 PM thru 06:00 AM").
    True = in effect at departure, or no schedule given (ongoing)."""
    low = desc.lower()
    dates = sorted((int(y), MONTHS[mo.lower()], int(d)) for mo, d, y in DATE_RE.findall(desc))
    if dates:
        if (depart.year, depart.month, depart.day) < dates[0]:
            return False
        if depart >= datetime(*dates[-1]) + timedelta(days=1):     # overnight windows end next morning
            return False
    m = WINDOW_RE.search(desc)
    if m and "continuous" not in low:
        def hm(h, mi, ap):
            return (int(h) % 12 + (12 if ap.upper() == "PM" else 0)) * 60 + int(mi)
        start, end = hm(m[1], m[2], m[3]), hm(m[4], m[5], m[6])
        t = depart.hour * 60 + depart.minute
        if not ((start <= t < end) if start <= end else (t >= start or t < end)):
            return False
        lists = DAYLIST_RE.findall(desc)               # the days the window starts on
        if lists:
            allowed = {x.lower() for x in lists[0].split("/")}
            day = depart - timedelta(days=1) if (start > end and t < end) else depart
            if DAYNAMES[day.weekday()] not in allowed:
                return False
    return True


def nj511_short(desc: str) -> str:
    """Work notices: the what/where before the schedule, plus the lane detail after it."""
    m = SCHED_START_RE.search(desc)
    if not m:
        return desc
    head = desc[:m.start()].rstrip(" ,")
    tail = ""
    w = list(WINDOW_RE.finditer(desc))
    if w:
        tail = desc[w[-1].end():].strip(" ,")
    out = f"{head}; {tail}" if tail else head
    return out if len(out) <= 240 else out[:237].rstrip() + "..."


def annotate_nj511(ev: dict, routes: list[dict], depart: datetime) -> dict | None:
    rels = {idx: r["index"].nearest(ev["lat"], ev["lon"]) for idx, r in enumerate(routes)}
    near_m = min(d for d, _ in rels.values())
    if near_m > NEARBY_METERS:
        return None
    ev_dir = next((v for k, v in DIR_WORDS.items() if k in ev["road"].lower()), "")
    on, opposite = {}, False
    for idx, (d, seg) in rels.items():
        if d > ON_ROUTE_METERS:
            continue
        route_dir = _dir_suffix(road_at(routes[idx], routes[idx]["cum"][seg]))
        if ev_dir and route_dir and ev_dir != route_dir:
            opposite = True
            continue
        on[idx] = d
    cls = nj511_class(ev["kind"])
    active = cls != "work" or nj511_active(ev["desc"], depart)
    ev["kind"] = ev["kind"][:1].upper() + ev["kind"][1:]
    ev.update(on=on, near_m=near_m, opposite=opposite and not on, cls=cls, active=active,
              accident_issue=cls == "incident",
              show_desc=nj511_short(ev["desc"]) if cls == "work" else ev["desc"])
    return ev


def nj511_lines(ev: dict, tag: str) -> list[str]:
    when = clock(ev["when"].astimezone()) if ev["when"] else ""
    head = (f"{tag:<10}{'511NJ ' + ev['kind']:<22.20}{ev['road']:<46.44}{'':>8}   "
            f"{'':<9}{'':<9}{'':<7}{when}")
    lines = textwrap.wrap(ev["show_desc"], 116)[: 1 if ev["cls"] == "work" else 2]
    return [head.rstrip()] + [f"{'':<10}  {line}" for line in lines]


# ---- the report: built once, printed and/or written to PDF -----------------------------------

def build_report(ctx: dict, routes: list[dict], order: list[int], best_idx: int,
                 incidents: list[dict], nj511: list[dict]) -> dict:
    best_t = routes[best_idx]["travel_s"]
    rep = dict(ctx, routes=[], best_idx=best_idx)
    for rank, idx in enumerate(order):
        r = routes[idx]
        items = sorted((a for a in incidents if idx in a["on"]),
                       key=lambda a: (not a["accident_issue"], -(a["delay_s"] or 0)))
        rep["routes"].append({
            "tag": "BEST" if rank == 0 else f"ALT {idx}", "via": r["via"],
            "travel_s": r["travel_s"], "delay_s": r["delay_s"], "length_m": r["length_m"],
            "arrive": ctx["depart"] + timedelta(seconds=r["travel_s"]),
            "delta_s": None if rank == 0 else r["travel_s"] - best_t,
            "official": [e for e in nj511 if idx in e["on"] and e["active"]],
            "items": items, "stretches": slow_stretches(r),
            "pts": [[round(lat, 5), round(lon, 5)] for lat, lon in r["pts"]],
        })
    kind_rank = {1: 0, 8: 1, 3: 2, 7: 3, 14: 4}       # accident, closure, hazard, lane, breakdown

    def sort_key(a):
        return (not a["on"], -SEVERITY.get(a["mag"], 0), kind_rank.get(a["cat"], 9),
                -(a["reports"] or 0), a["where"])

    rep["issues_on"] = sorted((a for a in incidents if a["accident_issue"] and a["on"]), key=sort_key)
    # Nearby: within a mile of a route. Planned ("probable") closures off-route are noise.
    rep["issues_near"] = sorted((a for a in incidents if a["accident_issue"] and not a["on"]
                                 and a["near_m"] <= NEARBY_METERS
                                 and (a["cat"] in (1, 3, 14) or a["prob"] == "certain")), key=sort_key)
    cls_rank = {"incident": 0, "delay": 1, "work": 2}
    rep["nj_on"] = sorted((e for e in nj511 if e["on"] and e["active"]),
                          key=lambda e: (cls_rank[e["cls"]], -(e["when"].timestamp() if e["when"] else 0)))
    rep["nj_near"] = sorted((e for e in nj511 if not e["on"] and e["accident_issue"]),
                            key=lambda e: (e["opposite"], e["near_m"]))
    sched = [e for e in nj511 if e["on"] and not e["active"]]
    rep["nj_sched_count"] = len(sched)
    rep["nj_sched_kinds"] = sorted({e["kind"].lower() for e in sched})
    rep["slow"] = sorted((a for a in incidents if not a["accident_issue"] and not a["on"]
                          and a["near_m"] <= NEARBY_METERS), key=lambda a: -(a["delay_s"] or 0))
    return rep


def print_report(rep: dict) -> None:
    best_idx = rep["best_idx"]
    for r in rep["routes"]:
        delta = "" if r["delta_s"] is None else f"   {plus_minutes(r['delta_s'])} vs best"
        print(f"{r['tag']:<7}{r['via']:<40.40}{minutes(r['travel_s']):>12}   "
              f"({plus_minutes(r['delay_s'])} traffic)   {r['length_m'] * M_TO_MI:5.1f} mi   "
              f"arrive {clock(r['arrive'])}{delta}")
        if not r["items"] and not r["stretches"] and not r["official"]:
            print("       on this route: nothing reported")
        else:
            print("       on this route:")
            for e in r["official"]:
                for line in nj511_lines(e, ""):
                    print("         " + line[10:])
            for a in r["items"][:PER_ROUTE_MAX]:
                print("         " + incident_line(a, "")[10:])
            if len(r["items"]) > PER_ROUTE_MAX:
                print(f"         ... and {len(r['items']) - PER_ROUTE_MAX} more minor slowdowns")
            for s in r["stretches"]:
                print("         " + stretch_line(s))
        print()

    if rep["have_incidents"]:
        on_issues, near_issues = rep["issues_on"], rep["issues_near"]
        print(f"ACCIDENT ISSUES   {len(on_issues)} on your routes, {len(near_issues)} within a mile of them")
        if not on_issues and not near_issues:
            print("  none reported right now")
        for a in on_issues + near_issues[:10]:
            print("  " + incident_line(a, route_tag(a, best_idx)))
        if len(near_issues) > 10:
            print(f"  ... and {len(near_issues) - 10} more nearby")
        print()

    if rep["have_nj511"]:
        nj_on, nj_near = rep["nj_on"], rep["nj_near"]
        print(f"511NJ OFFICIAL REPORTS (NJDOT / Turnpike Authority)   "
              f"{len(nj_on)} on your routes, {len(nj_near)} incidents within a mile")
        if not nj_on and not nj_near:
            print("  none on or near these routes")
        for e in nj_on + nj_near[:8]:
            for line in nj511_lines(e, route_tag(e, best_idx)):
                print("  " + line)
        if len(nj_near) > 8:
            print(f"  ... and {len(nj_near) - 8} more nearby")
        if rep["nj_sched_count"]:
            print(f"  Scheduled work on these routes outside your trip window, not shown: "
                  f"{rep['nj_sched_count']} ({', '.join(rep['nj_sched_kinds'][:6])})")
        print()

    if rep["have_incidents"] and rep["slow"]:
        slow = rep["slow"]
        print(f"Slowdowns within a mile of these routes, not on them: {len(slow)}   (worst 5)")
        for a in slow[:5]:
            print("  " + incident_line(a, route_tag(a, best_idx)))
        print()

    print(f"Open in Google Maps: {rep['gmaps']}")
    print(f"Open in Waze:        {rep['waze']}")


def write_pdf(rep: dict, path: Path) -> Path:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    ss = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=ss["BodyText"], fontName="Helvetica", fontSize=8, leading=10)
    bold = ParagraphStyle("bold", parent=body, fontName="Helvetica-Bold")
    small = ParagraphStyle("small", parent=body, fontSize=7, leading=9,
                           textColor=colors.HexColor("#4a5560"))
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontSize=15, leading=18, alignment=0, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=10.5, leading=13, spaceBefore=9,
                        spaceAfter=3, textColor=colors.HexColor("#1f3a5f"))

    def esc(s) -> str:
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def P(text, style=body):
        return Paragraph(esc(text), style)

    grid = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8edf3")),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#9aa7b5")),
        ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor("#d5dbe3")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ])

    def table(rows, widths, highlight_first=False):
        t = Table(rows, colWidths=[w * inch for w in widths], repeatRows=1)
        t.setStyle(grid)
        if highlight_first and len(rows) > 1:
            t.setStyle(TableStyle([("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#e6f4e6"))]))
        return t

    def header(labels):
        return [P(x, bold) for x in labels]

    def incident_row(a, tag=None):
        last = clock(a["last"].astimezone()) if a["last"] else ""
        sched = f"starts {clock(a['start'].astimezone())}" if a["future"] and a["start"] else ""
        note = "; ".join(x for x in (a.get("note", ""), sched) if x)
        cells = [P(a["kind"]), P(a["where"]), P(incident_delay(a)), P(a["mag"]),
                 P(incident_confidence(a).replace("  ", " / ")),
                 P(str(a["reports"]) if a["reports"] else ""), P(last), P(note, small)]
        return ([P(tag, bold)] if tag is not None else []) + cells

    def nj_row(e, tag=None):
        when = clock(e["when"].astimezone()) if e["when"] else ""
        cells = [P("511NJ " + e["kind"]), P(e["road"]), P(when), P(e["show_desc"], small)]
        return ([P(tag, bold)] if tag is not None else []) + cells

    story = [Paragraph(f"Route check: {esc(rep['origin'])} to {esc(rep['dest'])}", h1)]
    sub = f"Generated {day_and_clock(rep['now'])}.  Departure {day_and_clock(rep['depart'])}"
    sub += (" (future trip: TomTom's historic traffic model, not live conditions)."
            if rep["future"] else " (leaving now, live traffic).")
    story += [Paragraph(sub, small), Spacer(1, 6)]

    rows = [header(["Route", "Via", "Time", "Traffic delay", "Miles", "Arrive", "vs best"])]
    for r in rep["routes"]:
        rows.append([P(r["tag"], bold), P(r["via"]), P(minutes(r["travel_s"])),
                     P(plus_minutes(r["delay_s"])), P(f"{r['length_m'] * M_TO_MI:.1f}"),
                     P(clock(r["arrive"])),
                     P("" if r["delta_s"] is None else plus_minutes(r["delta_s"]))])
    story.append(table(rows, [0.7, 4.3, 1.0, 1.1, 0.7, 1.0, 1.0], highlight_first=True))

    inc_widths = [1.2, 2.9, 0.6, 0.7, 0.9, 0.55, 0.75, 2.4]
    for r in rep["routes"]:
        story.append(Paragraph(f"{esc(r['tag'])} - on this route ({esc(r['via'])})", h2))
        rows = [header(["Type", "Where", "Delay", "Severity", "Confidence", "Reports",
                        "Last report", "Note"])]
        for e in r["official"]:
            when = clock(e["when"].astimezone()) if e["when"] else ""
            rows.append([P("511NJ " + e["kind"]), P(e["road"]), P(""), P(""), P("official"),
                         P(""), P(when), P(e["show_desc"], small)])
        for a in r["items"][:PER_ROUTE_MAX]:
            rows.append(incident_row(a))
        if len(r["items"]) > PER_ROUTE_MAX:
            rows.append([P(""), P(f"... and {len(r['items']) - PER_ROUTE_MAX} more minor slowdowns", small)]
                        + [P("")] * 6)
        for s in r["stretches"]:
            rows.append([P("Slow stretch"), P(f"{s['road']}, {s['length_m'] * M_TO_MI:.1f} mi"),
                         P(plus_minutes(s["delay_s"])), P(s["mag"]), P(""), P(""), P(""),
                         P(s["extra"], small)])
        story.append(table(rows, inc_widths) if len(rows) > 1 else P("Nothing reported.", small))

    tag_widths = [0.75] + [1.1, 2.7, 0.6, 0.7, 0.9, 0.55, 0.7, 2.0]
    if rep["have_incidents"]:
        on_issues, near_issues = rep["issues_on"], rep["issues_near"]
        story.append(Paragraph(f"Accident issues: {len(on_issues)} on your routes, "
                               f"{len(near_issues)} within a mile of them", h2))
        rows = [header(["", "Type", "Where", "Delay", "Severity", "Confidence", "Reports",
                        "Last report", "Note"])]
        for a in on_issues + near_issues[:15]:
            rows.append(incident_row(a, route_tag(a, rep["best_idx"])))
        story.append(table(rows, tag_widths) if len(rows) > 1 else P("None reported right now.", small))
        if len(near_issues) > 15:
            story.append(P(f"... and {len(near_issues) - 15} more nearby", small))

    if rep["have_nj511"]:
        nj_on, nj_near = rep["nj_on"], rep["nj_near"]
        story.append(Paragraph(f"511NJ official reports (NJDOT / Turnpike Authority): "
                               f"{len(nj_on)} on your routes, {len(nj_near)} incidents within a mile", h2))
        rows = [header(["", "Type", "Road", "Reported", "Description"])]
        for e in nj_on + nj_near[:12]:
            rows.append(nj_row(e, route_tag(e, rep["best_idx"])))
        story.append(table(rows, [0.75, 1.3, 2.4, 0.75, 4.8]) if len(rows) > 1
                     else P("None on or near these routes.", small))
        if rep["nj_sched_count"]:
            story.append(P(f"Scheduled work on these routes outside your trip window, not shown: "
                           f"{rep['nj_sched_count']} ({', '.join(rep['nj_sched_kinds'][:6])}).", small))

    if rep["have_incidents"] and rep["slow"]:
        slow = rep["slow"]
        story.append(Paragraph(f"Slowdowns within a mile of these routes, not on them: "
                               f"{len(slow)} (worst 8)", h2))
        rows = [header(["", "Type", "Where", "Delay", "Severity", "Confidence", "Reports",
                        "Last report", "Note"])]
        for a in slow[:8]:
            rows.append(incident_row(a, route_tag(a, rep["best_idx"])))
        story.append(table(rows, tag_widths))

    story += [Spacer(1, 8),
              Paragraph(f'<a href="{esc(rep["gmaps"])}" color="#1a5fb4">Open this trip in Google Maps</a>'
                        f'   |   <a href="{esc(rep["waze"])}" color="#1a5fb4">Open the destination in Waze</a>',
                        body),
              Spacer(1, 4),
              Paragraph("POTENTIAL = TomTom rates it probable or at risk rather than confirmed. "
                        "opp. dir = the other carriageway of a road on your route. "
                        "Sources: TomTom Routing + Traffic Incident Details, 511NJ (NJDOT).", small)]

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(path), pagesize=landscape(letter), leftMargin=0.5 * inch,
                            rightMargin=0.5 * inch, topMargin=0.5 * inch, bottomMargin=0.5 * inch,
                            title=f"Route check {rep['origin']} to {rep['dest']}")
    doc.build(story)
    return path


# ---- interactive map: one self-contained HTML file (Leaflet + Esri tiles) ----------------------

MAP_KIND_COLOR = {"Accident": "#d93025", "Road closed": "#7b1fa2", "Lane closed": "#7b1fa2",
                  "Broken-down vehicle": "#f5a623", "Dangerous conditions": "#e8710a",
                  "Jam": "#5f7d95", "Road works": "#8a8f98"}
MAP_MAG_COLOR = {"minor": "#fbbc04", "moderate": "#f5a623", "major": "#d93025", "closure": "#7b1fa2"}
LEAFLET = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/"

MAP_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Route map</title>
<link rel="stylesheet" href="__LEAFLET__leaflet.min.css">
<style>
__CSS__
</style>
</head>
<body>
<div id="map"></div>
<aside id="panel" class="panel">
  <div class="panel-head">
    <div class="panel-title"><b id="p-title"></b><span id="p-sub"></span></div>
    <div class="nav" id="p-nav"></div>
    <button type="button" id="p-toggle" class="toggle" aria-expanded="true" aria-controls="p-body">Hide</button>
  </div>
  <div class="panel-body" id="p-body"></div>
</aside>
<script type="application/json" id="data">__DATA__</script>
<script src="__LEAFLET__leaflet.min.js"></script>
<script>
__JS__
</script>
</body>
</html>
"""

MAP_CSS = """
:root {
  --bg: #e9edf1; --panel: rgba(255, 255, 255, 0.97); --text: #1f2933; --muted: #5b6672;
  --border: #d5dbe3; --head: #e8edf3; --hi: #e3eefc; --link: #1a5fb4; --shadow: rgba(0, 0, 0, 0.18);
  --panel-h: 0px;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14171b; --panel: rgba(30, 34, 40, 0.97); --text: #e6e9ed; --muted: #9aa5b1;
    --border: #3a434d; --head: #262c34; --hi: #1f3654; --link: #8ab4f8; --shadow: rgba(0, 0, 0, 0.5);
  }
}
*, *::before, *::after { box-sizing: border-box; }
html, body { margin: 0; height: 100%; overflow: hidden; }
body { background: var(--bg); color: var(--text);
       font: 13px/1.4 system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif; }
#map { position: absolute; inset: 0; background: var(--bg); }
.rc-dark-tiles { filter: invert(100%) hue-rotate(180deg) brightness(95%) contrast(90%); }
.panel { position: absolute; z-index: 1000; top: 12px; left: 12px; width: 430px;
         max-width: calc(100vw - 24px); max-height: calc(100vh - 24px); display: flex;
         flex-direction: column; background: var(--panel); color: var(--text);
         border: 1px solid var(--border); border-radius: 10px; box-shadow: 0 4px 18px var(--shadow); }
.panel-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 8px; padding: 10px 12px;
              border-bottom: 1px solid var(--border); }
.panel.collapsed .panel-head { border-bottom: 0; }
.panel-title { flex: 1 1 100%; min-width: 0; }
.panel-title b { display: block; font-size: 14px; white-space: nowrap; overflow: hidden;
                 text-overflow: ellipsis; }
.panel-title span { display: block; color: var(--muted); font-size: 12px; white-space: nowrap;
                    overflow: hidden; text-overflow: ellipsis; }
.toggle { flex: none; font: inherit; font-size: 13px; font-weight: 600; min-height: 44px; padding: 0 12px;
          border-radius: 8px; border: 1px solid var(--border); background: var(--head); color: var(--text);
          cursor: pointer; }
.nav { flex: 1; min-width: 0; display: flex; gap: 8px; }
.nav a { display: flex; align-items: center; justify-content: center; min-height: 44px; padding: 0 12px;
         border-radius: 8px; text-decoration: none; white-space: nowrap; font-weight: 700; }
.nav a.waze { flex: 1 1 auto; font-size: 15px; background: #33ccff; color: #08202c;
              border: 1px solid #12a8dd; box-shadow: 0 1px 3px rgba(0, 0, 0, 0.25); }
.nav a.waze:active { background: #1cb8ee; }
.nav a.gmaps { flex: 0 1 auto; font-size: 13px; font-weight: 600; background: var(--head);
               color: var(--link); border: 1px solid var(--border); }
.panel-body { overflow: auto; padding: 8px 12px 12px; min-height: 0; }
.panel.collapsed .panel-body { display: none; }
.meta { color: var(--muted); font-size: 12px; margin: 0 0 8px; }
.meta div { margin: 1px 0; }
h3 { font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted);
     margin: 12px 0 4px; }
.tablewrap { overflow-x: auto; max-width: 100%; }
table.routes { border-collapse: collapse; width: 100%; font-size: 12px; }
.routes th, .routes td { text-align: left; padding: 4px 5px; border-bottom: 1px solid var(--border);
                         vertical-align: top; }
.routes th { background: var(--head); font-weight: 600; white-space: nowrap; }
.routes td.n { white-space: nowrap; }
.routes tr.route-row { cursor: pointer; }
.routes tr.route-row:hover td, .routes tr.route-row.active td { background: var(--hi); }
.routes td.tag { font-weight: 700; white-space: nowrap; }
.hint { color: var(--muted); font-size: 11px; margin-top: 3px; }
.counts { margin: 0; padding-left: 16px; }
.legend { display: grid; grid-template-columns: 1fr 1fr; gap: 3px 10px; font-size: 12px; }
.lg { display: flex; align-items: center; gap: 6px; min-width: 0; }
.sw { flex: none; display: inline-block; }
.sw.line { width: 22px; height: 5px; border-radius: 3px; }
.sw.dot { width: 12px; height: 12px; border-radius: 50%; border: 2px solid #202124; }
.sw.dot.near { width: 10px; height: 10px; border: 1px solid #fff; opacity: 0.7; }
.sw.sq { width: 11px; height: 11px; border: 1.5px solid #fff; outline: 1px solid #555; }
.sw.small { width: 8px; height: 8px; border-radius: 50%; background: #7b93a8; border: 1px solid #fff;
            outline: 1px solid #7b93a8; }
.foot { color: var(--muted); font-size: 11px; margin-top: 10px; }
.nj-icon span { display: block; width: 12px; height: 12px; border: 2px solid #fff;
                box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.6); }
.pin { width: 24px; height: 24px; border-radius: 50%; color: #fff; font: 700 12px/20px system-ui, sans-serif;
       text-align: center; border: 2px solid #fff; box-shadow: 0 1px 4px rgba(0, 0, 0, 0.5); }
.pin.a { background: #188038; } .pin.b { background: #202124; }
.place-label { font-weight: 700; }
.pop { font: 12px/1.4 system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif; max-width: 300px; }
.pop-title { font-weight: 700; font-size: 13px; margin-bottom: 4px; }
.pop-k { color: #5b6672; }
.pop-desc { margin-top: 4px; }
@media (max-width: 700px) {
  .panel { top: auto; bottom: 0; left: 0; right: 0; width: auto; max-width: none; max-height: 60vh;
           border-radius: 12px 12px 0 0; border-width: 1px 0 0; }
  .leaflet-bottom { bottom: var(--panel-h); }
  .legend { grid-template-columns: 1fr; }
}
"""

MAP_JS = r"""
(function () {
  "use strict";
  var D = JSON.parse(document.getElementById("data").textContent);
  var NJ_COLOR = {incident: "#d93025", delay: "#f5a623", work: "#8a8f98"};

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c];
    });
  }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function popupBox(title, rows, desc) {
    var box = el("div", "pop");
    box.appendChild(el("div", "pop-title", title));
    rows.forEach(function (r) {
      if (r[1] === "" || r[1] == null) return;
      var line = el("div");
      line.appendChild(el("span", "pop-k", r[0] + ": "));
      line.appendChild(document.createTextNode(String(r[1])));
      box.appendChild(line);
    });
    if (desc) box.appendChild(el("div", "pop-desc", desc));
    return box;
  }
  function mid(pts) { return pts[Math.floor(pts.length / 2)]; }

  var wide = window.matchMedia("(min-width: 701px)").matches;
  var map = L.map("map", {zoomControl: false});
  L.control.zoom({position: "topright"}).addTo(map);
  // ---- base maps.  OpenStreetMap's own tile server blocks apps like this one, and CARTO's
  // basemaps now answer "API KEY REQUIRED" without a key, so: Esri's free tiles (no key).
  var ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/";
  function esri(service, cls) {
    return L.tileLayer(ESRI + service + "/MapServer/tile/{z}/{y}/{x}", {maxZoom: 19, maxNativeZoom: 18,
                       className: cls || "", attribution: "Tiles &copy; Esri"});
  }
  // Streets is the default everywhere (road names matter more than the theme).  Dark is the same
  // street map inverted by CSS (.rc-dark-tiles, on this layer's container in the tile pane only),
  // so every label survives; Satellite gets Esri's road-name overlay on top.  The first tile layer
  // of each is the one whose errors count for the fallback below.
  var bases = [
    {name: "Streets", tiles: [esri("World_Street_Map")]},
    {name: "Dark", tiles: [esri("World_Street_Map", "rc-dark-tiles")]},
    {name: "Satellite", tiles: [esri("World_Imagery"), esri("Reference/World_Transportation")]}
  ];
  var baseMaps = {};
  bases.forEach(function (b) {
    b.layer = b.tiles.length > 1 ? L.layerGroup(b.tiles) : b.tiles[0];
    baseMaps[b.name] = b.layer;
  });
  var base = bases[0];
  base.layer.addTo(map);
  // More than 6 failed tiles on the active base map: switch once to the next one
  // (Streets -> Dark -> Satellite -> Streets).
  var tileErrors = 0, switched = false;
  map.on("baselayerchange", function (e) {
    bases.forEach(function (b) { if (b.layer === e.layer) base = b; });
    tileErrors = 0;
  });
  bases.forEach(function (b, i) {
    b.tiles[0].on("tileerror", function () {
      if (switched || b !== base || ++tileErrors <= 6) return;
      switched = true;
      var next = bases[(i + 1) % bases.length];
      map.removeLayer(b.layer);
      map.addLayer(next.layer);         // the layer control follows layeradd / layerremove
      base = next;
    });
  });
  [["routes", 410], ["stretch", 420], ["inclines", 430], ["incs", 440]].forEach(function (p) {
    map.createPane(p[0]).style.zIndex = p[1];
  });

  var altLayer = L.layerGroup().addTo(map), bestLayer = L.layerGroup().addTo(map);
  var stretchLayer = L.layerGroup().addTo(map), incOn = L.layerGroup().addTo(map);
  var incNear = L.layerGroup().addTo(map), slowLayer = L.layerGroup().addTo(map);
  var njLayer = L.layerGroup().addTo(map);
  var bounds = L.latLngBounds([]);

  // ---- routes: alternates first so the best one is drawn on top
  function lineStyle(r, hi) {
    return r.best ? {color: "#1a73e8", weight: hi ? 9 : 6, opacity: 1}
                  : {color: hi ? "#5f6368" : "#8a8f98", weight: hi ? 7 : 4, opacity: hi ? 1 : 0.9};
  }
  var lines = [];
  for (var i = D.routes.length - 1; i >= 0; i--) {
    (function (i) {
      var r = D.routes[i], layer = r.best ? bestLayer : altLayer, s = lineStyle(r, false);
      var casing = L.polyline(r.pts, {pane: "routes", color: "#ffffff", weight: s.weight + 4,
                                      opacity: 0.9, interactive: false}).addTo(layer);
      var line = L.polyline(r.pts, {pane: "routes", color: s.color, weight: s.weight,
                                    opacity: s.opacity}).addTo(layer);
      line.bindTooltip(esc(r.label), {sticky: true});
      line.bindPopup(popupBox(r.tag + " " + r.via, [["Time", r.time + " (" + r.traffic + " traffic)"],
        ["Distance", r.miles + " mi"], ["Arrive", r.arrive], ["vs best", r.vs]]));
      line.on("click", function () { highlight(i, false); });
      lines[i] = {r: r, casing: casing, line: line, layer: layer};
      bounds.extend(line.getBounds());
      r.stretches.forEach(function (st) {
        var pts = r.pts.slice(st.i0, st.i1 + 1);
        if (pts.length < 2) return;
        L.polyline(pts, {pane: "stretch", color: st.color, weight: 8, opacity: 0.95, lineCap: "butt"})
          .bindTooltip(esc(st.text), {sticky: true}).addTo(stretchLayer);
      });
    })(i);
  }

  // ---- TomTom incidents (the ones the report lists) and nearby slowdowns
  function addIncident(a, layer, on) {
    if (!a.pts.length) return;
    if (a.pts.length > 1) {
      L.polyline(a.pts, {pane: "inclines", color: a.color, weight: on ? 5 : 4,
                         opacity: on ? 0.45 : 0.3, interactive: false}).addTo(layer);
    }
    var opts = on ? {radius: 8, color: "#202124", weight: 2, fillColor: a.color, fillOpacity: 1}
                  : {radius: 6, color: "#ffffff", weight: 1.5, opacity: 0.7, fillColor: a.color,
                     fillOpacity: 0.7};
    opts.pane = "incs";
    var title = a.kind + (a.note ? ": " + a.note : "");
    L.circleMarker(mid(a.pts), opts)
      .bindTooltip(esc(title + "  (" + a.tag + ")"))
      .bindPopup(popupBox(title, [["Where", a.where], ["Delay", a.delay], ["Severity", a.sev],
        ["Confidence", a.conf], ["Reports", a.reports], ["Last report", a.last],
        ["Starts", a.starts], ["Route", a.tag]]))
      .addTo(layer);
  }
  D.issues_on.forEach(function (a) { addIncident(a, incOn, true); });
  D.issues_near.forEach(function (a) { addIncident(a, incNear, false); });
  D.slow.forEach(function (a) {
    if (!a.pts.length) return;
    var title = a.kind + (a.note ? ": " + a.note : "");
    L.circleMarker(mid(a.pts), {pane: "inclines", radius: 4, color: "#ffffff", weight: 1,
                                fillColor: "#7b93a8", fillOpacity: 0.9})
      .bindTooltip(esc(title + "  (" + a.tag + ")"))
      .bindPopup(popupBox(title, [["Where", a.where], ["Delay", a.delay], ["Severity", a.sev],
        ["Confidence", a.conf], ["Route", a.tag]]))
      .addTo(slowLayer);
  });

  // ---- 511NJ official reports: small squares
  function addNj(e) {
    var color = NJ_COLOR[e.cls] || "#8a8f98";
    var icon = L.divIcon({className: "nj-icon", html: '<span style="background:' + color + '"></span>',
                          iconSize: [16, 16], iconAnchor: [8, 8], popupAnchor: [0, -8]});
    L.marker([e.lat, e.lon], {icon: icon, zIndexOffset: e.on ? 200 : 0})
      .bindTooltip(esc("511NJ " + e.kind + ": " + e.road + "  (" + e.tag + ")"))
      .bindPopup(popupBox("511NJ " + e.kind, [["Road", e.road], ["Reported", e.when],
        ["Route", e.tag]], e.desc))
      .addTo(njLayer);
  }
  D.nj_on.forEach(addNj);
  D.nj_near.forEach(addNj);

  // ---- start and end
  [[D.start, "a", "A", "right"], [D.end, "b", "B", "left"]].forEach(function (p) {
    if (!p[0].pt) return;
    L.marker(p[0].pt, {icon: L.divIcon({className: "", html: '<div class="pin ' + p[1] + '">' + p[2] + "</div>",
                                        iconSize: [24, 24], iconAnchor: [12, 12]}), zIndexOffset: 1000})
      .bindTooltip(esc(p[0].label), {permanent: true, direction: p[3], offset: p[3] === "right" ? [12, 0] : [-12, 0],
                                     className: "place-label"})
      .addTo(map);
  });

  // Folded to the layers icon on phones (the panel's breakpoint) and on short screens such as a
  // phone held sideways; open on a desktop, where it fits.
  var layersFold = window.matchMedia("(max-width: 700px), (max-height: 560px)").matches;
  var layers = L.control.layers(baseMaps, {
    "Alternate routes": altLayer, "Slow stretches": stretchLayer, "TomTom incidents": incOn,
    "Nearby incidents": incNear, "Nearby slowdowns": slowLayer, "511NJ reports": njLayer
  }, {collapsed: layersFold, position: "topright"}).addTo(map);
  if (layersFold) {
    // Leaflet folds the list on a map click already; also on a drag or zoom, and say it outright
    // so a tap on the map always closes it on a phone.
    map.on("click movestart zoomstart", function () { layers.collapse(); });
  }

  // ---- side panel
  var panel = document.getElementById("panel"), body = document.getElementById("p-body");
  var toggle = document.getElementById("p-toggle");
  document.getElementById("p-title").textContent = D.title;
  var nav = document.getElementById("p-nav");
  [["Open in Waze", D.waze, "waze"], ["Google Maps", D.gmaps, "gmaps"]].forEach(function (l) {
    if (!/^https:\/\//.test(l[1] || "")) return;
    var a = el("a", l[2], l[0]); a.href = l[1]; a.target = "_blank"; a.rel = "noopener";
    nav.appendChild(a);
  });
  var best = D.routes[0];
  document.getElementById("p-sub").textContent = best ? "BEST " + best.via + "  " + best.time +
    ", arrive " + best.arrive : "";

  var meta = el("div", "meta");
  meta.appendChild(el("div", null, "Generated " + D.generated));
  meta.appendChild(el("div", null, "Departure " + D.departure + " (" + D.departure_note + ")"));
  body.appendChild(meta);

  var wrap = el("div", "tablewrap"), table = el("table", "routes"), thead = el("thead"), hr = el("tr");
  ["Route", "Via", "Time", "Traffic", "Mi", "Arrive", "vs best"].forEach(function (h) {
    hr.appendChild(el("th", null, h));
  });
  thead.appendChild(hr); table.appendChild(thead);
  var tbody = el("tbody"), rows = [];
  D.routes.forEach(function (r, i) {
    var tr = el("tr", "route-row");
    tr.appendChild(el("td", "tag", r.tag));
    tr.appendChild(el("td", null, r.via.replace(/^via /, "")));
    [r.time, r.traffic, r.miles, r.arrive, r.vs].forEach(function (v) { tr.appendChild(el("td", "n", v)); });
    tr.tabIndex = 0;
    tr.addEventListener("click", function () { highlight(i, true); });
    tr.addEventListener("keydown", function (ev) { if (ev.key === "Enter") highlight(i, true); });
    rows[i] = tr; tbody.appendChild(tr);
  });
  table.appendChild(tbody); wrap.appendChild(table); body.appendChild(wrap);
  body.appendChild(el("div", "hint", "Click a route to highlight it on the map."));

  body.appendChild(el("h3", null, "On the map"));
  var counts = el("ul", "counts");
  D.counts.forEach(function (c) { counts.appendChild(el("li", null, c)); });
  body.appendChild(counts);

  body.appendChild(el("h3", null, "Legend"));
  var legend = el("div", "legend");
  function lg(sw, label) {
    var d = el("div", "lg"); d.appendChild(sw); d.appendChild(el("span", null, label)); legend.appendChild(d);
  }
  function swatch(kind, color) { var s = el("span", "sw " + kind); if (color) s.style.background = color; return s; }
  lg(swatch("line", "#1a73e8"), "Best route");
  lg(swatch("line", "#8a8f98"), "Alternate route");
  D.legend_mag.forEach(function (m) { lg(swatch("line", m[1]), "Slow stretch, " + m[0]); });
  D.legend_kind.forEach(function (k) { lg(swatch("dot", k[1]), k[0]); });
  lg(swatch("dot near", "#d93025"), "Faded = nearby, not on route");
  lg(swatch("small"), "Slowdown nearby");
  lg(swatch("sq", NJ_COLOR.incident), "511NJ incident");
  lg(swatch("sq", NJ_COLOR.delay), "511NJ delay");
  lg(swatch("sq", NJ_COLOR.work), "511NJ work");
  body.appendChild(legend);

  body.appendChild(el("div", "foot", "POTENTIAL = TomTom rates it probable or at risk rather than " +
    "confirmed. Sources: TomTom Routing + Traffic Incident Details, 511NJ (NJDOT). Map tiles: " +
    "Esri (layer button: streets, dark, satellite)."));

  function syncPanel() {
    var h = wide ? 0 : panel.offsetHeight;
    document.documentElement.style.setProperty("--panel-h", h + "px");
  }
  function setCollapsed(c) {
    panel.classList.toggle("collapsed", c);
    toggle.textContent = c ? "Show" : "Hide";
    toggle.setAttribute("aria-expanded", c ? "false" : "true");
    syncPanel();
  }
  toggle.addEventListener("click", function () { setCollapsed(!panel.classList.contains("collapsed")); });
  window.addEventListener("resize", function () {
    wide = window.matchMedia("(min-width: 701px)").matches; syncPanel();
  });
  setCollapsed(!wide);

  function highlight(i, open) {
    lines.forEach(function (o, j) {
      var s = lineStyle(o.r, j === i);
      o.line.setStyle(s); o.casing.setStyle({weight: s.weight + 4});
    });
    var o = lines[i];
    if (!map.hasLayer(o.layer)) map.addLayer(o.layer);
    o.casing.bringToFront(); o.line.bringToFront();
    rows.forEach(function (tr, j) { tr.classList.toggle("active", j === i); });
    if (open) o.line.openPopup(L.latLng(mid(o.r.pts)));
  }

  if (bounds.isValid()) {
    if (wide) {
      map.fitBounds(bounds, {paddingTopLeft: [panel.offsetWidth + 36, 30], paddingBottomRight: [40, 30]});
    } else {
      map.fitBounds(bounds, {paddingTopLeft: [20, 20], paddingBottomRight: [20, panel.offsetHeight + 20]});
    }
  } else {
    map.setView([40.4, -74.2], 9);
  }
})();
"""


def _map_incident(a: dict, best_idx: int) -> dict:
    return {
        "kind": a["kind"], "note": a.get("note", ""), "where": a["where"], "delay": incident_delay(a),
        "sev": a["mag"], "conf": incident_confidence(a), "reports": a["reports"] or "",
        "last": clock(a["last"].astimezone()) if a["last"] else "",
        "starts": clock(a["start"].astimezone()) if a["future"] and a["start"] else "",
        "tag": route_tag(a, best_idx), "color": MAP_KIND_COLOR.get(a["kind"], "#5f7d95"),
        "pts": a.get("pts") or [],
    }


def route_summary(r: dict) -> str:
    s = (f"{r['tag']} {r['via']}  {minutes(r['travel_s'])} ({plus_minutes(r['delay_s'])} traffic)  "
         f"{r['length_m'] * M_TO_MI:.1f} mi  arrive {clock(r['arrive'])}")
    return s if r["delta_s"] is None else s + f"  {plus_minutes(r['delta_s'])} vs best"


def map_html(rep: dict) -> str:
    """The interactive map as one self-contained HTML string (Leaflet from cdnjs)."""
    best_idx = rep["best_idx"]
    routes = []
    for r in rep["routes"]:
        stretches = []
        for s in r["stretches"]:
            extra = f" ({s['extra']})" if s["extra"] else ""
            stretches.append({"i0": s["i0"], "i1": s["i1"], "mag": s["mag"],
                              "color": MAP_MAG_COLOR.get(s["mag"], "#f5a623"),
                              "text": f"Slow stretch: {s['road']}, {s['length_m'] * M_TO_MI:.1f} mi, "
                                      f"{plus_minutes(s['delay_s'])}{extra}"})
        routes.append({
            "tag": r["tag"], "best": r["delta_s"] is None, "via": r["via"],
            "time": minutes(r["travel_s"]), "traffic": plus_minutes(r["delay_s"]),
            "miles": f"{r['length_m'] * M_TO_MI:.1f}", "arrive": clock(r["arrive"]),
            "vs": "" if r["delta_s"] is None else plus_minutes(r["delta_s"]),
            "label": route_summary(r), "pts": r["pts"], "stretches": stretches,
        })

    def nj(e: dict) -> dict:
        return {"cls": e["cls"], "kind": e["kind"], "road": e["road"], "desc": e["desc"],
                "when": clock(e["when"].astimezone()) if e["when"] else "", "on": bool(e["on"]),
                "tag": route_tag(e, best_idx), "lat": round(e["lat"], 5), "lon": round(e["lon"], 5)}

    counts = []
    if rep["have_incidents"]:
        counts.append(f"TomTom accident issues: {len(rep['issues_on'])} on your routes, "
                      f"{len(rep['issues_near'])} within a mile")
        counts.append(f"Slowdowns nearby, not on the routes: {len(rep['slow'])}")
    else:
        counts.append("TomTom incidents: not available for this run")
    if rep["have_nj511"]:
        counts.append(f"511NJ reports: {len(rep['nj_on'])} on your routes, "
                      f"{len(rep['nj_near'])} incidents within a mile")
        if rep["nj_sched_count"]:
            counts.append(f"Scheduled 511NJ work outside your trip window, not drawn: "
                          f"{rep['nj_sched_count']}")
    else:
        counts.append("511NJ feed: unavailable right now")
    counts.append(f"Slow stretches on the routes: {sum(len(r['stretches']) for r in rep['routes'])}")

    best_pts = rep["routes"][0]["pts"] if rep["routes"] else []
    data = {
        "title": f"{rep['origin']} -> {rep['dest']}",
        "generated": day_and_clock(rep["now"]), "departure": day_and_clock(rep["depart"]),
        "departure_note": ("future trip: TomTom's historic traffic model, not live conditions"
                           if rep["future"] else "leaving now, live traffic"),
        "routes": routes,
        "issues_on": [_map_incident(a, best_idx) for a in rep["issues_on"]],
        "issues_near": [_map_incident(a, best_idx) for a in rep["issues_near"]],
        "slow": [_map_incident(a, best_idx) for a in rep["slow"]],
        "nj_on": [nj(e) for e in rep["nj_on"]], "nj_near": [nj(e) for e in rep["nj_near"]],
        "counts": counts,
        "legend_mag": [[m, c] for m, c in MAP_MAG_COLOR.items()],
        "legend_kind": [["Accident", MAP_KIND_COLOR["Accident"]],
                        ["Road / lane closed", MAP_KIND_COLOR["Road closed"]],
                        ["Broken-down vehicle", MAP_KIND_COLOR["Broken-down vehicle"]],
                        ["Dangerous conditions", MAP_KIND_COLOR["Dangerous conditions"]]],
        "start": {"label": rep["origin"], "pt": best_pts[0] if best_pts else None},
        "end": {"label": rep["dest"], "pt": best_pts[-1] if best_pts else None},
        "gmaps": rep["gmaps"], "waze": rep["waze"],
    }
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/").replace("<!--", "\\u003c!--")
    return (MAP_HTML.replace("__LEAFLET__", LEAFLET).replace("__CSS__", MAP_CSS.strip())
            .replace("__JS__", MAP_JS.strip()).replace("__DATA__", blob))


def write_map(rep: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(map_html(rep), encoding="utf-8")
    return path


# ---- one check: key + direction + departure -> finished report ------------------------------

def load_key() -> str:
    """TOMTOM_API_KEY from the .env next to this file, else the environment ('' if neither)."""
    env = dotenv_values(ENV_FILE) if ENV_FILE.is_file() else {}
    return (env.get("TOMTOM_API_KEY") or os.environ.get("TOMTOM_API_KEY") or "").strip()


def load_places(extra: dict | None = None) -> dict:
    """{"hospital": (label, address), "home": (label, address)}. Each setting is looked up in
    `extra` (the Streamlit app passes its secrets), then the environment, then .env.
    Raises ConfigError naming the missing address variable(s); labels fall back to
    "Hospital" / "Home"."""
    sources = [extra or {}, os.environ, dotenv_values(ENV_FILE) if ENV_FILE.is_file() else {}]

    def lookup(name: str) -> str:
        for src in sources:
            try:
                val = src.get(name)
            except Exception:        # a secrets object that cannot be read counts as empty
                val = None
            if val is not None and str(val).strip():
                return str(val).strip()
        return ""

    places, missing = {}, []
    for key, (label_var, addr_var, fallback) in PLACE_VARS.items():
        addr = lookup(addr_var)
        if not addr:
            missing.append(addr_var)
        places[key] = (lookup(label_var) or fallback, addr)
    if missing:
        raise ConfigError(f"Missing {' and '.join(missing)}. Add the line(s) to {ENV_FILE} (copy "
                          "them from .env.example), or to the app's Secrets on Streamlit, e.g. "
                          f'{missing[0]}="123 Main St, Town, NJ 00000".')
    return places


def report_stem(direction: str, now: datetime) -> str:
    """File name (no extension) used for the PDF / map of one check."""
    return f"route_{direction}_{now:%Y-%m-%d_%H%M}"


def trip(direction: str, places: dict | None = None) -> tuple[tuple[str, str], tuple[str, str]]:
    """((origin label, query), (destination label, query)) for 'there' / 'back'."""
    places = places or load_places()
    o_key, d_key = DEFAULT_TRIP if direction == "there" else DEFAULT_TRIP[::-1]
    return places[o_key], places[d_key]


def run_check(key: str, direction: str, depart: datetime, now: datetime, alts: int = 2,
              no_tolls: bool = False, places: dict | None = None) -> dict:
    """Geocode, route, fetch incidents + 511NJ and build the report dict. Raises ApiError,
    or ConfigError if no `places` are given and the addresses are not configured.
    The raw replies (for --json) ride along in rep["raw"]."""
    (o_label, o_query), (d_label, d_query) = trip(direction, places)
    depart_aware = depart.astimezone()
    future = depart - now > timedelta(minutes=3)
    depart_param = depart_aware.isoformat(timespec="seconds") if future else "now"

    o = geocode(key, o_query)
    d = geocode(key, d_query)
    raw_routes = fetch_routes(key, o, d, depart_param, max(0, min(5, alts)), no_tolls)
    routes = [parse_route(r) for r in raw_routes.get("routes") or []]
    if not routes:
        raise ApiError("TomTom found no route between these two addresses.")
    order = sorted(range(len(routes)), key=lambda i: routes[i]["travel_s"])
    best_idx = order[0]
    horizon = timedelta(seconds=routes[best_idx]["travel_s"]) + timedelta(minutes=20)
    raw_incidents = fetch_incidents(key, routes)
    raw_nj511 = fetch_nj511()

    depart_utc = depart_aware.astimezone(timezone.utc)
    incidents = []
    if raw_incidents:
        incidents = dedupe([a for a in (annotate(i, routes, depart_utc, horizon) for i in raw_incidents) if a])
    nj511 = [e for e in (annotate_nj511(ev, routes, depart) for ev in (raw_nj511 or [])) if e]

    ctx = {
        "origin": o_label, "dest": d_label, "now": now, "depart": depart, "future": future,
        "have_incidents": raw_incidents is not None, "have_nj511": raw_nj511 is not None,
        "gmaps": ("https://www.google.com/maps/dir/?api=1&origin=" + quote_plus(o_query)
                  + "&destination=" + quote_plus(d_query) + "&travelmode=driving"),
        "waze": f"https://waze.com/ul?ll={d['lat']:.5f},{d['lon']:.5f}&navigate=yes",
    }
    rep = build_report(ctx, routes, order, best_idx, incidents, nj511)
    rep["direction"] = direction
    rep["raw"] = {"run_at": now.isoformat(), "depart": depart_aware.isoformat(),
                  "origin": o, "destination": d, "routes": raw_routes, "incidents": raw_incidents}
    return rep


# ---- main ------------------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("direction", nargs="?", choices=["there", "back"], default="there",
                    help="'back' = the return trip (default: hospital -> home)")
    ap.add_argument("--now", action="store_true", help="leave now, don't ask")
    ap.add_argument("--at", metavar="WHEN", help='departure, e.g. "5:30pm", "tomorrow 7am", 2026-10-29T08:00')
    ap.add_argument("--alts", type=int, default=2, help="alternate routes to compare (0-5, default 2)")
    ap.add_argument("--no-tolls", action="store_true", help="avoid toll roads")
    ap.add_argument("--pdf", action="store_true", help="also write the report to reports\\ as a PDF and open it")
    ap.add_argument("--map", action="store_true",
                    help="also write an interactive map to reports\\ as an HTML file and open it")
    ap.add_argument("--no-open", action="store_true", help="with --pdf / --map: write it but don't open it")
    ap.add_argument("--json", action="store_true", help="also dump the raw API replies to cache/last_run.json")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    key = load_key()
    if not key or key.lower().startswith("your"):
        die("No TomTom API key yet.\n"
            "  1. Register free (no credit card) at https://my.tomtom.com, then copy the key at https://my.tomtom.com/keys\n"
            f"  2. Paste it into {ENV_FILE} as   TOMTOM_API_KEY=your-key\n"
            "  README.md in this folder has the step-by-step.")

    try:
        places = load_places()
    except ConfigError as e:
        die(str(e))
    (o_label, _), (d_label, _) = trip(args.direction, places)

    now = datetime.now().replace(microsecond=0)
    if args.at:
        depart = parse_departure(args.at, now)
        if depart is None:
            die(f"Could not read the departure time '{args.at}'. Try 5:30pm, 17:30, tomorrow 7am, fri 6:15am.")
    elif args.now or not sys.stdin.isatty():
        depart = now
    else:
        depart = ask_departure(now)
    future = depart - now > timedelta(minutes=3)

    print()
    print(f"ROUTE CHECK   {o_label}  ->  {d_label}     {day_and_clock(now)}")
    if future:
        print(f"Departure: {day_and_clock(depart)}   (future trip: TomTom's historic traffic model, "
              "not live conditions)")
        if depart - now > timedelta(hours=2):
            print("Incidents below are what is reported right now plus anything scheduled for your "
                  "window; live ones may well clear before you leave.")
    print()

    try:
        rep = run_check(key, args.direction, depart, now, args.alts, args.no_tolls, places)
    except ApiError as e:
        die(f"\n{e}", 2)
    print_report(rep)

    if args.pdf:
        path = REPORT_DIR / f"{report_stem(args.direction, now)}.pdf"
        try:
            write_pdf(rep, path)
        except ImportError:
            print("\nPDF skipped: the reportlab package is missing (pip install reportlab).")
        except OSError as e:
            print(f"\nPDF could not be written ({e}). Is an old copy open in a viewer?")
        else:
            print(f"\nPDF saved: {path}")
            if not args.no_open:
                try:
                    os.startfile(path)
                except (OSError, AttributeError):
                    pass

    if args.map:
        path = REPORT_DIR / f"{report_stem(args.direction, now)}.html"
        try:
            write_map(rep, path)
        except OSError as e:
            print(f"\nMap could not be written ({e}).")
        else:
            print(f"\nMap saved: {path}")
            if not args.no_open:
                try:
                    os.startfile(path)
                except (OSError, AttributeError):
                    pass

    if args.json:
        save_json(LAST_RUN, rep["raw"])
        print(f"\nRaw API replies saved to {LAST_RUN}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
