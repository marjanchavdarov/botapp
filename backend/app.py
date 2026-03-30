from flask import Flask, request, jsonify, send_from_directory
from twilio.twiml.messaging_response import MessagingResponse
import requests
import os
import json
from dotenv import load_dotenv
load_dotenv()
import base64
import threading
import uuid
from datetime import datetime, date, timedelta
from urllib.parse import quote, urlparse, urlunparse
import re
import logging
import urllib.parse
from functools import wraps
import time

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static")

# Configuration
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Validate environment variables
if not GEMINI_API_KEY:
    logger.error("GEMINI_API_KEY environment variable not set")
    raise ValueError("GEMINI_API_KEY environment variable not set")
if not SUPABASE_URL or not SUPABASE_KEY:
    logger.error("Supabase credentials not set")
    raise ValueError("Supabase credentials not set")

# Ensure SUPABASE_URL doesn't have trailing slash
SUPABASE_URL = SUPABASE_URL.rstrip('/')

def db_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": "Bearer " + SUPABASE_KEY,
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

# ─────────────────────────────────────────
# STORAGE BUCKET SETUP
# ─────────────────────────────────────────

def ensure_bucket_exists():
    """Check if the storage bucket exists, create if not"""
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": "Bearer " + SUPABASE_KEY
    }
    
    try:
        # Check if bucket exists
        response = requests.get(
            f"{SUPABASE_URL}/storage/v1/bucket/katalog-images",
            headers=headers,
            timeout=10
        )
        
        if response.status_code == 404:
            logger.info("Storage bucket 'katalog-images' not found, creating...")
            # Create bucket
            create_response = requests.post(
                f"{SUPABASE_URL}/storage/v1/bucket",
                headers={**headers, "Content-Type": "application/json"},
                json={
                    "name": "katalog-images",
                    "public": True,
                    "file_size_limit": 10485760,  # 10MB
                    "allowed_mime_types": ["image/jpeg", "image/png", "image/jpg"]
                },
                timeout=10
            )
            if create_response.status_code in [200, 201]:
                logger.info("✅ Storage bucket created successfully")
                return True
            else:
                logger.error(f"Failed to create bucket: {create_response.text}")
                return False
        elif response.status_code == 200:
            logger.info("✅ Storage bucket exists")
            return True
        else:
            logger.error(f"Unexpected response checking bucket: {response.status_code}")
            return False
    except Exception as e:
        logger.error(f"Error checking/creating bucket: {e}")
        return False

# Call at startup
ensure_bucket_exists()

# ─────────────────────────────────────────
# HELPER FUNCTIONS
# ─────────────────────────────────────────

def sanitize_path_component(text):
    """Remove special characters and spaces from path components"""
    if not text:
        return ""
    # Replace spaces and special chars with underscore
    text = str(text).lower()
    # Keep only alphanumeric, underscore, hyphen
    text = re.sub(r'[^a-zA-Z0-9_-]', '_', text)
    # Remove multiple underscores
    text = re.sub(r'_+', '_', text)
    return text.strip('_')

def encode_url(url):
    """Properly encode URL for WhatsApp"""
    if not url:
        return url
    
    try:
        parsed = urlparse(url)
        # Encode each path segment
        path_parts = parsed.path.split('/')
        encoded_path = '/'.join(urllib.parse.quote(part) for part in path_parts)
        
        # Reconstruct URL
        encoded = urlunparse((
            parsed.scheme,
            parsed.netloc,
            encoded_path,
            parsed.params,
            parsed.query,
            parsed.fragment
        ))
        return encoded
    except Exception as e:
        logger.error(f"URL encoding error: {e}")
        return url

def validate_image_for_whatsapp(image_url):
    """Check if an image meets WhatsApp requirements"""
    try:
        # Check HTTPS
        if not image_url.startswith("https://"):
            return False, "URL must use HTTPS"
        
        # Test accessibility
        response = requests.head(image_url, timeout=10, allow_redirects=True)
        
        if response.status_code != 200:
            return False, f"HTTP {response.status_code}"
        
        # Check content type
        content_type = response.headers.get("content-type", "").lower()
        valid_types = ["image/jpeg", "image/jpg", "image/png", "image/gif"]
        if not any(ct in content_type for ct in valid_types):
            return False, f"Invalid content type: {content_type}"
        
        # Check file size (WhatsApp limit is ~5MB for images)
        content_length = int(response.headers.get("content-length", 0))
        if content_length > 5 * 1024 * 1024:  # 5MB
            return False, f"Image too large: {content_length} bytes"
        
        return True, "Valid"
    except Exception as e:
        return False, str(e)

# ─────────────────────────────────────────
# UPLOAD TOOL
# ─────────────────────────────────────────

UPLOAD_HTML = '''<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>katalog.ai Upload</title>
<style>
body{font-family:monospace;background:#111;color:#eee;padding:40px;max-width:700px;margin:0 auto}
h1{color:#00ff88}
input{background:#222;border:1px solid #444;color:#eee;padding:8px;width:100%;margin:5px 0 15px 0;font-family:monospace;display:block;box-sizing:border-box}
label{color:#aaa;font-size:13px}
button{background:#00ff88;color:#000;border:none;padding:15px;font-weight:bold;font-size:16px;cursor:pointer;width:100%;margin-top:10px}
button:disabled{background:#444;color:#888;cursor:not-allowed}
#log{background:#000;padding:20px;margin-top:20px;min-height:100px;font-size:12px;line-height:1.8;white-space:pre-wrap}
#bar-wrap{background:#222;height:24px;margin-top:10px;display:none;border-radius:4px;overflow:hidden}
#fill{background:#00ff88;height:24px;width:0%;transition:width 0.5s;display:flex;align-items:center;justify-content:center;font-size:11px;color:#000;font-weight:bold}
</style>
</head>
<body>
<h1>katalog.ai - Upload</h1>
<label>PDF File:</label>
<input type="file" id="pdffile" accept=".pdf">
<label>Store:</label>
<input type="text" id="store" placeholder="Lidl">
<label>Valid From (YYYY-MM-DD):</label>
<input type="text" id="validfrom" placeholder="2026-03-02">
<label>Valid Until (empty = 14 days auto):</label>
<input type="text" id="validuntil" placeholder="2026-03-16">
<label>Resume Job ID (optional):</label>
<input type="text" id="resumejob" placeholder="leave empty for new upload">
<button id="btn" onclick="go()">Process</button>
<div id="bar-wrap"><div id="fill">0%</div></div>
<div id="log">Ready.</div>
<script>
var pollInterval = null;
var lastPage = 0;
var lastProducts = 0;
var totalPages = 0;

function go() {
  var f = document.getElementById("pdffile").files[0];
  var s = document.getElementById("store").value;
  var vf = document.getElementById("validfrom").value;
  var vu = document.getElementById("validuntil").value;
  var rj = document.getElementById("resumejob").value.trim();
  if (!f) { alert("Pick a file"); return; }
  if (!s) { alert("Enter store"); return; }
  if (!vf) { alert("Enter date"); return; }
  if (!vu) {
    var d = new Date(vf);
    d.setDate(d.getDate() + 14);
    vu = d.toISOString().split("T")[0];
  }
  var btn = document.getElementById("btn");
  btn.disabled = true;
  btn.textContent = "Processing...";
  var log = document.getElementById("log");
  log.textContent = "Uploading file...\\n";
  document.getElementById("bar-wrap").style.display = "block";
  document.getElementById("fill").style.width = "0%";
  document.getElementById("fill").textContent = "0%";
  lastPage = 0;
  lastProducts = 0;
  var fd = new FormData();
  fd.append("file", f);
  fd.append("store", s);
  fd.append("valid_from", vf);
  fd.append("valid_until", vu);
  if (rj) fd.append("resume_job_id", rj);
  fetch("/upload", { method: "POST", body: fd }).then(function(r) {
    return r.json();
  }).then(function(data) {
    if (data.error) {
      log.textContent += "ERROR: " + data.error + "\\n";
      btn.disabled = false;
      btn.textContent = "Process";
      return;
    }
    totalPages = data.total_pages;
    log.textContent += "Job started! " + data.total_pages + " pages\\n";
    if (data.start_page > 0) log.textContent += "Resuming from page " + data.start_page + "\\n";
    log.textContent += "Job ID: " + data.job_id + "\\n";
    log.textContent += "─────────────────────────────\\n";
    pollInterval = setInterval(function() { poll(data.job_id); }, 4000);
  }).catch(function(e) {
    log.textContent += "ERROR: " + e.message + "\\n";
    btn.disabled = false;
    btn.textContent = "Process";
  });
}

function poll(job_id) {
  fetch("/status/" + job_id).then(function(r) {
    return r.json();
  }).then(function(data) {
    var log = document.getElementById("log");
    var cur = data.current_page || 0;
    var curProducts = data.total_products || 0;
    if (cur > lastPage) {
      for (var i = lastPage + 1; i <= cur; i++) {
        var line = "Page " + String(i).padStart(3, "0") + " / " + totalPages;
        if (i === cur) line += "  |  +" + (curProducts - lastProducts) + " products  |  total: " + curProducts;
        log.textContent += line + "\\n";
      }
      lastPage = cur;
      lastProducts = curProducts;
      log.scrollTop = log.scrollHeight;
      var pct = Math.round((cur / totalPages) * 100);
      document.getElementById("fill").style.width = pct + "%";
      document.getElementById("fill").textContent = pct + "%";
    }
    if (data.status === "done") {
      clearInterval(pollInterval);
      log.textContent += "─────────────────────────────\\n";
      log.textContent += "DONE! " + data.total_products + " products saved!\\n";
      document.getElementById("fill").style.width = "100%";
      document.getElementById("fill").textContent = "100% DONE!";
      document.getElementById("btn").disabled = false;
      document.getElementById("btn").textContent = "Process Another";
    } else if (data.status === "error") {
      clearInterval(pollInterval);
      log.textContent += "ERROR - check Render logs\\n";
      document.getElementById("btn").disabled = false;
      document.getElementById("btn").textContent = "Process";
    }
  }).catch(function(e) { console.log("Poll error:", e); });
}
</script>
</body>
</html>'''

@app.route("/upload-tool")
def upload_tool():
    return UPLOAD_HTML

@app.route("/upload", methods=["POST"])
def upload():
    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.error("PyMuPDF not installed")
        return jsonify({"error": "PyMuPDF not installed"}), 500

    f = request.files.get("file")
    sn = request.form.get("store", "").strip()
    vf = request.form.get("valid_from", "").strip()
    vu = request.form.get("valid_until", "").strip()

    if not f or not sn or not vf:
        return jsonify({"error": "Missing fields"}), 400

    # Validate file
    if not f.filename.lower().endswith('.pdf'):
        return jsonify({"error": "File must be PDF"}), 400

    if not vu:
        d = datetime.strptime(vf, "%Y-%m-%d")
        vu = (d + timedelta(days=14)).strftime("%Y-%m-%d")

    fd = f.read()
    fn = f.filename
    cat_name = fn.replace(".pdf", "")

    try:
        import fitz
        tmp = f"/tmp/{uuid.uuid4()}.pdf"
        with open(tmp, "wb") as fp:
            fp.write(fd)
        doc = fitz.open(tmp)
        total_pages = len(doc)
        doc.close()
        os.remove(tmp)
    except Exception as e:
        logger.error(f"Could not read PDF: {e}")
        return jsonify({"error": "Could not read PDF: " + str(e)}), 500

    resume_job_id = request.form.get("resume_job_id", "").strip()
    if resume_job_id:
        existing = requests.get(
            f"{SUPABASE_URL}/rest/v1/jobs?id=eq.{resume_job_id}",
            headers=db_headers()
        )
        if existing.status_code == 200 and existing.json():
            job = existing.json()[0]
            job_id = resume_job_id
            start_page = job.get("current_page", 0)
            total_products_so_far = job.get("total_products", 0)
            requests.patch(
                f"{SUPABASE_URL}/rest/v1/jobs?id=eq.{job_id}",
                headers={**db_headers(), "Prefer": "return=minimal"},
                json={"status": "processing"}
            )
        else:
            return jsonify({"error": "Job ID not found"}), 400
    else:
        job_id = str(uuid.uuid4())[:8]
        start_page = 0
        total_products_so_far = 0
        requests.post(
            f"{SUPABASE_URL}/rest/v1/jobs",
            headers={**db_headers(), "Prefer": "return=minimal"},
            json={
                "id": job_id,
                "store": sn,
                "catalogue_name": cat_name,
                "valid_from": vf,
                "valid_until": vu,
                "total_pages": total_pages,
                "current_page": 0,
                "total_products": 0,
                "status": "processing"
            }
        )

    def process():
        try:
            import fitz
            tmp = f"/tmp/{job_id}.pdf"
            with open(tmp, "wb") as fp:
                fp.write(fd)
            doc = fitz.open(tmp)
            cat_fp = None
            total_products = total_products_so_far

            for i in range(start_page, total_pages):
                try:
                    page = doc[i]
                    pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
                    img_bytes = pix.tobytes("jpeg")
                    img_b64 = base64.b64encode(img_bytes).decode()

                    # Sanitize path components
                    safe_store = sanitize_path_component(sn)
                    safe_date = vf.replace("-", "_")
                    safe_filename = sanitize_path_component(f"{sn}_{cat_name}_page_{str(i+1).zfill(3)}.jpg")
                    
                    storage_path = f"{safe_store}/{safe_date}/{safe_filename}"
                    page_url = upload_image(img_bytes, storage_path)

                    products, fine_print = extract(img_b64, sn, i+1, vf)
                    if fine_print:
                        cat_fp = (cat_fp + " " + fine_print) if cat_fp else fine_print
                    
                    saved = save_products(products, sn, i+1, page_url, cat_name, vf, vu)
                    total_products += saved
                    
                    requests.patch(
                        f"{SUPABASE_URL}/rest/v1/jobs?id=eq.{job_id}",
                        headers={**db_headers(), "Prefer": "return=minimal"},
                        json={"current_page": i + 1, "total_products": total_products, "fine_print": cat_fp}
                    )
                    
                    logger.info(f"Page {i+1}/{total_pages} processed: {saved} products")
                    
                except Exception as e:
                    logger.error(f"Page {i+1} error: {e}")
                    continue

            doc.close()
            os.remove(tmp)
            save_catalogue(sn, cat_name, vf, vu, cat_fp, total_pages, total_products)
            requests.patch(
                f"{SUPABASE_URL}/rest/v1/jobs?id=eq.{job_id}",
                headers={**db_headers(), "Prefer": "return=minimal"},
                json={"status": "done", "current_page": total_pages, "total_products": total_products}
            )
            logger.info(f"Job {job_id} completed successfully")
            
        except Exception as e:
            logger.error(f"Job error: {e}")
            requests.patch(
                f"{SUPABASE_URL}/rest/v1/jobs?id=eq.{job_id}",
                headers={**db_headers(), "Prefer": "return=minimal"},
                json={"status": "error"}
            )

    t = threading.Thread(target=process)
    t.daemon = True
    t.start()

    return jsonify({"job_id": job_id, "total_pages": total_pages, "start_page": start_page})

@app.route("/status/<job_id>")
def status(job_id):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/jobs?id=eq.{job_id}",
        headers=db_headers()
    )
    if r.status_code == 200 and r.json():
        return jsonify(r.json()[0])
    return jsonify({"error": "Job not found"}), 404

# ─────────────────────────────────────────
# GEMINI EXTRACT
# ─────────────────────────────────────────

def extract(img_b64, store, page_num, valid_from):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={GEMINI_API_KEY}"
    try:
        year = str(datetime.strptime(valid_from, "%Y-%m-%d").year)
    except:
        year = str(date.today().year)

    prompt = (
        f"Page {page_num} of {store} catalogue. "
        "Extract ONLY real purchasable products that have a clear price in euros. "
        "Translate ALL product names, brands and categories to ENGLISH. "
        "STRICT RULES: "
        "1. Product MUST have a visible euro price - skip if no price. "
        "2. Skip promotional items, gifts, loyalty rewards, contest prizes, stuffed animals. "
        "3. Convert dates to YYYY-MM-DD, year is {year}. od/von=valid_from, do/bis=valid_until. "
        "4. fine_print ONLY for legal disclaimers like limited quantity, while supplies last - otherwise null. "
        "Return ONLY JSON array: [{\"product\":\"English name\",\"brand\":\"brand or null\","
        "\"quantity\":\"250g or null\",\"original_price\":\"2.99 or null\",\"sale_price\":\"1.99\","
        "\"discount_percent\":\"33% or null\",\"valid_from\":\"{year}-03-02 or null\","
        "\"valid_until\":\"{year}-03-08 or null\",\"category\":\"English category\","
        "\"subcategory\":\"English subcategory\",\"fine_print\":\"disclaimer or null\"}] "
        "Categories: Meat and Fish, Dairy, Bread and Bakery, Fruit and Vegetables, Drinks, "
        "Snacks and Sweets, Canned Food, Cosmetics and Hygiene, Household and Cleaning, "
        "Tools and Construction, Home and Garden, Electronics, Clothing and Shoes, Pet Food, "
        "Health and Pharmacy, Other. If no valid products return: []"
    )
    
    body = {
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}},
                {"text": prompt}
            ]
        }],
        "generationConfig": {"maxOutputTokens": 8192, "temperature": 0.1}
    }
    
    for attempt in range(3):
        try:
            r = requests.post(url, json=body, timeout=45)
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            text = text.replace("```json", "").replace("```", "").strip()
            result = json.loads(text)
            if not isinstance(result, list):
                return [], None
            fine_print = None
            for p in result:
                if p.get("fine_print") and p.get("fine_print") not in [None, "null"]:
                    fine_print = p.get("fine_print")
                    break
            return result, fine_print
        except Exception as e:
            logger.error(f"Gemini error page {page_num} attempt {attempt+1}: {e}")
            if attempt == 2:
                return [], None
            continue
    return [], None

# ─────────────────────────────────────────
# SUPABASE HELPERS
# ─────────────────────────────────────────

def upload_image(img_bytes, storage_path):
    """Upload image to Supabase storage with fallback options"""
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": "Bearer " + SUPABASE_KEY,
        "Content-Type": "image/jpeg",
        "x-upsert": "true"
    }
    
    # Try main path
    try:
        response = requests.put(
            f"{SUPABASE_URL}/storage/v1/object/katalog-images/{storage_path}",
            headers=headers,
            data=img_bytes,
            timeout=30
        )
        
        if response.status_code in [200, 201]:
            public_url = f"{SUPABASE_URL}/storage/v1/object/public/katalog-images/{storage_path}"
            logger.info(f"✅ Image uploaded: {public_url}")
            return public_url
        
        logger.warning(f"Upload to main path failed: {response.status_code}")
    except Exception as e:
        logger.warning(f"Upload to main path exception: {e}")
    
    # Fallback: try without subfolders
    try:
        fallback_path = storage_path.replace("/", "_")
        response2 = requests.put(
            f"{SUPABASE_URL}/storage/v1/object/katalog-images/{fallback_path}",
            headers=headers,
            data=img_bytes,
            timeout=30
        )
        
        if response2.status_code in [200, 201]:
            public_url = f"{SUPABASE_URL}/storage/v1/object/public/katalog-images/{fallback_path}"
            logger.info(f"✅ Image uploaded to fallback path: {public_url}")
            return public_url
    except Exception as e:
        logger.error(f"Fallback upload failed: {e}")
    
    return None

def parse_date(s):
    if not s or s == "null":
        return None
    for fmt in ["%Y-%m-%d", "%d.%m.%Y.", "%d.%m.%Y"]:
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
        except:
            continue
    return None

def save_products(products, store, page_num, page_url, catalogue_name, valid_from, valid_until):
    if not products:
        return 0
    
    records = []
    for p in products:
        if not p.get("sale_price") or p.get("sale_price") in [None, "null", ""]:
            continue
        
        vu = parse_date(p.get("valid_until")) or valid_until
        vf = parse_date(p.get("valid_from")) or valid_from
        if not vu:
            vu = valid_until
        
        # Ensure page_url is stored
        final_page_url = page_url if page_url else None
        
        records.append({
            "store": store,
            "product": p.get("product", ""),
            "brand": p.get("brand") if p.get("brand") not in [None, "null"] else None,
            "quantity": p.get("quantity") if p.get("quantity") not in [None, "null"] else None,
            "original_price": p.get("original_price") if p.get("original_price") not in [None, "null"] else None,
            "sale_price": p.get("sale_price", ""),
            "discount_percent": p.get("discount_percent") if p.get("discount_percent") not in [None, "null"] else None,
            "category": p.get("category", "Other"),
            "subcategory": p.get("subcategory"),
            "valid_from": vf,
            "valid_until": vu,
            "is_expired": False,
            "page_image_url": final_page_url,
            "page_number": page_num,
            "catalogue_name": catalogue_name,
            "catalogue_week": datetime.now().strftime("%Y-W%V")
        })
    
    if not records:
        return 0
    
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/products",
            headers={**db_headers(), "Prefer": "return=minimal"},
            json=records,
            timeout=30
        )
        if r.status_code in [200, 201]:
            logger.info(f"Saved {len(records)} products for page {page_num}")
            return len(records)
        else:
            logger.error(f"Failed to save products: {r.status_code} - {r.text[:200]}")
            return 0
    except Exception as e:
        logger.error(f"Exception saving products: {e}")
        return 0

def save_catalogue(store, catalogue_name, valid_from, valid_until, fine_print, pages, products_count):
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/catalogues",
            headers={**db_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
            json={
                "store": store,
                "catalogue_name": catalogue_name,
                "valid_from": valid_from,
                "valid_until": valid_until,
                "fine_print": fine_print,
                "pages": pages,
                "products_count": products_count
            },
            timeout=30
        )
    except Exception as e:
        logger.error(f"Failed to save catalogue: {e}")

# ─────────────────────────────────────────
# WHATSAPP BOT
# ─────────────────────────────────────────

def get_products():
    today = date.today().strftime("%Y-%m-%d")
    future = (date.today() + timedelta(days=7)).strftime("%Y-%m-%d")
    h = db_headers()
    
    try:
        active = requests.get(
            f"{SUPABASE_URL}/rest/v1/products?valid_from=lte.{today}&or=(valid_until.gte.{today},valid_until.is.null)&is_expired=eq.false&limit=300&order=store",
            headers=h,
            timeout=30
        )
        upcoming = requests.get(
            f"{SUPABASE_URL}/rest/v1/products?valid_from=gt.{today}&valid_from=lte.{future}&is_expired=eq.false&limit=100&order=valid_from",
            headers=h,
            timeout=30
        )
        catalogues = requests.get(
            f"{SUPABASE_URL}/rest/v1/catalogues?valid_until=gte.{today}&select=store,fine_print",
            headers=h,
            timeout=30
        )
        
        fine_prints = {}
        if catalogues.status_code == 200:
            for c in catalogues.json():
                if c.get("fine_print"):
                    fine_prints[c["store"]] = c["fine_print"]
        
        return (
            active.json() if active.status_code == 200 else [],
            upcoming.json() if upcoming.status_code == 200 else [],
            fine_prints
        )
    except Exception as e:
        logger.error(f"Error getting products: {e}")
        return [], [], {}

def get_or_create_user(phone):
    phone_encoded = quote(phone, safe='')
    h = db_headers()
    
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/users?phone=eq.{phone_encoded}",
            headers=h,
            timeout=30
        )
        
        if r.status_code == 200 and r.json():
            return r.json()[0]
        
        # Create new user
        new_user = {
            "phone": phone,
            "total_searches": 0,
            "conversation": [],
            "created_at": datetime.now().isoformat(),
            "last_active": datetime.now().isoformat()
        }
        
        requests.post(
            f"{SUPABASE_URL}/rest/v1/users",
            headers={**h, "Prefer": "return=minimal"},
            json=new_user,
            timeout=30
        )
        
        return new_user
    except Exception as e:
        logger.error(f"Error in get_or_create_user: {e}")
        return {"phone": phone, "total_searches": 0, "conversation": []}

def update_user(phone, updates):
    phone_encoded = quote(phone, safe='')
    try:
        r = requests.patch(
            f"{SUPABASE_URL}/rest/v1/users?phone=eq.{phone_encoded}",
            headers={**db_headers(), "Prefer": "return=minimal"},
            json={**updates, "last_active": datetime.now().isoformat()},
            timeout=10
        )
        if r.status_code not in [200, 201, 204]:
            logger.error(f"update_user failed: {r.status_code} - {r.text[:200]}")
    except Exception as e:
        logger.error(f"update_user exception: {e}")

def get_conversation(user):
    conv = user.get("conversation") or []
    if isinstance(conv, list):
        return conv
    try:
        return json.loads(conv) if conv else []
    except:
        return []

def save_conversation(phone, conversation, user_message, bot_reply):
    conv = conversation or []
    conv.append({
        "role": "user",
        "content": user_message[:500],
        "time": datetime.now().strftime("%H:%M")
    })
    conv.append({
        "role": "bot",
        "content": bot_reply[:500],
        "time": datetime.now().strftime("%H:%M")
    })
    conv = conv[-30:]  # Keep last 30 messages
    
    phone_encoded = quote(phone, safe='')
    try:
        r = requests.patch(
            f"{SUPABASE_URL}/rest/v1/users?phone=eq.{phone_encoded}",
            headers={**db_headers(), "Prefer": "return=minimal"},
            json={"conversation": conv, "last_active": datetime.now().isoformat()},
            timeout=10
        )
        logger.info(f"save_conversation status: {r.status_code}")
    except Exception as e:
        logger.error(f"save_conversation error: {e}")
    
    return conv

def filter_products(message, active, upcoming):
    translations = {
        "mlijeko": "milk", "mlijeka": "milk", "mlijecni": "dairy", "mlijecnih": "dairy",
        "meso": "meat", "mesa": "meat", "mesni": "meat",
        "pile": "chicken", "piletina": "chicken", "pileca": "chicken",
        "kruh": "bread", "kruha": "bread", "pecivo": "bakery",
        "voce": "fruit", "voca": "fruit", "povrce": "vegetables", "povrca": "vegetables",
        "jogurt": "yogurt", "jogurta": "yogurt",
        "sir": "cheese", "sira": "cheese",
        "grickalice": "snacks", "slatkisi": "sweets",
        "cokolada": "chocolate", "cokolade": "chocolate",
        "pivo": "beer", "vino": "wine", "sokovi": "juice", "sok": "juice",
        "ulje": "oil", "brasno": "flour", "secer": "sugar",
        "kava": "coffee", "caj": "tea",
        "riba": "fish", "ribe": "fish",
        "svinjetina": "pork", "svinjski": "pork", "svinjska": "pork",
        "govedina": "beef", "jaja": "eggs", "jaje": "eggs",
        "sapun": "soap", "ljubimci": "pets", "pas": "dog", "macka": "cat",
    }
    
    msg_lower = message.lower()
    for cro, eng in translations.items():
        msg_lower = msg_lower.replace(cro, eng)

    keywords = [w for w in msg_lower.split() if len(w) > 2]
    if not keywords:
        return active[:50], upcoming[:20]

    def matches(p):
        name = (p.get("product") or "").lower()
        brand = (p.get("brand") or "").lower()
        cat = (p.get("category") or "").lower()
        subcat = (p.get("subcategory") or "").lower()
        store = (p.get("store") or "").lower()
        for kw in keywords:
            if kw in name or kw in brand or kw in cat or kw in subcat or kw in store:
                return True
        return False

    filtered_active = [p for p in active if matches(p)]
    filtered_upcoming = [p for p in upcoming if matches(p)]
    
    if not filtered_active and not filtered_upcoming:
        return active[:50], upcoming[:20]
    
    return filtered_active[:50], filtered_upcoming[:20]

def format_products(active, upcoming, fine_prints):
    result = ""
    if active:
        result += "=== ACTIVE DEALS ===\n"
        for p in active:
            result += f"{p.get('store', '')} | {p.get('product', '')}"
            if p.get('brand'): result += f" ({p.get('brand')})"
            if p.get('quantity'): result += f" {p.get('quantity')}"
            result += f" | {p.get('sale_price', '')}€"
            if p.get('original_price'): result += f" (was {p.get('original_price')}€)"
            result += f" | until: {p.get('valid_until', '')}"
            if p.get('page_number'): result += f" | page: {p.get('page_number')}"
            result += "\n"
    
    if upcoming:
        result += "\n=== UPCOMING DEALS ===\n"
        for p in upcoming:
            result += f"{p.get('store', '')} | {p.get('product', '')}"
            result += f" | {p.get('sale_price', '')}€"
            result += f" | from: {p.get('valid_from', '')} to {p.get('valid_until', '')}"
            if p.get('page_number'): result += f" | page: {p.get('page_number')}"
            result += "\n"
    
    if fine_prints:
        result += "\n=== STORE NOTES ===\n"
        for s, fp in fine_prints.items():
            result += f"{s}: {fp}\n"
    
    return result or "No matching products found."

def get_page_image_url(store, page_num, all_products):
    """Get image URL for a specific store and page number"""
    logger.info(f"Looking for image: store={store}, page={page_num}")
    
    store_lower = store.lower() if store else ""
    
    # Strategy 1: Exact match with store and page
    for p in all_products:
        p_store = (p.get("store") or "").lower()
        p_page = p.get("page_number")
        p_url = p.get("page_image_url")
        
        if p_url and p_page == page_num:
            if not store or p_store == store_lower:
                logger.info(f"Found exact match: {p_url}")
                return p_url
    
    # Strategy 2: Any product with that page number
    for p in all_products:
        if p.get("page_number") == page_num and p.get("page_image_url"):
            logger.info(f"Found page match (different store): {p.get('page_image_url')}")
            return p.get("page_image_url")
    
    # Strategy 3: Try to construct URL from pattern
    sample = next((p for p in all_products if p.get("page_image_url")), None)
    if sample and sample.get("page_image_url"):
        url = sample.get("page_image_url")
        # Try different page number patterns
        patterns = [
            (r'_page_(\d+)\.jpg', f'_page_{str(page_num).zfill(3)}.jpg'),
            (r'_(\d+)\.jpg', f'_{str(page_num)}.jpg'),
            (r'page-(\d+)\.jpg', f'page-{page_num}.jpg')
        ]
        
        for pattern, replacement in patterns:
            match = re.search(pattern, url)
            if match:
                new_url = url.replace(match.group(0), replacement)
                logger.info(f"Constructed URL from pattern: {new_url}")
                return new_url
    
    logger.warning(f"No image found for page {page_num}")
    return None

def get_adjacent_page(current_url, direction, all_products):
    """Get next/previous page image URL"""
    if not current_url:
        return None
    
    try:
        # Try different page number patterns
        patterns = [
            (r'_page_(\d+)\.jpg', '_page_'),
            (r'_(\d+)\.jpg', '_'),
            (r'page-(\d+)\.jpg', 'page-')
        ]
        
        for pattern, prefix in patterns:
            match = re.search(pattern, current_url)
            if match:
                current_num = int(match.group(1))
                new_num = current_num + direction
                
                if new_num < 1:
                    return None
                
                # Try to find existing product with this page number
                for p in all_products:
                    if p.get("page_number") == new_num and p.get("page_image_url"):
                        logger.info(f"Found adjacent page {new_num}: {p.get('page_image_url')}")
                        return p.get("page_image_url")
                
                # Construct new URL
                new_url = current_url.replace(
                    match.group(0),
                    f"{prefix}{str(new_num).zfill(3)}.jpg"
                )
                logger.info(f"Constructed adjacent URL: {new_url}")
                return new_url
        
        return None
    except Exception as e:
        logger.error(f"get_adjacent_page error: {e}")
        return None

def extract_page_numbers(text):
    numbers = re.findall(r'\b(\d{1,3})\b', text)
    return [int(n) for n in numbers if 1 <= int(n) <= 200]

def build_conversation_context(conversation):
    if not conversation:
        return ""
    ctx = "CONVERSATION HISTORY (last messages):\n"
    for msg in conversation[-10:]:
        ctx += f"{msg.get('role', '')} [{msg.get('time', '')}]: {msg.get('content', '')}\n"
    return ctx

def ask_gemini(message, products, user, conversation):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    today = date.today().strftime("%d.%m.%Y.")
    
    user_ctx = ""
    if user.get("user_summary"):
        user_ctx = f"User profile: {user.get('user_summary')}\n"
    
    conv_ctx = build_conversation_context(conversation)
    
    prompt = (
        f"You are katalog.ai - a smart friendly shopping assistant. Today is {today}. "
        f"{user_ctx}{conv_ctx}"
        f"\nPRODUCT DATABASE (English, with page numbers):\n{products}\n\n"
        "INSTRUCTIONS:\n"
        "- Max 4096 characters total. Be concise.\n"
        "- Respond in the same language the user writes in. Translate product names naturally.\n"
        "- When listing products always mention which PAGE they are on.\n"
        "- After listing products always end with page numbers like: Pages: 1, 3, 7 — reply with a number to see that page 📖\n"
        "- You can split into 2 messages using [MSG2] tag when it improves readability.\n"
        "- On first greeting introduce yourself and tell user: type a page number to see it, + next page, - previous page.\n"
        "- Use conversation history to remember context - never ask what was already answered.\n"
        "- Be warm and natural. No markdown, no asterisks. Emojis welcome.\n"
        "- Use all data you can get to give the users. You are like google for katalogs.\n"
        f"\nUser message: {message}"
    )
    
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    
    try:
        r = requests.post(url, json=body, timeout=30)
        response_text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        return response_text
    except Exception as e:
        logger.error(f"ask_gemini error: {e}")
        return "Sorry, could not process your request right now."

def create_media_message(resp, text, image_url):
    """Helper to create media messages with proper formatting"""
    # Validate image first
    valid, message = validate_image_for_whatsapp(image_url)
    if not valid:
        logger.error(f"Image validation failed: {message}")
        resp.message(f"⚠️ Image not available: {message}")
        return resp
    
    # Encode URL properly
    encoded_url = encode_url(image_url)
    logger.info(f"Sending media with URL: {encoded_url}")
    
    # Create message with media
    msg = resp.message(text)
    msg.media(encoded_url)
    return resp

@app.route("/webhook", methods=["POST"])
def webhook():
    phone = request.form.get("From", "")
    message = request.form.get("Body", "").strip()
    
    # Log incoming request
    logger.info(f"📱 Webhook received - Phone: {phone}, Message: '{message}'")
    
    user = get_or_create_user(phone)
    active, upcoming, fine_prints = get_products()
    all_products = active + upcoming
    conversation = get_conversation(user)
    resp = MessagingResponse()

    # Handle page navigation with +/-
    if message in ["+", ">", "➕", "➡️"]:
        logger.info("➡️ Page forward requested")
        adj = get_adjacent_page(user.get("last_page_url"), 1, all_products)
        if adj:
            logger.info(f"Found next page: {adj}")
            create_media_message(resp, "➡️  ( + / - )", adj)
            update_user(phone, {"last_page_url": adj})
        else:
            logger.info("No next page found")
            resp.message("⛔ Nema sljedeće stranice.")
        return str(resp)

    if message in ["-", "<", "➖", "⬅️"]:
        logger.info("⬅️ Page backward requested")
        adj = get_adjacent_page(user.get("last_page_url"), -1, all_products)
        if adj:
            logger.info(f"Found previous page: {adj}")
            create_media_message(resp, "⬅️  ( + / - )", adj)
            update_user(phone, {"last_page_url": adj})
        else:
            logger.info("No previous page found")
            resp.message("⛔ Nema prethodne stranice.")
        return str(resp)

    # Check for page number requests
    waiting = user.get("waiting_for_page") or False
    available = user.get("available_pages") or []
    if isinstance(available, str):
        try:
            available = json.loads(available)
        except:
            available = []

    nums = extract_page_numbers(message)
    is_only_numbers = bool(nums) and not re.search(r'[a-zA-ZčćšđžČĆŠĐŽ]{3,}', message)
    page_request_nums = []

    if waiting and nums:
        page_request_nums = [n for n in nums if n in available] or nums[:3]
    elif is_only_numbers:
        page_request_nums = nums[:3]
    else:
        explicit = re.findall(r'(?:stranica|str\.|strana|page|pg\.?|pagina|seite|sida)\s*(\d+)', message.lower())
        if explicit:
            page_request_nums = [int(n) for n in explicit[:3]]

    if page_request_nums:
        store = user.get("last_catalogue_store") or ""
        logger.info(f"📄 Page number request: {page_request_nums}, store: {store}")
        sent_any = False
        
        for pg in page_request_nums[:2]:  # Max 2 pages per request
            img_url = get_page_image_url(store, pg, all_products)
            if img_url:
                logger.info(f"Found image for page {pg}: {img_url}")
                create_media_message(resp, f"📖 Str. {pg}  ( + / - )", img_url)
                update_user(phone, {"last_page_url": img_url, "waiting_for_page": False})
                sent_any = True
            else:
                logger.warning(f"No image URL found for page {pg}")
        
        if not sent_any:
            resp.message("Nemam sliku za tu stranicu. Pokušaj drugi broj.")
        
        return str(resp)

    # Handle text queries
    filtered_active, filtered_upcoming = filter_products(message, active, upcoming)
    page_nums = sorted(set([p.get("page_number") for p in filtered_active + filtered_upcoming if p.get("page_number")]))
    stores = list(set([p.get("store") for p in filtered_active + filtered_upcoming if p.get("store")]))
    main_store = stores[0] if len(stores) == 1 else ""
    
    products_ctx = format_products(filtered_active, filtered_upcoming, fine_prints)
    reply = ask_gemini(message, products_ctx, user, conversation)
    
    save_conversation(phone, conversation, message, reply)

    if page_nums:
        update_user(phone, {
            "waiting_for_page": True,
            "available_pages": page_nums,
            "last_catalogue_store": main_store
        })
    else:
        update_user(phone, {"waiting_for_page": False})

    # Split message if needed
    parts = reply.split("[MSG2]")
    msg1 = parts[0].strip()
    msg2 = parts[1].strip() if len(parts) > 1 else ""
    
    resp.message(msg1)
    if msg2:
        resp.message(msg2)
    
    logger.info(f"Response sent: {msg1[:100]}...")
    return str(resp)

# ─────────────────────────────────────────
# DEBUG ENDPOINTS
# ─────────────────────────────────────────

@app.route("/debug/products/<phone>")
def debug_products(phone):
    """Debug endpoint to check what products/images are stored for a user"""
    user = get_or_create_user(phone)
    active, upcoming, fine_prints = get_products()
    all_products = active + upcoming
    
    # Get products with images
    products_with_images = [p for p in all_products if p.get("page_image_url")]
    
    # Group by store and page
    by_store = {}
    for p in products_with_images:
        store = p.get("store", "Unknown")
        if store not in by_store:
            by_store[store] = []
        by_store[store].append({
            "page": p.get("page_number"),
            "product": p.get("product"),
            "image_url": p.get("page_image_url")
        })
    
    # Test a sample image
    sample_test = None
    if products_with_images:
        sample_url = products_with_images[0].get("page_image_url")
        valid, message = validate_image_for_whatsapp(sample_url)
        sample_test = {
            "url": sample_url,
            "valid": valid,
            "message": message
        }
    
    return jsonify({
        "total_products": len(all_products),
        "products_with_images": len(products_with_images),
        "by_store": by_store,
        "user": {
            "last_page_url": user.get("last_page_url"),
            "waiting_for_page": user.get("waiting_for_page"),
            "available_pages": user.get("available_pages"),
            "last_catalogue_store": user.get("last_catalogue_store")
        },
        "sample_image_test": sample_test,
        "fine_prints": fine_prints
    })

@app.route("/test/image-url")
def test_image_url():
    """Test if an image URL is accessible and properly formatted for Twilio"""
    url = request.args.get("url")
    if not url:
        return "Please provide a url parameter", 400
    
    results = {
        "url": url,
        "encoded_url": encode_url(url),
        "tests": {}
    }
    
    # Test 1: Is URL accessible?
    try:
        r = requests.head(url, timeout=10, allow_redirects=True)
        results["tests"]["http_head"] = {
            "status": r.status_code,
            "success": r.status_code == 200,
            "content_type": r.headers.get("content-type"),
            "content_length": r.headers.get("content-length")
        }
    except Exception as e:
        results["tests"]["http_head"] = {"error": str(e)}
    
    # Test 2: WhatsApp validation
    valid, message = validate_image_for_whatsapp(url)
    results["tests"]["whatsapp_validation"] = {
        "valid": valid,
        "message": message
    }
    
    return jsonify(results)

@app.route("/test-webhook", methods=["POST"])
def test_webhook():
    """Test endpoint to simulate sending images"""
    phone = request.form.get("From", "test-user")
    message = request.form.get("Body", "").strip()
    
    resp = MessagingResponse()
    
    # Get any product with an image
    active, upcoming, _ = get_products()
    all_products = active + upcoming
    
    sample_product = next((p for p in all_products if p.get("page_image_url")), None)
    
    if sample_product:
        test_url = sample_product["page_image_url"]
        logger.info(f"Test webhook - sending image: {test_url}")
        
        valid, msg = validate_image_for_whatsapp(test_url)
        if valid:
            create_media_message(resp, "Test image from database:", test_url)
        else:
            resp.message(f"Sample image invalid: {msg}")
    else:
        resp.message("No images found in database. Upload a catalog first.")
    
    return str(resp)

@app.route("/", methods=["GET"])
def home():
    return "katalog.ai is running! Use /upload-tool to upload catalogs or /webhook for WhatsApp."
# ─────────────────────────────────────────
# BARCODE LOOKUP — add this to app.py
# ─────────────────────────────────────────
# Paste this entire block into app.py, just before the if __name__ == "__main__": line
@app.route('/manifest.json')
def manifest():
    return send_from_directory('static', 'Manifest.json')

@app.route('/sw.js')
def service_worker():
    return send_from_directory('static', 'sw.js', mimetype='application/javascript')
@app.route("/api/barcode/<barcode>")

def barcode_lookup(barcode):
    """
    Look up a product by barcode.
    1. First checks our own Supabase products table
    2. Then calls cijene.dev MCP for live price comparison
    """
    country = request.args.get("country", "croatia")

    results = {
        "barcode": barcode,
        "product_name": None,
        "brand": None,
        "prices": [],
        "source": None
    }

    # ── Step 1: Check our own database first ─────────────────────────────
    try:
        own = requests.get(
            f"{SUPABASE_URL}/rest/v1/products?barcode=eq.{barcode}&limit=20",
            headers=db_headers(),
            timeout=10
        )
        if own.status_code == 200 and own.json():
            products = own.json()
            results["product_name"] = products[0].get("product")
            results["brand"]        = products[0].get("brand")
            results["source"]       = "supabase"

            for p in products:
                results["prices"].append({
                    "store":          p.get("store"),
                    "price":          p.get("sale_price"),
                    "original_price": p.get("original_price"),
                    "discount_percent": p.get("discount_percent"),
                    "valid_until":    p.get("valid_until"),
                })

            logger.info(f"Barcode {barcode}: found {len(products)} results in Supabase")
    except Exception as e:
        logger.warning(f"Supabase barcode lookup failed: {e}")

    # ── Step 2: Call cijene.dev MCP for live prices ───────────────────────
    try:
        mcp_result = lookup_barcode_mcp(barcode, country)
        if mcp_result:
            # If we didn't find product name in Supabase, use MCP result
            if not results["product_name"] and mcp_result.get("product_name"):
                results["product_name"] = mcp_result["product_name"]
            if not results["brand"] and mcp_result.get("brand"):
                results["brand"] = mcp_result["brand"]

            # Merge MCP prices — avoid duplicate stores
            existing_stores = {p["store"] for p in results["prices"]}
            for p in mcp_result.get("prices", []):
                if p.get("store") not in existing_stores:
                    results["prices"].append(p)

            if not results["source"]:
                results["source"] = "cijene.dev"

            logger.info(f"Barcode {barcode}: MCP returned {len(mcp_result.get('prices', []))} prices")

    except Exception as e:
        logger.warning(f"MCP barcode lookup failed: {e}")

    # Sort prices cheapest first
    results["prices"].sort(key=lambda x: float(x.get("price") or 999))

    return jsonify(results)


def lookup_barcode_mcp(barcode, country="croatia"):
    """
    Call the cijene.dev MCP server to look up a barcode.
    Adjust the MCP endpoint URL to match your actual cijene.dev MCP setup.
    """
    try:
        # ── Option A: cijene.dev has a direct barcode search API ─────────
        # Adjust this URL to your actual cijene.dev MCP endpoint
        mcp_url = os.environ.get("CIJENE_MCP_URL", "https://cijene.dev/api")

        response = requests.get(
            f"{mcp_url}/barcode/{barcode}",
            params={"country": country},
            headers={
                "Authorization": f"Bearer {os.environ.get('CIJENE_API_KEY', '')}",
                "Accept": "application/json"
            },
            timeout=15
        )

        if response.status_code != 200:
            logger.warning(f"cijene.dev MCP returned {response.status_code} for barcode {barcode}")
            return None

        data = response.json()

        # ── Normalize cijene.dev response to our format ───────────────────
        # Adjust field names below to match actual cijene.dev API response
        normalized = {
            "product_name": data.get("name") or data.get("product_name") or data.get("naziv"),
            "brand":        data.get("brand") or data.get("marka"),
            "prices": []
        }

        # cijene.dev typically returns a list of store prices
        price_list = (
            data.get("prices") or
            data.get("store_prices") or
            data.get("cijene") or
            []
        )

        for item in price_list:
            normalized["prices"].append({
                "store":            item.get("store") or item.get("trgovina") or item.get("name"),
                "price":            item.get("price") or item.get("cijena"),
                "original_price":   item.get("original_price") or item.get("redovna_cijena"),
                "discount_percent": item.get("discount") or item.get("popust"),
                "valid_until":      item.get("valid_until") or item.get("vrijedi_do"),
            })

        return normalized

    except requests.exceptions.Timeout:
        logger.warning(f"cijene.dev MCP timeout for barcode {barcode}")
        return None
    except Exception as e:
        logger.error(f"lookup_barcode_mcp error: {e}")
        return None

# ─────────────────────────────────────────
# BASKET OPTIMIZER — add this to app.py
# Paste just above: if __name__ == "__main__":
# ─────────────────────────────────────────

@app.route("/api/basket", methods=["POST"])
def basket_optimizer():
    """
    Given a list of product names + quantities, find the cheapest
    combination of stores to buy everything.

    Returns combinations sorted by total price:
    - 1-store result (cheapest single store for whole basket)
    - 2-store result (cheapest split across 2 stores)
    - 3-store result (cheapest split across 3 stores)
    - Absolute cheapest (best price per item regardless of store count)
    """
    data    = request.json or {}
    items   = data.get("items", [])   # [{"name": "mlijeko", "qty": 2}, ...]
    country = data.get("country", "croatia")

    if not items:
        return jsonify({"error": "No items provided"}), 400

    today  = date.today().strftime("%Y-%m-%d")
    future = (date.today() + timedelta(days=7)).strftime("%Y-%m-%d")

    # ── Step 1: Search Supabase for each item ─────────────────────────────
    item_results = []   # [{name, qty, matches: [{store, price, product, ...}]}]
    not_found    = []

    for item in items[:20]:  # max 20 items
        name = item.get("name", "").strip()
        qty  = int(item.get("qty", 1))
        if not name:
            continue

        matches = search_item_prices(name, today, future)

        if matches:
            item_results.append({
                "name":    name,
                "qty":     qty,
                "matches": matches
            })
        else:
            not_found.append(name)
            logger.warning(f"Basket: no matches for '{name}'")

    if not item_results:
        return jsonify({
            "combinations": [],
            "not_found":    not_found,
            "item_results": []
        })

    # ── Step 2: Get all stores that have at least some items ──────────────
    all_stores = set()
    for item in item_results:
        for m in item["matches"]:
            all_stores.add(m["store"])
    all_stores = sorted(all_stores)

    # ── Step 3: Build store price matrix ─────────────────────────────────
    # store_matrix[store][item_name] = cheapest price for that item at that store
    store_matrix = {}
    for store in all_stores:
        store_matrix[store] = {}
        for item in item_results:
            store_matches = [m for m in item["matches"] if m["store"] == store]
            if store_matches:
                # Pick cheapest match at this store
                best = min(store_matches, key=lambda m: float(m.get("sale_price") or 999))
                store_matrix[store][item["name"]] = {
                    "price":   float(best.get("sale_price") or 0),
                    "product": best.get("product", item["name"]),
                    "matched": True
                }

    # ── Step 4: Calculate basket total for a set of stores ───────────────
    def calc_basket(store_set):
        """
        For a given set of stores, assign each item to the cheapest store
        in that set. Return total + per-store breakdown.
        """
        total        = 0.0
        store_items  = {s: [] for s in store_set}
        item_coverage = 0

        for item in item_results:
            best_price = None
            best_store = None

            for store in store_set:
                price_info = store_matrix.get(store, {}).get(item["name"])
                if price_info and (best_price is None or price_info["price"] < best_price):
                    best_price = price_info["price"]
                    best_store = store

            if best_store is not None:
                line_total = best_price * item["qty"]
                total     += line_total
                store_items[best_store].append({
                    "name":    item["name"],
                    "qty":     item["qty"],
                    "price":   best_price,
                    "matched": True
                })
                item_coverage += 1
            else:
                # Item not available in any store in this set — add to cheapest store
                if store_set:
                    fallback = list(store_set)[0]
                    store_items[fallback].append({
                        "name":    item["name"],
                        "qty":     item["qty"],
                        "price":   0,
                        "matched": False
                    })

        stores_breakdown = []
        for store in store_set:
            s_items = store_items[store]
            s_total = sum(i["price"] * i["qty"] for i in s_items if i["matched"])
            if s_items:  # only include stores that have items
                stores_breakdown.append({
                    "store":    store,
                    "subtotal": s_total,
                    "items":    s_items
                })

        return {
            "total":          total,
            "stores":         stores_breakdown,
            "item_coverage":  item_coverage,
            "total_items":    len(item_results)
        }

    # ── Step 5: Find best combination for each store count ────────────────
    from itertools import combinations as iter_combos

    results = []
    max_stores = min(len(all_stores), 4)  # up to 4 store combinations

    for n in range(1, max_stores + 1):
        best_combo   = None
        best_total   = float('inf')

        for store_combo in iter_combos(all_stores, n):
            result = calc_basket(set(store_combo))
            if result["total"] < best_total:
                best_total = result["total"]
                best_combo = result

        if best_combo:
            best_combo["store_count"] = n
            best_combo["is_absolute_cheapest"] = False
            results.append(best_combo)

    # Mark absolute cheapest (best price per item, any number of stores)
    absolute = calc_basket(set(all_stores))
    absolute["store_count"] = len(all_stores)
    absolute["is_absolute_cheapest"] = True

    # Check if absolute cheapest is different from existing results
    already_included = any(
        abs(r["total"] - absolute["total"]) < 0.01
        for r in results
    )
    if not already_included and len(all_stores) > max_stores:
        results.append(absolute)

    # Sort by total price
    results.sort(key=lambda r: r["total"])

    # Most expensive single store (for savings comparison)
    most_expensive_single = None
    if results:
        single_store_results = [r for r in results if r["store_count"] == 1]
        if single_store_results:
            most_expensive_single = max(r["total"] for r in single_store_results)

    return jsonify({
        "combinations":          results,
        "item_results":          [{"name": i["name"], "qty": i["qty"]} for i in item_results],
        "not_found":             not_found,
        "most_expensive_single": most_expensive_single,
        "total_items":           len(item_results),
        "stores_searched":       all_stores
    })


def search_item_prices(name, today, future):
    """
    Search Supabase products for a given item name.
    Returns list of active deals matching the name.
    """
    try:
        # Try exact keyword search using ilike
        keywords = name.lower().split()
        
        # Build a filter — search product name for any keyword
        # Supabase REST doesn't support OR across fields directly,
        # so we fetch broadly and filter in Python
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/products",
            headers=db_headers(),
            params={
                "product":    f"ilike.*{keywords[0]}*",
                "valid_from": f"lte.{future}",
                "or":         f"(valid_until.gte.{today},valid_until.is.null)",
                "is_expired": "eq.false",
                "order":      "sale_price",
                "limit":      100,
                "select":     "store,product,brand,sale_price,original_price,discount_percent,valid_until,category"
            },
            timeout=15
        )

        if r.status_code != 200:
            logger.error(f"search_item_prices failed: {r.status_code}")
            return []

        products = r.json()

        # Further filter: all keywords must appear in product name
        if len(keywords) > 1:
            products = [
                p for p in products
                if all(kw in p.get("product", "").lower() for kw in keywords)
            ]

        return products

    except Exception as e:
        logger.error(f"search_item_prices error for '{name}': {e}")
        return []

        

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
