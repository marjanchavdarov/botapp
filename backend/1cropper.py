"""
katalog.ai — Cropper Tool (standalone)
Takes catalogue pages from the database, uses Gemini to detect product
bounding boxes, crops individual product images, saves to storage bucket
under: product-images/{store}/{catalogue_name}/{product_id}.jpg

Requirements: flask flask-cors requests pymupdf gunicorn pillow
Start command: gunicorn cropper:app --worker-class gthread -w 1 --threads 4 --bind 0.0.0.0:$PORT
Root directory: backend

Env vars needed:
  SUPABASE_URL
  SUPABASE_KEY
  SUPABASE_SERVICE_KEY  (recommended)
  GEMINI_API_KEY
  UPLOAD_PASSWORD       (same as upload tool)
"""

import os, json, uuid, base64, logging, threading, time, re, io
from datetime import datetime
import requests
from flask import Flask, request, jsonify, make_response
from flask_cors import CORS

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    logging.warning("Pillow not installed — install with: pip install pillow")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cropper")

app = Flask(__name__)
CORS(app)

# ── CONFIG ───────────────────────────────────────────────────────────────────
class Config:
    SUPABASE_URL         = os.environ.get("SUPABASE_URL")
    SUPABASE_KEY         = os.environ.get("SUPABASE_KEY")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
    GEMINI_API_KEY       = os.environ.get("GEMINI_API_KEY")
    STORAGE_BUCKET       = "katalog-images"
    UPLOAD_PASSWORD      = os.environ.get("UPLOAD_PASSWORD", "katalog2026")

# ── SUPABASE HELPERS ─────────────────────────────────────────────────────────
def _headers():
    return {
        "apikey":        Config.SUPABASE_KEY,
        "Authorization": f"Bearer {Config.SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal",
    }

def _sb_get(path, params=None):
    r = requests.get(f"{Config.SUPABASE_URL}{path}", headers=_headers(),
                     params=params, timeout=20, verify=False)
    r.raise_for_status()
    return r.json()

def _sb_patch(path, data):
    r = requests.patch(f"{Config.SUPABASE_URL}{path}", headers=_headers(),
                       json=data, timeout=20, verify=False)
    r.raise_for_status()
    return r

def _sb_storage_put(path, img_bytes, content_type="image/jpeg"):
    key = Config.SUPABASE_SERVICE_KEY or Config.SUPABASE_KEY
    url = f"{Config.SUPABASE_URL}/storage/v1/object/{Config.STORAGE_BUCKET}/{path}"
    r = requests.put(url, headers={
        "apikey": key, "Authorization": f"Bearer {key}",
        "Content-Type": content_type, "x-upsert": "true",
    }, data=img_bytes, timeout=30, verify=False)
    if not r.ok:
        raise Exception(f"Storage {r.status_code}: {r.text[:300]}")
    return f"{Config.SUPABASE_URL}/storage/v1/object/public/{Config.STORAGE_BUCKET}/{path}"

# ── GEMINI BBOX DETECTION ────────────────────────────────────────────────────
_GEMINI_URL = (
    "https://generativelanguage.googleapis.com"
    "/v1beta/models/gemini-2.5-flash:generateContent"
)

def detect_products_bbox(img_b64, img_width, img_height):
    """
    Ask Gemini to return product bounding boxes as normalized 0.0-1.0 fractions.
    This is the most reliable format across all Gemini versions.
    """
    prompt = """You are a precise product detector for retail catalogues.

Look at this catalogue page and find every individual product.
Each product box must include: the product image AND its price tag.

Return ONLY a JSON array. No markdown, no explanation. Example:
[
  {"label": "Gloria kava 500g", "x1": 0.05, "y1": 0.10, "x2": 0.48, "y2": 0.55},
  {"label": "Doppelkeks 600g",  "x1": 0.52, "y1": 0.08, "x2": 0.95, "y2": 0.52}
]

Rules:
- x1,y1 = top-left corner (0.0 to 1.0)
- x2,y2 = bottom-right corner (0.0 to 1.0)
- Include price tag in each box
- Do NOT overlap boxes
- Return [] if no products found
"""
    body = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}},
            {"text": prompt},
        ]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 4096},
    }

    for attempt in range(3):
        try:
            r = requests.post(f"{_GEMINI_URL}?key={Config.GEMINI_API_KEY}",
                              json=body, timeout=90)
            if r.status_code != 200:
                logger.error(f"Gemini {r.status_code}: {r.text[:300]}")
                time.sleep(2 ** attempt)
                continue

            result = r.json()
            if "candidates" not in result:
                logger.error("Gemini: no candidates")
                time.sleep(2 ** attempt)
                continue

            text = result["candidates"][0]["content"]["parts"][0]["text"]
            logger.info(f"Gemini raw response (first 300): {text[:300]}")

            # Strip markdown code fences if present
            text = re.sub(r"```json|```", "", text).strip()

            match = re.search(r"\[.*?\]", text, re.DOTALL)
            if not match:
                logger.warning(f"No JSON array in response: {text[:200]}")
                continue

            parsed = json.loads(match.group())
            if not isinstance(parsed, list):
                continue

            # Convert normalized 0-1 → pixels
            boxes = []
            for item in parsed:
                try:
                    x1 = float(item.get("x1", 0))
                    y1 = float(item.get("y1", 0))
                    x2 = float(item.get("x2", 1))
                    y2 = float(item.get("y2", 1))
                    # Validate range
                    if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
                        logger.warning(f"Invalid bbox: {item}")
                        continue
                    boxes.append({
                        "label": item.get("label", "product"),
                        "x1": int(x1 * img_width),
                        "y1": int(y1 * img_height),
                        "x2": int(x2 * img_width),
                        "y2": int(y2 * img_height),
                    })
                except Exception as e:
                    logger.warning(f"Bad box {item}: {e}")

            if boxes:
                logger.info(f"Detected {len(boxes)} product boxes")
                return boxes
            else:
                logger.warning("All boxes were invalid")

        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error attempt {attempt+1}: {e}")
        except Exception as e:
            logger.error(f"BBox detect attempt {attempt+1}: {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)

    return []

# ── CROPPER ──────────────────────────────────────────────────────────────────
def crop_product(img_bytes, x1, y1, x2, y2, padding=15):
    """Crop a product from page image bytes. Returns JPEG bytes."""
    if not PIL_AVAILABLE:
        raise Exception("Pillow not installed")

    img = Image.open(io.BytesIO(img_bytes))
    w, h = img.size

    # Add padding, clamp to image bounds
    x1 = max(0, x1 - padding)
    y1 = max(0, y1 - padding)
    x2 = min(w, x2 + padding)
    y2 = min(h, y2 + padding)

    # Minimum crop size check
    if (x2 - x1) < 20 or (y2 - y1) < 20:
        raise Exception(f"Crop too small: {x2-x1}x{y2-y1}")

    cropped = img.crop((x1, y1, x2, y2))
    out = io.BytesIO()
    cropped.save(out, format="JPEG", quality=90)
    return out.getvalue()

def download_image(url):
    """Download a page image from Supabase storage."""
    r = requests.get(url, timeout=30, verify=False)
    r.raise_for_status()
    return r.content

# ── CROP JOB ─────────────────────────────────────────────────────────────────
crop_jobs = {}  # in-memory job tracking {job_id: {status, done, total, errors}}

def process_crop_job(job_id, catalogue_name, store):
    """
    Main crop pipeline:
    1. Fetch all distinct pages for this catalogue from DB
    2. For each page: download image, ask Gemini for bboxes
    3. Crop each product, upload to storage, update product record
    """
    crop_jobs[job_id] = {"status": "running", "done": 0, "total": 0, "errors": 0}

    try:
        # Get all distinct pages for this catalogue
        params = {
            "catalogue_name": f"eq.{catalogue_name}",
            "store":          f"eq.{store}",
            "select":         "id,product,page_number,page_image_url,product_image_url",
            "order":          "page_number",
            "limit":          1000,
        }
        products = _sb_get("/rest/v1/products", params) or []
        if not products:
            crop_jobs[job_id]["status"] = "error"
            crop_jobs[job_id]["message"] = "No products found for this catalogue"
            return

        # Group by page
        pages = {}
        for p in products:
            pn = p.get("page_number")
            if pn not in pages:
                pages[pn] = {"url": p.get("page_image_url"), "products": []}
            pages[pn]["products"].append(p)

        total = sum(len(v["products"]) for v in pages.values())
        crop_jobs[job_id]["total"] = total
        done = 0

        safe_store = store.lower().replace(" ", "_")
        safe_cat   = catalogue_name.lower().replace(" ", "_")

        for page_num, page_data in sorted(pages.items()):
            page_url = page_data["url"]
            page_products = page_data["products"]

            if not page_url:
                logger.warning(f"Page {page_num} has no image URL — skipping")
                done += len(page_products)
                crop_jobs[job_id]["done"] = done
                continue

            try:
                logger.info(f"Cropping page {page_num} ({len(page_products)} products)")
                img_bytes = download_image(page_url)

                # Get image dimensions
                if PIL_AVAILABLE:
                    img = Image.open(io.BytesIO(img_bytes))
                    img_w, img_h = img.size
                else:
                    img_w, img_h = 1240, 1754  # A4 at 150dpi fallback

                img_b64 = base64.b64encode(img_bytes).decode()

                # Ask Gemini for bounding boxes
                boxes = detect_products_bbox(img_b64, img_w, img_h)

                if not boxes:
                    logger.warning(f"No boxes detected on page {page_num}")
                    done += len(page_products)
                    crop_jobs[job_id]["done"] = done
                    continue

                # Match boxes to products by label similarity
                # For each box, find closest matching product name
                for box in boxes:
                    label = box.get("label", "").lower()
                    x1 = int(box.get("x1", 0))
                    y1 = int(box.get("y1", 0))
                    x2 = int(box.get("x2", img_w))
                    y2 = int(box.get("y2", img_h))

                    # Skip invalid boxes
                    if x2 <= x1 or y2 <= y1 or (x2-x1) < 10 or (y2-y1) < 10:
                        continue

                    # Find best matching product on this page
                    best_product = None
                    best_score = 0
                    for prod in page_products:
                        prod_name = (prod.get("product") or "").lower()
                        # Simple overlap score
                        label_words = set(label.split())
                        prod_words  = set(prod_name.split())
                        common = label_words & prod_words
                        score = len(common) / max(len(label_words), 1)
                        if score > best_score:
                            best_score = score
                            best_product = prod

                    if not best_product:
                        best_product = page_products[0]  # fallback to first on page

                    # Skip if already has a product image
                    if best_product.get("product_image_url"):
                        done += 1
                        crop_jobs[job_id]["done"] = done
                        continue

                    try:
                        # Crop and upload
                        cropped = crop_product(img_bytes, x1, y1, x2, y2)
                        prod_id = best_product.get("id") or str(uuid.uuid4())[:8]
                        storage_path = f"product-images/{safe_store}/{safe_cat}/{prod_id}.jpg"
                        product_img_url = _sb_storage_put(storage_path, cropped)

                        # Update product record with image URL
                        _sb_patch(
                            f"/rest/v1/products?id=eq.{best_product['id']}",
                            {"product_image_url": product_img_url}
                        )
                        logger.info(f"Cropped: {best_product.get('product')} → {storage_path}")

                    except Exception as e:
                        logger.error(f"Crop failed for box on page {page_num}: {e}")
                        crop_jobs[job_id]["errors"] += 1

                    done += 1
                    crop_jobs[job_id]["done"] = done

            except Exception as e:
                logger.error(f"Page {page_num} crop failed: {e}")
                crop_jobs[job_id]["errors"] += len(page_products)
                done += len(page_products)
                crop_jobs[job_id]["done"] = done

        crop_jobs[job_id]["status"] = "done"
        logger.info(f"Crop job {job_id} complete. Done: {done}, Errors: {crop_jobs[job_id]['errors']}")

    except Exception as e:
        logger.error(f"Crop job {job_id} crashed: {e}")
        crop_jobs[job_id]["status"] = "error"
        crop_jobs[job_id]["message"] = str(e)

# ── ROUTES ───────────────────────────────────────────────────────────────────
@app.route("/")
def home():
    return make_response(CROPPER_HTML, 200, {"Content-Type": "text/html"})

@app.route("/api/catalogues")
def get_catalogues():
    """Return distinct catalogues available for cropping."""
    password = request.headers.get("X-Password") or request.args.get("password","")
    if password != Config.UPLOAD_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403
    try:
        rows = _sb_get("/rest/v1/products", {
            "select": "store,catalogue_name,page_number,product_image_url",
            "limit":  2000,
            "order":  "store,catalogue_name",
        }) or []

        # Group into catalogues
        cats = {}
        for r in rows:
            key = f"{r['store']}|{r['catalogue_name']}"
            if key not in cats:
                cats[key] = {
                    "store":          r["store"],
                    "catalogue_name": r["catalogue_name"],
                    "total":          0,
                    "cropped":        0,
                }
            cats[key]["total"] += 1
            if r.get("product_image_url"):
                cats[key]["cropped"] += 1

        return jsonify(list(cats.values()))
    except Exception as e:
        logger.error(f"get_catalogues failed: {e}")
        return jsonify([])

@app.route("/api/crop", methods=["POST"])
def start_crop():
    """Start a crop job for a catalogue."""
    data     = request.json or {}
    password = data.get("password","")
    if password != Config.UPLOAD_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403

    catalogue_name = data.get("catalogue_name","")
    store          = data.get("store","")
    if not catalogue_name or not store:
        return jsonify({"error": "catalogue_name and store required"}), 400

    job_id = str(uuid.uuid4())[:8]
    threading.Thread(
        target=process_crop_job,
        args=(job_id, catalogue_name, store),
        daemon=True
    ).start()

    return jsonify({"job_id": job_id})

@app.route("/api/crop/status/<job_id>")
def crop_status(job_id):
    job = crop_jobs.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)

@app.route("/debug/health")
def health():
    return jsonify({
        "status": "ok", "service": "katalog-cropper",
        "pillow": PIL_AVAILABLE,
        "time": datetime.now().isoformat()
    })

# ── HTML UI ──────────────────────────────────────────────────────────────────
CROPPER_HTML = '''<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>katalog.ai — Cropper</title>
  <style>
    *{margin:0;padding:0;box-sizing:border-box;font-family:monospace}
    body{background:#111;color:#eee;padding:40px;max-width:900px;margin:0 auto}
    h1{color:#ff9900;margin-bottom:8px;font-size:24px}
    .sub{color:#666;margin-bottom:30px;font-size:13px}
    .card{background:#1a1a1a;border-radius:10px;padding:20px;margin-bottom:16px;border:1px solid #333}
    label{color:#aaa;font-size:11px;text-transform:uppercase;letter-spacing:1px;display:block;margin-bottom:5px}
    input{width:100%;padding:12px;background:#222;border:1px solid #444;color:#eee;border-radius:5px;margin-bottom:15px;font-size:15px;font-family:monospace}
    button{background:#ff9900;color:#000;border:none;padding:12px 24px;font-size:14px;font-weight:bold;border-radius:5px;cursor:pointer;font-family:monospace}
    button:hover{background:#cc7700}
    button:disabled{background:#333;color:#666;cursor:not-allowed}
    .btn-sm{padding:7px 14px;font-size:12px}
    table{width:100%;border-collapse:collapse;margin-top:8px}
    th{text-align:left;color:#666;font-size:11px;text-transform:uppercase;letter-spacing:1px;padding:8px 12px;border-bottom:1px solid #333}
    td{padding:10px 12px;border-bottom:1px solid #222;font-size:13px}
    tr:hover td{background:#1f1f1f}
    .badge{display:inline-block;padding:2px 8px;border-radius:20px;font-size:11px;font-weight:bold}
    .badge-done{background:#1a4a1a;color:#00ff88}
    .badge-partial{background:#4a3a00;color:#ffcc00}
    .badge-none{background:#3a1a1a;color:#ff5555}
    .progress-bar{background:#222;height:20px;border-radius:4px;overflow:hidden;margin:8px 0}
    .progress-fill{background:linear-gradient(90deg,#ff9900,#ffcc00);height:100%;width:0%;transition:width .3s;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:bold;color:#000;font-family:monospace}
    #log{background:#000;padding:14px;border-radius:5px;font-size:12px;line-height:1.7;max-height:300px;overflow-y:auto;border:1px solid #222;margin-top:12px;display:none}
    .status-row{display:flex;align-items:center;gap:12px;margin:8px 0}
  </style>
</head>
<body>
  <h1>✂️ katalog.ai Cropper</h1>
  <div class="sub">Extract individual product images from catalogue pages using AI</div>

  <div class="card">
    <label>Password</label>
    <input type="password" id="password" placeholder="••••••••" onchange="loadCatalogues()">
    <button onclick="loadCatalogues()">🔍 Load Catalogues</button>
  </div>

  <div class="card" id="cat-card" style="display:none">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <div style="color:#ff9900;font-weight:bold;font-size:14px">AVAILABLE CATALOGUES</div>
      <button class="btn-sm" onclick="loadCatalogues()">↻ Refresh</button>
    </div>
    <table>
      <thead><tr><th>Store</th><th>Catalogue</th><th>Products</th><th>Cropped</th><th>Action</th></tr></thead>
      <tbody id="cat-table"></tbody>
    </table>
  </div>

  <div class="card" id="job-card" style="display:none">
    <div style="color:#ff9900;font-weight:bold;margin-bottom:8px" id="job-title">Cropping...</div>
    <div class="progress-bar"><div class="progress-fill" id="job-fill">0%</div></div>
    <div class="status-row">
      <span id="job-status" style="color:#666;font-size:13px">Starting...</span>
    </div>
    <div id="log"></div>
  </div>

  <script>
    let activeJobId = null;
    let pollTimer = null;

    function log(msg, type='info') {
      const el = document.getElementById('log');
      el.style.display = 'block';
      const colors = {success:'#00ff88',error:'#ff5555',info:'#66ccff',warn:'#ffcc00'};
      const t = new Date().toLocaleTimeString();
      el.innerHTML += `<span style="color:#555">[${t}]</span> <span style="color:${colors[type]||'#eee'}">${msg}</span>\n`;
      el.scrollTop = el.scrollHeight;
    }

    async function loadCatalogues() {
      const pw = document.getElementById('password').value;
      if(!pw){alert('Enter password first');return;}
      try {
        const res = await fetch(`/api/catalogues?password=${encodeURIComponent(pw)}`);
        if(res.status===403){alert('Wrong password');return;}
        const cats = await res.json();
        const tbody = document.getElementById('cat-table');
        tbody.innerHTML = cats.map(c => {
          const pct = c.total > 0 ? Math.round(c.cropped/c.total*100) : 0;
          const badge = pct===100 ? 'done' : pct>0 ? 'partial' : 'none';
          const label = pct===100 ? '✅ Complete' : pct>0 ? `${pct}% done` : '⚪ Not started';
          return `<tr>
            <td style="text-transform:uppercase;font-weight:bold">${c.store}</td>
            <td style="max-width:240px;overflow:hidden;text-overflow:ellipsis">${c.catalogue_name}</td>
            <td>${c.total}</td>
            <td><span class="badge badge-${badge}">${label}</span></td>
            <td><button class="btn-sm" onclick="startCrop('${c.store}','${c.catalogue_name.replace(/'/g,"\\'")}')">▶ Crop</button></td>
          </tr>`;
        }).join('');
        document.getElementById('cat-card').style.display = 'block';
      } catch(e) {
        alert('Failed to load: ' + e.message);
      }
    }

    async function startCrop(store, catName) {
      const pw = document.getElementById('password').value;
      document.getElementById('job-card').style.display = 'block';
      document.getElementById('job-title').textContent = `✂️ Cropping: ${store.toUpperCase()} — ${catName}`;
      document.getElementById('log').innerHTML = '';
      document.getElementById('job-fill').style.width = '0%';
      document.getElementById('job-fill').textContent = '0%';
      document.getElementById('job-status').textContent = 'Starting...';
      log(`Starting crop job for ${store} / ${catName}`,'info');

      try {
        const res = await fetch('/api/crop', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify({password:pw, store, catalogue_name:catName})
        });
        if(res.status===403){log('Wrong password','error');return;}
        const data = await res.json();
        activeJobId = data.job_id;
        log(`Job started: ${data.job_id}`,'success');
        if(pollTimer) clearInterval(pollTimer);
        pollTimer = setInterval(pollCrop, 2000);
      } catch(e){
        log(`Failed: ${e.message}`,'error');
      }
    }

    async function pollCrop() {
      if(!activeJobId) return;
      try {
        const res = await fetch(`/api/crop/status/${activeJobId}`);
        const d = await res.json();
        const pct = d.total > 0 ? Math.round(d.done/d.total*100) : 0;
        document.getElementById('job-fill').style.width = pct+'%';
        document.getElementById('job-fill').textContent = pct+'%';
        document.getElementById('job-status').textContent =
          `${d.done} / ${d.total} products | ${d.errors} errors`;

        if(d.status === 'done'){
          clearInterval(pollTimer); pollTimer = null;
          log(`🎉 Done! ${d.done} products cropped, ${d.errors} errors.`,'success');
          loadCatalogues();
        }
        if(d.status === 'error'){
          clearInterval(pollTimer); pollTimer = null;
          log(`❌ Job failed: ${d.message||'unknown error'}`,'error');
        }
      } catch(e){
        log(`Poll error: ${e.message}`,'error');
      }
    }
  </script>
</body>
</html>'''

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5002))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
