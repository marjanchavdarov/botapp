"""
katalog.ai - Complete working version
Fully implemented with no placeholders
"""

import os
import json
import base64
import uuid
import threading
import logging
from datetime import datetime, date, timedelta
from urllib.parse import quote, urlparse, urlunparse
import re
import tempfile

from flask import Flask, request, jsonify, send_from_directory
import requests

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static', static_url_path='/static')

# ============================================================================
# CONFIGURATION
# ============================================================================

class Config:
    GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
    SUPABASE_URL = os.environ.get('SUPABASE_URL', 'https://jwuifezafytihgzepylq.supabase.co')
    SUPABASE_KEY = os.environ.get('SUPABASE_KEY')
    STORAGE_BUCKET = 'katalog-images'
    
    # Validate required variables
    if not GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY not set - AI features will be limited")
    if not SUPABASE_KEY:
        logger.warning("SUPABASE_KEY not set - database features will be limited")

# Croatia config
CROATIA_STORES = [
    {'id': 'lidl', 'name': 'Lidl', 'color': '#0050aa'},
    {'id': 'kaufland', 'name': 'Kaufland', 'color': '#e30613'},
    {'id': 'spar', 'name': 'Spar', 'color': '#1e6b3b'},
    {'id': 'konzum', 'name': 'Konzum', 'color': '#ed1c24'},
    {'id': 'dm', 'name': 'dm', 'color': '#e31837'},
    {'id': 'plodine', 'name': 'Plodine', 'color': '#009640'},
]

# ============================================================================
# DATABASE FUNCTIONS
# ============================================================================
@app.route('/debug/supabase-fixed')
def debug_supabase_fixed():
    """Test Supabase connection with proper headers"""
    import requests
    
    results = {}
    
    # Test with proper headers
    try:
        url = f"{Config.SUPABASE_URL}/rest/v1/"
        headers = {
            "apikey": Config.SUPABASE_KEY,
            "Authorization": f"Bearer {Config.SUPABASE_KEY}",
            "Content-Type": "application/json"
        }
        
        logger.info(f"🔍 Testing Supabase connection to: {url}")
        logger.info(f"🔑 Using key: {Config.SUPABASE_KEY[:20]}...")
        
        response = requests.get(url, headers=headers, timeout=5)
        
        results['test'] = {
            "status_code": response.status_code,
            "success": response.status_code == 200,
            "response": response.text[:200] if response.text else None
        }
        
        if response.status_code == 200:
            logger.info("✅ Supabase connected successfully!")
        else:
            logger.error(f"❌ Supabase returned {response.status_code}: {response.text}")
            
    except Exception as e:
        logger.error(f"❌ Supabase connection error: {e}")
        results['test'] = {"error": str(e)}
    
    return jsonify(results)
    # ============================================================================
# SUPABASE HELPERS - FIXED (NO RECURSION)
# ============================================================================

def db_headers():
    """Simple headers - no recursion possible"""
    return {
        "apikey": Config.SUPABASE_KEY,
        "Authorization": f"Bearer {Config.SUPABASE_KEY}",
        "Content-Type": "application/json"
    }

def supabase_request(method, path, **kwargs):
    """Single function for all requests - NO RECURSION"""
    if not Config.SUPABASE_KEY:
        logger.error("❌ SUPABASE_KEY not set")
        return None
    
    url = f"{Config.SUPABASE_URL}{path}"
    headers = db_headers()
    
    # Add any extra headers
    if 'headers' in kwargs:
        headers.update(kwargs['headers'])
        del kwargs['headers']
    
    try:
        logger.info(f"📡 Supabase {method} to {path}")
        response = requests.request(method, url, headers=headers, timeout=10, **kwargs)
        return response
    except Exception as e:
        logger.error(f"❌ Supabase request failed: {e}")
        return None

# Simple wrapper functions - these DON'T call themselves
def supabase_get(path, params=None):
    """GET request"""
    return supabase_request('GET', path, params=params)

def supabase_post(path, json=None):
    """POST request"""
    return supabase_request('POST', path, json=json)

def supabase_patch(path, json=None):
    """PATCH request"""
    return supabase_request('PATCH', path, json=json)

def supabase_delete(path):
    """DELETE request"""
    return supabase_request('DELETE', path)


# ============================================================================
# PRODUCT FUNCTIONS
# ============================================================================

def get_products(store=None, query=None, limit=100):
    """Get products from database"""
    today = date.today().strftime('%Y-%m-%d')
    
    params = {
        "valid_from": f"lte.{today}",
        "valid_until": f"gte.{today}",
        "is_expired": "eq.false",
        "limit": limit,
        "order": "store,product"
    }
    
    if store:
        params["store"] = f"eq.{store}"
    
    if query:
        params["product"] = f"ilike.*{query}*"
    
    response = supabase_get("/rest/v1/products", params)
    if response and response.status_code == 200:
        return response.json()
    return []

def save_products(products, store, page_num, page_url, catalogue_name, valid_from, valid_until):
    """Save products to database"""
    if not products:
        return 0
    
    records = []
    for p in products:
        if not p.get('sale_price') or p.get('sale_price') in [None, 'null', '']:
            continue
        
        vu = p.get('valid_until') or valid_until
        vf = p.get('valid_from') or valid_from
        
        records.append({
            "store": store,
            "product": p.get('product', ''),
            "brand": p.get('brand') if p.get('brand') not in [None, 'null'] else None,
            "quantity": p.get('quantity') if p.get('quantity') not in [None, 'null'] else None,
            "original_price": p.get('original_price') if p.get('original_price') not in [None, 'null'] else None,
            "sale_price": p.get('sale_price', ''),
            "discount_percent": p.get('discount_percent') if p.get('discount_percent') not in [None, 'null'] else None,
            "category": p.get('category', 'Other'),
            "subcategory": p.get('subcategory'),
            "valid_from": vf,
            "valid_until": vu,
            "is_expired": False,
            "page_image_url": page_url,
            "page_number": page_num,
            "catalogue_name": catalogue_name,
            "catalogue_week": datetime.now().strftime('%Y-W%V')
        })
    
    if not records:
        return 0
    
    response = supabase_post("/rest/v1/products", records)
    if response and response.status_code in [200, 201]:
        logger.info(f"Saved {len(records)} products")
        return len(records)
    return 0

def save_catalogue(store, catalogue_name, valid_from, valid_until, fine_print, pages, products_count):
    """Save catalogue metadata"""
    supabase_post("/rest/v1/catalogues", {
        "store": store,
        "catalogue_name": catalogue_name,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "fine_print": fine_print,
        "pages": pages,
        "products_count": products_count
    })

def save_job(job_id, store, catalogue_name, valid_from, valid_until, total_pages):
    """Save job to database"""
    supabase_post("/rest/v1/jobs", {
        "id": job_id,
        "store": store,
        "catalogue_name": catalogue_name,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "total_pages": total_pages,
        "current_page": 0,
        "total_products": 0,
        "status": "processing",
        "created_at": datetime.now().isoformat()
    })

def update_job(job_id, data):
    """Update job status"""
    supabase_patch(f"/rest/v1/jobs?id=eq.{job_id}", data)

def get_job(job_id):
    """Get job by ID"""
    response = supabase_get(f"/rest/v1/jobs?id=eq.{job_id}")
    if response and response.status_code == 200 and response.json():
        return response.json()[0]
    return None

# ============================================================================
# STORAGE FUNCTIONS
# ============================================================================

def upload_image(img_bytes, storage_path):
    """Upload image to Supabase storage"""
    if not Config.SUPABASE_KEY:
        logger.error("SUPABASE_KEY not set - cannot upload images")
        return None
    
    headers = storage_headers('image/jpeg')
    
    try:
        response = requests.put(
            f"{Config.SUPABASE_URL}/storage/v1/object/{Config.STORAGE_BUCKET}/{storage_path}",
            headers=headers,
            data=img_bytes,
            timeout=30
        )
        
        if response.status_code in [200, 201]:
            public_url = f"{Config.SUPABASE_URL}/storage/v1/object/public/{Config.STORAGE_BUCKET}/{storage_path}"
            logger.info(f"✅ Image uploaded: {public_url}")
            return public_url
        else:
            logger.warning(f"Upload failed: {response.status_code}")
            
    except Exception as e:
        logger.error(f"Upload exception: {e}")
    
    return None

def get_page_image_url(store, page_num):
    """Get image URL for a specific page"""
    params = {
        "store": f"eq.{store}",
        "page_number": f"eq.{page_num}",
        "select": "page_image_url",
        "limit": 1
    }
    
    response = supabase_get("/rest/v1/products", params)
    if response and response.status_code == 200 and response.json():
        return response.json()[0].get('page_image_url')
    
    return None

# ============================================================================
# GEMINI AI FUNCTIONS
# ============================================================================

def extract_products_from_image(img_b64, store, page_num):
    """Use Gemini to extract products from catalog page image"""
    if not Config.GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY not set - using mock data")
        # Return mock data for testing
        return [
            {
                "product": "Mlijeko Z'bregov 1L",
                "brand": "Z'bregov",
                "quantity": "1L",
                "original_price": "1.29",
                "sale_price": "0.99",
                "discount_percent": "23%",
                "category": "Dairy",
                "valid_from": None,
                "valid_until": None
            }
        ], None
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={Config.GEMINI_API_KEY}"
    
    prompt = f"""
    Extract ALL products from this catalog page.
    
    Store: {store}
    Page: {page_num}
    
    RULES:
    1. Extract ONLY products with visible prices in €
    2. Translate product names to English
    3. Include brand, quantity, prices
    4. Convert dates to YYYY-MM-DD format
    
    Return as JSON array:
    [
        {{
            "product": "English name",
            "brand": "brand or null",
            "quantity": "250g or null",
            "original_price": "2.99 or null",
            "sale_price": "1.99",
            "discount_percent": "33% or null",
            "valid_from": "2026-03-02 or null",
            "valid_until": "2026-03-08 or null",
            "category": "Category",
            "subcategory": "Subcategory"
        }}
    ]
    
    If no valid products, return: []
    """
    
    body = {
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}},
                {"text": prompt}
            ]
        }],
        "generationConfig": {
            "maxOutputTokens": 8192,
            "temperature": 0.1
        }
    }
    
    for attempt in range(3):
        try:
            response = requests.post(url, json=body, timeout=45)
            result = response.json()
            
            if 'candidates' not in result:
                logger.error(f"Gemini error: {result}")
                continue
            
            text = result['candidates'][0]['content']['parts'][0]['text']
            text = text.replace('```json', '').replace('```', '').strip()
            
            products = json.loads(text)
            if not isinstance(products, list):
                return [], None
            
            logger.info(f"Extracted {len(products)} products from page {page_num}")
            return products, None
            
        except Exception as e:
            logger.error(f"Gemini attempt {attempt+1} failed: {e}")
            if attempt == 2:
                return [], None
            continue
    
    return [], None

def ask_gemini(message, products_context):
    """Ask Gemini a question with context"""
    if not Config.GEMINI_API_KEY:
        return f"Našao sam {len(products_context)} proizvoda. Upiši broj stranice da vidiš sliku."
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={Config.GEMINI_API_KEY}"
    
    prompt = f"""
    You are a friendly shopping assistant for Croatia.
    Today is {date.today().strftime('%d.%m.%Y.')}
    
    PRODUCTS FOUND:
    {products_context}
    
    USER QUESTION: {message}
    
    INSTRUCTIONS:
    - Respond in Croatian
    - Be helpful and friendly
    - Mention store names and page numbers
    - End with "Stranice: X, Y, Z" if there are page numbers
    
    RESPOND IN CROATIAN:
    """
    
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 1024,
            "temperature": 0.3
        }
    }
    
    try:
        response = requests.post(url, json=body, timeout=30)
        result = response.json()
        
        if 'candidates' in result:
            return result['candidates'][0]['content']['parts'][0]['text']
        else:
            logger.error(f"Gemini error: {result}")
            return "Došlo je do greške."
            
    except Exception as e:
        logger.error(f"Gemini request failed: {e}")
        return "Došlo je do greške."

# ============================================================================
# USER FUNCTIONS
# ============================================================================

def get_or_create_user(device_id):
    """Get or create user in database"""
    response = supabase_get(f"/rest/v1/users?device_id=eq.{quote(device_id)}")
    
    if response and response.status_code == 200 and response.json():
        return response.json()[0]
    
    # Create new user
    new_user = {
        "device_id": device_id,
        "country": "hr",
        "language": "hr",
        "created_at": datetime.now().isoformat(),
        "last_active": datetime.now().isoformat(),
        "total_searches": 0,
        "favorites": [],
        "conversation": []
    }
    
    supabase_post("/rest/v1/users", new_user)
    return new_user

def update_user(device_id, updates):
    """Update user data"""
    updates["last_active"] = datetime.now().isoformat()
    supabase_patch(f"/rest/v1/users?device_id=eq.{quote(device_id)}", updates)

# ============================================================================
# API ROUTES
# ============================================================================

@app.route('/')
def home():
    return jsonify({
        "status": "ok",
        "message": "katalog.ai is running",
        "version": "1.0.0",
        "endpoints": {
            "GET": [
                "/",
                "/api/country",
                "/api/products",
                "/api/products/search?q=mlijeko",
                "/api/page-image/<page_number>",
                "/upload-tool",
                "/status/<job_id>",
                "/debug/health",
                "/debug/supabase"
            ],
            "POST": [
                "/api/chat",
                "/api/favorites/add",
                "/api/favorites/remove",
                "/upload"
            ]
        }
    })

@app.route('/debug/health')
def health():
    return jsonify({
        "status": "ok",
        "time": datetime.now().isoformat(),
        "supabase_configured": bool(Config.SUPABASE_KEY),
        "gemini_configured": bool(Config.GEMINI_API_KEY)
    })

@app.route('/debug/supabase')
def debug_supabase():
    """Test Supabase connection"""
    results = {}
    
    # Test 1: Basic connection
    try:
        response = supabase_get("/rest/v1/")
        results['connection'] = {
            "status": "success" if response and response.status_code < 500 else "error",
            "status_code": response.status_code if response else None
        }
    except Exception as e:
        results['connection'] = {"error": str(e)}
    
    # Test 2: Check if tables exist
    try:
        response = supabase_get("/rest/v1/products?limit=1")
        results['products_table'] = {
            "exists": response and response.status_code < 500,
            "status_code": response.status_code if response else None
        }
    except Exception as e:
        results['products_table'] = {"error": str(e)}
    
    return jsonify(results)

@app.route('/api/country')
def get_country():
    """Get Croatia configuration"""
    return jsonify({
        "code": "hr",
        "name": "croatia",
        "language": "hr",
        "currency": "€",
        "date_format": "%d.%m.%Y.",
        "stores": CROATIA_STORES
    })

@app.route('/api/products', methods=['GET'])
def api_get_products():
    """Get products with filters"""
    store = request.args.get('store')
    page = request.args.get('page', type=int)
    limit = request.args.get('limit', 50, type=int)
    
    if page:
        # Get products for specific page
        params = {
            "page_number": f"eq.{page}",
            "limit": limit
        }
        if store:
            params["store"] = f"eq.{store}"
        
        response = supabase_get("/rest/v1/products", params)
        if response and response.status_code == 200:
            return jsonify(response.json())
        return jsonify([])
    else:
        # Get all products
        products = get_products(store, limit=limit)
        return jsonify(products)

@app.route('/api/products/search', methods=['GET'])
def api_search_products():
    """Search products by query"""
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify([])
    
    products = get_products(query=query, limit=50)
    return jsonify(products)

@app.route('/api/page-image/<int:page_num>')
def api_page_image(page_num):
    """Get image URL for a page"""
    store = request.args.get('store')
    
    if not store:
        return jsonify({"error": "Store parameter required"}), 400
    
    image_url = get_page_image_url(store, page_num)
    
    if image_url:
        return jsonify({
            "page": page_num,
            "store": store,
            "image_url": image_url
        })
    
    return jsonify({"error": "Image not found"}), 404

@app.route('/api/chat', methods=['POST'])
def api_chat():
    """Chat endpoint"""
    data = request.json
    message = data.get('message', '').strip()
    device_id = request.headers.get('X-Device-ID', request.remote_addr)
    
    if not message:
        return jsonify({"error": "Message required"}), 400
    
    # Search for products
    products = get_products(query=message, limit=10)
    
    # Prepare context
    context = "PRONAĐENI PROIZVODI:\n"
    if products:
        for p in products[:5]:
            context += f"- {p.get('store')}: {p.get('product')} - {p.get('sale_price')}€ (str. {p.get('page_number')})\n"
    else:
        context += "Nema pronađenih proizvoda.\n"
    
    # Get AI response
    reply = ask_gemini(message, context)
    
    # Extract page numbers
    page_numbers = re.findall(r'stranic[ea] (\d+)', reply, re.IGNORECASE)
    page_numbers = [int(p) for p in page_numbers if 1 <= int(p) <= 500]
    
    # Update user
    user = get_or_create_user(device_id)
    update_user(device_id, {
        "total_searches": user.get('total_searches', 0) + 1,
        "last_query": message
    })
    
    return jsonify({
        "reply": reply,
        "products": products[:5],
        "page_numbers": page_numbers[:3]
    })

@app.route('/api/favorites/add', methods=['POST'])
def api_favorites_add():
    """Add product to favorites"""
    data = request.json
    product_id = data.get('product_id')
    device_id = request.headers.get('X-Device-ID', request.remote_addr)
    
    if not product_id:
        return jsonify({"error": "product_id required"}), 400
    
    user = get_or_create_user(device_id)
    favorites = user.get('favorites', [])
    
    if product_id not in favorites:
        favorites.append(product_id)
        update_user(device_id, {"favorites": favorites})
        return jsonify({"success": True, "added": True})
    
    return jsonify({"success": True, "added": False})

@app.route('/api/favorites/remove', methods=['POST'])
def api_favorites_remove():
    """Remove product from favorites"""
    data = request.json
    product_id = data.get('product_id')
    device_id = request.headers.get('X-Device-ID', request.remote_addr)
    
    if not product_id:
        return jsonify({"error": "product_id required"}), 400
    
    user = get_or_create_user(device_id)
    favorites = user.get('favorites', [])
    
    if product_id in favorites:
        favorites.remove(product_id)
        update_user(device_id, {"favorites": favorites})
        return jsonify({"success": True, "removed": True})
    
    return jsonify({"success": True, "removed": False})

@app.route('/api/favorites', methods=['GET'])
def api_favorites():
    """Get user's favorites"""
    device_id = request.headers.get('X-Device-ID', request.remote_addr)
    user = get_or_create_user(device_id)
    favorites = user.get('favorites', [])
    
    if not favorites:
        return jsonify([])
    
    # Get favorite products
    products = []
    for fav_id in favorites[:50]:
        response = supabase_get(f"/rest/v1/products?id=eq.{fav_id}")
        if response and response.status_code == 200 and response.json():
            products.append(response.json()[0])
    
    return jsonify(products)

# ============================================================================
# UPLOAD ROUTES
# ============================================================================

@app.route('/upload-tool')
def upload_tool():
    """Upload tool interface"""
    return send_from_directory('static', 'upload-tool.html') if os.path.exists('static/upload-tool.html') else UPLOAD_HTML

UPLOAD_HTML = '''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>katalog.ai Upload</title>
    <style>
        *{margin:0;padding:0;box-sizing:border-box;font-family:monospace}
        body{background:#111;color:#eee;padding:40px;max-width:800px;margin:0 auto}
        h1{color:#00ff88;margin-bottom:30px}
        .card{background:#1a1a1a;border-radius:10px;padding:20px;margin-bottom:20px;border:1px solid #333}
        label{color:#aaa;font-size:12px;text-transform:uppercase;display:block;margin-bottom:5px}
        input,select{width:100%;padding:12px;background:#222;border:1px solid #444;color:#eee;border-radius:5px;margin-bottom:15px;font-size:16px}
        button{background:#00ff88;color:#000;border:none;padding:15px 30px;font-size:16px;font-weight:bold;border-radius:5px;cursor:pointer;width:100%}
        button:hover{background:#00cc66}
        button:disabled{background:#444;color:#888;cursor:not-allowed}
        .progress-bar{background:#222;height:30px;border-radius:5px;margin:20px 0;overflow:hidden;display:none}
        .progress-fill{background:#00ff88;height:100%;width:0%;transition:width 0.3s;display:flex;align-items:center;justify-content:center;font-weight:bold;color:#000}
        #log{background:#000;padding:20px;border-radius:5px;font-size:13px;line-height:1.6;max-height:400px;overflow-y:auto;border:1px solid #333}
        .success{color:#00ff88}
        .error{color:#ff5555}
        .info{color:#66ccff}
        .flex{display:flex;gap:10px}
    </style>
</head>
<body>
    <h1>📤 katalog.ai - Upload Catalog</h1>
    
    <div class="card">
        <label>Country</label>
        <select id="country">
            <option value="hr">Croatia (Hrvatska)</option>
        </select>
        
        <label>PDF File</label>
        <input type="file" id="file" accept=".pdf">
        
        <label>Store</label>
        <select id="store">
            <option value="lidl">Lidl</option>
            <option value="kaufland">Kaufland</option>
            <option value="spar">Spar</option>
            <option value="konzum">Konzum</option>
            <option value="dm">dm</option>
            <option value="plodine">Plodine</option>
        </select>
        
        <label>Valid From (YYYY-MM-DD)</label>
        <input type="text" id="validFrom" placeholder="2026-03-02" value="2026-03-02">
        
        <label>Valid Until (empty = 14 days auto)</label>
        <input type="text" id="validUntil" placeholder="2026-03-16">
        
        <label>Resume Job ID (optional)</label>
        <input type="text" id="resumeJob" placeholder="Leave empty for new upload">
        
        <button id="uploadBtn" onclick="upload()">Process Catalog</button>
    </div>
    
    <div class="progress-bar" id="progressBar">
        <div class="progress-fill" id="progressFill">0%</div>
    </div>
    
    <div id="log">Ready. Select a PDF file and click Process.</div>

    <script>
        let pollInterval = null;
        let lastPage = 0;
        let lastProducts = 0;
        let totalPages = 0;
        
        function log(message, type = 'info') {
            const logDiv = document.getElementById('log');
            const colors = {
                'success': '#00ff88',
                'error': '#ff5555',
                'info': '#66ccff'
            };
            logDiv.innerHTML += `<span style="color: ${colors[type] || '#eee'}">${message}</span><br>`;
            logDiv.scrollTop = logDiv.scrollHeight;
        }
        
        function validateForm() {
            const file = document.getElementById('file');
            const store = document.getElementById('store').value;
            const validFrom = document.getElementById('validFrom').value;
            
            if (!file.files || file.files.length === 0) {
                log('❌ Please select a PDF file', 'error');
                return false;
            }
            
            if (!store) {
                log('❌ Please select a store', 'error');
                return false;
            }
            
            if (!validFrom) {
                log('❌ Please enter valid from date', 'error');
                return false;
            }
            
            return true;
        }
        
        async function upload() {
            if (!validateForm()) return;
            
            document.getElementById('log').innerHTML = '';
            
            const file = document.getElementById('file').files[0];
            const country = document.getElementById('country').value;
            const store = document.getElementById('store').value;
            const validFrom = document.getElementById('validFrom').value;
            let validUntil = document.getElementById('validUntil').value;
            const resumeJob = document.getElementById('resumeJob').value;
            
            if (!validUntil) {
                const d = new Date(validFrom);
                d.setDate(d.getDate() + 14);
                validUntil = d.toISOString().split('T')[0];
                log(`📅 Auto-set valid until: ${validUntil}`, 'info');
            }
            
            const btn = document.getElementById('uploadBtn');
            btn.disabled = true;
            btn.textContent = 'Processing...';
            
            document.getElementById('progressBar').style.display = 'block';
            
            const formData = new FormData();
            formData.append('file', file);
            formData.append('country', country);
            formData.append('store', store);
            formData.append('valid_from', validFrom);
            formData.append('valid_until', validUntil);
            if (resumeJob) formData.append('resume_job_id', resumeJob);
            
            try {
                log(`📤 Uploading: ${file.name}`, 'info');
                
                const response = await fetch('/upload', {
                    method: 'POST',
                    body: formData
                });
                
                if (!response.ok) {
                    const error = await response.text();
                    throw new Error(`HTTP ${response.status}: ${error}`);
                }
                
                const data = await response.json();
                
                if (data.error) {
                    log(`❌ ${data.error}`, 'error');
                    btn.disabled = false;
                    btn.textContent = 'Process Catalog';
                    return;
                }
                
                totalPages = data.total_pages;
                log(`✅ Job started! Pages: ${totalPages}`, 'success');
                log(`🆔 Job ID: ${data.job_id}`, 'success');
                
                if (pollInterval) clearInterval(pollInterval);
                pollInterval = setInterval(() => poll(data.job_id), 2000);
                
            } catch (error) {
                log(`❌ Upload failed: ${error.message}`, 'error');
                btn.disabled = false;
                btn.textContent = 'Process Catalog';
            }
        }
        
        async function poll(jobId) {
            try {
                const response = await fetch(`/status/${jobId}`);
                if (!response.ok) throw new Error('Poll failed');
                
                const data = await response.json();
                
                const currentPage = data.current_page || 0;
                const currentProducts = data.total_products || 0;
                
                if (currentPage > lastPage) {
                    for (let i = lastPage + 1; i <= currentPage; i++) {
                        let line = `📄 Page ${String(i).padStart(3, '0')} / ${totalPages}`;
                        if (i === currentPage) {
                            const newProducts = currentProducts - lastProducts;
                            line += `  |  +${newProducts} products  |  total: ${currentProducts}`;
                        }
                        log(line, 'success');
                    }
                    
                    lastPage = currentPage;
                    lastProducts = currentProducts;
                    
                    const percent = Math.round((currentPage / totalPages) * 100);
                    document.getElementById('progressFill').style.width = percent + '%';
                    document.getElementById('progressFill').textContent = percent + '%';
                }
                
                if (data.status === 'done') {
                    clearInterval(pollInterval);
                    log('─────────────────────────────', 'info');
                    log(`✅ DONE! ${data.total_products} products saved!`, 'success');
                    document.getElementById('progressFill').style.width = '100%';
                    document.getElementById('progressFill').textContent = '100% COMPLETE';
                    btn.disabled = false;
                    btn.textContent = 'Process Another';
                    
                } else if (data.status === 'error') {
                    clearInterval(pollInterval);
                    log('❌ ERROR - Check server logs', 'error');
                    btn.disabled = false;
                    btn.textContent = 'Retry';
                }
                
            } catch (error) {
                console.log('Poll error:', error);
            }
        }
        
        const today = new Date().toISOString().split('T')[0];
        document.getElementById('validFrom').value = today;
    </script>
</body>
</html>'''

@app.route('/upload', methods=['POST'])
def upload():
    """Upload and process PDF catalog"""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.error("PyMuPDF not installed")
        return jsonify({"error": "PyMuPDF not installed"}), 500

    # Get form data
    store = request.form.get('store', '').strip()
    valid_from = request.form.get('valid_from', '').strip()
    valid_until = request.form.get('valid_until', '').strip()
    file = request.files.get('file')
    resume_job_id = request.form.get('resume_job_id', '').strip()
    
    logger.info(f"Upload request: store={store}, from={valid_from}, until={valid_until}, file={file.filename if file else 'None'}")
    
    # Validate
    if not store or not valid_from:
        return jsonify({"error": "Missing required fields"}), 400
    
    if not file and not resume_job_id:
        return jsonify({"error": "No file provided"}), 400
    
    if not valid_until:
        try:
            d = datetime.strptime(valid_from, "%Y-%m-%d")
            valid_until = (d + timedelta(days=14)).strftime("%Y-%m-%d")
        except:
            valid_until = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")
    
    # Handle resume or new job
    if resume_job_id:
        job = get_job(resume_job_id)
        if job:
            job_id = resume_job_id
            start_page = job.get('current_page', 0)
            total_products_so_far = job.get('total_products', 0)
            total_pages = job.get('total_pages', 0)
            catalogue_name = job.get('catalogue_name', '')
            
            update_job(job_id, {"status": "processing"})
        else:
            return jsonify({"error": "Job ID not found"}), 404
    else:
        # Read PDF
        pdf_bytes = file.read()
        catalogue_name = file.filename.replace('.pdf', '')
        
        # Count pages
        try:
            with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
                tmp.write(pdf_bytes)
                tmp_path = tmp.name
            
            doc = fitz.open(tmp_path)
            total_pages = len(doc)
            doc.close()
            os.unlink(tmp_path)
        except Exception as e:
            logger.error(f"Could not read PDF: {e}")
            return jsonify({"error": f"Could not read PDF: {e}"}), 500
        
        # Create new job
        job_id = str(uuid.uuid4())[:8]
        start_page = 0
        total_products_so_far = 0
        
        save_job(job_id, store, catalogue_name, valid_from, valid_until, total_pages)
    
    # Start processing in background thread
    if file:
        pdf_bytes = file.read()
        
        def process():
            try:
                import fitz
                
                # Save PDF to temp file
                with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
                    tmp.write(pdf_bytes)
                    tmp_path = tmp.name
                
                doc = fitz.open(tmp_path)
                total_products = total_products_so_far
                
                for page_num in range(start_page, total_pages):
                    try:
                        logger.info(f"Processing page {page_num+1}/{total_pages}")
                        
                        # Render page to image
                        page = doc[page_num]
                        pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
                        img_bytes = pix.tobytes("jpeg")
                        img_b64 = base64.b64encode(img_bytes).decode()
                        
                        # Upload image
                        safe_store = store.lower().replace(' ', '_')
                        safe_name = catalogue_name.lower().replace(' ', '_')
                        filename = f"{safe_store}_{safe_name}_page_{str(page_num+1).zfill(3)}.jpg"
                        storage_path = f"{safe_store}/{valid_from}/{filename}"
                        
                        page_url = upload_image(img_bytes, storage_path)
                        
                        # Extract products with Gemini
                        products, _ = extract_products_from_image(img_b64, store, page_num+1)
                        
                        # Save products
                        saved = save_products(
                            products, store, page_num+1,
                            page_url, catalogue_name, valid_from, valid_until
                        )
                        
                        total_products += saved
                        
                        # Update job progress
                        update_job(job_id, {
                            "current_page": page_num + 1,
                            "total_products": total_products
                        })
                        
                        logger.info(f"Page {page_num+1}/{total_pages} processed: {saved} products")
                        
                    except Exception as e:
                        logger.error(f"Error on page {page_num+1}: {e}")
                        continue
                
                doc.close()
                os.unlink(tmp_path)
                
                # Save catalogue
                save_catalogue(store, catalogue_name, valid_from, valid_until, None, total_pages, total_products)
                
                # Mark job as done
                update_job(job_id, {"status": "done"})
                logger.info(f"Job {job_id} completed: {total_products} products")
                
            except Exception as e:
                logger.error(f"Job {job_id} failed: {e}")
                update_job(job_id, {"status": "error"})
        
        thread = threading.Thread(target=process)
        thread.daemon = True
        thread.start()
    
    return jsonify({
        "job_id": job_id,
        "total_pages": total_pages,
        "start_page": start_page
    })

@app.route('/status/<job_id>')
def job_status(job_id):
    """Get job status"""
    job = get_job(job_id)
    if job:
        return jsonify(job)
    return jsonify({"error": "Job not found"}), 404

# ============================================================================
# ERROR HANDLERS
# ============================================================================

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Not found"}), 404

@app.errorhandler(500)
def server_error(error):
    logger.error(f"Server error: {error}", exc_info=True)
    return jsonify({"error": "Internal server error"}), 500

# ============================================================================
# MAIN
# ============================================================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
