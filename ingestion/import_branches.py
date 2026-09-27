"""
import_branches.py — import physical shop locations into Supabase.

Branch locations are reference data and barely change, so this runs rarely
(not daily). Once store_branches is populated, barcode_lookup.py can price a
product by joining prices to branches and computing distance locally, instead
of calling api.cijene.dev on every scan.

Usage:
    python import_branches.py                    # fetch + clean, report only
    python import_branches.py --push             # also upsert to Supabase
    python import_branches.py --geocode          # fill in missing coordinates
    python import_branches.py --push --geocode   # the full one-off run

Environment:
    CIJENE_API_KEY  cijene.dev key (required)
    SUPABASE_URL    only needed with --push
    SUPABASE_KEY    only needed with --push

Notes on the upstream data (measured 2026-09-27):
    29 chains, 1495 branches.
    - 98% arrive with a street address.
    - Only 53% arrive with lat/lon; the biggest gap is ntl (339 of 339 missing)
      and tommy (87 of 166). --geocode fills these via Nominatim.
    - Addresses arrive as "Savska 58 87079 Spar" — street, then the branch code,
      then the chain name. clean_address() strips the trailing noise so the app
      shows "Savska 58" rather than "Savska 58 87079 Spar".
"""

import argparse
import datetime
import logging
import os
import re
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("import_branches")

CIJENE_BASE = "https://api.cijene.dev/v1"
CIJENE_API_KEY = os.environ.get("CIJENE_API_KEY", "")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

NOMINATIM = "https://nominatim.openstreetmap.org/search"
# Nominatim's usage policy asks for a real identifying User-Agent and at most
# one request per second. Do not speed this up.
USER_AGENT = "Stedko/1.0 (katalog.ai price comparison; contact: mcavdarov@gmail.com)"
GEOCODE_DELAY_S = 1.1


def cijene_headers():
    return {"Authorization": f"Bearer {CIJENE_API_KEY}"}


def supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }


def list_chains():
    r = requests.get(f"{CIJENE_BASE}/chains/", headers=cijene_headers(), timeout=30)
    r.raise_for_status()
    data = r.json()
    return [c["code"] if isinstance(c, dict) else c for c in data.get("chains", data)]


def list_stores(chain_code):
    r = requests.get(f"{CIJENE_BASE}/{chain_code}/stores/", headers=cijene_headers(), timeout=30)
    if r.status_code == 404:
        logger.warning("%s: no stores endpoint (404)", chain_code)
        return []
    r.raise_for_status()
    return r.json().get("stores", [])


def clean_address(address, code, chain_code):
    """
    "Savska 58 87079 Spar" -> "Savska 58"
    "Ulica Maria Gennaria 18 8720 Interspar" -> "Ulica Maria Gennaria 18"

    The upstream address field is street + branch code + chain name, and both
    trailing parts already have their own columns. Showing them to shoppers
    looks like a bug ("Ozaljska 148 87029 Spar").

    The chain name in the address does not always equal the chain code — Spar's
    larger shops say "Interspar" — so the trailing word is allowed a prefix.
    That allowance is only made for codes of 4+ characters, where a match can't
    be coincidence (it would be unsafe for a 2-letter code like "dm").
    """
    if not address:
        return address

    out = str(address).strip()
    base = re.escape(str(chain_code or "")).replace(r"\-", r"[\s-]?")
    name_pat = (r"[A-Za-zÀ-ž]*" + base) if len(str(chain_code or "")) >= 4 else base

    # Several passes: "… 8720 Interspar" needs the name removed before the code
    # is exposed at the end.
    for _ in range(4):
        before = out
        if code:
            out = re.sub(r"[\s,]+" + re.escape(str(code)) + r"\s*$", "", out, flags=re.I)
        if chain_code:
            out = re.sub(r"[\s,]+" + name_pat + r"\s*$", "", out, flags=re.I)
        if out == before:
            break

    return out.strip(" ,-")


def geocode(address, city):
    """Address -> (lat, lon) via Nominatim, or (None, None)."""
    query = ", ".join(p for p in (address, city, "Hrvatska") if p)
    try:
        r = requests.get(
            NOMINATIM,
            params={"q": query, "format": "json", "limit": 1, "countrycodes": "hr"},
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        if r.status_code != 200:
            logger.warning("nominatim HTTP %s for %r", r.status_code, query[:60])
            return None, None
        hits = r.json()
        if hits:
            return float(hits[0]["lat"]), float(hits[0]["lon"])
    except Exception as e:                                    # noqa: BLE001
        logger.warning("geocode failed for %r: %s", query[:60], e)
    return None, None


def collect(geocode_missing=False):
    records = []
    missing = 0
    cleaned = 0

    chains = list_chains()
    logger.info("%d chains", len(chains))

    for chain in chains:
        try:
            stores = list_stores(chain)
        except Exception as e:                                # noqa: BLE001
            logger.error("%s: %s", chain, e)
            continue

        for s in stores:
            code = s.get("code")
            if not code:
                continue
            raw = s.get("address")
            addr = clean_address(raw, code, chain)
            if raw and addr != raw:
                cleaned += 1

            lat, lon = s.get("lat"), s.get("lon")
            source = "source" if lat is not None and lon is not None else "none"

            if lat is None and geocode_missing and addr:
                lat, lon = geocode(addr, s.get("city"))
                if lat is not None:
                    source = "geocoded"
                    time.sleep(GEOCODE_DELAY_S)               # Nominatim policy
            if lat is None:
                missing += 1

            records.append({
                "chain_code": chain,
                "code": str(code),
                "type": s.get("type"),
                "address": addr,
                "city": s.get("city"),
                "lat": lat,
                "lon": lon,
                "phone": s.get("phone"),
                "geocode_source": source,
                "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })

        logger.info("  %-20s %d branches", chain, len(stores))

    logger.info("%d branches; %d addresses cleaned; %d still without coordinates",
                len(records), cleaned, missing)
    return records


def push(records):
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise SystemExit("--push needs SUPABASE_URL and SUPABASE_KEY in the environment")
    total = 0
    for i in range(0, len(records), 500):
        batch = records[i:i + 500]
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/store_branches?on_conflict=chain_code,code",
            headers=supabase_headers(),
            json=batch,
            timeout=60,
        )
        if r.status_code not in (200, 201, 204):
            logger.error("batch %d failed: %s %s", i // 500 + 1, r.status_code, r.text[:200])
        else:
            total += len(batch)
    logger.info("upserted %d branch rows", total)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true", help="upsert into Supabase")
    ap.add_argument("--geocode", action="store_true",
                    help="look up missing coordinates via Nominatim (~12 min for the current set)")
    args = ap.parse_args()

    if not CIJENE_API_KEY:
        raise SystemExit("CIJENE_API_KEY is not set")

    records = collect(geocode_missing=args.geocode)

    print()
    print("sample of cleaned addresses:")
    for r in records[:8]:
        print("  %-18s %-6s %-34s %-16s %s" % (
            r["chain_code"], r["code"], (r["address"] or "")[:34],
            (r["city"] or "")[:16],
            "%.4f,%.4f" % (r["lat"], r["lon"]) if r["lat"] is not None else "no coords",
        ))

    if args.push:
        push(records)


if __name__ == "__main__":
    main()
