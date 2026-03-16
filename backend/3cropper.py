
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

def detect_products_bbox(img_b64, img_width, img_height, examples=None):
    """
    Ask Gemini to return product bounding boxes as normalized 0.0-1.0 fractions.
    Few-shot examples (human annotations) injected when available.
    Labels are optional — boxes alone are enough.
    """
    # Build few-shot text from saved annotations
    fewshot_text = ""
    if examples:
        parts = []
        for ex in examples[:3]:
            boxes = ex.get("boxes", [])
            # Strip labels if empty, just keep coords
            clean = [{"x1":b["x1"],"y1":b["y1"],"x2":b["x2"],"y2":b["y2"]} for b in boxes]
            parts.append(
                f"Page {ex.get('page_number','?')} example — {len(clean)} products:\n"
                + json.dumps(clean, ensure_ascii=False)
            )
        if parts:
            fewshot_text = (
                "\n\nHere are examples of CORRECT bounding boxes from the same store "
                "(normalized 0.0-1.0). Learn the layout pattern:\n"
                + "\n---\n".join(parts)
                + "\n\nNow detect ALL products in the new image using the same style.\n"
            )

    prompt = """You are a precise product detector for retail catalogues.

Find EVERY individual product on this page.
Each box must cover: the product photo AND its price tag together.

Return ONLY a JSON array, no markdown. Each item:
{"label": "short name or empty string", "x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0}

Rules:
- Coordinates are fractions 0.0 to 1.0 (top-left origin)
- x1,y1 = top-left of product box
- x2,y2 = bottom-right of product box  
- Boxes should NOT overlap
- Detect as many products as possible — do not miss any
- Return [] only if the page has no products at all
""" + fewshot_text

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
            logger.info(f"Gemini bbox response (first 200): {text[:200]}")

            # Strip markdown fences
            text = re.sub(r"```json|```", "", text).strip()

            match = re.search(r"\[.*?\]", text, re.DOTALL)
            if not match:
                logger.warning(f"No JSON array found: {text[:200]}")
                continue

            parsed = json.loads(match.group())
            if not isinstance(parsed, list):
                continue

            boxes = []
            for item in parsed:
                try:
                    x1 = float(item.get("x1", 0))
                    y1 = float(item.get("y1", 0))
                    x2 = float(item.get("x2", 1))
                    y2 = float(item.get("y2", 1))
                    if not (0 <= x1 < x2 <= 1.0 and 0 <= y1 < y2 <= 1.0):
                        logger.warning(f"Invalid bbox skipped: {item}")
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
                logger.warning("All boxes invalid or empty list")

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


# ── FEW-SHOT EXAMPLES ────────────────────────────────────────────────────────

def get_fewshot_examples(store, limit=3):
    """Fetch saved annotation examples for this store from Supabase."""
    try:
        rows = _sb_get("/rest/v1/annotations", {
            "store":  f"eq.{store}",
            "order":  "created_at.desc",
            "limit":  limit,
            "select": "page_image_url,boxes,page_number",
        }) or []
        return rows
    except Exception as e:
        logger.error(f"get_fewshot_examples failed: {e}")
        return []

def save_annotation(store, catalogue_name, page_number, page_image_url, boxes):
    """Save human-drawn annotation boxes to Supabase."""
    try:
        r = requests.post(
            f"{Config.SUPABASE_URL}/rest/v1/annotations",
            headers={
                "apikey":        Config.SUPABASE_KEY,
                "Authorization": f"Bearer {Config.SUPABASE_KEY}",
                "Content-Type":  "application/json",
                "Prefer":        "return=minimal",
            },
            json={
                "store":           store,
                "catalogue_name":  catalogue_name,
                "page_number":     page_number,
                "page_image_url":  page_image_url,
                "boxes":           boxes,
            },
            timeout=10, verify=False
        )
        r.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"save_annotation failed: {e}")
        return False

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

        # Get distinct pages (deduplicated by page_number)
        page_map = {}
        for p in products:
            pn = p.get("page_number")
            if pn not in page_map and p.get("page_image_url"):
                page_map[pn] = {
                    "url":      p["page_image_url"],
                    "products": []  # for DB linking
                }
        # Re-pass all products into page_map for DB linking
        for p in products:
            pn = p.get("page_number")
            if pn in page_map:
                page_map[pn]["products"].append(p)

        total_pages = len(page_map)
        crop_jobs[job_id]["total"] = total_pages  # count by pages not products
        done = 0

        safe_store = store.lower().replace(" ", "_")
        safe_cat   = catalogue_name.lower().replace(" ", "_")

        # Fetch few-shot examples once per job
        examples = get_fewshot_examples(store)
        if examples:
            logger.info(f"Using {len(examples)} few-shot examples for {store}")

        for page_num, page_data in sorted(page_map.items()):
            page_url      = page_data["url"]
            page_products = page_data["products"]

            if not page_url:
                done += 1
                crop_jobs[job_id]["done"] = done
                continue

            try:
                logger.info(f"Cropping page {page_num}/{total_pages}")
                img_bytes = download_image(page_url)

                if PIL_AVAILABLE:
                    img_obj = Image.open(io.BytesIO(img_bytes))
                    img_w, img_h = img_obj.size
                else:
                    img_w, img_h = 1240, 1754

                img_b64 = base64.b64encode(img_bytes).decode()
                boxes = detect_products_bbox(img_b64, img_w, img_h, examples=examples)

                if not boxes:
                    logger.warning(f"No boxes on page {page_num}")
                    done += 1
                    crop_jobs[job_id]["done"] = done
                    continue

                logger.info(f"Page {page_num}: {len(boxes)} boxes detected")

                # Build a pool of unmatched products on this page for DB linking
                unmatched = [p for p in page_products if not p.get("product_image_url")]

                for box_idx, box in enumerate(boxes):
                    x1 = int(box.get("x1", 0))
                    y1 = int(box.get("y1", 0))
                    x2 = int(box.get("x2", img_w))
                    y2 = int(box.get("y2", img_h))

                    if x2 <= x1 or y2 <= y1 or (x2-x1) < 15 or (y2-y1) < 15:
                        continue

                    try:
                        cropped = crop_product(img_bytes, x1, y1, x2, y2)

                        # Use a product ID for storage path:
                        # Match by box index if available, otherwise generate UUID
                        if box_idx < len(unmatched):
                            prod = unmatched[box_idx]
                            prod_id = prod.get("id") or str(uuid.uuid4())
                        else:
                            prod = None
                            prod_id = str(uuid.uuid4())

                        storage_path    = f"product-images/{safe_store}/{safe_cat}/{prod_id}.jpg"
                        product_img_url = _sb_storage_put(storage_path, cropped)

                        # Update DB record if we have a matching product
                        if prod:
                            _sb_patch(
                                f"/rest/v1/products?id=eq.{prod['id']}",
                                {"product_image_url": product_img_url}
                            )
                            logger.info(f"  Box {box_idx+1}: {prod.get('product','?')} → saved")
                        else:
                            logger.info(f"  Box {box_idx+1}: no DB match, saved to storage only")

                        crop_jobs[job_id]["cropped"] = crop_jobs[job_id].get("cropped",0) + 1

                    except Exception as e:
                        logger.error(f"  Box {box_idx+1} crop failed: {e}")
                        crop_jobs[job_id]["errors"] += 1

                done += 1
                crop_jobs[job_id]["done"] = done

            except Exception as e:
                logger.error(f"Page {page_num} failed: {e}")
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

@app.route("/annotate")
def annotate_page():
    return make_response(ANNOTATE_HTML, 200, {"Content-Type": "text/html"})

@app.route("/api/pages")
def get_pages():
    """Get distinct pages for annotation — one page per catalogue."""
    password = request.args.get("password","")
    if password != Config.UPLOAD_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403
    store = request.args.get("store","")
    try:
        params = {
            "select": "store,catalogue_name,page_number,page_image_url",
            "order":  "store,catalogue_name,page_number",
            "limit":  2000,
        }
        if store:
            params["store"] = f"eq.{store}"
        rows = _sb_get("/rest/v1/products", params) or []
        # Deduplicate by store+catalogue+page
        seen = set()
        pages = []
        for r in rows:
            key = (r["store"], r["catalogue_name"], r["page_number"])
            if key not in seen and r.get("page_image_url"):
                seen.add(key)
                pages.append({
                    "store":          r["store"],
                    "catalogue_name": r["catalogue_name"],
                    "page_number":    r["page_number"],
                    "page_image_url": r["page_image_url"],
                })
        return jsonify(pages)
    except Exception as e:
        logger.error(f"get_pages failed: {e}")
        return jsonify([])

@app.route("/api/annotations", methods=["GET"])
def get_annotations():
    """Get existing annotations."""
    password = request.args.get("password","")
    if password != Config.UPLOAD_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403
    store = request.args.get("store","")
    try:
        params = {"order": "created_at.desc", "limit": 100}
        if store:
            params["store"] = f"eq.{store}"
        return jsonify(_sb_get("/rest/v1/annotations", params) or [])
    except Exception as e:
        return jsonify([])

@app.route("/api/annotations", methods=["POST"])
def post_annotation():
    """Save annotation boxes drawn by user."""
    data     = request.json or {}
    password = data.get("password","")
    if password != Config.UPLOAD_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403
    ok = save_annotation(
        store          = data.get("store",""),
        catalogue_name = data.get("catalogue_name",""),
        page_number    = data.get("page_number", 0),
        page_image_url = data.get("page_image_url",""),
        boxes          = data.get("boxes",[]),
    )
    return jsonify({"ok": ok})

@app.route("/api/annotations/<ann_id>", methods=["DELETE"])
def delete_annotation(ann_id):
    """Delete an annotation."""
    password = request.args.get("password","")
    if password != Config.UPLOAD_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403
    try:
        key = Config.SUPABASE_SERVICE_KEY or Config.SUPABASE_KEY
        r = requests.delete(
            f"{Config.SUPABASE_URL}/rest/v1/annotations?id=eq.{ann_id}",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=10, verify=False
        )
        return jsonify({"ok": r.ok})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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


ANNOTATE_HTML = '''<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>katalog.ai — Annotator</title>
  <style>
    *{margin:0;padding:0;box-sizing:border-box;font-family:monospace}
    body{background:#111;color:#eee;height:100vh;display:flex;flex-direction:column}
    .toolbar{background:#1a1a1a;border-bottom:1px solid #333;padding:10px 16px;display:flex;align-items:center;gap:10px;flex-shrink:0;flex-wrap:wrap}
    .toolbar h1{color:#ff9900;font-size:16px;margin-right:8px}
    input[type=password]{background:#222;border:1px solid #444;color:#eee;padding:6px 10px;border-radius:4px;font-family:monospace;font-size:13px;width:130px}
    select{background:#222;border:1px solid #444;color:#eee;padding:6px 10px;border-radius:4px;font-family:monospace;font-size:13px}
    button{background:#333;color:#eee;border:1px solid #555;padding:7px 14px;border-radius:4px;cursor:pointer;font-family:monospace;font-size:13px}
    button:hover{background:#444}
    button.primary{background:#ff9900;color:#000;border-color:#ff9900;font-weight:bold}
    button.primary:hover{background:#cc7700}
    button.danger{background:#5a1a1a;color:#ff6666;border-color:#ff3333}
    button.success{background:#1a4a1a;color:#00ff88;border-color:#00ff88}
    .sep{width:1px;height:28px;background:#444;flex-shrink:0}
    .main{flex:1;display:flex;overflow:hidden}
    /* Canvas area */
    .canvas-wrap{flex:1;overflow:auto;background:#000;position:relative;display:flex;align-items:flex-start;justify-content:center;padding:10px}
    #canvas{cursor:crosshair;display:block}
    /* Sidebar */
    .sidebar{width:280px;flex-shrink:0;background:#1a1a1a;border-left:1px solid #333;display:flex;flex-direction:column;overflow:hidden}
    .sidebar-head{padding:12px;border-bottom:1px solid #333;font-size:12px;color:#ff9900;font-weight:bold;display:flex;justify-content:space-between;align-items:center}
    .box-list{flex:1;overflow-y:auto;padding:8px}
    .box-item{background:#222;border:1px solid #333;border-radius:6px;padding:8px 10px;margin-bottom:6px;cursor:pointer;transition:background .15s}
    .box-item:hover{background:#2a2a2a}
    .box-item.selected{border-color:#ff9900;background:#2a1a00}
    .box-item .lbl{font-size:12px;color:#eee;margin-bottom:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .box-item .coords{font-size:10px;color:#666}
    .box-item .del{float:right;background:none;border:none;color:#ff5555;cursor:pointer;font-size:14px;padding:0;line-height:1}
    /* Status bar */
    .statusbar{background:#111;border-top:1px solid #333;padding:6px 14px;font-size:11px;color:#666;flex-shrink:0;display:flex;gap:16px}
    .statusbar span{color:#aaa}
    /* Page nav */
    .page-nav{display:flex;align-items:center;gap:6px}
    #page-info{font-size:12px;color:#aaa;min-width:80px;text-align:center}
    /* Toast */
    #toast{position:fixed;bottom:40px;left:50%;transform:translateX(-50%);background:#00ff88;color:#000;padding:10px 24px;border-radius:8px;font-weight:bold;font-size:14px;display:none;z-index:999}
  </style>
</head>
<body>

<div class="toolbar">
  <h1>✏️ Annotator</h1>
  <input type="password" id="pw" placeholder="Password" onchange="init()">
  <select id="store-sel" onchange="loadPages()"><option value="">-- store --</option></select>
  <div class="sep"></div>
  <div class="page-nav">
    <button onclick="prevPage()">◀</button>
    <span id="page-info">–</span>
    <button onclick="nextPage()">▶</button>
  </div>
  <div class="sep"></div>
  <button onclick="clearBoxes()">🗑 Clear all</button>
  <button class="primary" onclick="saveAnnotation()">💾 Save annotation</button>
  <div class="sep"></div>
  <a href="/" style="color:#aaa;font-size:12px;text-decoration:none">← Cropper</a>
  <span id="saved-count" style="font-size:11px;color:#666;margin-left:auto"></span>
</div>

<div class="main">
  <div class="canvas-wrap" id="canvas-wrap">
    <canvas id="canvas"></canvas>
  </div>
  <div class="sidebar">
    <div class="sidebar-head">
      <span>BOXES (<span id="box-count">0</span>)</span>
      <button style="font-size:11px;padding:3px 8px" onclick="clearBoxes()">Clear</button>
    </div>
    <div class="box-list" id="box-list"></div>
  </div>
</div>

<div class="statusbar">
  <div>Store: <span id="st-store">–</span></div>
  <div>Catalogue: <span id="st-cat">–</span></div>
  <div>Page: <span id="st-page">–</span></div>
  <div id="st-mode" style="margin-left:auto;color:#ff9900">Draw mode — drag to create box</div>
</div>

<div id="toast"></div>

<script>
// ── STATE ──
let pw = '', pages = [], pageIdx = 0;
let boxes = [], selectedBox = -1;
let img = null, imgNaturalW = 0, imgNaturalH = 0;
let scale = 1;
let drawing = false, startX = 0, startY = 0, curX = 0, curY = 0;
let dragging = false, dragBox = -1, dragOffX = 0, dragOffY = 0;
let resizing = false, resizeBox = -1, resizeHandle = '';
const HANDLE_SIZE = 8;
const COLORS = ['#ff9900','#00ff88','#ff5555','#66ccff','#ff66ff','#ffff00'];

const canvas = document.getElementById('canvas');
const ctx = canvas.getContext('2d');

// ── INIT ──
async function init() {
  pw = document.getElementById('pw').value;
  if(!pw) return;
  // Load stores from pages endpoint
  try {
    const res = await fetch(`/api/pages?password=${encodeURIComponent(pw)}&store=`);
    const data = await res.json();
    if(data.error){alert('Wrong password');return;}
    const stores = [...new Set(data.map(p=>p.store))].sort();
    const sel = document.getElementById('store-sel');
    sel.innerHTML = '<option value="">-- store --</option>' +
      stores.map(s=>`<option value="${s}">${s.toUpperCase()}</option>`).join('');
  } catch(e){alert('Failed: '+e.message);}
}

async function loadPages() {
  const store = document.getElementById('store-sel').value;
  if(!store || !pw) return;
  try {
    const res = await fetch(`/api/pages?password=${encodeURIComponent(pw)}&store=${store}`);
    pages = await res.json();
    pageIdx = 0;
    // Load existing annotation count
    const annRes = await fetch(`/api/annotations?password=${encodeURIComponent(pw)}&store=${store}`);
    const anns = await annRes.json();
    document.getElementById('saved-count').textContent = `${anns.length} annotations saved for ${store}`;
    showPage();
  } catch(e){alert('Failed: '+e.message);}
}

function showPage() {
  if(!pages.length) return;
  const p = pages[pageIdx];
  boxes = [];
  selectedBox = -1;
  document.getElementById('page-info').textContent = `${pageIdx+1} / ${pages.length}`;
  document.getElementById('st-store').textContent = p.store;
  document.getElementById('st-cat').textContent = p.catalogue_name;
  document.getElementById('st-page').textContent = `Page ${p.page_number}`;
  renderBoxList();

  img = new Image();
  img.crossOrigin = 'anonymous';
  img.onload = () => {
    imgNaturalW = img.naturalWidth;
    imgNaturalH = img.naturalHeight;
    // Scale to fit in view
    const wrap = document.getElementById('canvas-wrap');
    const maxW = wrap.clientWidth - 20;
    const maxH = wrap.clientHeight - 20;
    scale = Math.min(maxW/imgNaturalW, maxH/imgNaturalH, 1);
    canvas.width  = Math.round(imgNaturalW * scale);
    canvas.height = Math.round(imgNaturalH * scale);
    render();
  };
  img.src = p.page_image_url;
}

function prevPage() { if(pageIdx>0){pageIdx--;showPage();} }
function nextPage() { if(pageIdx<pages.length-1){pageIdx++;showPage();} }

// ── RENDER ──
function render() {
  ctx.clearRect(0,0,canvas.width,canvas.height);
  if(img) ctx.drawImage(img, 0, 0, canvas.width, canvas.height);

  // Draw boxes
  boxes.forEach((b,i) => {
    const color = COLORS[i % COLORS.length];
    const x = b.x1*scale, y = b.y1*scale;
    const w = (b.x2-b.x1)*scale, h = (b.y2-b.y1)*scale;

    // Fill
    ctx.fillStyle = color + '22';
    ctx.fillRect(x,y,w,h);
    // Border
    ctx.strokeStyle = i===selectedBox ? '#fff' : color;
    ctx.lineWidth   = i===selectedBox ? 2.5 : 1.5;
    ctx.strokeRect(x,y,w,h);

    // Label background
    ctx.fillStyle = color;
    ctx.font = 'bold 11px monospace';
    const lw = ctx.measureText(b.label).width + 8;
    ctx.fillRect(x, y-16, lw, 16);
    ctx.fillStyle = '#000';
    ctx.fillText(b.label, x+4, y-4);

    // Resize handles for selected box
    if(i===selectedBox) {
      const handles = getHandles(b);
      Object.values(handles).forEach(h => {
        ctx.fillStyle = '#fff';
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.5;
        ctx.fillRect(h.x-HANDLE_SIZE/2, h.y-HANDLE_SIZE/2, HANDLE_SIZE, HANDLE_SIZE);
        ctx.strokeRect(h.x-HANDLE_SIZE/2, h.y-HANDLE_SIZE/2, HANDLE_SIZE, HANDLE_SIZE);
      });
    }
  });

  // Live drawing rect
  if(drawing) {
    const x = Math.min(startX,curX), y = Math.min(startY,curY);
    const w = Math.abs(curX-startX), h = Math.abs(curY-startY);
    ctx.strokeStyle = '#ff9900';
    ctx.lineWidth = 2;
    ctx.setLineDash([6,3]);
    ctx.strokeRect(x,y,w,h);
    ctx.fillStyle = 'rgba(255,153,0,0.1)';
    ctx.fillRect(x,y,w,h);
    ctx.setLineDash([]);
  }
}

function getHandles(b) {
  return {
    tl:{x:b.x1*scale, y:b.y1*scale},
    tr:{x:b.x2*scale, y:b.y1*scale},
    bl:{x:b.x1*scale, y:b.y2*scale},
    br:{x:b.x2*scale, y:b.y2*scale},
    tm:{x:(b.x1+b.x2)/2*scale, y:b.y1*scale},
    bm:{x:(b.x1+b.x2)/2*scale, y:b.y2*scale},
    ml:{x:b.x1*scale, y:(b.y1+b.y2)/2*scale},
    mr:{x:b.x2*scale, y:(b.y1+b.y2)/2*scale},
  };
}

function hitHandle(b, mx, my) {
  const handles = getHandles(b);
  for(const [name, h] of Object.entries(handles)) {
    if(Math.abs(mx-h.x)<=HANDLE_SIZE && Math.abs(my-h.y)<=HANDLE_SIZE) return name;
  }
  return null;
}

function hitBox(mx,my) {
  for(let i=boxes.length-1;i>=0;i--) {
    const b=boxes[i];
    if(mx>=b.x1*scale && mx<=b.x2*scale && my>=b.y1*scale && my<=b.y2*scale) return i;
  }
  return -1;
}

// ── MOUSE EVENTS ──
function canvasXY(e) {
  const rect = canvas.getBoundingClientRect();
  return {x: e.clientX - rect.left, y: e.clientY - rect.top};
}

canvas.addEventListener('mousedown', e => {
  const {x,y} = canvasXY(e);

  // Check resize handles first
  if(selectedBox>=0) {
    const h = hitHandle(boxes[selectedBox], x, y);
    if(h) {
      resizing=true; resizeBox=selectedBox; resizeHandle=h;
      return;
    }
  }

  // Check hit on existing box
  const hit = hitBox(x,y);
  if(hit>=0) {
    selectedBox=hit;
    dragging=true; dragBox=hit;
    dragOffX = x - boxes[hit].x1*scale;
    dragOffY = y - boxes[hit].y1*scale;
    renderBoxList();
    render();
    return;
  }

  // Start drawing new box
  selectedBox=-1;
  drawing=true;
  startX=x; startY=y; curX=x; curY=y;
  render();
});

canvas.addEventListener('mousemove', e => {
  const {x,y} = canvasXY(e);

  if(resizing) {
    const b = boxes[resizeBox];
    const px = x/scale, py = y/scale;
    if(resizeHandle.includes('l')) b.x1=Math.min(px, b.x2-10/scale);
    if(resizeHandle.includes('r')) b.x2=Math.max(px, b.x1+10/scale);
    if(resizeHandle.includes('t')) b.y1=Math.min(py, b.y2-10/scale);
    if(resizeHandle.includes('b')) b.y2=Math.max(py, b.y1+10/scale);
    render(); return;
  }

  if(dragging) {
    const b = boxes[dragBox];
    const w = b.x2-b.x1, h = b.y2-b.y1;
    b.x1 = Math.max(0, (x-dragOffX)/scale);
    b.y1 = Math.max(0, (y-dragOffY)/scale);
    b.x2 = b.x1+w; b.y2 = b.y1+h;
    render(); return;
  }

  if(drawing) { curX=x; curY=y; render(); return; }

  // Cursor style
  if(selectedBox>=0 && hitHandle(boxes[selectedBox],x,y)) {
    canvas.style.cursor='nwse-resize';
  } else if(hitBox(x,y)>=0) {
    canvas.style.cursor='move';
  } else {
    canvas.style.cursor='crosshair';
  }
});

canvas.addEventListener('mouseup', e => {
  if(resizing){ resizing=false; renderBoxList(); render(); return; }
  if(dragging){ dragging=false; renderBoxList(); render(); return; }
  if(!drawing) return;
  drawing=false;
  const {x,y} = canvasXY(e);
  const x1=Math.min(startX,x)/scale, y1=Math.min(startY,y)/scale;
  const x2=Math.max(startX,x)/scale, y2=Math.max(startY,y)/scale;
  if((x2-x1)<8 || (y2-y1)<8){ render(); return; }
  const label = prompt('Product label (e.g. "Gloria kava 500g"):', `product_${boxes.length+1}`);
  if(label===null){ render(); return; }
  boxes.push({x1,y1,x2,y2, label: label||`product_${boxes.length+1}`});
  selectedBox=boxes.length-1;
  renderBoxList();
  render();
});

canvas.addEventListener('dblclick', e => {
  const {x,y} = canvasXY(e);
  const hit=hitBox(x,y);
  if(hit<0) return;
  const newLabel=prompt('Rename box:', boxes[hit].label);
  if(newLabel!==null) { boxes[hit].label=newLabel; renderBoxList(); render(); }
});

document.addEventListener('keydown', e => {
  if(e.key==='Delete'||e.key==='Backspace') {
    if(selectedBox>=0 && document.activeElement===document.body) {
      boxes.splice(selectedBox,1);
      selectedBox=Math.min(selectedBox, boxes.length-1);
      renderBoxList(); render();
    }
  }
});

// ── SIDEBAR ──
function renderBoxList() {
  const list=document.getElementById('box-list');
  document.getElementById('box-count').textContent=boxes.length;
  if(!boxes.length){list.innerHTML='<div style="color:#555;font-size:12px;padding:8px">No boxes yet.<br>Drag on the image to draw.</div>';return;}
  list.innerHTML=boxes.map((b,i)=>`
    <div class="box-item${i===selectedBox?' selected':''}" onclick="selectBox(${i})">
      <span class="del" onclick="event.stopPropagation();deleteBox(${i})">×</span>
      <div class="lbl">${i+1}. ${b.label}</div>
      <div class="coords">${Math.round(b.x1/imgNaturalW*100)}%,${Math.round(b.y1/imgNaturalH*100)}% → ${Math.round(b.x2/imgNaturalW*100)}%,${Math.round(b.y2/imgNaturalH*100)}%</div>
    </div>`).join('');
}

function selectBox(i){ selectedBox=i; renderBoxList(); render(); }
function deleteBox(i){ boxes.splice(i,1); selectedBox=Math.min(selectedBox,boxes.length-1); renderBoxList(); render(); }
function clearBoxes(){ boxes=[]; selectedBox=-1; renderBoxList(); render(); }

// ── SAVE ──
async function saveAnnotation() {
  if(!boxes.length){toast('No boxes to save!','error');return;}
  const p=pages[pageIdx];
  // Normalize boxes to 0.0-1.0
  const normalized=boxes.map(b=>({
    label: b.label,
    x1: parseFloat((b.x1/imgNaturalW).toFixed(4)),
    y1: parseFloat((b.y1/imgNaturalH).toFixed(4)),
    x2: parseFloat((b.x2/imgNaturalW).toFixed(4)),
    y2: parseFloat((b.y2/imgNaturalH).toFixed(4)),
  }));
  try {
    const res=await fetch('/api/annotations',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        password: pw,
        store: p.store,
        catalogue_name: p.catalogue_name,
        page_number: p.page_number,
        page_image_url: p.page_image_url,
        boxes: normalized,
      })
    });
    const data=await res.json();
    if(data.ok){
      toast(`✅ Saved ${boxes.length} boxes for page ${p.page_number}!`);
      // Load count
      const annRes=await fetch(`/api/annotations?password=${encodeURIComponent(pw)}&store=${p.store}`);
      const anns=await annRes.json();
      document.getElementById('saved-count').textContent=`${anns.length} annotations saved for ${p.store}`;
    } else {
      toast('Save failed','error');
    }
  } catch(e){toast('Error: '+e.message,'error');}
}

function toast(msg, type='success') {
  const el=document.getElementById('toast');
  el.textContent=msg;
  el.style.background=type==='error'?'#ff5555':'#00ff88';
  el.style.color=type==='error'?'#fff':'#000';
  el.style.display='block';
  setTimeout(()=>el.style.display='none',2500);
}
</script>
</body>
</html>'''


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5002))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
