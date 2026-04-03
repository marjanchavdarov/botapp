import os, requests
from flask import Blueprint, request, jsonify

search_bp = Blueprint("search", __name__)
CIJENE_API_KEY = os.environ.get("CIJENE_API_KEY", "")
CIJENE_BASE = "https://api.cijene.dev/v1"

def headers():
    return {"Authorization": f"Bearer {CIJENE_API_KEY}"}

@search_bp.route("/api/search")
def search_products():
    q = request.args.get("q", "").strip()
    if not q or len(q) < 2:
        return jsonify({"products": []})
    try:
        r = requests.get(f"{CIJENE_BASE}/products/", headers=headers(),
            params={"q": q, "limit": 20}, timeout=10)
        if r.status_code != 200:
            return jsonify({"products": []})
        
        products = []
        for p in r.json().get("products", []):
            chains = p.get("chains", [])
            if not chains:
                continue
            cheapest = min(chains, key=lambda c: float(c["min_price"] or 999))
            products.append({
                "ean": p["ean"],
                "name": p["name"],
                "brand": p["brand"],
                "quantity": p["quantity"],
                "unit": p["unit"],
                "cheapest_store": cheapest["chain"],
                "cheapest_price": cheapest["min_price"],
                "store_count": len(chains),
            })
        return jsonify({"products": products})
    except Exception as e:
        return jsonify({"products": [], "error": str(e)})

@search_bp.route("/api/chain-stats")
def chain_stats():
    try:
        r = requests.get(f"{CIJENE_BASE}/chain-stats/", headers=headers(), timeout=10)
        return jsonify(r.json() if r.status_code == 200 else {})
    except:
        return jsonify({})

@search_bp.route("/api/stores")
def stores():
    q = request.args.get("q", "")
    try:
        r = requests.get(f"{CIJENE_BASE}/stores/", headers=headers(),
            params={"q": q} if q else {}, timeout=10)
        return jsonify(r.json() if r.status_code == 200 else {})
    except:
        return jsonify({})
