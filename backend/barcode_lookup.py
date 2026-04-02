import os, requests
from flask import Blueprint, jsonify, request
from datetime import date

barcode_bp = Blueprint('barcode', __name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

def headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}"
    }

@barcode_bp.route("/api/barcode/<barcode>")
def barcode_lookup(barcode):
    # Get clean product info from master_products
    mp = requests.get(
        f"{SUPABASE_URL}/rest/v1/master_products",
        headers=headers(),
        params={"barcode": f"eq.{barcode}", "limit": 1},
        timeout=10
    )
    master = mp.json()[0] if mp.status_code == 200 and mp.json() else None

    # Get prices from products table
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/products",
        headers=headers(),
        params={
            "barcode": f"eq.{barcode}",
            "select": "store,product,sale_price,original_price,valid_until",
            "limit": 100,
            "order": "sale_price"
        },
        timeout=15
    )
    prices = r.json() if r.status_code == 200 else []

    # Deduplicate by store, keep cheapest
    seen = {}
    for p in prices:
        store = p["store"]
        if store not in seen or float(p["sale_price"] or 999) < float(seen[store]["sale_price"] or 999):
            seen[store] = p

    unique = sorted(seen.values(), key=lambda x: float(x["sale_price"] or 999))

    # Track scan if user phone provided
    phone = request.args.get("phone")
    if phone and unique:
        try:
            # Increment user search count
            requests.post(
                f"{SUPABASE_URL}/rest/v1/rpc/increment_searches",
                headers={**headers(), "Content-Type": "application/json"},
                json={"user_phone": phone}
            )
            # Log scan event
            requests.post(
                f"{SUPABASE_URL}/rest/v1/scan_events",
                headers={**headers(), "Content-Type": "application/json", "Prefer": "return=minimal"},
                json={
                    "user_phone": phone,
                    "barcode": barcode,
                    "product_name": master["name"] if master else (unique[0]["product"] if unique else ""),
                    "cheapest_store": unique[0]["store"] if unique else None,
                    "cheapest_price": float(unique[0]["sale_price"]) if unique else None,
                }
            )
        except Exception as e:
            print(f"Track error: {e}")

    return jsonify({
        "barcode": barcode,
        "name": master["name"] if master else (unique[0]["product"] if unique else ""),
        "brand": master["brand"] if master else "",
        "unit": master["unit"] if master else "",
        "quantity": master["quantity"] if master else "",
        "prices": unique
    })
