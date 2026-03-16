"""
katalog.ai — Cropper Tool (standalone) - FIXED VERSION
Fixed: download_image function scope, error handling, and import issues
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
    
    # Configuration options
    MIN_PRODUCT_SIZE     = 100  # minimum pixels for a product (width or height)
    MAX_PRODUCT_SIZE     = 2000 # maximum pixels for a product
    CONFIDENCE_THRESHOLD = 0.6  # minimum confidence for boxes
    MAX_BOXES_PER_PAGE   = 50   # sanity limit
    PADDING_PERCENT      = 0.02 # 2% padding relative to box size

# ── SUPABASE HELPERS ─────────────────────────────────────────────────────────
def _headers():
    return {
        "apikey":        Config.SUPABASE_KEY,
        "Authorization": f"Bearer {Config.SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal",
    }

def _sb_get(path, params=None):
    """Make a GET request to Supabase"""
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
    """Make a PATCH request to Supabase"""
    try:
        r = requests.patch(f"{Config.SUPABASE_URL}{path}", headers=_headers(),
                           json=data, timeout=20)
        r.raise_for_status()
        return r
    except requests.exceptions.RequestException as e:
        logger.error(f"Supabase PATCH failed: {e}")
        raise

def _sb_storage_put(path, img_bytes, content_type="image/jpeg"):
    """Upload an image to Supabase storage"""
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

# ── IMAGE DOWNLOAD FUNCTION ──────────────────────────────────────────────────
def download_image(url):
    """Download an image from a URL"""
    try:
        logger.info(f"Downloading image from: {url[:100]}...")
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        logger.info(f"Downloaded {len(r.content)} bytes")
        return r.content
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to download image: {e}")
        raise

# ── GEMINI DETECTION ─────────────────────────────────────────────────────────
_GEMINI_URL = (
    "https://generativelanguage.googleapis.com"
    "/v1beta/models/gemini-2.5-flash:generateContent"
)

class LayoutPattern:
    """Detects and stores layout patterns from examples"""
    def __init__(self):
        self.avg_width = 0
        self.avg_height = 0
        self.rows = 0
        self.cols = 0
        self.vertical_spacing = 0
        self.horizontal_spacing = 0
        self.top_margin = 0
        self.left_margin = 0
        
    @classmethod
    def from_boxes(cls, boxes: List[Dict], img_width: int, img_height: int):
        """Analyze boxes to detect grid pattern"""
        if not boxes or len(boxes) < 3 or not NUMPY_AVAILABLE:
            return None
            
        pattern = cls()
        
        # Convert to relative coordinates
        centers_x = [(b["x1"] + b["x2"]) / (2 * img_width) for b in boxes]
        centers_y = [(b["y1"] + b["y2"]) / (2 * img_height) for b in boxes]
        widths = [(b["x2"] - b["x1"]) / img_width for b in boxes]
        heights = [(b["y2"] - b["y1"]) / img_height for b in boxes]
        
        # Average dimensions
        pattern.avg_width = np.mean(widths)
        pattern.avg_height = np.mean(heights)
        
        # Detect grid if enough boxes
        if len(centers_x) >= 4:
            # Cluster centers to find rows/columns
            x_clusters = np.round(np.array(centers_x) * 10)
            y_clusters = np.round(np.array(centers_y) * 10)
            
            pattern.cols = len(set(x_clusters))
            pattern.rows = len(set(y_clusters))
            
            # Calculate spacing
            unique_x = sorted(set(x_clusters))
            if len(unique_x) > 1:
                pattern.horizontal_spacing = (unique_x[-1] - unique_x[0]) / (len(unique_x) - 1) / 10
            
            unique_y = sorted(set(y_clusters))
            if len(unique_y) > 1:
                pattern.vertical_spacing = (unique_y[-1] - unique_y[0]) / (len(unique_y) - 1) / 10
            
            # Estimate margins
            pattern.left_margin = min([b["x1"] / img_width for b in boxes])
            pattern.top_margin = min([b["y1"] / img_height for b in boxes])
        
        return pattern

def get_fewshot_examples(store, limit=5):
    """Fetch saved annotation examples for this store from Supabase."""
    try:
        logger.info(f"Fetching few-shot examples for store: {store}")
        rows = _sb_get("/rest/v1/annotations", {
            "store":  f"eq.{store}",
            "order":  "created_at.desc",
            "limit":  limit,
            "select": "page_image_url,boxes,page_number,layout_type",
        })
        
        if not rows:
            logger.info(f"No examples found for store: {store}")
            return []
            
        logger.info(f"Found {len(rows)} examples for store: {store}")
        
        # Enhance examples with layout type if not present
        for row in rows:
            if "layout_type" not in row and row.get("boxes"):
                # Try to detect layout type
                if len(row["boxes"]) >= 4:
                    # Simple heuristic: check if boxes are in grid
                    x_positions = set([round(b["x1"]*10) for b in row["boxes"]])
                    if len(x_positions) <= 3:  # Few distinct x positions suggests grid
                        row["layout_type"] = "grid"
                    else:
                        row["layout_type"] = "mixed"
                else:
                    row["layout_type"] = "unknown"
        
        return rows
    except Exception as e:
        logger.error(f"get_fewshot_examples failed: {e}")
        return []

def detect_products_bbox(img_b64, img_width, img_height, examples=None, store=""):
    """
    Detect product bounding boxes using Gemini.
    Returns list of boxes with coordinates.
    """
    
    # Build prompt with examples
    prompt = build_detection_prompt(store, examples)
    
    # Make API call with retries
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
            
            # Extract JSON array
            boxes = parse_gemini_response(text, img_width, img_height)
            
            if boxes:
                logger.info(f"Detected {len(boxes)} boxes")
                # Post-process
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

def build_detection_prompt(store, examples):
    """Build the prompt for Gemini"""
    
    base_prompt = """You are a precise product detector for retail catalogues.

Find EVERY individual product on this page.
Each box must cover: the product photo AND its price tag together.

Return ONLY a JSON array, no markdown. Each item:
{"x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0}

Rules:
- Coordinates are fractions 0.0 to 1.0 (top-left origin)
- x1,y1 = top-left, x2,y2 = bottom-right
- Boxes should NOT overlap significantly
- Detect ALL products - do not miss any
- If uncertain, still include the box

Guidelines:
1. Look for repeating patterns - products are often arranged in grids
2. Include both product image AND price tag in each box
3. Don't merge multiple products into one box
4. Ignore page headers, footers, and decorations
"""
    
    # Add examples if available
    if examples:
        example_text = "\n\nHere are examples of correct boxes from the same store:\n"
        for i, ex in enumerate(examples[:2]):  # Use up to 2 examples
            boxes = ex.get("boxes", [])
            if boxes:
                # Convert to simple format
                simple_boxes = [{"x1": b["x1"], "y1": b["y1"], "x2": b["x2"], "y2": b["y2"]} 
                               for b in boxes[:10]]  # Limit to 10 boxes per example
                example_text += f"\nExample {i+1} ({len(boxes)} products):\n{json.dumps(simple_boxes)}\n"
        
        base_prompt += example_text + "\nNow detect products in the new image following the same style.\n"
    
    return base_prompt

def parse_gemini_response(text, img_width, img_height):
    """Parse Gemini response to extract boxes"""
    try:
        # Clean up response
        text = re.sub(r"```json|```", "", text).strip()
        
        # Find JSON array
        match = re.search(r"\[.*?\]", text, re.DOTALL)
        if not match:
            logger.warning(f"No JSON array found in: {text[:200]}")
            return []
        
        parsed = json.loads(match.group())
        if not isinstance(parsed, list):
            return []
        
        # Convert to absolute coordinates
        boxes = []
        for item in parsed:
            try:
                # Handle both formats (with or without label/confidence)
                if isinstance(item, dict):
                    x1 = float(item.get("x1", 0))
                    y1 = float(item.get("y1", 0))
                    x2 = float(item.get("x2", 1))
                    y2 = float(item.get("y2", 1))
                elif isinstance(item, list) and len(item) == 4:
                    x1, y1, x2, y2 = map(float, item)
                else:
                    continue
                
                # Validate
                if not (0 <= x1 < x2 <= 1.0 and 0 <= y1 < y2 <= 1.0):
                    continue
                
                # Check minimum size
                if (x2 - x1) < 0.02 or (y2 - y1) < 0.02:
                    continue
                
                boxes.append({
                    "x1": int(x1 * img_width),
                    "y1": int(y1 * img_height),
                    "x2": int(x2 * img_width),
                    "y2": int(y2 * img_height),
                    "confidence": 1.0
                })
            except Exception as e:
                logger.warning(f"Failed to parse box {item}: {e}")
                continue
        
        return boxes
        
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {e}")
        return []
    except Exception as e:
        logger.error(f"Unexpected error parsing response: {e}")
        return []

def post_process_boxes(boxes: List[Dict], img_width: int, img_height: int) -> List[Dict]:
    """Clean up detected boxes"""
    if not boxes:
        return []
    
    # Remove duplicates (boxes that overlap significantly)
    unique_boxes = []
    for box in boxes:
        is_duplicate = False
        for existing in unique_boxes:
            # Calculate IoU (Intersection over Union)
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
                
                if iou > 0.7:  # High overlap - duplicate
                    is_duplicate = True
                    break
        
        if not is_duplicate:
            unique_boxes.append(box)
    
    # Filter by size
    filtered = []
    for box in unique_boxes:
        width = box["x2"] - box["x1"]
        height = box["y2"] - box["y1"]
        
        if (Config.MIN_PRODUCT_SIZE <= width <= Config.MAX_PRODUCT_SIZE and
            Config.MIN_PRODUCT_SIZE <= height <= Config.MAX_PRODUCT_SIZE):
            filtered.append(box)
    
    # Sort by position (top-left to bottom-right)
    filtered.sort(key=lambda b: (b["y1"], b["x1"]))
    
    return filtered[:Config.MAX_BOXES_PER_PAGE]

# ── CROPPER FUNCTIONS ────────────────────────────────────────────────────────
def crop_product(img_bytes, x1, y1, x2, y2):
    """Crop a product from page image bytes. Returns JPEG bytes."""
    if not PIL_AVAILABLE:
        raise Exception("Pillow not installed")

    try:
        img = Image.open(io.BytesIO(img_bytes))
        w, h = img.size

        # Add padding (5% of box size)
        box_width = x2 - x1
        box_height = y2 - y1
        padding = max(5, int(min(box_width, box_height) * 0.05))

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
        
    except Exception as e:
        logger.error(f"Crop failed: {e}")
        raise

# ── CROP JOB ─────────────────────────────────────────────────────────────────
crop_jobs = {}  # in-memory job tracking

def process_crop_job(job_id, catalogue_name, store):
    """
    Main crop pipeline with proper error handling
    """
    crop_jobs[job_id] = {
        "status": "running", 
        "done": 0, 
        "total": 0, 
        "errors": 0,
        "detected": 0,
        "pages_processed": 0
    }
    
    try:
        logger.info(f"Starting crop job {job_id} for {store}/{catalogue_name}")
        
        # Get all products for this catalogue
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
        
        # Fetch few-shot examples once
        examples = get_fewshot_examples(store)
        if examples:
            logger.info(f"Using {len(examples)} few-shot examples")
        
        for page_num, page_data in sorted(page_map.items()):
            page_url = page_data["url"]
            page_products = page_data["products"]
            
            crop_jobs[job_id]["pages_processed"] += 1
            logger.info(f"Processing page {page_num} ({crop_jobs[job_id]['pages_processed']}/{total_pages})")
            
            if not page_url:
                logger.warning(f"Page {page_num} has no image URL")
                done += 1
                crop_jobs[job_id]["done"] = done
                continue
            
            try:
                # Download page image
                logger.info(f"Downloading page {page_num} image")
                img_bytes = download_image(page_url)
                
                # Get image dimensions
                if PIL_AVAILABLE:
                    img_obj = Image.open(io.BytesIO(img_bytes))
                    img_w, img_h = img_obj.size
                    logger.info(f"Image dimensions: {img_w}x{img_h}")
                else:
                    img_w, img_h = 1240, 1754
                
                # Detect products
                img_b64 = base64.b64encode(img_bytes).decode()
                boxes = detect_products_bbox(img_b64, img_w, img_h, examples, store)
                
                if not boxes:
                    logger.warning(f"No products detected on page {page_num}")
                    done += 1
                    crop_jobs[job_id]["done"] = done
                    continue
                
                logger.info(f"Detected {len(boxes)} boxes on page {page_num}")
                
                # Match boxes to products
                unmatched_products = [p for p in page_products if not p.get("product_image_url")]
                
                for idx, box in enumerate(boxes):
                    try:
                        # Crop product
                        cropped = crop_product(img_bytes, box["x1"], box["y1"], box["x2"], box["y2"])
                        
                        # Get product ID
                        if idx < len(unmatched_products):
                            prod = unmatched_products[idx]
                            prod_id = prod["id"]
                            product_name = prod.get("product", "unknown")
                        else:
                            prod = None
                            prod_id = str(uuid.uuid4())
                            product_name = f"product_{idx}"
                        
                        # Upload to storage
                        storage_path = f"product-images/{safe_store}/{safe_cat}/{prod_id}.jpg"
                        product_img_url = _sb_storage_put(storage_path, cropped)
                        
                        # Update database if matched
                        if prod:
                            _sb_patch(
                                f"/rest/v1/products?id=eq.{prod['id']}",
                                {"product_image_url": product_img_url}
                            )
                            logger.info(f"  Box {idx+1}: {product_name} → saved")
                            crop_jobs[job_id]["detected"] += 1
                        else:
                            logger.info(f"  Box {idx+1}: No DB match, saved to storage")
                        
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
        logger.info(f"Crop job {job_id} complete. Pages: {done}/{total_pages}, Detected: {crop_jobs[job_id]['detected']}, Errors: {crop_jobs[job_id]['errors']}")
        
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
        })

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
                    "pages":          set(),
                }
            cats[key]["total"] += 1
            cats[key]["pages"].add(r.get("page_number"))
            if r.get("product_image_url"):
                cats[key]["cropped"] += 1
        
        # Convert to list with progress
        result = []
        for cat in cats.values():
            result.append({
                "store": cat["store"],
                "catalogue_name": cat["catalogue_name"],
                "total": cat["total"],
                "cropped": cat["cropped"],
                "pages": len(cat["pages"]),
                "progress": round(cat["cropped"]/cat["total"]*100) if cat["total"] > 0 else 0
            })
        
        return jsonify(result)
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
        "status": "ok", 
        "service": "katalog-cropper",
        "pillow": PIL_AVAILABLE,
        "numpy": NUMPY_AVAILABLE,
        "time": datetime.now().isoformat()
    })

@app.route("/annotate")
def annotate_page():
    return make_response(ANNOTATE_HTML, 200, {"Content-Type": "text/html"})

@app.route("/api/pages")
def get_pages():
    """Get distinct pages for annotation."""
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
        
        # Deduplicate
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
        return jsonify(_sb_get("/rest/v1/annotations", params))
    except Exception as e:
        logger.error(f"get_annotations failed: {e}")
        return jsonify([])

@app.route("/api/annotations", methods=["POST"])
def post_annotation():
    """Save annotation boxes drawn by user."""
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
                "store":           data.get("store",""),
                "catalogue_name":  data.get("catalogue_name",""),
                "page_number":     data.get("page_number", 0),
                "page_image_url":  data.get("page_image_url",""),
                "boxes":           data.get("boxes",[]),
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
    """Delete an annotation."""
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

# ── HTML UI (Simplified for reliability) ─────────────────────────────────────
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
  </style>
</head>
<body>
  <h1>✂️ katalog.ai Cropper</h1>
  <div class="sub">Extract individual product images using AI</div>

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
          const badge = c.progress === 100 ? 'done' : c.progress > 0 ? 'partial' : 'none';
          const label = c.progress === 100 ? '✅ Complete' : c.progress > 0 ? `${c.progress}%` : '⚪ Not started';
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
      document.getElementById('stat-done').textContent = '0';
      document.getElementById('stat-total').textContent = '0';
      document.getElementById('stat-detected').textContent = '0';
      document.getElementById('stat-errors').textContent = '0';
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
        document.getElementById('stat-done').textContent = d.done || 0;
        document.getElementById('stat-total').textContent = d.total || 0;
        document.getElementById('stat-detected').textContent = d.detected || 0;
        document.getElementById('stat-errors').textContent = d.errors || 0;

        if(d.status === 'done'){
          clearInterval(pollTimer); pollTimer = null;
          log(`🎉 Done! Detected: ${d.detected || 0}, Errors: ${d.errors || 0}`,'success');
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
  <a href="/" style="color:#aaa;font-size:12px;text-decoration:none;margin-left:auto">← Cropper</a>
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
  <div id="st-mode" style="margin-left:auto;color:#ff9900">Drag to draw boxes</div>
</div>

<div id="toast"></div>

<script>
let pw = '', pages = [], pageIdx = 0;
let boxes = [], selectedBox = -1;
let img = null, imgNaturalW = 0, imgNaturalH = 0;
let scale = 1;
let drawing = false, startX = 0, startY = 0, curX = 0, curY = 0;

const canvas = document.getElementById('canvas');
const ctx = canvas.getContext('2d');

async function init() {
  pw = document.getElementById('pw').value;
  if(!pw) return;
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

function render() {
  ctx.clearRect(0,0,canvas.width,canvas.height);
  if(img) ctx.drawImage(img, 0, 0, canvas.width, canvas.height);

  boxes.forEach((b,i) => {
    const x = b.x1*scale, y = b.y1*scale;
    const w = (b.x2-b.x1)*scale, h = (b.y2-b.y1)*scale;
    
    ctx.strokeStyle = i===selectedBox ? '#fff' : '#ff9900';
    ctx.lineWidth = i===selectedBox ? 2.5 : 1.5;
    ctx.strokeRect(x,y,w,h);
    
    ctx.fillStyle = '#ff9900';
    ctx.font = 'bold 11px monospace';
    const label = b.label || `box ${i+1}`;
    const lw = ctx.measureText(label).width + 8;
    ctx.fillRect(x, y-16, lw, 16);
    ctx.fillStyle = '#000';
    ctx.fillText(label, x+4, y-4);
  });

  if(drawing) {
    const x = Math.min(startX,curX), y = Math.min(startY,curY);
    const w = Math.abs(curX-startX), h = Math.abs(curY-startY);
    ctx.strokeStyle = '#fff';
    ctx.lineWidth = 2;
    ctx.setLineDash([6,3]);
    ctx.strokeRect(x,y,w,h);
    ctx.setLineDash([]);
  }
}

function canvasXY(e) {
  const rect = canvas.getBoundingClientRect();
  return {x: e.clientX - rect.left, y: e.clientY - rect.top};
}

canvas.addEventListener('mousedown', e => {
  const {x,y} = canvasXY(e);
  drawing = true;
  startX = x; startY = y; curX = x; curY = y;
  selectedBox = -1;
  renderBoxList();
});

canvas.addEventListener('mousemove', e => {
  if(!drawing) return;
  const {x,y} = canvasXY(e);
  curX = x; curY = y;
  render();
});

canvas.addEventListener('mouseup', e => {
  if(!drawing) return;
  drawing = false;
  const {x,y} = canvasXY(e);
  const x1 = Math.min(startX,x)/scale, y1 = Math.min(startY,y)/scale;
  const x2 = Math.max(startX,x)/scale, y2 = Math.max(startY,y)/scale;
  
  if((x2-x1) < 20 || (y2-y1) < 20) {
    render();
    return;
  }
  
  const label = prompt('Product label:', `product_${boxes.length+1}`);
  if(label === null) {
    render();
    return;
  }
  
  boxes.push({x1,y1,x2,y2, label: label || `product_${boxes.length+1}`});
  selectedBox = boxes.length-1;
  renderBoxList();
  render();
});

function renderBoxList() {
  const list = document.getElementById('box-list');
  document.getElementById('box-count').textContent = boxes.length;
  if(!boxes.length) {
    list.innerHTML = '<div style="color:#555;font-size:12px;padding:8px">No boxes yet.<br>Drag on the image to draw.</div>';
    return;
  }
  list.innerHTML = boxes.map((b,i)=>`
    <div class="box-item${i===selectedBox?' selected':''}" onclick="selectBox(${i})">
      <span class="del" onclick="event.stopPropagation();deleteBox(${i})">×</span>
      <div class="lbl">${i+1}. ${b.label}</div>
      <div class="coords">${Math.round(b.x1)}x${Math.round(b.y1)} → ${Math.round(b.x2)}x${Math.round(b.y2)}</div>
    </div>
  `).join('');
}

function selectBox(i) { selectedBox = i; renderBoxList(); render(); }
function deleteBox(i) { boxes.splice(i,1); selectedBox = -1; renderBoxList(); render(); }
function clearBoxes() { boxes = []; selectedBox = -1; renderBoxList(); render(); }

async function saveAnnotation() {
  if(!boxes.length) { toast('No boxes to save'); return; }
  const p = pages[pageIdx];
  const normalized = boxes.map(b => ({
    label: b.label,
    x1: b.x1 / imgNaturalW,
    y1: b.y1 / imgNaturalH,
    x2: b.x2 / imgNaturalW,
    y2: b.y2 / imgNaturalH,
  }));
  
  try {
    const res = await fetch('/api/annotations', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        password: pw,
        store: p.store,
        catalogue_name: p.catalogue_name,
        page_number: p.page_number,
        page_image_url: p.page_image_url,
        boxes: normalized,
      })
    });
    const data = await res.json();
    if(data.ok) {
      toast(`✅ Saved ${boxes.length} boxes!`);
    } else {
      toast('Save failed', 'error');
    }
  } catch(e) {
    toast('Error: '+e.message, 'error');
  }
}

function toast(msg, type='success') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.style.background = type==='error'?'#ff5555':'#00ff88';
  el.style.display = 'block';
  setTimeout(()=>el.style.display='none', 2000);
}
</script>
</body>
</html>'''

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5002))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
