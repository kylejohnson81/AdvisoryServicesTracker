#!/usr/bin/env python3
"""FCC broadband import for the Advisory Services Tracker.

Runs in GitHub Actions (see .github/workflows/fcc-broadband.yml). For every
tracker property it:
  1. geocodes the property's address with the free Census Bureau batch geocoder
     (lat/lon + 2020 census block),
  2. pulls the FCC National Broadband Map's official public fixed-availability
     files for the states involved (Public Data API, needs a free FCC account
     token), keeping only rows in the properties' census blocks,
  3. writes a per-property provider summary to Supabase through the
     broadband-ingest edge function.

No manual entry: matching is by census block. If the block holds no FCC
locations it falls back to the surrounding H3 hexagon ("area" match). A
property can be pinned to one FCC location_id with override_location_id.

Env: FCC_USERNAME, FCC_API_TOKEN, BROADBAND_INGEST_KEY, INGEST_URL, FORCE (optional "true").
Logs stay at counts only (the repository is public).
"""
import csv
import io
import os
import sys
import tempfile
import time
import zipfile
from collections import defaultdict
from datetime import datetime, timezone

import h3
import requests

FCC = "https://broadbandmap.fcc.gov/api/public/map"
CENSUS_BATCH = "https://geocoding.geo.census.gov/geocoder/geographies/addressbatch"
INGEST_URL = os.environ.get("INGEST_URL", "https://nqmlbyfmmlsioeqdlivs.supabase.co/functions/v1/broadband-ingest")
FORCE = os.environ.get("FORCE", "").lower() == "true"

TECH = {0: "Other", 10: "Copper", 40: "Cable", 50: "Fiber to the Premises", 60: "GSO Satellite",
        61: "NGSO Satellite", 70: "Unlicensed Fixed Wireless", 71: "Licensed Fixed Wireless", 72: "LBR Fixed Wireless"}
SKIP_TECH = {60, 61}  # satellite files cover every location in a state and add nothing property-specific

STATE_FIPS = {"AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08", "CT": "09", "DE": "10", "DC": "11",
              "FL": "12", "GA": "13", "HI": "15", "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20", "KY": "21",
              "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27", "MS": "28", "MO": "29", "MT": "30",
              "NE": "31", "NV": "32", "NH": "33", "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
              "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46", "TN": "47", "TX": "48", "UT": "49",
              "VT": "50", "VA": "51", "WA": "53", "WV": "54", "WI": "55", "WY": "56", "PR": "72"}

csv.field_size_limit(10_000_000)
now_iso = lambda: datetime.now(timezone.utc).isoformat()


def ingest(payload):
    r = requests.post(INGEST_URL, json=payload, headers={"x-ingest-key": os.environ["BROADBAND_INGEST_KEY"]}, timeout=120)
    if r.status_code != 200:
        sys.exit(f"ingest {payload.get('action')} failed: HTTP {r.status_code} {r.text[:300]}")
    return r.json()


def clean_secret(v):
    """Pasted secrets can carry spaces, line breaks or invisible/curly characters; keep printable ASCII only."""
    return "".join(ch for ch in v if 33 <= ord(ch) <= 126)


def check_fcc_secrets():
    for name in ("FCC_USERNAME", "FCC_API_TOKEN"):
        raw = os.environ.get(name, "")
        kept = clean_secret(raw)
        print(f"{name}: {len(raw)} chars pasted, {len(kept)} usable" + ("" if kept else " -- EMPTY, re-enter this secret"))
        if not kept:
            sys.exit(1)
    if "@" not in clean_secret(os.environ.get("FCC_USERNAME", "")):
        sys.exit("FCC_USERNAME should be the email address you sign in to broadbandmap.fcc.gov with")


def fcc_get(path, stream=False):
    h = {"username": clean_secret(os.environ.get("FCC_USERNAME", "")), "hash_value": clean_secret(os.environ.get("FCC_API_TOKEN", ""))}
    for attempt in range(4):
        r = requests.get(f"{FCC}/{path}", headers=h, stream=stream, timeout=600)
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code != 200:
            sys.exit(f"FCC {path.split('?')[0]} failed: HTTP {r.status_code} {r.text[:300] if not stream else ''}")
        return r
    sys.exit(f"FCC {path} kept failing")


def full_address(t):
    return ", ".join(x for x in [t["address"].strip(), t["city"].strip(), f"{t['state'].strip()} {t['zip'].strip()}".strip()] if x)


def geocode(targets):
    """Census batch geocode -> {code: (lat, lon, block_geoid)}."""
    buf = io.StringIO()
    w = csv.writer(buf)
    for t in targets:
        w.writerow([t["code"], t["address"], t["city"], t["state"], t["zip"]])
    out = {}
    if not targets:
        return out
    r = requests.post(CENSUS_BATCH, data={"benchmark": "Public_AR_Current", "vintage": "Current_Current"},
                      files={"addressFile": ("addresses.csv", buf.getvalue(), "text/csv")}, timeout=600)
    r.raise_for_status()
    for row in csv.reader(io.StringIO(r.text)):
        # id, input, Match|No_Match|Tie, Exact|Non_Exact, matched address, "lon,lat", tiger id, side, state, county, tract, block
        if len(row) < 12 or row[2] != "Match":
            continue
        try:
            lon, lat = (float(v) for v in row[5].split(","))
        except ValueError:
            continue
        out[row[0]] = (lat, lon, f"{row[8]}{row[9]}{row[10]}{row[11]}")
    return out


def open_csv(path):
    with open(path, "rb") as f:
        magic = f.read(2)
    if magic == b"PK":
        z = zipfile.ZipFile(path)
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        return io.TextIOWrapper(z.open(name), encoding="utf-8", newline="")
    return open(path, encoding="utf-8", newline="")


def main():
    targets = ingest({"action": "targets"})["targets"]
    for t in targets:  # some attributes are stored as numbers (e.g. ZIP 2116 for 02116)
        for k in ("address", "city", "state", "zip"):
            t[k] = "" if t.get(k) is None else str(t[k]).strip()
        z = t["zip"].split(".")[0].split("-")[0]
        if z.isdigit() and len(z) < 5:
            t["zip"] = z.zfill(5)
    print(f"{len(targets)} tracker properties")

    # 1. geocode where the address is new or changed
    need_geo = [t for t in targets if t["address"].strip() and (FORCE or not t["current"] or not t["current"].get("block_geoid")
                or t["current"].get("address_used") != full_address(t))]
    geo = geocode(need_geo)
    print(f"geocoded {len(geo)} of {len(need_geo)} that needed it")

    place = {}  # code -> dict(lat, lon, block, state_fips, address_used, geocoded_at, override)
    no_match = []
    for t in targets:
        cur = t["current"] or {}
        override = cur.get("override_location_id")
        if not t["address"].strip():
            no_match.append({"property_code": t["code"], "match_type": "no_address", "providers": [], "checked_at": now_iso()})
            continue
        if t["code"] in geo:
            lat, lon, block = geo[t["code"]]
            place[t["code"]] = dict(lat=lat, lon=lon, block=block, geocoded_at=now_iso(), address_used=full_address(t), override=override, fresh=True)
        elif cur.get("block_geoid") and cur.get("address_used") == full_address(t):
            place[t["code"]] = dict(lat=cur.get("lat"), lon=cur.get("lon"), block=cur["block_geoid"], geocoded_at=None,
                                    address_used=cur.get("address_used"), override=override, fresh=False)
        else:
            no_match.append({"property_code": t["code"], "match_type": "no_geocode", "address_used": full_address(t),
                             "block_geoid": None, "providers": [], "checked_at": now_iso()})
    if no_match:
        ingest({"action": "upsert", "rows": no_match})
    print(f"{len(place)} placed, {len(no_match)} without a usable address")

    # 2. latest FCC availability release
    check_fcc_secrets()
    dates = fcc_get("listAsOfDates").json().get("data", [])
    as_of = max(d["as_of_date"][:10] for d in dates if d.get("data_type") == "availability")
    print(f"FCC availability as of {as_of}")

    cur_by_code = {t["code"]: (t["current"] or {}) for t in targets}
    todo = {c: p for c, p in place.items() if FORCE or p["fresh"] or str(cur_by_code[c].get("fcc_as_of") or "")[:10] != as_of}
    if not todo:
        print("everything is current")
        return
    for p in todo.values():
        if p["lat"] is not None:
            p["hex"] = h3.latlng_to_cell(p["lat"], p["lon"], 8)
    blocks = defaultdict(set)
    hexes = defaultdict(set)
    for c, p in todo.items():
        blocks[p["block"][:2]].add(p["block"])
        if p.get("hex"):
            hexes[p["block"][:2]].add(p["hex"])
    print(f"{len(todo)} properties to refresh across {len(blocks)} states")

    files = fcc_get(f"downloads/listAvailabilityData/{as_of}?category=State&subcategory=Fixed%20Broadband").json().get("data", [])
    if not files:
        files = fcc_get(f"downloads/listAvailabilityData/{as_of}").json().get("data", [])
    print(f"FCC file list: {len(files)} entries")
    if files:
        print("  fields:", sorted(files[0].keys()))
        kinds = sorted({(str(f.get("category")), str(f.get("subcategory")), str(f.get("technology_type"))) for f in files})
        print("  kinds:", kinds[:15])

    def lo(v):
        return str(v or "").lower()

    def is_state_fixed(f):
        if f.get("category") and lo(f.get("category")) != "state":
            return False
        text = lo(f.get("subcategory")) + " " + lo(f.get("technology_type")) + " " + lo(f.get("file_name"))
        return "fixed" in text and "mobile" not in text

    files = [f for f in files if is_state_fixed(f)]
    print(f"  {len(files)} state fixed-broadband files")
    # rows kept: by block and by hex -> list of (location_id, brand, tech_code, down, up, low_latency)
    by_block = defaultdict(list)
    by_hex = defaultdict(list)
    for f in files:
        fips = str(f.get("state_fips") or f.get("state_code") or f.get("fips") or "").zfill(2)
        try:
            tech = int(f.get("technology_code") or f.get("technology") or 0)
        except (TypeError, ValueError):
            tech = 0
        if fips not in blocks or tech in SKIP_TECH:
            continue
        want_b, want_h = blocks[fips], hexes[fips]
        r = fcc_get(f"downloads/downloadFile/availability/{f['file_id']}", stream=True)
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            for chunk in r.iter_content(1 << 20):
                tmp.write(chunk)
        kept = 0
        with open_csv(tmp.name) as fh:
            for row in csv.DictReader(fh):
                b, hx = row.get("block_geoid", ""), row.get("h3_res8_id", "")
                if b not in want_b and hx not in want_h:
                    continue
                if row.get("business_residential_code") == "B":
                    continue  # business-only service; apartments want residential / mixed
                rec = (row["location_id"], row.get("brand_name") or row.get("provider_id"), int(row.get("technology") or tech),
                       float(row.get("max_advertised_download_speed") or 0), float(row.get("max_advertised_upload_speed") or 0),
                       row.get("low_latency") in ("1", "True", "true"))
                if b in want_b:
                    by_block[b].append(rec)
                if hx in want_h:
                    by_hex[hx].append(rec)
                kept += 1
        os.unlink(tmp.name)
        print(f"state {fips} tech {tech}: kept {kept} rows")

    # 3. summarise per property
    out = []
    for c, p in todo.items():
        recs, match = by_block.get(p["block"], []), "block"
        if p["override"]:
            recs = [r for r in recs + by_hex.get(p.get("hex"), []) if r[0] == p["override"]]
            match = "override"
        elif not recs and p.get("hex"):
            recs, match = by_hex.get(p["hex"], []), "area"
        locs = sorted({r[0] for r in recs})
        agg = {}
        for loc, brand, tc, down, up, ll in recs:
            k = (brand, tc)
            a = agg.setdefault(k, {"provider": brand, "tech_code": tc, "technology": TECH.get(tc, str(tc)), "down": 0, "up": 0, "low_latency": False, "locs": set()})
            a["down"], a["up"] = max(a["down"], down), max(a["up"], up)
            a["low_latency"] = a["low_latency"] or ll
            a["locs"].add(loc)
        providers = sorted(({**{k: v for k, v in a.items() if k != "locs"}, "locations": len(a["locs"])} for a in agg.values()),
                           key=lambda a: (a["tech_code"] != 50, -a["down"]))
        row = {"property_code": c, "address_used": p["address_used"], "lat": p["lat"], "lon": p["lon"], "block_geoid": p["block"],
               "match_type": match if recs else "no_fcc_locations", "location_count": len(locs), "location_ids": locs[:200],
               "providers": providers, "fcc_as_of": as_of, "checked_at": now_iso()}
        if p["geocoded_at"]:
            row["geocoded_at"] = p["geocoded_at"]
        out.append(row)
    for i in range(0, len(out), 50):
        ingest({"action": "upsert", "rows": out[i:i + 50]})
    print(f"wrote {len(out)} property broadband summaries")


if __name__ == "__main__":
    main()
