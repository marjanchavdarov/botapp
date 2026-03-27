"""
katalog.ai — Cropper Tool (standalone) - FIXED VERSION
Key fixes:
1. Gemini prompt now requests boxes in strict reading order (left→right, top→bottom)
2. Box matching uses spatial row-band sorting instead of raw index
3. Better logging for debugging mismatches
4. Improved padding and size validation
"""

import os, json, uuid, base64, logging, threading, time, re, io
from datetime import datetime
from collections import defaultdict
from typing import List, Dict, Any, Tuple, Optional
import requests
from flask import Flask, request, jsonify, make_response
from flask_cors import CORS

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    logging.warning("Pillow not installed — install with: pip install pillow")

try:
    import numpy as np
    from scipy import stats
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False
    logging.warning("NumPy/SciPy not available - install for better post-processing: pip install numpy scipy")

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
    
    MIN_PRODUCT_SIZE     = 80   # minimum pixels (relaxed slightly)
    MAX_PRODUCT_SIZE     = 2000
    CONFIDENCE_THRESHOLD = 0.6
    MAX_BOXES_PER_PAGE   = 50
    PADDING_PERCENT      = 0.03  # 3% padding

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
    except requests.exceptions.RequestException as e:
        logger.error(f"Supabase GET failed: {e}")
        if hasattr(e, 'response') and e.response:
            logger.error(f"Response: {e.response.text[:500]}")
        return []

def _sb_patch(path, data):
    try:
        r = requests.patch(f"{Config.SUPABASE_URL}{path}", headers=_headers(),
                           json=data, timeout=20)
        r.raise_for_status()
        return r
    except requests.exceptions.RequestException as e:
        logger.error(f"Supabase PATCH failed: {e}")
        raise

def _sb_storage_put(path, img_bytes, content_type="image/jpeg"):
    key = Config.SUPABASE_SERVICE_KEY or Config.SUPABASE_KEY
    url = f"{Config.SUPABASE_URL}/storage/v1/object/{Config.STORAGE_BUCKET}/{path}"
    try:
        r = requests.put(url, headers={
            "apikey": key, "Authorization": f"Bearer {key}",
            "Content-Type": content_type, "x-upsert": "true",
        }, data=img_bytes, timeout=30)
        if not r.ok:
            raise Exception(f"Storage {r.status_code}: {r.text[:300]}")
        return f"{Config.SUPABASE_URL}/storage/v1/object/public/{Config.STORAGE_BUCKET}/{path}"
    except requests.exceptions.RequestException as e:
        logger.error(f"Storage upload failed: {e}")
        raise

# ── IMAGE DOWNLOAD ────────────────────────────────────────────────────────────
def download_image(url):
    try:
        logger.info(f"Downloading image from: {url[:100]}...")
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        logger.info(f"Downloaded {len(r.content)} bytes")
        return r.content
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to download image: {e}")
        raise

# ── GEMINI DETECTION ──────────────────────────────────────────────────────────
_GEMINI_URL = (
    "https://generativelanguage.googleapis.com"
    "/v1beta/models/gemini-2.5-flash:generateContent"
)

def get_fewshot_examples(store, limit=5):
    try:
        logger.info(f"Fetching few-shot examples for store: {store}")
        rows = _sb_get("/rest/v1/annotations", {
            "store":  f"eq.{store}",
            "order":  "created_at.desc",
            "limit":  limit,
            "select": "page_image_url,boxes,page_number,layout_type",
        })
        logger.info(f"Found {len(rows)} examples for store: {store}")
        return rows
    except Exception as e:
        logger.error(f"get_fewshot_examples failed: {e}")
        return []

def build_detection_prompt(store, examples):
    """
    FIX: Prompt now explicitly requests reading order and explains
    why order matters (so matching works correctly).
    """
    base_prompt = """You are a precise product detector for retail catalogues.

Find EVERY individual product on this page.
Each box must tightly cover: the product photo AND its price tag together.

⚠️ CRITICAL - RETURN BOXES IN STRICT READING ORDER:
- Scan left-to-right, top-to-bottom (like reading a book)
- Complete each row fully before moving to the next row
- Example for a 3-column grid: row1-col1, row1-col2, row1-col3, row2-col1, ...
- This order is essential for correct product matching

Return ONLY a JSON array, no markdown, no explanation. Each item:
{"x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0}

Rules:
- Coordinates are fractions 0.0 to 1.0 (top-left is 0,0 — bottom-right is 1,1)
- x1,y1 = top-left corner of box, x2,y2 = bottom-right corner
- Boxes must NOT overlap significantly
- Detect ALL products — do not skip any
- Each box = exactly ONE product (do not merge two products into one box)
- Boxes should be tight — avoid excessive whitespace
- Ignore page headers, footers, logos, decorations, and navigation elements
- If a page has a 3x4 grid of products → return exactly 12 boxes
"""

    if examples:
        example_text = "\n\nHere are verified examples of correct boxes from this same store:\n"
        for i, ex in enumerate(examples[:2]):
            boxes = ex.get("boxes", [])
            if boxes:
                simple_boxes = [
                    {"x1": round(b["x1"], 3), "y1": round(b["y1"], 3),
                     "x2": round(b["x2"], 3), "y2": round(b["y2"], 3)}
                    for b in boxes[:10]
                ]
                example_text += f"\nExample {i+1} ({len(boxes)} products, already in reading order):\n"
                example_text += json.dumps(simple_boxes) + "\n"
        base_prompt += example_text
        base_prompt += "\nNow detect all products in the NEW image, in the same reading-order style.\n"

    return base_prompt

def parse_gemini_response(text, img_width, img_height):
    try:
        text = re.sub(r"```json|```", "", text).strip()
        match = re.search(r"\[.*?\]", text, re.DOTALL)
        if not match:
            logger.warning(f"No JSON array found in: {text[:200]}")
            return []

        parsed = json.loads(match.group())
        if not isinstance(parsed, list):
            return []

        boxes = []
        for item in parsed:
            try:
                if isinstance(item, dict):
                    x1 = float(item.get("x1", 0))
                    y1 = float(item.get("y1", 0))
                    x2 = float(item.get("x2", 1))
                    y2 = float(item.get("y2", 1))
                elif isinstance(item, list) and len(item) == 4:
                    x1, y1, x2, y2 = map(float, item)
                else:
                    continue

                # Validate fractions
                if not (0 <= x1 < x2 <= 1.0 and 0 <= y1 < y2 <= 1.0):
                    logger.warning(f"Invalid box coords: {x1},{y1},{x2},{y2} — skipping")
                    continue

                # Minimum size check (2% of image)
                if (x2 - x1) < 0.02 or (y2 - y1) < 0.02:
                    continue

                boxes.append({
                    "x1": int(x1 * img_width),
                    "y1": int(y1 * img_height),
                    "x2": int(x2 * img_width),
                    "y2": int(y2 * img_height),
                    # Store relative coords for sorting
                    "rx1": x1, "ry1": y1, "rx2": x2, "ry2": y2,
                    "confidence": 1.0
                })
            except Exception as e:
                logger.warning(f"Failed to parse box {item}: {e}")
                continue

        return boxes

    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {e}")
        return []

def sort_boxes_reading_order(boxes, row_band=0.08):
    """
    FIX: Sort boxes in reading order (left→right, top→bottom).
    Uses row bands to group boxes that are roughly on the same row,
    then sorts within each row by x position.
    row_band = fraction of image height to consider "same row"
    """
    if not boxes:
        return []

    # Use relative y1 if available, else absolute
    def get_ry1(b):
        return b.get("ry1", b["y1"])

    def get_rx1(b):
        return b.get("rx1", b["x1"])

    # Sort by y1 first
    sorted_by_y = sorted(boxes, key=get_ry1)

    rows = []
    current_row = [sorted_by_y[0]]
    row_start_y = get_ry1(sorted_by_y[0])

    for box in sorted_by_y[1:]:
        if get_ry1(box) - row_start_y < row_band:
            current_row.append(box)
        else:
            rows.append(sorted(current_row, key=get_rx1))
            current_row = [box]
            row_start_y = get_ry1(box)

    rows.append(sorted(current_row, key=get_rx1))

    result = []
    for row in rows:
        result.extend(row)

    return result

def post_process_boxes(boxes, img_width, img_height):
    if not boxes:
        return []

    # Remove duplicates via IoU
    unique_boxes = []
    for box in boxes:
        is_duplicate = False
        for existing in unique_boxes:
            x1 = max(box["x1"], existing["x1"])
            y1 = max(box["y1"], existing["y1"])
            x2 = min(box["x2"], existing["x2"])
            y2 = min(box["y2"], existing["y2"])

            if x2 > x1 and y2 > y1:
                intersection = (x2 - x1) * (y2 - y1)
                box_area = (box["x2"] - box["x1"]) * (box["y2"] - box["y1"])
                existing_area = (existing["x2"] - existing["x1"]) * (existing["y2"] - existing["y1"])
                union = box_area + existing_area - intersection
                iou = intersection / union if union > 0 else 0
                if iou > 0.7:
                    is_duplicate = True
                    break

        if not is_duplicate:
            unique_boxes.append(box)

    # Filter by size
    filtered = []
    for box in unique_boxes:
        width  = box["x2"] - box["x1"]
        height = box["y2"] - box["y1"]
        if (Config.MIN_PRODUCT_SIZE <= width  <= Config.MAX_PRODUCT_SIZE and
            Config.MIN_PRODUCT_SIZE <= height <= Config.MAX_PRODUCT_SIZE):
            filtered.append(box)
        else:
            logger.debug(f"Filtered out box (size {width}x{height})")

    # FIX: Sort in reading order AFTER dedup/filter
    filtered = sort_boxes_reading_order(filtered)

    return filtered[:Config.MAX_BOXES_PER_PAGE]

def detect_products_bbox(img_b64, img_width, img_height, examples=None, store=""):
    prompt = build_detection_prompt(store, examples)

    for attempt in range(3):
        try:
            logger.info(f"Calling Gemini API (attempt {attempt+1})")
            body = {
                "contents": [{"parts": [
                    {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}},
                    {"text": prompt},
                ]}],
                "generationConfig": {
                    "temperature": 0.1,
                    "maxOutputTokens": 8192,
                    "topP": 0.95,
                },
            }

            r = requests.post(f"{_GEMINI_URL}?key={Config.GEMINI_API_KEY}",
                              json=body, timeout=90)

            if r.status_code != 200:
                logger.error(f"Gemini {r.status_code}: {r.text[:300]}")
                time.sleep(2 ** attempt)
                continue

            result = r.json()
            if "candidates" not in result:
                logger.error("No candidates in Gemini response")
                continue

            text = result["candidates"][0]["content"]["parts"][0]["text"]
            logger.info(f"Gemini response length: {len(text)} chars")

            boxes = parse_gemini_response(text, img_width, img_height)

            if boxes:
                logger.info(f"Detected {len(boxes)} raw boxes")
                boxes = post_process_boxes(boxes, img_width, img_height)
                logger.info(f"After post-processing: {len(boxes)} boxes")
                return boxes
            else:
                logger.warning("No valid boxes found in response")

        except Exception as e:
            logger.error(f"Detection attempt {attempt+1} failed: {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)

    return []

# ── CROPPER ───────────────────────────────────────────────────────────────────
def crop_product(img_bytes, x1, y1, x2, y2, img_w, img_h):
    """
    FIX: Now receives img dimensions to compute padding correctly.
    Padding is based on box size, clamped to image bounds.
    """
    if not PIL_AVAILABLE:
        raise Exception("Pillow not installed")

    try:
        img = Image.open(io.BytesIO(img_bytes))
        w, h = img.size

        box_width  = x2 - x1
        box_height = y2 - y1

        # 3% padding relative to box size, minimum 5px
        pad_x = max(5, int(box_width  * Config.PADDING_PERCENT))
        pad_y = max(5, int(box_height * Config.PADDING_PERCENT))

        x1 = max(0, x1 - pad_x)
        y1 = max(0, y1 - pad_y)
        x2 = min(w, x2 + pad_x)
        y2 = min(h, y2 + pad_y)

        if (x2 - x1) < 20 or (y2 - y1) < 20:
            raise Exception(f"Crop too small after padding: {x2-x1}x{y2-y1}")

        cropped = img.crop((x1, y1, x2, y2))
        out = io.BytesIO()
        cropped.save(out, format="JPEG", quality=92)
        return out.getvalue()

    except Exception as e:
        logger.error(f"Crop failed: {e}")
        raise

# ── CROP JOB ──────────────────────────────────────────────────────────────────
crop_jobs = {}

def process_crop_job(job_id, catalogue_name, store):
    crop_jobs[job_id] = {
        "status": "running",
        "done": 0,
        "total": 0,
        "errors": 0,
        "detected": 0,
        "pages_processed": 0,
        "match_log": []  # FIX: log mismatches for debugging
    }

    try:
        logger.info(f"Starting crop job {job_id} for {store}/{catalogue_name}")

        params = {
            "catalogue_name": f"eq.{catalogue_name}",
            "store":          f"eq.{store}",
            "select":         "id,product,page_number,page_image_url,product_image_url",
            "order":          "page_number",
            "limit":          1000,
        }
        products = _sb_get("/rest/v1/products", params)

        if not products:
            logger.error(f"No products found for {store}/{catalogue_name}")
            crop_jobs[job_id]["status"] = "error"
            crop_jobs[job_id]["message"] = "No products found"
            return

        logger.info(f"Found {len(products)} products")

        # Group by page
        page_map = defaultdict(lambda: {"url": None, "products": []})
        for p in products:
            pn = p.get("page_number")
            if pn is not None:
                if p.get("page_image_url"):
                    page_map[pn]["url"] = p["page_image_url"]
                page_map[pn]["products"].append(p)

        total_pages = len(page_map)
        crop_jobs[job_id]["total"] = total_pages
        logger.info(f"Processing {total_pages} pages")

        done = 0
        safe_store = store.lower().replace(" ", "_")
        safe_cat   = catalogue_name.lower().replace(" ", "_")

        examples = get_fewshot_examples(store)
        if examples:
            logger.info(f"Using {len(examples)} few-shot examples")

        for page_num, page_data in sorted(page_map.items()):
            page_url      = page_data["url"]
            page_products = page_data["products"]

            crop_jobs[job_id]["pages_processed"] += 1
            logger.info(f"Processing page {page_num} ({crop_jobs[job_id]['pages_processed']}/{total_pages})")

            if not page_url:
                logger.warning(f"Page {page_num} has no image URL")
                done += 1
                crop_jobs[job_id]["done"] = done
                continue

            try:
                img_bytes = download_image(page_url)

                if PIL_AVAILABLE:
                    img_obj = Image.open(io.BytesIO(img_bytes))
                    img_w, img_h = img_obj.size
                    logger.info(f"Image dimensions: {img_w}x{img_h}")
                else:
                    img_w, img_h = 1240, 1754

                img_b64 = base64.b64encode(img_bytes).decode()
                boxes   = detect_products_bbox(img_b64, img_w, img_h, examples, store)

                if not boxes:
                    logger.warning(f"No products detected on page {page_num}")
                    done += 1
                    crop_jobs[job_id]["done"] = done
                    continue

                # ── FIX: reading-order matching ───────────────────────────
                # Products from DB don't have x/y positions, so we rely on
                # Gemini returning boxes in reading order + our sort.
                # Only match unprocessed products.
                unmatched = [p for p in page_products if not p.get("product_image_url")]

                # Log mismatch warnings
                if len(boxes) != len(unmatched):
                    msg = (f"Page {page_num}: {len(boxes)} boxes detected, "
                           f"{len(unmatched)} unmatched products in DB")
                    logger.warning(msg)
                    crop_jobs[job_id]["match_log"].append(msg)

                for idx, box in enumerate(boxes):
                    try:
                        cropped = crop_product(
                            img_bytes,
                            box["x1"], box["y1"], box["x2"], box["y2"],
                            img_w, img_h
                        )

                        if idx < len(unmatched):
                            prod         = unmatched[idx]
                            prod_id      = prod["id"]
                            product_name = prod.get("product", "unknown")
                        else:
                            prod         = None
                            prod_id      = str(uuid.uuid4())
                            product_name = f"extra_box_{idx}"

                        storage_path    = f"product-images/{safe_store}/{safe_cat}/{prod_id}.jpg"
                        product_img_url = _sb_storage_put(storage_path, cropped)

                        if prod:
                            _sb_patch(
                                f"/rest/v1/products?id=eq.{prod['id']}",
                                {"product_image_url": product_img_url}
                            )
                            logger.info(f"  Box {idx+1}/{len(boxes)}: ✅ {product_name}")
                            crop_jobs[job_id]["detected"] += 1
                        else:
                            logger.info(f"  Box {idx+1}/{len(boxes)}: no DB match, stored as extra")

                    except Exception as e:
                        logger.error(f"  Box {idx+1} crop failed: {e}")
                        crop_jobs[job_id]["errors"] += 1

                done += 1
                crop_jobs[job_id]["done"] = done

            except Exception as e:
                logger.error(f"Page {page_num} failed: {e}")
                crop_jobs[job_id]["errors"] += len(page_products)
                done += 1
                crop_jobs[job_id]["done"] = done

        crop_jobs[job_id]["status"] = "done"
        logger.info(
            f"Crop job {job_id} complete. "
            f"Pages: {done}/{total_pages}, "
            f"Detected: {crop_jobs[job_id]['detected']}, "
            f"Errors: {crop_jobs[job_id]['errors']}"
        )

    except Exception as e:
        logger.error(f"Crop job {job_id} crashed: {e}")
        crop_jobs[job_id]["status"] = "error"
        crop_jobs[job_id]["message"] = str(e)

# ── ROUTES ────────────────────────────────────────────────────────────────────
@app.route("/")
def home():
    return make_response(CROPPER_HTML, 200, {"Content-Type": "text/html"})

@app.route("/api/catalogues")
def get_catalogues():
    password = request.headers.get("X-Password") or request.args.get("password","")
    if password != Config.UPLOAD_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403
    try:
        rows = _sb_get("/rest/v1/products", {
            "select": "store,catalogue_name,page_number,product_image_url",
            "limit":  2000,
            "order":  "store,catalogue_name",
        })
        cats = {}
        for r in rows:
            key = f"{r['store']}|{r['catalogue_name']}"
            if key not in cats:
                cats[key] = {
                    "store":          r["store"],
                    "catalogue_name": r["catalogue_name"],
                    "total":          0,
                    "cropped":        0,
                    "pages":          set(),
                }
            cats[key]["total"] += 1
            cats[key]["pages"].add(r.get("page_number"))
            if r.get("product_image_url"):
                cats[key]["cropped"] += 1

        result = []
        for cat in cats.values():
            result.append({
                "store":          cat["store"],
                "catalogue_name": cat["catalogue_name"],
                "total":          cat["total"],
                "cropped":        cat["cropped"],
                "pages":          len(cat["pages"]),
                "progress":       round(cat["cropped"]/cat["total"]*100) if cat["total"] > 0 else 0
            })
        return jsonify(result)
    except Exception as e:
        logger.error(f"get_catalogues failed: {e}")
        return jsonify([])

@app.route("/api/crop", methods=["POST"])
def start_crop():
    data     = request.json or {}
    password = data.get("password","")
    if password != Config.UPLOAD_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403

    catalogue_name = data.get("catalogue_name","")
    store          = data.get("store","")
    if not catalogue_name or not store:
        return jsonify({"error": "catalogue_name and store required"}), 400

    job_id = str(uuid.uuid4())[:8]
    thread = threading.Thread(
        target=process_crop_job,
        args=(job_id, catalogue_name, store),
        daemon=True
    )
    thread.start()
    logger.info(f"Started crop job {job_id} for {store}/{catalogue_name}")
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
        "status":  "ok",
        "service": "katalog-cropper",
        "pillow":  PIL_AVAILABLE,
        "numpy":   NUMPY_AVAILABLE,
        "time":    datetime.now().isoformat()
    })

@app.route("/annotate")
def annotate_page():
    return make_response(ANNOTATE_HTML, 200, {"Content-Type": "text/html"})

@app.route("/validate")
def validator_home():
    return make_response(VALIDATOR_HTML, 200, {"Content-Type": "text/html"})

@app.route("/api/pages")
def get_pages():
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
        rows = _sb_get("/rest/v1/products", params)
        seen  = set()
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
    password = request.args.get("password","")
    if password != Config.UPLOAD_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403
    store = request.args.get("store","")
    try:
        params = {"order": "created_at.desc", "limit": 100}
        if store:
            params["store"] = f"eq.{store}"
        return jsonify(_sb_get("/rest/v1/annotations", params))
    except Exception as e:
        logger.error(f"get_annotations failed: {e}")
        return jsonify([])

@app.route("/api/annotations", methods=["POST"])
def post_annotation():
    data     = request.json or {}
    password = data.get("password","")
    if password != Config.UPLOAD_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403
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
                "store":          data.get("store",""),
                "catalogue_name": data.get("catalogue_name",""),
                "page_number":    data.get("page_number", 0),
                "page_image_url": data.get("page_image_url",""),
                "boxes":          data.get("boxes",[]),
            },
            timeout=10
        )
        r.raise_for_status()
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"save_annotation failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/annotations/<ann_id>", methods=["DELETE"])
def delete_annotation(ann_id):
    password = request.args.get("password","")
    if password != Config.UPLOAD_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403
    try:
        key = Config.SUPABASE_SERVICE_KEY or Config.SUPABASE_KEY
        r = requests.delete(
            f"{Config.SUPABASE_URL}/rest/v1/annotations?id=eq.{ann_id}",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=10
        )
        return jsonify({"ok": r.ok})
    except Exception as e:
        logger.error(f"delete_annotation failed: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/products/<product_id>")
def get_product(product_id):
    password = request.args.get("password","")
    if password != Config.UPLOAD_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403
    try:
        products = _sb_get("/rest/v1/products", {
            "id":     f"eq.{product_id}",
            "select": "id,store,catalogue_name,page_number,product,product_image_url,page_image_url"
        })
        if not products:
            return jsonify({"error": "Product not found"}), 404
        product = products[0]
        return jsonify({
            "id":               product["id"],
            "name":             product.get("product", "Unknown"),
            "store":            product["store"],
            "catalogue":        product["catalogue_name"],
            "page_number":      product["page_number"],
            "page_image_url":   product.get("page_image_url"),
            "product_image_url":product.get("product_image_url")
        })
    except Exception as e:
        logger.error(f"Failed to get product: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/feedback", methods=["POST"])
def submit_feedback():
    data     = request.json or {}
    password = data.get("password", "")
    if password != Config.UPLOAD_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403

    product_id = data.get("product_id")
    rating     = data.get("rating")
    notes      = data.get("notes", "")
    store      = data.get("store")
    catalogue  = data.get("catalogue")

    if not all([product_id, rating, store, catalogue]):
        return jsonify({"error": "Missing required fields"}), 400
    if rating not in ['good', 'bad', 'needs_fix']:
        return jsonify({"error": "Invalid rating"}), 400

    if not hasattr(app, 'feedback_store'):
        app.feedback_store = []

    app.feedback_store.append({
        "product_id":     product_id,
        "store":          store,
        "catalogue_name": catalogue,
        "rating":         rating,
        "notes":          notes,
        "created_at":     datetime.now().isoformat()
    })
    return jsonify({"ok": True, "message": "Feedback saved"})

@app.route("/api/feedback/stats")
def feedback_stats():
    password = request.args.get("password","")
    if password != Config.UPLOAD_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403

    store     = request.args.get("store")
    catalogue = request.args.get("catalogue")

    if not hasattr(app, 'feedback_store'):
        return jsonify({"total": 0, "good": 0, "bad": 0, "needs_fix": 0})

    feedback = app.feedback_store
    if store:
        feedback = [f for f in feedback if f['store'] == store]
    if catalogue:
        feedback = [f for f in feedback if f['catalogue_name'] == catalogue]

    return jsonify({
        "total":      len(feedback),
        "good":       len([f for f in feedback if f['rating'] == 'good']),
        "bad":        len([f for f in feedback if f['rating'] == 'bad']),
        "needs_fix":  len([f for f in feedback if f['rating'] == 'needs_fix'])
    })

# ── HTML UI ───────────────────────────────────────────────────────────────────
CROPPER_HTML = '''<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>katalog.ai — Cropper</title>
  <style>
    *{margin:0;padding:0;box-sizing:border-box;font-family:monospace}
    body{background:#111;color:#eee;padding:40px;max-width:1200px;margin:0 auto}
    h1{color:#ff9900;margin-bottom:8px;font-size:24px}
    .sub{color:#666;margin-bottom:30px;font-size:13px}
    .nav-links{display:flex;gap:15px;margin-bottom:20px}
    .nav-links a{color:#888;text-decoration:none;padding:5px 10px}
    .nav-links a:hover{color:#ff9900}
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
    .progress-bar{background:#222;height:24px;border-radius:4px;overflow:hidden;margin:8px 0}
    .progress-fill{background:linear-gradient(90deg,#ff9900,#ffcc00);height:100%;width:0%;transition:width .3s;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:bold;color:#000;font-family:monospace}
    #log{background:#000;padding:14px;border-radius:5px;font-size:12px;line-height:1.7;max-height:300px;overflow-y:auto;border:1px solid #222;margin-top:12px;display:none}
    .stats{display:flex;gap:16px;margin-top:8px;flex-wrap:wrap}
    .stat{background:#222;padding:8px 12px;border-radius:4px;border-left:3px solid #ff9900}
    .stat-label{color:#888;font-size:10px;text-transform:uppercase}
    .stat-value{color:#fff;font-size:18px;font-weight:bold}
    .warn-log{background:#2a1a00;border:1px solid #ff9900;border-radius:5px;padding:10px;font-size:11px;margin-top:8px;display:none;max-height:120px;overflow-y:auto}
  </style>
</head>
<body>
  <div class="nav-links">
    <a href="/" style="color:#ff9900">Cropper</a>
    <a href="/annotate">Annotator</a>
    <a href="/validate">Validator</a>
  </div>

  <h1>✂️ katalog.ai Cropper</h1>
  <div class="sub">Extract individual product images using AI — v2 (reading-order fix)</div>

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
      <thead><tr>
        <th>Store</th><th>Catalogue</th><th>Pages</th><th>Products</th><th>Progress</th><th>Action</th>
      </tr></thead>
      <tbody id="cat-table"></tbody>
    </table>
  </div>

  <div class="card" id="job-card" style="display:none">
    <div style="color:#ff9900;font-weight:bold;margin-bottom:8px" id="job-title">Cropping...</div>
    <div class="progress-bar"><div class="progress-fill" id="job-fill">0%</div></div>
    <div class="stats">
      <div class="stat"><span class="stat-label">Processed</span><span class="stat-value" id="stat-done">0</span></div>
      <div class="stat"><span class="stat-label">Total</span><span class="stat-value" id="stat-total">0</span></div>
      <div class="stat"><span class="stat-label">Detected</span><span class="stat-value" id="stat-detected">0</span></div>
      <div class="stat"><span class="stat-label">Errors</span><span class="stat-value" id="stat-errors">0</span></div>
    </div>
    <div id="warn-log" class="warn-log"></div>
    <div id="log"></div>
  </div>

  <script>
    let activeJobId = null;
    let pollTimer   = null;

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
          const badge = c.progress===100?'done':c.progress>0?'partial':'none';
          const label = c.progress===100?'✅ Complete':c.progress>0?`${c.progress}%`:'⚪ Not started';
          return `<tr>
            <td style="text-transform:uppercase;font-weight:bold">${c.store}</td>
            <td style="max-width:240px;overflow:hidden;text-overflow:ellipsis">${c.catalogue_name}</td>
            <td>${c.pages}</td>
            <td>${c.total}</td>
            <td><span class="badge badge-${badge}">${label}</span></td>
            <td><button class="btn-sm" onclick="startCrop('${c.store}','${c.catalogue_name.replace(/'/g,"\\'")}')">▶ Crop</button></td>
          </tr>`;
        }).join('');
        document.getElementById('cat-card').style.display = 'block';
      } catch(e) {
        alert('Failed to load: '+e.message);
      }
    }

    async function startCrop(store, catName) {
      const pw = document.getElementById('password').value;
      document.getElementById('job-card').style.display = 'block';
      document.getElementById('job-title').textContent = `✂️ Cropping: ${store.toUpperCase()} — ${catName}`;
      document.getElementById('log').innerHTML = '';
      document.getElementById('warn-log').innerHTML = '';
      document.getElementById('warn-log').style.display = 'none';
      ['job-fill','stat-done','stat-total','stat-detected','stat-errors'].forEach(id=>{
        const el=document.getElementById(id);
        if(id==='job-fill'){el.style.width='0%';el.textContent='0%';}
        else el.textContent='0';
      });
      log(`Starting crop job for ${store} / ${catName}`,'info');

      try {
        const res = await fetch('/api/crop', {
          method:'POST',
          headers:{'Content-Type':'application/json'},
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
        const d   = await res.json();
        const pct = d.total>0 ? Math.round(d.done/d.total*100) : 0;
        document.getElementById('job-fill').style.width = pct+'%';
        document.getElementById('job-fill').textContent = pct+'%';
        document.getElementById('stat-done').textContent     = d.done||0;
        document.getElementById('stat-total').textContent    = d.total||0;
        document.getElementById('stat-detected').textContent = d.detected||0;
        document.getElementById('stat-errors').textContent   = d.errors||0;

        // Show match warnings
        if(d.match_log && d.match_log.length) {
          const wl = document.getElementById('warn-log');
          wl.style.display = 'block';
          wl.innerHTML = '⚠️ Match warnings:<br>' + d.match_log.map(m=>`• ${m}`).join('<br>');
        }

        if(d.status==='done'){
          clearInterval(pollTimer); pollTimer=null;
          log(`🎉 Done! Detected: ${d.detected||0}, Errors: ${d.errors||0}`,'success');
          loadCatalogues();
        }
        if(d.status==='error'){
          clearInterval(pollTimer); pollTimer=null;
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
    .nav-links{display:flex;gap:10px;margin-left:auto}
    .nav-links a{color:#888;text-decoration:none;padding:5px 10px}
    .nav-links a:hover{color:#ff9900}
    input[type=password]{background:#222;border:1px solid #444;color:#eee;padding:6px 10px;border-radius:4px;font-family:monospace;font-size:13px;width:130px}
    select{background:#222;border:1px solid #444;color:#eee;padding:6px 10px;border-radius:4px;font-family:monospace;font-size:13px}
    button{background:#333;color:#eee;border:1px solid #555;padding:7px 14px;border-radius:4px;cursor:pointer;font-family:monospace;font-size:13px}
    button:hover{background:#444}
    button.primary{background:#ff9900;color:#000;border-color:#ff9900;font-weight:bold}
    .sep{width:1px;height:28px;background:#444;flex-shrink:0}
    .main{flex:1;display:flex;overflow:hidden}
    .canvas-wrap{flex:1;overflow:auto;background:#000;position:relative;display:flex;align-items:flex-start;justify-content:center;padding:10px}
    #canvas{cursor:crosshair;display:block}
    .sidebar{width:280px;flex-shrink:0;background:#1a1a1a;border-left:1px solid #333;display:flex;flex-direction:column;overflow:hidden}
    .sidebar-head{padding:12px;border-bottom:1px solid #333;font-size:12px;color:#ff9900;font-weight:bold;display:flex;justify-content:space-between;align-items:center}
    .box-list{flex:1;overflow-y:auto;padding:8px}
    .box-item{background:#222;border:1px solid #333;border-radius:6px;padding:8px 10px;margin-bottom:6px;cursor:pointer}
    .box-item:hover{background:#2a2a2a}
    .box-item.selected{border-color:#ff9900;background:#2a1a00}
    .box-item .lbl{font-size:12px;color:#eee;margin-bottom:3px}
    .box-item .coords{font-size:10px;color:#666}
    .box-item .del{float:right;background:none;border:none;color:#ff5555;cursor:pointer;font-size:14px;padding:0}
    .statusbar{background:#111;border-top:1px solid #333;padding:6px 14px;font-size:11px;color:#666;flex-shrink:0;display:flex;gap:16px}
    #toast{position:fixed;bottom:40px;left:50%;transform:translateX(-50%);background:#00ff88;color:#000;padding:10px 24px;border-radius:8px;font-weight:bold;font-size:14px;display:none;z-index:999}
  </style>
</head>
<body>
<div class="toolbar">
  <h1>✏️ Annotator</h1>
  <input type="password" id="pw" placeholder="Password" onchange="init()">
  <select id="store-sel" onchange="loadPages()"><option value="">-- store --</option></select>
  <div class="sep"></div>
  <div style="display:flex;gap:6px">
    <button onclick="prevPage()">◀</button>
    <span id="page-info">–</span>
    <button onclick="nextPage()">▶</button>
  </div>
  <div class="sep"></div>
  <button onclick="clearBoxes()">🗑 Clear</button>
  <button class="primary" onclick="saveAnnotation()">💾 Save</button>
  <div class="nav-links">
    <a href="/">Cropper</a>
    <a href="#" style="color:#ff9900">Annotator</a>
    <a href="/validate">Validator</a>
  </div>
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
  <div id="st-mode" style="margin-left:auto;color:#ff9900">Draw boxes in reading order (left→right, top→bottom)</div>
</div>
<div id="toast"></div>
<script>
let pw='',pages=[],pageIdx=0;
let boxes=[],selectedBox=-1;
let img=null,imgNaturalW=0,imgNaturalH=0,scale=1;
let drawing=false,startX=0,startY=0,curX=0,curY=0;
const canvas=document.getElementById('canvas');
const ctx=canvas.getContext('2d');

async function init(){
  pw=document.getElementById('pw').value;
  if(!pw)return;
  try{
    const res=await fetch(`/api/pages?password=${encodeURIComponent(pw)}&store=`);
    const data=await res.json();
    if(data.error){alert('Wrong password');return;}
    const stores=[...new Set(data.map(p=>p.store))].sort();
    const sel=document.getElementById('store-sel');
    sel.innerHTML='<option value="">-- store --</option>'+stores.map(s=>`<option value="${s}">${s.toUpperCase()}</option>`).join('');
  }catch(e){alert('Failed: '+e.message);}
}

async function loadPages(){
  const store=document.getElementById('store-sel').value;
  if(!store||!pw)return;
  try{
    const res=await fetch(`/api/pages?password=${encodeURIComponent(pw)}&store=${store}`);
    pages=await res.json();
    pageIdx=0;
    showPage();
  }catch(e){alert('Failed: '+e.message);}
}

function showPage(){
  if(!pages.length)return;
  const p=pages[pageIdx];
  boxes=[];selectedBox=-1;
  document.getElementById('page-info').textContent=`${pageIdx+1} / ${pages.length}`;
  document.getElementById('st-store').textContent=p.store;
  document.getElementById('st-cat').textContent=p.catalogue_name;
  document.getElementById('st-page').textContent=`Page ${p.page_number}`;
  renderBoxList();
  img=new Image();
  img.crossOrigin='anonymous';
  img.onload=()=>{
    imgNaturalW=img.naturalWidth;imgNaturalH=img.naturalHeight;
    const wrap=document.getElementById('canvas-wrap');
    const maxW=wrap.clientWidth-20,maxH=wrap.clientHeight-20;
    scale=Math.min(maxW/imgNaturalW,maxH/imgNaturalH,1);
    canvas.width=Math.round(imgNaturalW*scale);
    canvas.height=Math.round(imgNaturalH*scale);
    render();
  };
  img.src=p.page_image_url;
}

function prevPage(){if(pageIdx>0){pageIdx--;showPage();}}
function nextPage(){if(pageIdx<pages.length-1){pageIdx++;showPage();}}

function render(){
  ctx.clearRect(0,0,canvas.width,canvas.height);
  if(img)ctx.drawImage(img,0,0,canvas.width,canvas.height);
  boxes.forEach((b,i)=>{
    const x=b.x1*scale,y=b.y1*scale,w=(b.x2-b.x1)*scale,h=(b.y2-b.y1)*scale;
    ctx.strokeStyle=i===selectedBox?'#fff':'#ff9900';
    ctx.lineWidth=i===selectedBox?2.5:1.5;
    ctx.strokeRect(x,y,w,h);
    ctx.fillStyle='#ff9900';
    ctx.font='bold 11px monospace';
    const label=b.label||`box ${i+1}`;
    const lw=ctx.measureText(label).width+8;
    ctx.fillRect(x,y-16,lw,16);
    ctx.fillStyle='#000';
    ctx.fillText(label,x+4,y-4);
  });
  if(drawing){
    const x=Math.min(startX,curX),y=Math.min(startY,curY);
    const w=Math.abs(curX-startX),h=Math.abs(curY-startY);
    ctx.strokeStyle='#fff';ctx.lineWidth=2;
    ctx.setLineDash([6,3]);ctx.strokeRect(x,y,w,h);ctx.setLineDash([]);
  }
}

function canvasXY(e){const r=canvas.getBoundingClientRect();return{x:e.clientX-r.left,y:e.clientY-r.top};}
canvas.addEventListener('mousedown',e=>{const{x,y}=canvasXY(e);drawing=true;startX=x;startY=y;curX=x;curY=y;selectedBox=-1;renderBoxList();});
canvas.addEventListener('mousemove',e=>{if(!drawing)return;const{x,y}=canvasXY(e);curX=x;curY=y;render();});
canvas.addEventListener('mouseup',e=>{
  if(!drawing)return;drawing=false;
  const{x,y}=canvasXY(e);
  const x1=Math.min(startX,x)/scale,y1=Math.min(startY,y)/scale;
  const x2=Math.max(startX,x)/scale,y2=Math.max(startY,y)/scale;
  if((x2-x1)<20||(y2-y1)<20){render();return;}
  const label=prompt('Product label (in reading order):',`product_${boxes.length+1}`);
  if(label===null){render();return;}
  boxes.push({x1,y1,x2,y2,label:label||`product_${boxes.length+1}`});
  selectedBox=boxes.length-1;renderBoxList();render();
});

function renderBoxList(){
  const list=document.getElementById('box-list');
  document.getElementById('box-count').textContent=boxes.length;
  if(!boxes.length){list.innerHTML='<div style="color:#555;font-size:12px;padding:8px">No boxes yet.<br>Draw in reading order:<br>left→right, top→bottom.</div>';return;}
  list.innerHTML=boxes.map((b,i)=>`
    <div class="box-item${i===selectedBox?' selected':''}" onclick="selectBox(${i})">
      <span class="del" onclick="event.stopPropagation();deleteBox(${i})">×</span>
      <div class="lbl">${i+1}. ${b.label}</div>
      <div class="coords">${Math.round(b.x1)}×${Math.round(b.y1)} → ${Math.round(b.x2)}×${Math.round(b.y2)}</div>
    </div>`).join('');
}

function selectBox(i){selectedBox=i;renderBoxList();render();}
function deleteBox(i){boxes.splice(i,1);selectedBox=-1;renderBoxList();render();}
function clearBoxes(){boxes=[];selectedBox=-1;renderBoxList();render();}

async function saveAnnotation(){
  if(!boxes.length){toast('No boxes to save');return;}
  const p=pages[pageIdx];
  const normalized=boxes.map(b=>({
    label:b.label,
    x1:b.x1/imgNaturalW,y1:b.y1/imgNaturalH,
    x2:b.x2/imgNaturalW,y2:b.y2/imgNaturalH,
  }));
  try{
    const res=await fetch('/api/annotations',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({password:pw,store:p.store,catalogue_name:p.catalogue_name,
        page_number:p.page_number,page_image_url:p.page_image_url,boxes:normalized})
    });
    const data=await res.json();
    if(data.ok)toast(`✅ Saved ${boxes.length} boxes in reading order!`);
    else toast('Save failed','error');
  }catch(e){toast('Error: '+e.message,'error');}
}

function toast(msg,type='success'){
  const el=document.getElementById('toast');
  el.textContent=msg;el.style.background=type==='error'?'#ff5555':'#00ff88';
  el.style.display='block';setTimeout(()=>el.style.display='none',2000);
}
</script>
</body>
</html>'''

VALIDATOR_HTML = '''<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>katalog.ai — Validator</title>
  <style>
    *{margin:0;padding:0;box-sizing:border-box;font-family:monospace}
    body{background:#111;color:#eee;padding:20px;max-width:1400px;margin:0 auto}
    .header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}
    h1{color:#ff9900;font-size:24px}
    .sub{color:#666;font-size:13px}
    .nav-links{display:flex;gap:15px}
    .nav-links a{color:#888;text-decoration:none;padding:5px 10px}
    .nav-links a:hover,.nav-links a.active{color:#ff9900}
    .login-card{background:#1a1a1a;border-radius:10px;padding:20px;margin-bottom:20px;border:1px solid #333}
    input{background:#222;border:1px solid #444;color:#eee;padding:10px;border-radius:5px;width:200px;margin-right:10px}
    button{background:#ff9900;color:#000;border:none;padding:10px 20px;border-radius:5px;cursor:pointer;font-weight:bold}
    button:hover{background:#cc7700}
    .catalogue-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(350px,1fr));gap:20px;margin-top:20px}
    .catalogue-card{background:#1a1a1a;border-radius:10px;padding:20px;border:1px solid #333}
    .catalogue-card:hover{border-color:#ff9900}
    .store-badge{background:#333;padding:4px 8px;border-radius:4px;color:#ff9900;font-weight:bold}
    .stats{display:flex;gap:10px;margin:15px 0;flex-wrap:wrap}
    .stat{background:#222;padding:8px;border-radius:4px;flex:1;text-align:center}
    .stat-label{font-size:10px;color:#888;text-transform:uppercase}
    .stat-value{font-size:18px;font-weight:bold}
    .stat.good .stat-value{color:#00ff88}
    .stat.bad .stat-value{color:#ff5555}
    .stat.needs-fix .stat-value{color:#ffcc00}
    .view-btn{width:100%;padding:10px;background:#333;border:none;color:#eee;border-radius:5px;cursor:pointer;margin-top:10px}
    .view-btn:hover{background:#444}
    .review-container{display:none;background:#1a1a1a;border-radius:10px;padding:20px;margin-top:20px;border:1px solid #333}
    .comparison-area{display:flex;gap:20px;margin-bottom:20px;flex-wrap:wrap}
    .image-box{flex:1;min-width:300px}
    .image-box h3{color:#ff9900;margin-bottom:10px;font-size:14px}
    .image-box img{max-width:100%;border-radius:5px;border:1px solid #333}
    .product-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px;margin:20px 0;max-height:400px;overflow-y:auto;padding:10px;background:#222;border-radius:5px}
    .product-item{padding:10px;background:#1a1a1a;border-radius:5px;cursor:pointer;border:1px solid #333}
    .product-item:hover,.product-item.selected{border-color:#ff9900;background:#2a1a00}
    .rating-buttons{display:flex;gap:10px;margin:20px 0}
    .rating-btn{flex:1;padding:15px;border:none;border-radius:5px;cursor:pointer;font-weight:bold;font-size:16px}
    .rating-btn.good{background:#00ff88;color:#000}
    .rating-btn.bad{background:#ff5555;color:#fff}
    .rating-btn.fix{background:#ffcc00;color:#000}
    .rating-btn.selected{transform:scale(1.05)}
    .notes-area{width:100%;padding:10px;background:#222;border:1px solid #444;color:#eee;border-radius:5px;margin:10px 0;min-height:80px}
    .stats-summary{display:flex;gap:15px;margin-bottom:20px}
    .stat-card{background:#222;padding:15px;border-radius:5px;flex:1}
    .loading{text-align:center;padding:40px;color:#888}
    .spinner{border:3px solid #333;border-top-color:#ff9900;border-radius:50%;width:40px;height:40px;animation:spin 1s linear infinite;margin:20px auto}
    @keyframes spin{to{transform:rotate(360deg)}}
    #toast{position:fixed;bottom:40px;left:50%;transform:translateX(-50%);background:#00ff88;color:#000;padding:10px 24px;border-radius:8px;font-weight:bold;display:none;z-index:999}
  </style>
</head>
<body>
<div class="header">
  <div><h1>🔍 katalog.ai Validator</h1><div class="sub">Review cropped products</div></div>
  <div class="nav-links">
    <a href="/">Cropper</a><a href="/annotate">Annotator</a><a href="/validate" class="active">Validator</a>
  </div>
</div>
<div class="login-card" id="loginCard">
  <input type="password" id="password" placeholder="Password" onkeypress="if(event.key==='Enter')loadCatalogues()">
  <button onclick="loadCatalogues()">🔓 Login & Load</button>
</div>
<div class="stats-summary" id="statsSummary" style="display:none">
  <div class="stat-card"><div style="color:#888;font-size:12px">Total Reviews</div><div style="font-size:24px;font-weight:bold" id="totalReviews">0</div></div>
  <div class="stat-card" style="border-left:3px solid #00ff88"><div style="color:#888;font-size:12px">Good</div><div style="font-size:24px;font-weight:bold;color:#00ff88" id="goodReviews">0</div></div>
  <div class="stat-card" style="border-left:3px solid #ff5555"><div style="color:#888;font-size:12px">Bad</div><div style="font-size:24px;font-weight:bold;color:#ff5555" id="badReviews">0</div></div>
  <div class="stat-card" style="border-left:3px solid #ffcc00"><div style="color:#888;font-size:12px">Needs Fix</div><div style="font-size:24px;font-weight:bold;color:#ffcc00" id="fixReviews">0</div></div>
</div>
<div id="catalogueGrid" class="catalogue-grid"></div>
<div id="loading" class="loading" style="display:none"><div class="spinner"></div><div>Loading...</div></div>
<div id="reviewContainer" class="review-container">
  <div style="display:flex;justify-content:space-between;margin-bottom:20px">
    <div><h2 id="currentStoreCat">Store - Catalogue</h2><div style="color:#888;font-size:14px" id="currentProductInfo"></div></div>
    <button style="background:#333;color:#eee;border:none;padding:8px 15px;border-radius:5px;cursor:pointer" onclick="showCatalogueGrid()">← Back</button>
  </div>
  <div id="productList" class="product-list"></div>
  <div class="comparison-area">
    <div class="image-box"><h3>📄 Original Page</h3><img id="pageImage" src="" alt="Page"></div>
    <div class="image-box"><h3>✂️ Cropped</h3><img id="productImage" src="" alt="Crop"></div>
  </div>
  <div class="rating-buttons">
    <button class="rating-btn good" onclick="setRating('good')">✅ Good</button>
    <button class="rating-btn bad" onclick="setRating('bad')">❌ Bad</button>
    <button class="rating-btn fix" onclick="setRating('needs_fix')">🔧 Needs Fix</button>
  </div>
  <textarea id="feedbackNotes" class="notes-area" placeholder="Notes (optional)..."></textarea>
  <button class="view-btn" style="background:#ff9900;color:#000;font-weight:bold;margin-top:20px" onclick="submitFeedback()">💾 Submit Feedback</button>
</div>
<div id="toast"></div>
<script>
let currentCatalogue=null,currentProductId=null,currentRating=null,allCatalogues=[],feedbackStats={};

async function loadCatalogues(){
  const pw=document.getElementById('password').value;
  if(!pw){alert('Enter password');return;}
  document.getElementById('loginCard').style.display='none';
  document.getElementById('loading').style.display='block';
  try{
    const res=await fetch(`/api/catalogues?password=${encodeURIComponent(pw)}`);
    if(res.status===403){alert('Wrong password');location.reload();return;}
    allCatalogues=await res.json();
    const sRes=await fetch(`/api/feedback/stats?password=${encodeURIComponent(pw)}`);
    feedbackStats=await sRes.json();
    updateStats();renderCatalogues();
    document.getElementById('loading').style.display='none';
    document.getElementById('statsSummary').style.display='flex';
  }catch(e){alert('Failed: '+e.message);location.reload();}
}

function updateStats(){
  document.getElementById('totalReviews').textContent=feedbackStats.total||0;
  document.getElementById('goodReviews').textContent=feedbackStats.good||0;
  document.getElementById('badReviews').textContent=feedbackStats.bad||0;
  document.getElementById('fixReviews').textContent=feedbackStats.needs_fix||0;
}

function renderCatalogues(){
  const grid=document.getElementById('catalogueGrid');
  if(!allCatalogues.length){grid.innerHTML='<div style="color:#888;text-align:center;padding:40px">No catalogues found</div>';return;}
  grid.innerHTML=allCatalogues.map(cat=>`
    <div class="catalogue-card">
      <div style="display:flex;justify-content:space-between;margin-bottom:10px">
        <span class="store-badge">${cat.store}</span>
        <span style="color:#888;font-size:12px">${cat.progress}% cropped</span>
      </div>
      <div style="font-size:18px;margin-bottom:10px">${cat.catalogue_name}</div>
      <div style="color:#888;margin-bottom:10px">${cat.pages} pages • ${cat.total} products</div>
      <button class="view-btn" onclick="viewCatalogue('${cat.store}','${cat.catalogue_name}')">📋 Review Products</button>
    </div>`).join('');
}

async function viewCatalogue(store,catalogue){
  const cat=allCatalogues.find(c=>c.store===store&&c.catalogue_name===catalogue);
  if(!cat)return;
  currentCatalogue=cat;
  document.getElementById('currentStoreCat').textContent=`${store} — ${catalogue}`;
  document.getElementById('reviewContainer').style.display='block';
  document.getElementById('catalogueGrid').style.display='none';
  document.getElementById('statsSummary').style.display='none';

  // Load products for this catalogue
  const pw=document.getElementById('password').value;
  try{
    const rows=await fetch(`/api/pages?password=${encodeURIComponent(pw)}&store=${store}`).then(r=>r.json());
    const catRows=rows.filter(r=>r.catalogue_name===catalogue);
    const list=document.getElementById('productList');
    list.innerHTML=catRows.map(r=>`
      <div class="product-item" onclick="loadPageProduct('${r.page_image_url}')">
        <div style="font-size:12px">Page ${r.page_number}</div>
        <div style="font-size:10px;color:#888">${r.catalogue_name}</div>
      </div>`).join('');
    if(catRows.length>0){
      document.getElementById('pageImage').src=catRows[0].page_image_url;
    }
  }catch(e){console.error(e);}
}

function loadPageProduct(url){document.getElementById('pageImage').src=url;}

function setRating(rating){
  currentRating=rating;
  document.querySelectorAll('.rating-btn').forEach(b=>b.classList.remove('selected'));
  document.querySelector(`.rating-btn.${rating==='needs_fix'?'fix':rating}`).classList.add('selected');
}

async function submitFeedback(){
  if(!currentRating){toast('Select a rating first','error');return;}
  const pw=document.getElementById('password').value;
  try{
    const res=await fetch('/api/feedback',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({password:pw,product_id:currentProductId||'unknown',
        rating:currentRating,notes:document.getElementById('feedbackNotes').value,
        store:currentCatalogue.store,catalogue:currentCatalogue.catalogue_name})
    });
    const data=await res.json();
    if(data.ok){toast('Feedback saved! ✓');currentRating=null;document.querySelectorAll('.rating-btn').forEach(b=>b.classList.remove('selected'));}
    else toast('Failed','error');
  }catch(e){toast('Error: '+e.message,'error');}
}

function showCatalogueGrid(){
  document.getElementById('reviewContainer').style.display='none';
  document.getElementById('catalogueGrid').style.display='grid';
  document.getElementById('statsSummary').style.display='flex';
}

function toast(msg,type='success'){
  const el=document.getElementById('toast');
  el.textContent=msg;el.style.background=type==='error'?'#ff5555':'#00ff88';
  el.style.display='block';setTimeout(()=>el.style.display='none',2000);
}
</script>
</body>
</html>'''

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5002))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
