import os, requests
from flask import Blueprint, request, jsonify

barcode_bp = Blueprint("barcode", __name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
CIJENE_API_KEY = os.environ.get("CIJENE_API_KEY", "")
CIJENE_BASE = "https://api.cijene.dev/v1"

def sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}"
    }

def cijene_headers():
    return {"Authorization": f"Bearer {CIJENE_API_KEY}"}

def get_product_image(barcode):
    """Fetch product image from Supabase cache or Open Food Facts"""
    try:
        # Check Supabase cache first
        r = requests.get(
            f"{SUPABASE_URL}/storage/v1/object/public/katalog-images/products/{barcode}.jpg",
            timeout=3
        )
        if r.status_code == 200:
            return f"{SUPABASE_URL}/storage/v1/object/public/katalog-images/products/{barcode}.jpg"
    except: pass
    try:
        # Try Open Food Facts
        r = requests.get(f"https://world.openfoodfacts.org/api/v0/product/{barcode}.json", timeout=4)
        if r.status_code == 200:
            data = r.json()
            img = data.get("product", {}).get("image_front_url") or data.get("product", {}).get("image_url")
            if img:
                return img
    except: pass
    try:
        # Try Open Beauty Facts
        r = requests.get(f"https://world.openbeautyfacts.org/api/v0/product/{barcode}.json", timeout=4)
        if r.status_code == 200:
            data = r.json()
            img = data.get("product", {}).get("image_front_url") or data.get("product", {}).get("image_url")
            if img:
                return img
    except: pass
    return None

@barcode_bp.route("/api/chains")
def get_chains():
    try:
        r = requests.get(f"{CIJENE_BASE}/chains/", headers=cijene_headers(), timeout=5)
        if r.status_code == 200:
            chains = r.json().get("chains", [])
            return jsonify({"chains": chains, "count": len(chains)})
    except: pass
    return jsonify({"chains": [], "count": 25})

@barcode_bp.route("/api/barcode/<barcode>")
def barcode_lookup(barcode):
    # Call cijene.dev API
    try:
        r = requests.get(
            f"{CIJENE_BASE}/products/{barcode}/",
            headers=cijene_headers(),
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            # Normalize to our format
            prices = []
            for chain in data.get("chains", []):
                prices.append({
                    "store": chain["chain"],
                    "product": chain["name"],
                    "sale_price": chain["min_price"],
                    "original_price": None,
                    "min_price": chain["min_price"],
                    "max_price": chain["max_price"],
                    "avg_price": chain["avg_price"],
                    "valid_until": chain["price_date"],
                })
            # Sort by min price
            prices.sort(key=lambda x: float(x["sale_price"] or 999))

            # Track scan if phone provided
            phone = request.args.get("phone")
            if phone and prices:
                try:
                    requests.post(
                        f"{SUPABASE_URL}/rest/v1/rpc/increment_searches",
                        headers={**sb_headers(), "Content-Type": "application/json"},
                        json={"user_phone": phone}
                    )
                    requests.post(
                        f"{SUPABASE_URL}/rest/v1/scan_events",
                        headers={**sb_headers(), "Content-Type": "application/json", "Prefer": "return=minimal"},
                        json={
                            "user_phone": phone,
                            "barcode": barcode,
                            "product_name": data.get("name"),
                            "cheapest_store": prices[0]["store"] if prices else None,
                            "cheapest_price": float(prices[0]["sale_price"]) if prices else None,
                        }
                    )
                except:
                    pass

            image_url = get_product_image(barcode)
            return jsonify({
                "barcode": barcode,
                "name": data.get("name"),
                "brand": data.get("brand"),
                "quantity": data.get("quantity"),
                "unit": data.get("unit"),
                "image_url": image_url,
                "prices": prices
            })
    except Exception as e:
        print(f"cijene.dev error: {e}")

    # Fallback to our Supabase DB
    mp = requests.get(f"{SUPABASE_URL}/rest/v1/master_products", headers=sb_headers(),
        params={"barcode": f"eq.{barcode}", "limit": 1}, timeout=10)
    master = mp.json()[0] if mp.status_code == 200 and mp.json() else None

    r = requests.get(f"{SUPABASE_URL}/rest/v1/products", headers=sb_headers(),
        params={"barcode": f"eq.{barcode}", "select": "store,product,sale_price,original_price,valid_until",
                "limit": 100, "order": "sale_price"}, timeout=15)
    prices = r.json() if r.status_code == 200 else []

    seen = {}
    for p in prices:
        store = p["store"]
        if store not in seen or float(p["sale_price"] or 999) < float(seen[store]["sale_price"] or 999):
            seen[store] = p
    unique = sorted(seen.values(), key=lambda x: float(x["sale_price"] or 999))

    image_url = get_product_image(barcode)
    return jsonify({
        "barcode": barcode,
        "name": master["name"] if master else (unique[0]["product"] if unique else ""),
        "brand": master["brand"] if master else "",
        "unit": master["unit"] if master else "",
        "quantity": master["quantity"] if master else "",
        "image_url": image_url,
        "prices": unique
    })


import math

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

@barcode_bp.route("/api/barcode/<barcode>/nearby")
def barcode_nearby(barcode):
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    d = request.args.get("d", 5.0, type=float)

    if not lat or not lon:
        return jsonify({"error": "lat and lon required"}), 400

    try:
        # Fetch per-branch prices filtered by location from cijene.dev
        r = requests.get(
            f"{CIJENE_BASE}/prices/",
            headers=cijene_headers(),
            params={"eans": barcode, "lat": lat, "lon": lon, "d": d},
            timeout=12
        )
        if r.status_code != 200:
            return jsonify({"error": "cijene.dev error", "status": r.status_code}), 502

        data = r.json()
        raw_prices = data.get("prices", [])

        # Get product meta from regular endpoint
        meta = {}
        try:
            mr = requests.get(
                f"{CIJENE_BASE}/products/{barcode}/",
                headers=cijene_headers(),
                timeout=6
            )
            if mr.status_code == 200:
                md = mr.json()
                meta = {
                    "name": md.get("name", ""),
                    "brand": md.get("brand", ""),
                    "quantity": md.get("quantity", ""),
                    "unit": md.get("unit", ""),
                }
        except:
            pass

        # Normalise and attach distance
        prices = []
        for p in raw_prices:
            store_lat = p.get("lat")
            store_lon = p.get("lon")
            dist = None
            if store_lat and store_lon:
                dist = round(haversine_km(lat, lon, store_lat, store_lon), 2)

            prices.append({
                "store": p.get("chain_code") or p.get("chain") or p.get("store", ""),
                "store_code": p.get("store_code") or p.get("code", ""),
                "address": p.get("address", ""),
                "city": p.get("city", ""),
                "sale_price": str(p.get("price") or p.get("sale_price") or 0),
                "original_price": str(p.get("regular_price") or p.get("original_price") or ""),
                "distance_km": dist,
            })

        # Sort by price, then distance
        prices.sort(key=lambda x: (float(x["sale_price"] or 999), x["distance_km"] or 999))

        image_url = get_product_image(barcode)

        # Track scan
        phone = request.args.get("phone")
        if phone and prices:
            try:
                requests.post(
                    f"{SUPABASE_URL}/rest/v1/scan_events",
                    headers={**sb_headers(), "Content-Type": "application/json", "Prefer": "return=minimal"},
                    json={
                        "user_phone": phone,
                        "barcode": barcode,
                        "product_name": meta.get("name"),
                        "cheapest_store": prices[0]["store"] if prices else None,
                        "cheapest_price": float(prices[0]["sale_price"]) if prices else None,
                    }
                )
            except:
                pass

        return jsonify({
            "barcode": barcode,
            **meta,
            "image_url": image_url,
            "prices": prices,
            "location": {"lat": lat, "lon": lon, "radius_km": d},
        })

    except Exception as e:
        print(f"nearby error: {e}")
        return jsonify({"error": str(e)}), 500
