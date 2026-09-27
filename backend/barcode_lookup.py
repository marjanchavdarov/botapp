"""
barcode_lookup.py — location-aware barcode prices, served through a daily cache.

WHAT CHANGED AND WHY
    Every scan used to call cijene.dev twice: /v1/prices/ for the branch
    prices, then /v1/products/ for the name and brand. Against roughly a
    1000/day allowance that is about 500 scans a day, and exceeding it risks
    the key being blocked — which breaks the app for everyone at once rather
    than degrading gracefully.

    Now each barcode is fetched from cijene.dev at most once per price day, in
    full, nationwide. Everything after that is served from price_cache in
    Supabase.

    Two properties make this work, both verified against the live API on
    2026-09-27:

    1. With a wide radius the response is location-independent. Asking for
       d=500 centred on Zagreb and centred on Split returns byte-identical
       results (493 branches). So the cache needs no location key — one row
       serves the whole country, and distance is still computed per request
       from the coordinates stored in the payload.

    2. Prices refresh at 08:00 Europe/Zagreb and the upstream response carries
       its own price_date, so the cache expires when the data changes rather
       than on a rolling 24h timer — which would serve yesterday's prices from
       midnight until 08:00.

    Net effect: upstream calls become distinct BARCODES per day, not SCANS per
    day. A barcode scanned 500 times costs one call.

REQUIRES
    The price_cache table — see sql/price_cache.sql. Without it the cache
    lookups return None and the endpoint falls back to calling cijene.dev on
    every request, i.e. the old behaviour. It degrades, it does not break.
"""

import math
import os
import threading
from datetime import datetime, timedelta, timezone

import requests
from flask import Blueprint, request, jsonify

try:
    from zoneinfo import ZoneInfo
    ZAGREB = ZoneInfo("Europe/Zagreb")
except Exception:                                  # tzdata missing
    ZAGREB = timezone(timedelta(hours=2))

barcode_bp = Blueprint("barcode", __name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
CIJENE_API_KEY = os.environ.get("CIJENE_API_KEY", "")
CIJENE_BASE = "https://api.cijene.dev/v1"

CACHE_TABLE = "price_cache"

# Wide enough to reach every branch in the country. The reference point is
# arbitrary — the result is identical from anywhere — but fixing it keeps
# responses reproducible.
NATIONWIDE_RADIUS_KM = 500
NATIONWIDE_REF = (45.81, 15.98)

# While waiting for the new day's prices to land upstream, keep serving the
# stale row rather than retrying on every request. Without this, a provider
# that publishes at 08:20 gets hammered from 08:00 to 08:20.
REFRESH_RETRY_MINUTES = 15


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sb_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}


def cijene_headers():
    return {"Authorization": f"Bearer {CIJENE_API_KEY}"}


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def get_product_image(barcode):
    try:
        url = f"{SUPABASE_URL}/storage/v1/object/public/katalog-images/products/{barcode}.jpg"
        if requests.get(url, timeout=3).status_code == 200:
            return url
    except Exception:
        pass
    try:
        r = requests.get(f"https://world.openfoodfacts.org/api/v0/product/{barcode}.json", timeout=4)
        if r.status_code == 200:
            p = r.json().get("product", {})
            return p.get("image_front_url") or p.get("image_url")
    except Exception:
        pass
    return None


def get_product_meta(barcode):
    """Name/brand/quantity from cijene.dev. Kept for the fallback path."""
    try:
        r = requests.get(f"{CIJENE_BASE}/products/{barcode}/", headers=cijene_headers(), timeout=8)
        if r.status_code == 200:
            d = r.json()
            return {"name": d.get("name", ""), "brand": d.get("brand", ""),
                    "quantity": d.get("quantity", ""), "unit": d.get("unit", "")}
    except Exception:
        pass
    return {"name": "", "brand": "", "quantity": "", "unit": ""}


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def current_price_day():
    """
    Which price day a request belongs to, in Croatian local time.

    Prices refresh at 08:00, so at 07:59 on the 27th we are still on the 26th's
    data. Subtracting the refresh hour before taking the date puts the boundary
    exactly at 08:00 local.
    """
    return (datetime.now(ZAGREB) - timedelta(hours=8)).date()


def cache_get(barcode):
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/{CACHE_TABLE}",
            headers=sb_headers(),
            params={"barcode": f"eq.{barcode}", "select": "*", "limit": 1},
            timeout=5,
        )
        if r.status_code != 200:
            return None
        rows = r.json()
        return rows[0] if rows else None
    except Exception:
        return None


def cache_put(barcode, price_day, prices, meta):
    """Best effort — a failed cache write must never fail the user's request."""
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/{CACHE_TABLE}",
            headers={**sb_headers(), "Content-Type": "application/json",
                     "Prefer": "resolution=merge-duplicates,return=minimal"},
            json=[{
                "barcode": barcode,
                "price_day": str(price_day),
                "prices": prices,
                "meta": meta,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }],
            timeout=8,
        )
    except Exception as e:
        print(f"price_cache: write failed for {barcode}: {e}")


def cache_is_usable(row):
    if not row or not row.get("prices"):
        return False
    try:
        stored = datetime.strptime(str(row["price_day"]), "%Y-%m-%d").date()
    except Exception:
        return False
    if stored >= current_price_day():
        return True
    # Stale, but we tried recently: the new day's data hasn't landed yet. Keep
    # serving yesterday's rather than spending a call per request.
    try:
        fetched = datetime.fromisoformat(str(row["fetched_at"]).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - fetched) < timedelta(minutes=REFRESH_RETRY_MINUTES)
    except Exception:
        return False


# One lock per barcode, so a burst of scans on a cold cache makes one upstream
# call rather than one per request.
_locks = {}
_locks_guard = threading.Lock()


def _lock_for(barcode):
    with _locks_guard:
        lock = _locks.get(barcode)
        if lock is None:
            lock = _locks[barcode] = threading.Lock()
        return lock


def fetch_nationwide(barcode):
    """
    One call covering every branch in the country.

    Returns (price_day, store_prices). price_day comes from the upstream's own
    price_date, so the cache tracks the data rather than a wall-clock guess.
    """
    try:
        r = requests.get(
            f"{CIJENE_BASE}/prices/",
            headers=cijene_headers(),
            params={"eans": barcode, "lat": NATIONWIDE_REF[0],
                    "lon": NATIONWIDE_REF[1], "d": NATIONWIDE_RADIUS_KM},
            timeout=15,
        )
        if r.status_code != 200:
            print(f"cijene /prices/ HTTP {r.status_code} for {barcode}")
            return None, None
        rows = r.json().get("store_prices", [])
        if not rows:
            return None, []
        dates = [x.get("price_date") for x in rows if x.get("price_date")]
        return (max(dates) if dates else str(current_price_day())), rows
    except Exception as e:
        print(f"cijene /prices/ failed for {barcode}: {e}")
        return None, None


def load_barcode(barcode):
    """
    Cached nationwide payload for a barcode.

    Returns (price_day, store_prices, meta, served_from) where served_from is
    'cache', 'upstream', 'stale' or 'error'.
    """
    row = cache_get(barcode)
    if cache_is_usable(row):
        return row["price_day"], row["prices"], row.get("meta") or {}, "cache"

    with _lock_for(barcode):
        row = cache_get(barcode)                 # another thread may have filled it
        if cache_is_usable(row):
            return row["price_day"], row["prices"], row.get("meta") or {}, "cache"

        day, prices = fetch_nationwide(barcode)

        if prices is None:                       # upstream unreachable
            if row and row.get("prices"):
                return row["price_day"], row["prices"], row.get("meta") or {}, "stale"
            return None, None, {}, "error"

        meta = (row or {}).get("meta") or get_product_meta(barcode)
        cache_put(barcode, day or current_price_day(), prices, meta)
        return day, prices, meta, "upstream"


# ---------------------------------------------------------------------------
# Shaping
# ---------------------------------------------------------------------------

def rows_to_prices(store_prices, user_lat=None, user_lon=None, radius_km=None):
    """
    Turn the cached nationwide rows into the shape the app expects, keeping only
    branches within the user's radius and computing distance from the branch
    coordinates already in the payload.
    """
    out = []
    for sp in store_prices:
        store = sp.get("store") or {}
        lat, lon = store.get("lat"), store.get("lon")

        dist = None
        if user_lat and user_lon:
            if not lat or not lon:
                # We know roughly where the user is but this branch has no
                # coordinates, so there is no way to tell whether it is near
                # them. Dropping it beats quietly pricing an item at a shop on
                # the far side of the country. (All 493 rows in a typical
                # payload have coordinates, so this rarely fires.)
                continue
            dist = round(haversine_km(user_lat, user_lon, lat, lon), 2)
            if radius_km and dist > radius_km:
                continue

        sale = sp.get("special_price") or sp.get("regular_price") or "0"
        original = sp.get("regular_price") if sp.get("special_price") else None

        out.append({
            "store": sp.get("chain", ""),
            "store_code": store.get("code", ""),
            "address": store.get("address", ""),
            "city": store.get("city", ""),
            "zipcode": store.get("zipcode", ""),
            "store_type": store.get("type", ""),
            "lat": lat,
            "lon": lon,
            "sale_price": str(sale),
            "original_price": str(original) if original else None,
            "unit_price": sp.get("unit_price"),
            "best_price_30": sp.get("best_price_30"),
            "price_date": sp.get("price_date"),
            "distance_km": dist,
        })

    out.sort(key=lambda x: (float(x["sale_price"] or 999),
                            x["distance_km"] if x["distance_km"] is not None else 999))
    return out


def normalize_store_prices(store_prices, user_lat=None, user_lon=None):
    """Deprecated shim kept for compatibility. Use rows_to_prices."""
    return rows_to_prices(store_prices, user_lat, user_lon, None)


def aggregate_from(store_prices):
    """
    Cheapest price per chain, for requests with no location.

    Keeps the original price string rather than round-tripping it through a
    float: str(float("1.00")) is "1.0", which disagrees with rows_to_prices,
    which preserves the source string. Harmless once the app reformats with
    toFixed(2), but there is no reason for the two paths to differ.
    """
    best = {}
    for sp in store_prices:
        chain = sp.get("chain", "")
        sale = sp.get("special_price") or sp.get("regular_price")
        if sale is None:
            continue
        try:
            price = float(sale)
        except (TypeError, ValueError):
            continue
        if chain not in best or price < best[chain][0]:
            best[chain] = (price, str(sale))
    prices = [{"store": c, "store_code": "", "address": "", "city": "",
               "sale_price": raw, "original_price": None,
               "distance_km": None} for c, (_, raw) in best.items()]
    prices.sort(key=lambda x: float(x["sale_price"] or 999))
    return prices


def track_scan(phone, barcode, product_name, prices):
    if not phone or not prices:
        return
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/scan_events",
            headers={**sb_headers(), "Content-Type": "application/json", "Prefer": "return=minimal"},
            json={
                "user_phone": phone,
                "barcode": barcode,
                "product_name": product_name,
                "cheapest_store": prices[0]["store"] if prices else None,
                "cheapest_price": float(prices[0]["sale_price"]) if prices else None,
            },
            timeout=5,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@barcode_bp.route("/api/chains")
def get_chains():
    try:
        r = requests.get(f"{CIJENE_BASE}/chains/", headers=cijene_headers(), timeout=5)
        if r.status_code == 200:
            chains = r.json().get("chains", [])
            return jsonify({"chains": chains, "count": len(chains)})
    except Exception:
        pass
    return jsonify({"chains": [], "count": 0})


def supabase_fallback(barcode):
    """
    Last resort when cijene.dev is unreachable: answer from our own tables.

    Both tables are barcode-indexed, which is why they are used here rather
    than the older `products` table — that one has no barcode column at all,
    so filtering it by barcode could never have matched anything.
    """
    name = brand = unit = quantity = ""
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/master_products", headers=sb_headers(),
                         params={"barcode": f"eq.{barcode}",
                                 "select": "name,brand,unit,quantity", "limit": 1}, timeout=8)
        if r.status_code == 200 and isinstance(r.json(), list) and r.json():
            row = r.json()[0]
            name = row.get("name") or ""
            brand = row.get("brand") or ""
            unit = row.get("unit") or ""
            quantity = row.get("quantity") or ""
    except Exception:
        pass

    prices = []
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/store_prices", headers=sb_headers(),
                         params={"barcode": f"eq.{barcode}",
                                 "select": "store,current_price,regular_price,price_date",
                                 "limit": 500}, timeout=12)
        if r.status_code == 200 and isinstance(r.json(), list):
            # store is "Chain - Branch"; collapse to the cheapest per chain.
            best = {}
            for p in r.json():
                chain = str(p.get("store", "")).split(" - ")[0]
                sale = p.get("current_price")
                if sale is None:
                    continue
                price = float(sale)
                if chain not in best or price < best[chain]:
                    best[chain] = price
            prices = [{"store": c, "store_code": "", "address": "", "city": "",
                       "sale_price": str(p), "original_price": None,
                       "distance_km": None} for c, p in best.items()]
            prices.sort(key=lambda x: float(x["sale_price"] or 999))
    except Exception:
        pass

    if not name and not prices:
        return None
    return {"name": name, "brand": brand, "unit": unit, "quantity": quantity, "prices": prices}


@barcode_bp.route("/api/barcode/<barcode>")
def barcode_lookup(barcode):
    """
    Location-aware barcode lookup.

    ?lat=&lon=&d=   live GPS plus radius (the app sends d=10)
    ?city=          match by city name instead
    neither         cheapest price per chain, nationwide
    """
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    city = request.args.get("city", "")
    phone = request.args.get("phone", "")
    d = request.args.get("d", 5.0, type=float)

    day, store_prices, meta, served = load_barcode(barcode)

    if store_prices is None:
        # cijene.dev unreachable and nothing cached — try our own tables.
        fb = supabase_fallback(barcode)
        if fb:
            track_scan(phone, barcode, fb["name"], fb["prices"])
            return jsonify({"barcode": barcode, "name": fb["name"], "brand": fb["brand"],
                            "quantity": fb["quantity"], "unit": fb["unit"],
                            "image_url": get_product_image(barcode),
                            "prices": fb["prices"], "mode": "supabase_fallback",
                            "served_from": "supabase"})
        return jsonify({"barcode": barcode, "name": "", "brand": "", "quantity": "",
                        "unit": "", "image_url": None, "prices": [],
                        "mode": "error", "served_from": "none"}), 503

    if lat and lon:
        prices = rows_to_prices(store_prices, lat, lon, d)
        mode = "nearby"
    elif city:
        wanted = city.strip().lower()
        prices = rows_to_prices(
            [sp for sp in store_prices
             if str((sp.get("store") or {}).get("city", "")).strip().lower() == wanted],
            None, None, None)
        mode = "nearby"
    else:
        prices = aggregate_from(store_prices)
        mode = "aggregate"

    track_scan(phone, barcode, meta.get("name"), prices)

    return jsonify({
        "barcode": barcode,
        "name": meta.get("name", ""),
        "brand": meta.get("brand", ""),
        "quantity": meta.get("quantity", ""),
        "unit": meta.get("unit", ""),
        "image_url": get_product_image(barcode),
        "prices": prices,
        "mode": mode,
        # Both extra fields are additive — the app ignores them, but they make
        # it obvious from any response whether the cache is doing its job.
        "price_day": day,
        "served_from": served,
    })


@barcode_bp.route("/api/geocode")
def geocode():
    city = request.args.get("city", "")
    if not city:
        return jsonify({"error": "city required"}), 400
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": f"{city}, Croatia", "format": "json", "limit": 1},
            headers={"User-Agent": "Katalog/1.0"},
            timeout=5,
        )
        results = r.json()
        if results:
            return jsonify({"lat": float(results[0]["lat"]), "lon": float(results[0]["lon"]),
                            "display": results[0]["display_name"]})
    except Exception as e:
        print(f"geocode error: {e}")
    return jsonify({"error": "not found"}), 404
