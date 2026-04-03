import os, re, requests
from flask import Blueprint, request, jsonify

eq_bp = Blueprint('equivalents', __name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

def headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}"
    }

def parse_unit_price(sale_price, quantity_str):
    """Calculate price per litre or per kg from quantity string"""
    if not quantity_str or not sale_price:
        return None, None
    
    try:
        price = float(sale_price)
        q = quantity_str.lower().strip()
        
        # Match patterns like: 0.5 l, 500ml, 6x0.5l, 4x330ml, 1kg, 500g
        # Multi-pack: 6x0.5l, 4x330ml
        multi = re.match(r'(\d+)\s*[x×]\s*([\d.]+)\s*(ml|l|g|kg)', q)
        if multi:
            count = int(multi.group(1))
            amount = float(multi.group(2))
            unit = multi.group(3)
            if unit == 'ml': amount /= 1000
            if unit == 'g': amount /= 1000
            total = count * amount
            unit_label = 'L' if unit in ('ml','l') else 'kg'
            return round(price / total, 2), unit_label

        # Plain number with unit in master_products (e.g. quantity="0.5", unit="L")
        plain = re.match(r'^([\d.]+)$', q)
        if plain:
            amount = float(plain.group(1))
            # Check if it looks like litres (< 5) or kg
            if amount <= 5:
                return round(price / amount, 2), 'L'
            else:
                return round(price / amount, 2), 'kg'

        # Single: 0.5l, 500ml, 1kg, 500g
        single = re.match(r'([\d.]+)\s*(ml|l|g|kg)', q)
        if single:
            amount = float(single.group(1))
            unit = single.group(2)
            if unit == 'ml': amount /= 1000
            if unit == 'g': amount /= 1000
            unit_label = 'L' if unit in ('ml','l') else 'kg'
            return round(price / amount, 2), unit_label

        return None, None
    except:
        return None, None

@eq_bp.route("/api/equivalents/<barcode>")
def find_equivalents(barcode):
    # Get the original product
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/master_products",
        headers=headers(),
        params={"barcode": f"eq.{barcode}", "limit": 1}
    )
    
    if r.status_code != 200 or not r.json():
        return jsonify({"equivalents": []})
    
    master = r.json()[0]
    brand = master.get("brand", "")
    name = master.get("name", "")
    
    if not brand or not name:
        return jsonify({"equivalents": []})

    # Extract base product name (remove size/quantity words)
    base_words = re.sub(r'\d+[\d.,]*\s*(ml|l|g|kg|kom|L)', '', name, flags=re.IGNORECASE).strip()
    base_words = ' '.join(base_words.split()[:3])  # First 3 words

    # Search master_products for same brand + similar name
    search = requests.get(
        f"{SUPABASE_URL}/rest/v1/master_products",
        headers=headers(),
        params={
            "brand": f"eq.{brand}",
            "name": f"ilike.*{base_words.split()[0]}*",
            "limit": 30
        }
    )

    if search.status_code != 200:
        return jsonify({"equivalents": []})

    candidates = search.json()
    
    if len(candidates) <= 1:
        return jsonify({"equivalents": []})

    # For each candidate, get cheapest price from products table
    results = []
    for c in candidates:
        prices_r = requests.get(
            f"{SUPABASE_URL}/rest/v1/products",
            headers=headers(),
            params={
                "barcode": f"eq.{c['barcode']}",
                "select": "store,sale_price,quantity",
                "order": "sale_price",
                "limit": 5
            }
        )
        
        if prices_r.status_code != 200 or not prices_r.json():
            continue
        
        prices = prices_r.json()
        cheapest = prices[0]
        
        unit_price, unit_label = parse_unit_price(
            cheapest["sale_price"],
            c.get("quantity") or cheapest.get("quantity")
        )
        
        results.append({
            "barcode": c["barcode"],
            "name": c["name"],
            "brand": c["brand"],
            "quantity": c.get("quantity"),
            "cheapest_store": cheapest["store"],
            "cheapest_price": float(cheapest["sale_price"]),
            "unit_price": unit_price,
            "unit_label": unit_label,
            "other_stores": len(prices) - 1,
            "is_scanned": c["barcode"] == barcode
        })

    # Sort by unit price, fallback to total price
    results.sort(key=lambda x: (x["unit_price"] or 999, x["cheapest_price"]))

    return jsonify({"equivalents": results, "brand": brand, "base_name": base_words})
