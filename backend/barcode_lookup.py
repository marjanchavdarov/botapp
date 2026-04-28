import os, requests, math
from flask import Blueprint, request, jsonify

barcode_bp = Blueprint("barcode", __name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
CIJENE_API_KEY = os.environ.get("CIJENE_API_KEY", "")
CIJENE_BASE = "https://api.cijene.dev/v1"

def sb_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}

def cijene_headers():
    return {"Authorization": f"Bearer {CIJENE_API_KEY}"}

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def get_product_image(barcode):
    try:
        r = requests.get(f"{SUPABASE_URL}/storage/v1/object/public/katalog-images/products/{barcode}.jpg", timeout=3)
        if r.status_code == 200:
            return f"{SUPABASE_URL}/storage/v1/object/public/katalog-images/products/{barcode}.jpg"
    except: pass
    try:
        r = requests.get(f"https://world.openfoodfacts.org/api/v0/product/{barcode}.json", timeout=4)
        if r.status_code == 200:
            data = r.json()
            img = data.get("product", {}).get("image_front_url") or data.get("product", {}).get("image_url")
            if img: return img
    except: pass
    return None

def get_product_meta(barcode):
    """Get product name/brand/quantity from cijene.dev aggregated endpoint"""
    try:
        r = requests.get(f"{CIJENE_BASE}/products/{barcode}/", headers=cijene_headers(), timeout=6)
        if r.status_code == 200:
            d = r.json()
            return {
                "name": d.get("name", ""),
                "brand": d.get("brand", ""),
                "quantity": d.get("quantity", ""),
                "unit": d.get("unit", ""),
            }
    except: pass
    return {"name": "", "brand": "", "quantity": "", "unit": ""}

def normalize_store_prices(store_prices, user_lat=None, user_lon=None):
    """Convert cijene.dev store_prices array to our format, one entry per branch"""
    results = []
    for sp in store_prices:
        store = sp.get("store", {})
        sale_price = sp.get("special_price") or sp.get("regular_price") or "0"
        original_price = sp.get("regular_price") if sp.get("special_price") else None
        store_lat = store.get("lat")
        store_lon = store.get("lon")
        dist = None
        if user_lat and user_lon and store_lat and store_lon:
            dist = round(haversine_km(user_lat, user_lon, store_lat, store_lon), 2)
        results.append({
            "store": sp.get("chain", ""),
            "store_code": store.get("code", ""),
            "address": store.get("address", ""),
            "city": store.get("city", ""),
            "zipcode": store.get("zipcode", ""),
            "store_type": store.get("type", ""),
            "lat": store_lat,
            "lon": store_lon,
            "sale_price": str(sale_price),
            "original_price": str(original_price) if original_price else None,
            "unit_price": sp.get("unit_price"),
            "best_price_30": sp.get("best_price_30"),
            "price_date": sp.get("price_date"),
            "distance_km": dist,
        })
    # Sort: price first, then distance
    results.sort(key=lambda x: (float(x["sale_price"] or 999), x["distance_km"] or 999))
    return results

def track_scan(phone, barcode, product_name, prices):
    if not phone or not prices: return
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
            }
        )
    except: pass

@barcode_bp.route("/api/chains")
def get_chains():
    try:
        r = requests.get(f"{CIJENE_BASE}/chains/", headers=cijene_headers(), timeout=5)
        if r.status_code == 200:
            chains = r.json().get("chains", [])
            return jsonify({"chains": chains, "count": len(chains)})
    except: pass
    return jsonify({"chains": [], "count": 0})

@barcode_bp.route("/api/barcode/<barcode>")
def barcode_lookup(barcode):
    """
    Location-aware barcode lookup.
    Accepts: ?lat=&lon= (live GPS) or ?city= (home address city)
    Falls back to chain-aggregate if no location given.
    """
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    city = request.args.get("city", "")
    phone = request.args.get("phone", "")
    d = request.args.get("d", 5.0, type=float)

    has_location = (lat and lon) or city

    if has_location:
        # Use per-branch prices endpoint
        try:
            params = {"eans": barcode}
            if lat and lon:
                params["lat"] = lat
                params["lon"] = lon
                params["d"] = d
            elif city:
                params["city"] = city

            r = requests.get(
                f"{CIJENE_BASE}/prices/",
                headers=cijene_headers(),
                params=params,
                timeout=12
            )
            if r.status_code == 200:
                data = r.json()
                store_prices = data.get("store_prices", [])
                meta = get_product_meta(barcode)
                prices = normalize_store_prices(store_prices, lat, lon)
                track_scan(phone, barcode, meta.get("name"), prices)
                image_url = get_product_image(barcode)
                return jsonify({
                    "barcode": barcode,
                    **meta,
                    "image_url": image_url,
                    "prices": prices,
                    "mode": "nearby",
                })
        except Exception as e:
            print(f"nearby lookup error: {e}")
            # fall through to aggregate

    # Aggregate fallback (no location or error)
    try:
        r = requests.get(
            f"{CIJENE_BASE}/products/{barcode}/",
            headers=cijene_headers(),
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            prices = []
            for chain in data.get("chains", []):
                prices.append({
                    "store": chain["chain"],
                    "store_code": "",
                    "address": "",
                    "city": "",
                    "sale_price": chain["min_price"],
                    "original_price": None,
                    "min_price": chain["min_price"],
                    "max_price": chain["max_price"],
                    "avg_price": chain["avg_price"],
                    "distance_km": None,
                })
            prices.sort(key=lambda x: float(x["sale_price"] or 999))
            track_scan(phone, barcode, data.get("name"), prices)
            image_url = get_product_image(barcode)
            return jsonify({
                "barcode": barcode,
                "name": data.get("name", ""),
                "brand": data.get("brand", ""),
                "quantity": data.get("quantity", ""),
                "unit": data.get("unit", ""),
                "image_url": image_url,
                "prices": prices,
                "mode": "aggregate",
            })
    except Exception as e:
        print(f"aggregate lookup error: {e}")

    # Final fallback: our Supabase DB
    mp = requests.get(f"{SUPABASE_URL}/rest/v1/master_products", headers=sb_headers(),
        params={"barcode": f"eq.{barcode}", "limit": 1}, timeout=10)
    master = mp.json()[0] if mp.status_code == 200 and mp.json() else None
    r = requests.get(f"{SUPABASE_URL}/rest/v1/products", headers=sb_headers(),
        params={"barcode": f"eq.{barcode}", "select": "store,product,sale_price,original_price,valid_until",
                "limit": 100, "order": "sale_price"}, timeout=15)
    prices = r.json() if r.status_code == 200 else []
    seen = {}
    for p in prices:
        s = p["store"]
        if s not in seen or float(p["sale_price"] or 999) < float(seen[s]["sale_price"] or 999):
            seen[s] = p
    unique = sorted(seen.values(), key=lambda x: float(x["sale_price"] or 999))
    image_url = get_product_image(barcode)
    return jsonify({
        "barcode": barcode,
        "name": master["name"] if master else (unique[0]["product"] if unique else ""),
        "brand": master["brand"] if master else "",
        "unit": master["unit"] if master else "",
        "quantity": master["quantity"] if master else "",
        "image_url": image_url,
        "prices": unique,
        "mode": "supabase_fallback",
    })
