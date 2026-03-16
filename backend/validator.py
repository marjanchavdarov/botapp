"""
katalog.ai — Validation Tool
Allows users to review cropped products, mark them as good/bad, and provide corrections.
This feedback can be used to improve future cropping jobs.
"""

import os, json, uuid, base64, logging, threading, time, re, io
from datetime import datetime
from collections import defaultdict
from typing import List, Dict, Any, Tuple, Optional
import requests
from flask import Flask, request, jsonify, make_response, send_file
from flask_cors import CORS

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    logging.warning("Pillow not installed")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("validator")

app = Flask(__name__)
CORS(app)

# ── CONFIG ───────────────────────────────────────────────────────────────────
class Config:
    SUPABASE_URL         = os.environ.get("SUPABASE_URL")
    SUPABASE_KEY         = os.environ.get("SUPABASE_KEY")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
    STORAGE_BUCKET       = "katalog-images"
    UPLOAD_PASSWORD      = os.environ.get("UPLOAD_PASSWORD", "katalog2026")
    FEEDBACK_BUCKET      = "feedback-images"  # New bucket for feedback data

# ── SUPABASE HELPERS ─────────────────────────────────────────────────────────
def _headers():
    return {
        "apikey":        Config.SUPABASE_KEY,
        "Authorization": f"Bearer {Config.SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal",
    }

def _sb_get(path, params=None):
    try:
        r = requests.get(f"{Config.SUPABASE_URL}{path}", headers=_headers(),
                         params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"Supabase GET failed: {e}")
        return []

def _sb_post(path, data):
    try:
        r = requests.post(f"{Config.SUPABASE_URL}{path}", headers=_headers(),
                          json=data, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"Supabase POST failed: {e}")
        return None

def _sb_patch(path, data):
    try:
        r = requests.patch(f"{Config.SUPABASE_URL}{path}", headers=_headers(),
                           json=data, timeout=20)
        r.raise_for_status()
        return r
    except Exception as e:
        logger.error(f"Supabase PATCH failed: {e}")
        raise

def _sb_storage_get(path):
    """Get an image from storage"""
    key = Config.SUPABASE_SERVICE_KEY or Config.SUPABASE_KEY
    url = f"{Config.SUPABASE_URL}/storage/v1/object/{Config.STORAGE_BUCKET}/{path}"
    try:
        r = requests.get(url, headers={"apikey": key, "Authorization": f"Bearer {key}"}, timeout=30)
        r.raise_for_status()
        return r.content
    except Exception as e:
        logger.error(f"Storage GET failed: {e}")
        return None

def _sb_storage_put(path, img_bytes, content_type="image/jpeg"):
    """Upload feedback image to storage"""
    key = Config.SUPABASE_SERVICE_KEY or Config.SUPABASE_KEY
    url = f"{Config.SUPABASE_URL}/storage/v1/object/{Config.FEEDBACK_BUCKET}/{path}"
    try:
        r = requests.put(url, headers={
            "apikey": key, "Authorization": f"Bearer {key}",
            "Content-Type": content_type, "x-upsert": "true",
        }, data=img_bytes, timeout=30)
        if not r.ok:
            raise Exception(f"Storage {r.status_code}: {r.text[:300]}")
        return f"{Config.SUPABASE_URL}/storage/v1/object/public/{Config.FEEDBACK_BUCKET}/{path}"
    except Exception as e:
        logger.error(f"Storage upload failed: {e}")
        raise

# ── VALIDATION FUNCTIONS ─────────────────────────────────────────────────────

def create_comparison_image(page_img_bytes, product_img_bytes, crop_box, page_width, page_height):
    """Create a side-by-side comparison of page and cropped product"""
    if not PIL_AVAILABLE:
        return None
    
    try:
        # Load images
        page_img = Image.open(io.BytesIO(page_img_bytes))
        product_img = Image.open(io.BytesIO(product_img_bytes))
        
        # Resize product to reasonable size for comparison
        product_img.thumbnail((300, 300), Image.Resampling.LANCZOS)
        
        # Create comparison canvas
        comparison = Image.new('RGB', (page_img.width + product_img.width + 20, max(page_img.height, product_img.height + 50)), 'black')
        
        # Paste page image
        comparison.paste(page_img, (0, 0))
        
        # Draw crop rectangle on page image in the comparison
        draw = ImageDraw.Draw(comparison)
        
        # Draw rectangle on the page part only
        x1, y1, x2, y2 = crop_box
        draw.rectangle([x1, y1, x2, y2], outline='#ff9900', width=3)
        
        # Add label
        draw.text((x1, y1-20), "Cropped Area", fill='#ff9900')
        
        # Paste product image
        comparison.paste(product_img, (page_img.width + 10, 10))
        
        # Add label for product
        draw.text((page_img.width + 10, product_img.height + 20), "Cropped Result", fill='#00ff88')
        
        # Add rating buttons area at bottom
        draw.text((page_img.width + 10, comparison.height - 40), "Rate: [G]ood [B]ad [F]ix", fill='#ffffff')
        
        # Convert to bytes
        out = io.BytesIO()
        comparison.save(out, format='JPEG', quality=85)
        return out.getvalue()
        
    except Exception as e:
        logger.error(f"Failed to create comparison: {e}")
        return None

def save_feedback(product_id, store, catalogue_name, rating, notes="", corrected_box=None):
    """Save user feedback about a cropped product"""
    try:
        feedback_data = {
            "product_id": product_id,
            "store": store,
            "catalogue_name": catalogue_name,
            "rating": rating,  # 'good', 'bad', 'needs_fix'
            "notes": notes,
            "corrected_box": corrected_box,
            "created_at": datetime.now().isoformat()
        }
        
        # Check if feedback table exists, if not we'll store in a JSON file or memory
        # For now, store in a simple dict (in production, use Supabase)
        if not hasattr(app, 'feedback_store'):
            app.feedback_store = []
        
        app.feedback_store.append(feedback_data)
        
        # Also try to save to Supabase if table exists
        try:
            _sb_post("/rest/v1/feedback", feedback_data)
        except:
            pass  # Table might not exist yet
            
        return True
    except Exception as e:
        logger.error(f"Failed to save feedback: {e}")
        return False

def get_feedback_stats(store=None, catalogue_name=None):
    """Get statistics about feedback"""
    if not hasattr(app, 'feedback_store'):
        return {"total": 0, "good": 0, "bad": 0, "needs_fix": 0}
    
    feedback = app.feedback_store
    
    if store:
        feedback = [f for f in feedback if f['store'] == store]
    if catalogue_name:
        feedback = [f for f in feedback if f['catalogue_name'] == catalogue_name]
    
    stats = {
        "total": len(feedback),
        "good": len([f for f in feedback if f['rating'] == 'good']),
        "bad": len([f for f in feedback if f['rating'] == 'bad']),
        "needs_fix": len([f for f in feedback if f['rating'] == 'needs_fix'])
    }
    
    return stats

# ── ROUTES ───────────────────────────────────────────────────────────────────

@app.route("/")
def home():
    return make_response(VALIDATOR_HTML, 200, {"Content-Type": "text/html"})

@app.route("/api/catalogues")
def get_catalogues():
    """Get catalogues with cropping stats and feedback stats"""
    password = request.headers.get("X-Password") or request.args.get("password","")
    if password != Config.UPLOAD_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403
    
    try:
        # Get products
        products = _sb_get("/rest/v1/products", {
            "select": "id,store,catalogue_name,page_number,product,product_image_url,page_image_url",
            "limit": 5000
        })
        
        # Group by catalogue
        catalogues = defaultdict(lambda: {
            "store": "",
            "catalogue_name": "",
            "total_products": 0,
            "cropped_products": 0,
            "pages": set(),
            "products": []
        })
        
        for p in products:
            key = f"{p['store']}|{p['catalogue_name']}"
            cat = catalogues[key]
            cat["store"] = p["store"]
            cat["catalogue_name"] = p["catalogue_name"]
            cat["total_products"] += 1
            cat["pages"].add(p.get("page_number"))
            
            if p.get("product_image_url"):
                cat["cropped_products"] += 1
                cat["products"].append({
                    "id": p["id"],
                    "name": p.get("product", "Unknown"),
                    "page_number": p.get("page_number"),
                    "product_image_url": p["product_image_url"],
                    "page_image_url": p.get("page_image_url")
                })
        
        # Add feedback stats
        result = []
        for cat in catalogues.values():
            stats = get_feedback_stats(cat["store"], cat["catalogue_name"])
            result.append({
                "store": cat["store"],
                "catalogue_name": cat["catalogue_name"],
                "total_products": cat["total_products"],
                "cropped_products": cat["cropped_products"],
                "pages": len(cat["pages"]),
                "products": cat["products"][:100],  # Limit for preview
                "progress": round(cat["cropped_products"]/cat["total_products"]*100) if cat["total_products"] > 0 else 0,
                "feedback": stats
            })
        
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"Failed to get catalogues: {e}")
        return jsonify([])

@app.route("/api/products/<product_id>")
def get_product(product_id):
    """Get details for a specific product"""
    password = request.args.get("password","")
    if password != Config.UPLOAD_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403
    
    try:
        products = _sb_get("/rest/v1/products", {
            "id": f"eq.{product_id}",
            "select": "id,store,catalogue_name,page_number,product,product_image_url,page_image_url"
        })
        
        if not products:
            return jsonify({"error": "Product not found"}), 404
        
        product = products[0]
        
        # Get page image
        page_img = None
        if product.get("page_image_url"):
            page_img = product["page_image_url"]
        
        # Get product image
        product_img = None
        if product.get("product_image_url"):
            product_img = product["product_image_url"]
        
        # Get any existing feedback
        feedback = None
        if hasattr(app, 'feedback_store'):
            for f in app.feedback_store:
                if f['product_id'] == product_id:
                    feedback = f
                    break
        
        return jsonify({
            "id": product["id"],
            "name": product.get("product", "Unknown"),
            "store": product["store"],
            "catalogue": product["catalogue_name"],
            "page_number": product["page_number"],
            "page_image_url": page_img,
            "product_image_url": product_img,
            "feedback": feedback
        })
        
    except Exception as e:
        logger.error(f"Failed to get product: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/products/<product_id>/compare")
def compare_product(product_id):
    """Generate comparison image for a product"""
    password = request.args.get("password","")
    if password != Config.UPLOAD_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403
    
    try:
        # Get product details
        products = _sb_get("/rest/v1/products", {
            "id": f"eq.{product_id}",
            "select": "id,product_image_url,page_image_url"
        })
        
        if not products or not products[0].get("product_image_url") or not products[0].get("page_image_url"):
            return jsonify({"error": "Missing images"}), 404
        
        product = products[0]
        
        # Download images
        product_img_bytes = _sb_storage_get(product["product_image_url"].replace(f"{Config.SUPABASE_URL}/storage/v1/object/public/{Config.STORAGE_BUCKET}/", ""))
        page_img_bytes = _sb_storage_get(product["page_image_url"].replace(f"{Config.SUPABASE_URL}/storage/v1/object/public/{Config.STORAGE_BUCKET}/", ""))
        
        if not product_img_bytes or not page_img_bytes:
            return jsonify({"error": "Failed to download images"}), 500
        
        # For now, use a default crop box (in reality, you'd store this)
        # This is a placeholder - you'd need to store the original crop coordinates
        if PIL_AVAILABLE:
            page_img = Image.open(io.BytesIO(page_img_bytes))
            crop_box = [100, 100, page_img.width-100, page_img.height-100]  # Placeholder
            
            comparison = create_comparison_image(
                page_img_bytes, 
                product_img_bytes,
                crop_box,
                page_img.width,
                page_img.height
            )
            
            if comparison:
                return send_file(
                    io.BytesIO(comparison),
                    mimetype='image/jpeg',
                    as_attachment=False,
                    download_name=f'comparison_{product_id}.jpg'
                )
        
        return jsonify({"error": "Failed to create comparison"}), 500
        
    except Exception as e:
        logger.error(f"Failed to create comparison: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/feedback", methods=["POST"])
def submit_feedback():
    """Submit feedback for a product"""
    data = request.json or {}
    password = data.get("password", "")
    
    if password != Config.UPLOAD_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403
    
    product_id = data.get("product_id")
    rating = data.get("rating")  # 'good', 'bad', 'needs_fix'
    notes = data.get("notes", "")
    store = data.get("store")
    catalogue = data.get("catalogue")
    
    if not all([product_id, rating, store, catalogue]):
        return jsonify({"error": "Missing required fields"}), 400
    
    if rating not in ['good', 'bad', 'needs_fix']:
        return jsonify({"error": "Invalid rating"}), 400
    
    success = save_feedback(product_id, store, catalogue, rating, notes)
    
    if success:
        return jsonify({"ok": True, "message": "Feedback saved"})
    else:
        return jsonify({"error": "Failed to save feedback"}), 500

@app.route("/api/feedback/stats")
def feedback_stats():
    """Get feedback statistics"""
    password = request.args.get("password","")
    if password != Config.UPLOAD_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403
    
    store = request.args.get("store")
    catalogue = request.args.get("catalogue")
    
    stats = get_feedback_stats(store, catalogue)
    return jsonify(stats)

@app.route("/api/feedback/export")
def export_feedback():
    """Export all feedback as JSON"""
    password = request.args.get("password","")
    if password != Config.UPLOAD_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403
    
    if not hasattr(app, 'feedback_store'):
        return jsonify([])
    
    return jsonify(app.feedback_store)

# ── VALIDATION UI ────────────────────────────────────────────────────────────

VALIDATOR_HTML = '''<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>katalog.ai — Validator</title>
  <style>
    *{margin:0;padding:0;box-sizing:border-box;font-family:monospace}
    body{background:#111;color:#eee;padding:20px;max-width:1400px;margin:0 auto}
    
    /* Header */
    .header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}
    h1{color:#ff9900;font-size:24px}
    .sub{color:#666;font-size:13px}
    .nav-links{display:flex;gap:15px}
    .nav-links a{color:#888;text-decoration:none;padding:5px 10px}
    .nav-links a:hover{color:#ff9900}
    
    /* Login */
    .login-card{background:#1a1a1a;border-radius:10px;padding:20px;margin-bottom:20px;border:1px solid #333}
    input{background:#222;border:1px solid #444;color:#eee;padding:10px;border-radius:5px;width:200px;margin-right:10px}
    button{background:#ff9900;color:#000;border:none;padding:10px 20px;border-radius:5px;cursor:pointer;font-weight:bold}
    button:hover{background:#cc7700}
    button:disabled{background:#333;color:#666;cursor:not-allowed}
    
    /* Catalogue grid */
    .catalogue-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(350px,1fr));gap:20px;margin-top:20px}
    .catalogue-card{background:#1a1a1a;border-radius:10px;padding:20px;border:1px solid #333;transition:all 0.2s}
    .catalogue-card:hover{border-color:#ff9900}
    .catalogue-header{display:flex;justify-content:space-between;margin-bottom:10px}
    .store-badge{background:#333;padding:4px 8px;border-radius:4px;color:#ff9900;font-weight:bold}
    .progress{font-size:12px;color:#888}
    .stats{display:flex;gap:10px;margin:15px 0;flex-wrap:wrap}
    .stat{background:#222;padding:8px;border-radius:4px;flex:1;text-align:center}
    .stat-label{font-size:10px;color:#888;text-transform:uppercase}
    .stat-value{font-size:18px;font-weight:bold}
    .stat.good .stat-value{color:#00ff88}
    .stat.bad .stat-value{color:#ff5555}
    .stat.needs-fix .stat-value{color:#ffcc00}
    .view-btn{width:100%;padding:10px;background:#333;border:none;color:#eee;border-radius:5px;cursor:pointer;margin-top:10px}
    .view-btn:hover{background:#444}
    
    /* Product review */
    .review-container{display:none;background:#1a1a1a;border-radius:10px;padding:20px;margin-top:20px;border:1px solid #333}
    .review-header{display:flex;justify-content:space-between;margin-bottom:20px}
    .back-btn{background:#333;color:#eee;border:none;padding:8px 15px;border-radius:5px;cursor:pointer}
    .product-info{font-size:14px;color:#888}
    
    .comparison-area{display:flex;gap:20px;margin-bottom:20px;flex-wrap:wrap}
    .image-box{flex:1;min-width:300px}
    .image-box h3{color:#ff9900;margin-bottom:10px;font-size:14px}
    .image-box img{max-width:100%;border-radius:5px;border:1px solid #333}
    
    .product-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px;margin:20px 0;max-height:400px;overflow-y:auto;padding:10px;background:#222;border-radius:5px}
    .product-item{padding:10px;background:#1a1a1a;border-radius:5px;cursor:pointer;border:1px solid #333}
    .product-item:hover{background:#333}
    .product-item.selected{border-color:#ff9900;background:#2a1a00}
    .product-item .name{font-size:12px;margin-bottom:5px}
    .product-item .page{font-size:10px;color:#888}
    .product-item .feedback-badge{display:inline-block;width:8px;height:8px;border-radius:4px;margin-right:5px}
    .feedback-badge.good{background:#00ff88}
    .feedback-badge.bad{background:#ff5555}
    .feedback-badge.needs-fix{background:#ffcc00}
    
    .rating-buttons{display:flex;gap:10px;margin:20px 0}
    .rating-btn{flex:1;padding:15px;border:none;border-radius:5px;cursor:pointer;font-weight:bold;font-size:16px}
    .rating-btn.good{background:#00ff88;color:#000}
    .rating-btn.bad{background:#ff5555;color:#fff}
    .rating-btn.fix{background:#ffcc00;color:#000}
    .rating-btn.selected{transform:scale(1.05);box-shadow:0 0 15px currentColor}
    
    .notes-area{width:100%;padding:10px;background:#222;border:1px solid #444;color:#eee;border-radius:5px;margin:10px 0;min-height:80px}
    
    .stats-summary{display:flex;gap:15px;margin-bottom:20px}
    .stat-card{background:#222;padding:15px;border-radius:5px;flex:1}
    
    .hidden{display:none}
    
    /* Loading */
    .loading{text-align:center;padding:40px;color:#888}
    .spinner{border:3px solid #333;border-top-color:#ff9900;border-radius:50%;width:40px;height:40px;animation:spin 1s linear infinite;margin:20px auto}
    @keyframes spin{to{transform:rotate(360deg)}}
    
    /* Toast */
    #toast{position:fixed;bottom:40px;left:50%;transform:translateX(-50%);background:#00ff88;color:#000;padding:10px 24px;border-radius:8px;font-weight:bold;display:none;z-index:999}
  </style>
</head>
<body>

<div class="header">
  <div>
    <h1>🔍 katalog.ai Validator</h1>
    <div class="sub">Review and provide feedback on cropped products</div>
  </div>
  <div class="nav-links">
    <a href="/">Cropper</a>
    <a href="/annotate">Annotator</a>
    <a href="#" style="color:#ff9900">Validator</a>
  </div>
</div>

<!-- Login -->
<div class="login-card" id="loginCard">
  <input type="password" id="password" placeholder="Password" onkeypress="if(event.key==='Enter') loadCatalogues()">
  <button onclick="loadCatalogues()">🔓 Login & Load Catalogues</button>
</div>

<!-- Stats Summary -->
<div class="stats-summary" id="statsSummary" style="display:none">
  <div class="stat-card">
    <div style="color:#888;font-size:12px">Total Reviews</div>
    <div style="font-size:24px;font-weight:bold" id="totalReviews">0</div>
  </div>
  <div class="stat-card" style="border-left:3px solid #00ff88">
    <div style="color:#888;font-size:12px">Good</div>
    <div style="font-size:24px;font-weight:bold;color:#00ff88" id="goodReviews">0</div>
  </div>
  <div class="stat-card" style="border-left:3px solid #ff5555">
    <div style="color:#888;font-size:12px">Bad</div>
    <div style="font-size:24px;font-weight:bold;color:#ff5555" id="badReviews">0</div>
  </div>
  <div class="stat-card" style="border-left:3px solid #ffcc00">
    <div style="color:#888;font-size:12px">Needs Fix</div>
    <div style="font-size:24px;font-weight:bold;color:#ffcc00" id="fixReviews">0</div>
  </div>
</div>

<!-- Catalogue Grid -->
<div id="catalogueGrid" class="catalogue-grid"></div>

<!-- Loading -->
<div id="loading" class="loading" style="display:none">
  <div class="spinner"></div>
  <div>Loading catalogues...</div>
</div>

<!-- Review Container -->
<div id="reviewContainer" class="review-container">
  <div class="review-header">
    <div>
      <h2 id="currentStoreCat">Store - Catalogue</h2>
      <div class="product-info" id="currentProductInfo">Page 1 • Product Name</div>
    </div>
    <button class="back-btn" onclick="showCatalogueGrid()">← Back to Catalogues</button>
  </div>
  
  <!-- Product List -->
  <div style="margin-bottom:20px">
    <h3 style="color:#888;margin-bottom:10px;font-size:14px">Products in this catalogue</h3>
    <div id="productList" class="product-list"></div>
  </div>
  
  <!-- Comparison -->
  <div class="comparison-area">
    <div class="image-box">
      <h3>📄 Original Page</h3>
      <img id="pageImage" src="" alt="Page image">
    </div>
    <div class="image-box">
      <h3>✂️ Cropped Product</h3>
      <img id="productImage" src="" alt="Cropped product">
    </div>
  </div>
  
  <!-- Rating -->
  <div style="margin:20px 0">
    <h3 style="color:#888;margin-bottom:10px">Rate this crop:</h3>
    <div class="rating-buttons">
      <button class="rating-btn good" onclick="setRating('good')">✅ Good</button>
      <button class="rating-btn bad" onclick="setRating('bad')">❌ Bad</button>
      <button class="rating-btn fix" onclick="setRating('needs_fix')">🔧 Needs Fix</button>
    </div>
  </div>
  
  <!-- Notes -->
  <div>
    <h3 style="color:#888;margin-bottom:10px">Notes (optional):</h3>
    <textarea id="feedbackNotes" class="notes-area" placeholder="Describe what's wrong with the crop..."></textarea>
  </div>
  
  <!-- Submit -->
  <button class="view-btn" style="background:#ff9900;color:#000;font-weight:bold;margin-top:20px" onclick="submitFeedback()">
    💾 Submit Feedback
  </button>
</div>

<!-- Toast -->
<div id="toast"></div>

<script>
let currentCatalogue = null;
let currentProductId = null;
let currentRating = null;
let allCatalogues = [];
let feedbackStats = {total:0, good:0, bad:0, needs_fix:0};

async function loadCatalogues() {
  const pw = document.getElementById('password').value;
  if(!pw) { alert('Enter password'); return; }
  
  document.getElementById('loginCard').style.display = 'none';
  document.getElementById('loading').style.display = 'block';
  document.getElementById('catalogueGrid').innerHTML = '';
  
  try {
    // Load catalogues
    const res = await fetch(`/api/catalogues?password=${encodeURIComponent(pw)}`);
    if(res.status === 403) { alert('Wrong password'); location.reload(); return; }
    
    allCatalogues = await res.json();
    
    // Load feedback stats
    const statsRes = await fetch(`/api/feedback/stats?password=${encodeURIComponent(pw)}`);
    feedbackStats = await statsRes.json();
    
    updateStatsSummary();
    renderCatalogues();
    
    document.getElementById('loading').style.display = 'none';
    document.getElementById('statsSummary').style.display = 'flex';
    
  } catch(e) {
    alert('Failed to load: ' + e.message);
    location.reload();
  }
}

function updateStatsSummary() {
  document.getElementById('totalReviews').textContent = feedbackStats.total || 0;
  document.getElementById('goodReviews').textContent = feedbackStats.good || 0;
  document.getElementById('badReviews').textContent = feedbackStats.bad || 0;
  document.getElementById('fixReviews').textContent = feedbackStats.needs_fix || 0;
}

function renderCatalogues() {
  const grid = document.getElementById('catalogueGrid');
  
  if(!allCatalogues.length) {
    grid.innerHTML = '<div style="color:#888;text-align:center;padding:40px">No catalogues found</div>';
    return;
  }
  
  grid.innerHTML = allCatalogues.map(cat => {
    const goodPct = cat.feedback.total ? Math.round(cat.feedback.good / cat.feedback.total * 100) : 0;
    
    return `
      <div class="catalogue-card">
        <div class="catalogue-header">
          <span class="store-badge">${cat.store}</span>
          <span class="progress">${cat.progress}% cropped</span>
        </div>
        <div style="font-size:18px;margin-bottom:15px">${cat.catalogue_name}</div>
        <div style="color:#888;margin-bottom:10px">${cat.pages} pages • ${cat.total_products} products</div>
        
        <div class="stats">
          <div class="stat good">
            <div class="stat-label">Good</div>
            <div class="stat-value">${cat.feedback.good || 0}</div>
          </div>
          <div class="stat bad">
            <div class="stat-label">Bad</div>
            <div class="stat-value">${cat.feedback.bad || 0}</div>
          </div>
          <div class="stat needs-fix">
            <div class="stat-label">Fix</div>
            <div class="stat-value">${cat.feedback.needs_fix || 0}</div>
          </div>
        </div>
        
        ${cat.feedback.total ? `
          <div style="height:4px;background:#333;border-radius:2px;margin:10px 0">
            <div style="height:4px;width:${goodPct}%;background:#00ff88;border-radius:2px"></div>
          </div>
          <div style="color:#888;font-size:11px">${goodPct}% positive feedback</div>
        ` : ''}
        
        <button class="view-btn" onclick="viewCatalogue('${cat.store}', '${cat.catalogue_name}')">
          📋 Review Products (${cat.cropped_products} cropped)
        </button>
      </div>
    `;
  }).join('');
}

function viewCatalogue(store, catalogue) {
  const cat = allCatalogues.find(c => c.store === store && c.catalogue_name === catalogue);
  if(!cat) return;
  
  currentCatalogue = cat;
  
  document.getElementById('currentStoreCat').textContent = `${store} - ${catalogue}`;
  document.getElementById('reviewContainer').style.display = 'block';
  document.getElementById('catalogueGrid').style.display = 'none';
  document.getElementById('statsSummary').style.display = 'none';
  
  renderProductList(cat.products);
  
  if(cat.products.length > 0) {
    selectProduct(cat.products[0].id);
  }
}

function renderProductList(products) {
  const list = document.getElementById('productList');
  
  list.innerHTML = products.map(p => {
    // Check if this product has feedback
    const hasFeedback = window.allFeedback && window.allFeedback[p.id];
    const feedbackClass = hasFeedback ? hasFeedback.rating : '';
    
    return `
      <div class="product-item ${p.id === currentProductId ? 'selected' : ''}" 
           onclick="selectProduct('${p.id}')">
        <div style="display:flex;align-items:center">
          ${hasFeedback ? `<span class="feedback-badge ${feedbackClass}"></span>` : ''}
          <span class="name">${p.name || 'Unknown'}</span>
        </div>
        <div class="page">Page ${p.page_number || '?'}</div>
      </div>
    `;
  }).join('');
}

async function selectProduct(productId) {
  currentProductId = productId;
  currentRating = null;
  
  // Update UI
  document.querySelectorAll('.product-item').forEach(el => {
    el.classList.remove('selected');
    if(el.querySelector(`[onclick="selectProduct('${productId}')"]`)) {
      el.classList.add('selected');
    }
  });
  
  // Clear rating buttons
  document.querySelectorAll('.rating-btn').forEach(btn => {
    btn.classList.remove('selected');
  });
  document.getElementById('feedbackNotes').value = '';
  
  try {
    const pw = document.getElementById('password').value;
    const res = await fetch(`/api/products/${productId}?password=${encodeURIComponent(pw)}`);
    const product = await res.json();
    
    if(product.product_image_url) {
      document.getElementById('productImage').src = product.product_image_url;
    }
    
    if(product.page_image_url) {
      document.getElementById('pageImage').src = product.page_image_url;
    }
    
    document.getElementById('currentProductInfo').textContent = 
      `Page ${product.page_number || '?'} • ${product.name || 'Unknown'}`;
    
    // Load existing feedback if any
    if(product.feedback) {
      setRating(product.feedback.rating);
      document.getElementById('feedbackNotes').value = product.feedback.notes || '';
    }
    
  } catch(e) {
    toast('Error loading product: ' + e.message, 'error');
  }
}

function setRating(rating) {
  currentRating = rating;
  
  document.querySelectorAll('.rating-btn').forEach(btn => {
    btn.classList.remove('selected');
  });
  
  if(rating === 'good') document.querySelector('.rating-btn.good').classList.add('selected');
  if(rating === 'bad') document.querySelector('.rating-btn.bad').classList.add('selected');
  if(rating === 'needs_fix') document.querySelector('.rating-btn.fix').classList.add('selected');
}

async function submitFeedback() {
  if(!currentRating) {
    toast('Please select a rating', 'error');
    return;
  }
  
  const pw = document.getElementById('password').value;
  const notes = document.getElementById('feedbackNotes').value;
  
  try {
    const res = await fetch('/api/feedback', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        password: pw,
        product_id: currentProductId,
        rating: currentRating,
        notes: notes,
        store: currentCatalogue.store,
        catalogue: currentCatalogue.catalogue_name
      })
    });
    
    const data = await res.json();
    
    if(data.ok) {
      toast('Feedback saved! ✓');
      
      // Refresh stats
      const statsRes = await fetch(`/api/feedback/stats?password=${encodeURIComponent(pw)}`);
      feedbackStats = await statsRes.json();
      updateStatsSummary();
      
      // Refresh catalogue data
      loadCatalogues();
      
      // Move to next product
      const currentIndex = currentCatalogue.products.findIndex(p => p.id === currentProductId);
      if(currentIndex < currentCatalogue.products.length - 1) {
        selectProduct(currentCatalogue.products[currentIndex + 1].id);
      } else {
        toast('Last product in catalogue!', 'info');
      }
    } else {
      toast('Failed to save feedback', 'error');
    }
    
  } catch(e) {
    toast('Error: ' + e.message, 'error');
  }
}

function showCatalogueGrid() {
  document.getElementById('reviewContainer').style.display = 'none';
  document.getElementById('catalogueGrid').style.display = 'grid';
  document.getElementById('statsSummary').style.display = 'flex';
  currentCatalogue = null;
  currentProductId = null;
}

function toast(msg, type='success') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.style.background = type==='error'?'#ff5555':'#00ff88';
  el.style.display = 'block';
  setTimeout(() => el.style.display = 'none', 2000);
}
</script>
</body>
</html>
'''

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5003))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
