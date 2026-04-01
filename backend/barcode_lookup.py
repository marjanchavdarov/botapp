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
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/products",
        headers=headers(),
        params={
            "barcode": f"eq.{barcode}",
            "select": "store,product,brand,quantity,sale_price,original_price,valid_until",
            "limit": 50,
            "order": "sale_price"
        },
        timeout=15
    )
    print(f"DEBUG barcode={barcode} status={r.status_code} url={r.url} response={r.text[:200]}")
    prices = r.json() if r.status_code == 200 else []
    # Deduplicate by store
    seen = set()
    unique = []
    for p in prices:
        if p["store"] not in seen:
            seen.add(p["store"])
            unique.append(p)
    return jsonify({"barcode": barcode, "prices": unique})
