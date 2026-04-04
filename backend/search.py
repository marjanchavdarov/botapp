import os, requests
from flask import Blueprint, request, jsonify

search_bp = Blueprint("search", __name__)
CIJENE_API_KEY = os.environ.get("CIJENE_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
CIJENE_BASE = "https://api.cijene.dev/v1"

def cijene_headers():
    return {"Authorization": f"Bearer {CIJENE_API_KEY}"}

def ai_filter(query, products):
    """Use Gemini to filter relevant products from search results"""
    if not products or not GEMINI_API_KEY:
        return products
    
    # Build product list for Gemini
    product_list = []
    for p in products:
        product_list.append(f"{p['ean']}: {p['name']} ({p['brand'] or ''}) {p['quantity'] or ''} {p['unit'] or ''}")
    
    prompt = f"""You are a strict product search filter for a Croatian grocery app.

User searched for: "{query}"

TASK: From the product list below, return ONLY EAN codes of products that ARE the thing the user searched for.
EXCLUDE products that merely CONTAIN the search word as a flavor or ingredient.

Rules:
- "luk" = fresh onion only. EXCLUDE: chips with onion, crackers with onion, seasonings, pasta
- "mlijeko" = plain cow milk only. EXCLUDE: chocolate milk, oat milk, soy milk, flavored milk  
- "kruh" = plain bread only. EXCLUDE: breadcrumbs, crackers, bread-flavored snacks
- "pivo" = beer only. EXCLUDE: beer-flavored snacks
- For brand searches like "coca cola" = only that brand's products

Products:
{chr(10).join(product_list)}

Return ONLY a JSON array of EAN strings. No explanation. Example: ["1234567890123"]
If nothing matches strictly, return []"""

    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-lite:generateContent?key={GEMINI_API_KEY}",
            json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.1, "maxOutputTokens": 500}},
            timeout=10
        )
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        text = text.replace("```json", "").replace("```", "").strip()
        import json
        eans = json.loads(text)
        if isinstance(eans, list) and eans:
            filtered = [p for p in products if p["ean"] in eans]
            return filtered if filtered else products
    except Exception as e:
        print(f"AI filter error: {e}")
    
    return products

@search_bp.route("/api/search")
def search_products():
    q = request.args.get("q", "").strip()
    ai = request.args.get("ai", "1")  # AI filter on by default
    if not q or len(q) < 2:
        return jsonify({"products": []})
    try:
        r = requests.get(f"{CIJENE_BASE}/products/", headers=cijene_headers(),
            params={"q": q, "limit": 50}, timeout=10)
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

        # AI filter
        if ai != "0" and len(products) > 3:
            products = ai_filter(q, products)

        return jsonify({"products": products, "total": len(products)})
    except Exception as e:
        return jsonify({"products": [], "error": str(e)})

@search_bp.route("/api/chain-stats")
def chain_stats():
    try:
        r = requests.get(f"{CIJENE_BASE}/chain-stats/", headers=cijene_headers(), timeout=10)
        return jsonify(r.json() if r.status_code == 200 else {})
    except:
        return jsonify({})

@search_bp.route("/api/stores")
def stores():
    q = request.args.get("q", "")
    try:
        r = requests.get(f"{CIJENE_BASE}/stores/", headers=cijene_headers(),
            params={"q": q} if q else {}, timeout=10)
        return jsonify(r.json() if r.status_code == 200 else {})
    except:
        return jsonify({})
