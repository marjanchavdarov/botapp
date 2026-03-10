from flask import Flask, request, jsonify, session, g
import importlib
import os

app = Flask(__name__)

# Country configuration
COUNTRIES = {
    'hr': 'croatia',
    'si': 'slovenia',
    # Add more countries
}

@app.before_request
def set_country():
    """Set country based on subdomain or header"""
    # Get from subdomain (hr.katalog.ai, si.katalog.ai)
    subdomain = request.host.split('.')[0]
    
    if subdomain in COUNTRIES:
        country_code = subdomain
    else:
        # Default to Croatia
        country_code = 'hr'
    
    # Load country-specific module
    country = COUNTRIES[country_code]
    g.country = country
    g.translations = importlib.import_module(f'countries.{country}.translations').TRANSLATIONS
    g.stores = load_stores(country)

@app.route("/api/products", methods=["GET"])
def get_products():
    """Get products for current country"""
    country = g.country
    today = date.today().strftime("%Y-%m-%d")
    
    # Query with country filter
    products = requests.get(
        f"{SUPABASE_URL}/rest/v1/products",
        headers=db_headers(),
        params={
            "country": f"eq.{country}",
            "valid_from": f"lte.{today}",
            "valid_until": f"gte.{today}",
            "limit": 100
        }
    )
    
    # Translate for response
    translated = translate_products(products.json(), g.translations)
    return jsonify(translated)

@app.route("/api/chat", methods=["POST"])
def chat():
    """Chat endpoint with country-specific responses"""
    data = request.json
    message = data.get("message")
    country = g.country
    
    # Search products in this country
    products = search_products(message, country)
    
    # Ask Gemini with country context
    prompt = f"""
    Country: {country}
    Language: {get_language_for_country(country)}
    
    Products: {products}
    
    User message: {message}
    
    Respond in the local language. Be friendly and helpful.
    Always mention store names and page numbers.
    """
    
    reply = ask_gemini(prompt)
    
    return jsonify({
        "reply": reply,
        "products": products[:5],
        "suggestions": get_suggestions(country)
    })

def translate_products(products, translations):
    """Translate product data for frontend"""
    translated = []
    for p in products:
        tp = p.copy()
        # Translate store names
        store_key = p.get('store', '').lower()
        tp['store_display'] = translations['stores'].get(store_key, store_key)
        # Format dates
        tp['valid_until_display'] = format_date(p.get('valid_until'), translations['date_format'])
        translated.append(tp)
    return translated
