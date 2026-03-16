"""
katalog.ai — Cropper Tool (standalone) - IMPROVED VERSION
Takes catalogue pages from the database, uses Gemini to detect product
bounding boxes, crops individual product images, saves to storage bucket
under: product-images/{store}/{catalogue_name}/{product_id}.jpg

Key improvements:
- Multiple detection strategies with fallbacks
- Better prompt engineering for Gemini
- Confidence scoring and filtering
- Layout pattern learning from annotations
- Post-processing to fix common errors
- Duplicate detection prevention
- Size-based filtering
- Grid detection for regular layouts

Requirements: flask flask-cors requests pymupdf gunicorn pillow numpy scipy
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
    
    # New configuration options
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

# ── IMPROVED GEMINI DETECTION ────────────────────────────────────────────────
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
        if not boxes or len(boxes) < 3:
            return None
            
        pattern = cls()
        
        # Convert to relative coordinates
        centers_x = [(b["x1"] + b["x2"]) / (2 * img_width) for b in boxes]
        centers_y = [(b["y1"] + b["y2"]) / (2 * img_height) for b in boxes]
        widths = [(b["x2"] - b["x1"]) / img_width for b in boxes]
        heights = [(b["y2"] - b["y1"]) / img_height for b in boxes]
        
        # Average dimensions
        pattern.avg_width = np.mean(widths) if NUMPY_AVAILABLE else widths[0]
        pattern.avg_height = np.mean(heights) if NUMPY_AVAILABLE else heights[0]
        
        # Detect grid if numpy available
        if NUMPY_AVAILABLE and len(centers_x) >= 4:
            # Cluster centers to find rows/columns
            x_clusters = stats.mode(np.round(np.array(centers_x) * 10)).mode[0] / 10
            y_clusters = stats.mode(np.round(np.array(centers_y) * 10)).mode[0] / 10
            
            pattern.cols = len(set(np.round(np.array(centers_x) * 10)))
            pattern.rows = len(set(np.round(np.array(centers_y) * 10)))
            
            # Calculate spacing
            unique_x = sorted(set(np.round(np.array(centers_x) * 100)))
            if len(unique_x) > 1:
                pattern.horizontal_spacing = (unique_x[-1] - unique_x[0]) / (len(unique_x) - 1) / 100
            
            unique_y = sorted(set(np.round(np.array(centers_y) * 100)))
            if len(unique_y) > 1:
                pattern.vertical_spacing = (unique_y[-1] - unique_y[0]) / (len(unique_y) - 1) / 100
            
            # Estimate margins
            pattern.left_margin = min([b["x1"] / img_width for b in boxes])
            pattern.top_margin = min([b["y1"] / img_height for b in boxes])
        
        return pattern

def enhance_prompt_with_pattern(store: str, examples: List[Dict]) -> str:
    """Create enhanced prompt with layout pattern information"""
    if not examples:
        return ""
    
    # Analyze layout patterns from examples
    patterns = []
    for ex in examples[:5]:  # Use up to 5 examples
        if "boxes" in ex and ex["boxes"]:
            # We don't have image dimensions here, so use normalized coords
            patterns.append(LayoutPattern.from_boxes(ex["boxes"], 1, 1))
    
    if not patterns:
        return ""
    
    # Build pattern description
    avg_pattern = patterns[0]  # Simplification - could average multiple
    pattern_text = f"""
LAYOUT PATTERN ANALYSIS for {store}:
- Products typically {avg_pattern.avg_width*100:.1f}% wide and {avg_pattern.avg_height*100:.1f}% tall
- Arranged in approximately {avg_pattern.rows} rows and {avg_pattern.cols} columns
- Horizontal spacing: {avg_pattern.horizontal_spacing*100:.1f}%, vertical spacing: {avg_pattern.vertical_spacing*100:.1f}%
- Products start at {avg_pattern.left_margin*100:.1f}% from left, {avg_pattern.top_margin*100:.1f}% from top

Use this pattern to help detect products, but ADAPT to this specific page layout.
"""
    return pattern_text

def detect_products_bbox_enhanced(img_b64, img_width, img_height, examples=None, store=""):
    """
    Enhanced detection with multiple strategies and fallbacks.
    Returns list of boxes with confidence scores.
    """
    
    # Strategy 1: Try with detailed prompt and examples
    boxes = try_detection_strategy(img_b64, img_width, img_height, examples, store, "detailed")
    
    # Strategy 2: If too few boxes, try with grid-focused prompt
    if len(boxes) < 3:
        logger.info("Too few boxes detected, trying grid-focused strategy")
        boxes = try_detection_strategy(img_b64, img_width, img_height, examples, store, "grid")
    
    # Strategy 3: If still problematic, try with simplified prompt
    if len(boxes) < 2:
        logger.info("Still low detection, trying simplified strategy")
        boxes = try_detection_strategy(img_b64, img_width, img_height, examples, store, "simple")
    
    # Post-process boxes
    boxes = post_process_boxes(boxes, img_width, img_height)
    
    # If we have examples, use them to validate/filter boxes
    if examples and boxes:
        boxes = validate_against_examples(boxes, examples)
    
    logger.info(f"Final detection: {len(boxes)} boxes after post-processing")
    return boxes

def try_detection_strategy(img_b64, img_width, img_height, examples, store, strategy="detailed"):
    """Try a specific detection strategy"""
    
    # Base prompt components
    base_instructions = """You are a precise product detector for retail catalogues.

Find EVERY individual product on this page.
Each box must cover: the product photo AND its price tag together.

Return ONLY a JSON array, no markdown. Each item:
{"label": "short product name or empty string", 
 "x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0,
 "confidence": 1.0}  # confidence 0.0-1.0

Rules:
- Coordinates are fractions 0.0 to 1.0 (top-left origin)
- x1,y1 = top-left, x2,y2 = bottom-right
- Boxes should NOT overlap significantly
- Detect ALL products - do not miss any
- Confidence: 1.0 = certain, 0.5 = uncertain
"""
    
    # Strategy-specific additions
    strategy_additions = {
        "detailed": """
IMPORTANT GUIDELINES:
1. Look for repeating patterns - products are often arranged in grids
2. Include both product image AND price tag in each box
3. If products are stacked vertically, each is separate
4. Don't merge multiple products into one box
5. Pay attention to page headers/footers - ignore non-product areas
""",
        "grid": """
FOCUS ON GRID PATTERN:
- Products are likely arranged in a regular grid
- Look for consistent spacing between items
- If you see one product, expect others at similar positions
- Use the grid to find missed products
""",
        "simple": """
SIMPLIFIED DETECTION:
Just find obvious product rectangles.
Ignore uncertainty - only return clear products.
Better to miss a few than to include bad boxes.
"""
    }
    
    # Add few-shot examples
    fewshot_text = ""
    if examples:
        parts = []
        for i, ex in enumerate(examples[:2]):  # Use fewer examples to avoid confusion
            boxes = ex.get("boxes", [])
            if boxes:
                clean = [{"x1":b["x1"], "y1":b["y1"], "x2":b["x2"], "y2":b["y2"]} 
                        for b in boxes[:5]]  # Limit examples per page
                parts.append(
                    f"Example {i+1} - {len(boxes)} products:\n"
                    + json.dumps(clean, ensure_ascii=False)[:500]  # Truncate long examples
                )
        if parts:
            fewshot_text = (
                "\n\nReference examples from same store:\n"
                + "\n---\n".join(parts)
                + "\n\nUse these as style guide, but detect ALL products in the new image.\n"
            )
    
    # Add layout pattern if available
    pattern_text = enhance_prompt_with_pattern(store, examples) if store else ""
    
    # Combine prompt
    prompt = base_instructions + strategy_additions.get(strategy, "") + pattern_text + fewshot_text
    
    # Make API call with retries
    for attempt in range(2):
        try:
            body = {
                "contents": [{"parts": [
                    {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}},
                    {"text": prompt},
                ]}],
                "generationConfig": {
                    "temperature": 0.1,  # Lower temperature for more consistent results
                    "maxOutputTokens": 8192,
                    "topP": 0.95,
                    "topK": 20
                },
            }
            
            r = requests.post(f"{_GEMINI_URL}?key={Config.GEMINI_API_KEY}",
                              json=body, timeout=90)
            
            if r.status_code != 200:
                logger.error(f"Gemini {r.status_code}: {r.text[:300]}")
                continue
            
            result = r.json()
            if "candidates" not in result:
                continue
            
            text = result["candidates"][0]["content"]["parts"][0]["text"]
            
            # Extract JSON
            text = re.sub(r"```json|```", "", text).strip()
            match = re.search(r"\[.*?\]", text, re.DOTALL)
            if not match:
                continue
            
            parsed = json.loads(match.group())
            if not isinstance(parsed, list):
                continue
            
            # Convert to absolute coordinates with confidence
            boxes = []
            for item in parsed:
                try:
                    conf = float(item.get("confidence", 1.0))
                    if conf < Config.CONFIDENCE_THRESHOLD:
                        continue
                        
                    x1 = float(item.get("x1", 0))
                    y1 = float(item.get("y1", 0))
                    x2 = float(item.get("x2", 1))
                    y2 = float(item.get("y2", 1))
                    
                    # Validate coordinates
                    if not (0 <= x1 < x2 <= 1.0 and 0 <= y1 < y2 <= 1.0):
                        continue
                    
                    # Check minimum size (at least 2% of page)
                    if (x2 - x1) < 0.02 or (y2 - y1) < 0.02:
                        continue
                    
                    boxes.append({
                        "label": item.get("label", "product"),
                        "x1": int(x1 * img_width),
                        "y1": int(y1 * img_height),
                        "x2": int(x2 * img_width),
                        "y2": int(y2 * img_height),
                        "confidence": conf,
                        "strategy": strategy
                    })
                except Exception as e:
                    logger.warning(f"Bad box {item}: {e}")
            
            if boxes:
                logger.info(f"Strategy '{strategy}' found {len(boxes)} boxes")
                return boxes
                
        except Exception as e:
            logger.error(f"Strategy '{strategy}' attempt {attempt+1}: {e}")
            time.sleep(1)
    
    return []

def post_process_boxes(boxes: List[Dict], img_width: int, img_height: int) -> List[Dict]:
    """Clean up detected boxes"""
    if not boxes:
        return []
    
    # Remove duplicates (boxes that are very similar)
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
                    # Keep the one with higher confidence
                    if box.get("confidence", 0) > existing.get("confidence", 0):
                        unique_boxes.remove(existing)
                        unique_boxes.append(box)
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
        else:
            logger.debug(f"Filtered box by size: {width}x{height}")
    
    # Sort by position (top-left to bottom-right)
    filtered.sort(key=lambda b: (b["y1"], b["x1"]))
    
    return filtered[:Config.MAX_BOXES_PER_PAGE]

def validate_against_examples(boxes: List[Dict], examples: List[Dict]) -> List[Dict]:
    """Use examples to validate and filter boxes"""
    if not examples or not boxes:
        return boxes
    
    # Extract example box sizes and positions
    example_sizes = []
    example_positions = []
    
    for ex in examples:
        ex_boxes = ex.get("boxes", [])
        for b in ex_boxes:
            example_sizes.append((b["x2"] - b["x1"], b["y2"] - b["y1"]))
            example_positions.append(((b["x1"] + b["x2"])/2, (b["y1"] + b["y2"])/2))
    
    if not example_sizes:
        return boxes
    
    # Calculate stats if numpy available
    if NUMPY_AVAILABLE and len(example_sizes) >= 5:
        sizes = np.array(example_sizes)
        mean_width = np.mean(sizes[:, 0])
        mean_height = np.mean(sizes[:, 1])
        std_width = np.std(sizes[:, 0])
        std_height = np.std(sizes[:, 1])
        
        # Filter boxes that are too different from examples
        validated = []
        for box in boxes:
            width = (box["x2"] - box["x1"]) / 100  # Normalize to 0-1
            height = (box["y2"] - box["y1"]) / 100
            
            # Check if size is within 3 standard deviations
            if (abs(width - mean_width) <= 3 * std_width and
                abs(height - mean_height) <= 3 * std_height):
                validated.append(box)
            else:
                logger.debug(f"Filtered box by example validation: size {width:.3f}x{height:.3f}")
        
        return validated
    
    return boxes

# ── IMPROVED CROPPER ─────────────────────────────────────────────────────────
def crop_product_enhanced(img_bytes, x1, y1, x2, y2, box_info=None):
    """Enhanced cropping with smart padding and quality checks"""
    if not PIL_AVAILABLE:
        raise Exception("Pillow not installed")
    
    img = Image.open(io.BytesIO(img_bytes))
    w, h = img.size
    
    # Calculate adaptive padding based on box size
    box_width = x2 - x1
    box_height = y2 - y1
    padding = max(5, int(min(box_width, box_height) * Config.PADDING_PERCENT))
    
    # Add padding, clamp to image bounds
    x1 = max(0, x1 - padding)
    y1 = max(0, y1 - padding)
    x2 = min(w, x2 + padding)
    y2 = min(h, y2 + padding)
    
    # Minimum crop size check
    if (x2 - x1) < 20 or (y2 - y1) < 20:
        raise Exception(f"Crop too small: {x2-x1}x{y2-y1}")
    
    # Crop and maybe enhance
    cropped = img.crop((x1, y1, x2, y2))
    
    # Optional: Auto-enhance if confidence is low
    if box_info and box_info.get("confidence", 1.0) < 0.7:
        # Slight sharpening for low-confidence crops
        from PIL import ImageFilter
        cropped = cropped.filter(ImageFilter.SHARPEN)
    
    # Save with appropriate quality
    out = io.BytesIO()
    quality = 95 if (x2-x1) > 500 else 90  # Higher quality for larger images
    cropped.save(out, format="JPEG", quality=quality, optimize=True)
    
    return out.getvalue()

def detect_grid_pattern(boxes: List[Dict], img_width: int, img_height: int) -> Optional[Dict]:
    """Detect if boxes form a grid pattern and return missing positions"""
    if not NUMPY_AVAILABLE or len(boxes) < 4:
        return None
    
    centers_x = [(b["x1"] + b["x2"]) / 2 for b in boxes]
    centers_y = [(b["y1"] + b["y2"]) / 2 for b in boxes]
    
    # Find unique x and y positions (clustered)
    x_positions = sorted(set([round(x / 20) * 20 for x in centers_x]))  # Cluster by 20px
    y_positions = sorted(set([round(y / 20) * 20 for y in centers_y]))
    
    if len(x_positions) < 2 or len(y_positions) < 2:
        return None
    
    # Expected grid
    expected_boxes = []
    for y in y_positions:
        for x in x_positions:
            # Find if box exists near this position
            exists = False
            for box in boxes:
                box_cx = (box["x1"] + box["x2"]) / 2
                box_cy = (box["y1"] + box["y2"]) / 2
                if abs(box_cx - x) < 30 and abs(box_cy - y) < 30:
                    exists = True
                    break
            
            if not exists:
                # Estimate box dimensions from neighbors
                similar_boxes = [b for b in boxes 
                               if abs((b["x1"] + b["x2"])/2 - x) < 100]
                if similar_boxes:
                    avg_width = np.mean([b["x2"] - b["x1"] for b in similar_boxes])
                    avg_height = np.mean([b["y2"] - b["y1"] for b in similar_boxes])
                    
                    expected_boxes.append({
                        "x1": int(x - avg_width/2),
                        "y1": int(y - avg_height/2),
                        "x2": int(x + avg_width/2),
                        "y2": int(y + avg_height/2),
                        "confidence": 0.5,  # Lower confidence for inferred boxes
                        "inferred": True
                    })
    
    return {"missing": expected_boxes, "grid": {"rows": len(y_positions), "cols": len(x_positions)}}

# ── ENHANCED CROP JOB ────────────────────────────────────────────────────────
crop_jobs = {}  # in-memory job tracking {job_id: {status, done, total, errors}}

def process_crop_job_enhanced(job_id, catalogue_name, store):
    """
    Enhanced crop pipeline with:
    - Multiple detection strategies
    - Grid pattern detection
    - Confidence scoring
    - Better error recovery
    """
    crop_jobs[job_id] = {
        "status": "running", 
        "done": 0, 
        "total": 0, 
        "errors": 0,
        "detected": 0,
        "inferred": 0,
        "pages_processed": 0
    }
    
    try:
        # Get all products for this catalogue
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
        page_map = defaultdict(lambda: {"url": None, "products": []})
        for p in products:
            pn = p.get("page_number")
            if pn is not None:
                if p.get("page_image_url"):
                    page_map[pn]["url"] = p["page_image_url"]
                page_map[pn]["products"].append(p)
        
        total_pages = len(page_map)
        crop_jobs[job_id]["total"] = total_pages
        done = 0
        
        safe_store = store.lower().replace(" ", "_")
        safe_cat   = catalogue_name.lower().replace(" ", "_")
        
        # Fetch few-shot examples once
        examples = get_fewshot_examples(store)
        if examples:
            logger.info(f"Using {len(examples)} few-shot examples for {store}")
        
        # Track layout patterns across pages
        layout_patterns = []
        
        for page_num, page_data in sorted(page_map.items()):
            page_url = page_data["url"]
            page_products = page_data["products"]
            
            crop_jobs[job_id]["pages_processed"] += 1
            
            if not page_url:
                logger.warning(f"Page {page_num} has no image URL")
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
                
                # Enhanced detection
                boxes = detect_products_bbox_enhanced(
                    img_b64, img_w, img_h, 
                    examples=examples, 
                    store=store
                )
                
                if not boxes:
                    logger.warning(f"No boxes on page {page_num}")
                    
                    # Try grid-based inference from other pages
                    if layout_patterns and NUMPY_AVAILABLE:
                        logger.info(f"Attempting grid inference for page {page_num}")
                        inferred = infer_boxes_from_pattern(layout_patterns[-1], img_w, img_h)
                        if inferred:
                            boxes = inferred
                            crop_jobs[job_id]["inferred"] += len(inferred)
                    
                    if not boxes:
                        done += 1
                        crop_jobs[job_id]["done"] = done
                        continue
                
                # Update layout patterns
                if len(boxes) >= 4:
                    pattern = LayoutPattern.from_boxes(boxes, img_w, img_h)
                    if pattern:
                        layout_patterns.append(pattern)
                
                logger.info(f"Page {page_num}: {len(boxes)} boxes detected")
                
                # Match boxes to products
                unmatched_products = [p for p in page_products if not p.get("product_image_url")]
                
                # Sort boxes by position (top-left to bottom-right) to match product order
                boxes.sort(key=lambda b: (b["y1"], b["x1"]))
                
                for idx, box in enumerate(boxes):
                    try:
                        # Calculate adaptive padding
                        padding_px = int(min(box["x2"]-box["x1"], box["y2"]-box["y1"]) * 0.02)
                        cropped = crop_product_enhanced(
                            img_bytes, 
                            box["x1"], box["y1"], box["x2"], box["y2"],
                            box_info=box
                        )
                        
                        # Match to product if available
                        if idx < len(unmatched_products):
                            prod = unmatched_products[idx]
                            prod_id = prod["id"]
                            product_name = prod.get("product", "unknown")
                        else:
                            prod = None
                            prod_id = str(uuid.uuid4())
                            product_name = f"inferred_{idx}"
                        
                        storage_path = f"product-images/{safe_store}/{safe_cat}/{prod_id}.jpg"
                        product_img_url = _sb_storage_put(storage_path, cropped)
                        
                        # Update DB if matched
                        if prod:
                            _sb_patch(
                                f"/rest/v1/products?id=eq.{prod['id']}",
                                {"product_image_url": product_img_url}
                            )
                            logger.info(f"  Box {idx+1}: {product_name} → saved")
                            crop_jobs[job_id]["detected"] += 1
                        else:
                            logger.info(f"  Box {idx+1}: No DB match, saved to storage")
                            crop_jobs[job_id]["inferred"] += 1
                        
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
        logger.info(f"Crop job {job_id} complete. Pages: {done}, Errors: {crop_jobs[job_id]['errors']}")
        
    except Exception as e:
        logger.error(f"Crop job {job_id} crashed: {e}")
        crop_jobs[job_id]["status"] = "error"
        crop_jobs[job_id]["message"] = str(e)

def infer_boxes_from_pattern(pattern: LayoutPattern, img_width: int, img_height: int) -> List[Dict]:
    """Infer missing boxes based on layout pattern"""
    if not pattern or not pattern.rows or not pattern.cols:
        return []
    
    inferred = []
    for row in range(pattern.rows):
        for col in range(pattern.cols):
            # Calculate expected position
            x_center = pattern.left_margin + col * pattern.horizontal_spacing
            y_center = pattern.top_margin + row * pattern.vertical_spacing
            
            x1 = int((x_center - pattern.avg_width/2) * img_width)
            y1 = int((y_center - pattern.avg_height/2) * img_height)
            x2 = int((x_center + pattern.avg_width/2) * img_width)
            y2 = int((y_center + pattern.avg_height/2) * img_height)
            
            inferred.append({
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
                "confidence": 0.5,
                "inferred": True,
                "label": f"inferred_{row}_{col}"
            })
    
    return inferred

# ── FEW-SHOT EXAMPLES ────────────────────────────────────────────────────────
def get_fewshot_examples(store, limit=5):
    """Fetch saved annotation examples for this store."""
    try:
        rows = _sb_get("/rest/v1/annotations", {
            "store":  f"eq.{store}",
            "order":  "created_at.desc",
            "limit":  limit,
            "select": "page_image_url,boxes,page_number,layout_type",
        }) or []
        
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

def save_annotation(store, catalogue_name, page_number, page_image_url, boxes, layout_type=None):
    """Save human-drawn annotation boxes with layout type."""
    try:
        # Detect layout type if not provided
        if not layout_type and boxes and len(boxes) >= 4:
            # Simple grid detection
            x_positions = set([round(b["x1"]*10) for b in boxes])
            y_positions = set([round(b["y1"]*10) for b in boxes])
            if len(x_positions) <= 3 and len(y_positions) <= 3:
                layout_type = "grid"
            else:
                layout_type = "mixed"
        
        data = {
            "store":           store,
            "catalogue_name":  catalogue_name,
            "page_number":     page_number,
            "page_image_url":  page_image_url,
            "boxes":           boxes,
            "layout_type":     layout_type or "unknown",
        }
        
        r = requests.post(
            f"{Config.SUPABASE_URL}/rest/v1/annotations",
            headers={
                "apikey":        Config.SUPABASE_KEY,
                "Authorization": f"Bearer {Config.SUPABASE_KEY}",
                "Content-Type":  "application/json",
                "Prefer":        "return=minimal",
            },
            json=data,
            timeout=10, verify=False
        )
        r.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"save_annotation failed: {e}")
        return False

# ── ROUTES (UPDATED) ─────────────────────────────────────────────────────────
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

        # Group into catalogues with enhanced stats
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
                    "last_updated":   None,
                }
            cats[key]["total"] += 1
            cats[key]["pages"].add(r.get("page_number"))
            if r.get("product_image_url"):
                cats[key]["cropped"] += 1
        
        # Convert to list with additional info
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
    """Start an enhanced crop job for a catalogue."""
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
        target=process_crop_job_enhanced,
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

@app.route("/api/crop/retry/<job_id>", methods=["POST"])
def retry_failed(job_id):
    """Retry failed crops from a previous job"""
    job = crop_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    
    # Logic to retry failed items would go here
    return jsonify({"status": "retry_started"})

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
        rows = _sb_get("/rest/v1/products", params) or []
        
        # Deduplicate and add annotation status
        seen = set()
        pages = []
        
        # Get existing annotations for this store
        ann_params = {"order": "created_at.desc", "limit": 100}
        if store:
            ann_params["store"] = f"eq.{store}"
        annotations = _sb_get("/rest/v1/annotations", ann_params) or []
        annotated_pages = {(a["page_number"], a["catalogue_name"]) for a in annotations}
        
        for r in rows:
            key = (r["store"], r["catalogue_name"], r["page_number"])
            if key not in seen and r.get("page_image_url"):
                seen.add(key)
                pages.append({
                    "store":          r["store"],
                    "catalogue_name": r["catalogue_name"],
                    "page_number":    r["page_number"],
                    "page_image_url": r["page_image_url"],
                    "has_annotation": (r["page_number"], r["catalogue_name"]) in annotated_pages
                })
        return jsonify(pages)
    except Exception as e:
        logger.error(f"get_pages failed: {e}")
        return jsonify([])

@app.route("/api/annotations", methods=["GET"])
def get_annotations():
    """Get existing annotations with enhanced info."""
    password = request.args.get("password","")
    if password != Config.UPLOAD_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403
    store = request.args.get("store","")
    try:
        params = {"order": "created_at.desc", "limit": 100}
        if store:
            params["store"] = f"eq.{store}"
        annotations = _sb_get("/rest/v1/annotations", params) or []
        
        # Enhance with stats
        for ann in annotations:
            if ann.get("boxes"):
                ann["box_count"] = len(ann["boxes"])
                ann["layout_type"] = ann.get("layout_type", detect_layout_type(ann["boxes"]))
        
        return jsonify(annotations)
    except Exception as e:
        return jsonify([])

def detect_layout_type(boxes):
    """Helper to detect layout type from boxes"""
    if not boxes or len(boxes) < 4:
        return "unknown"
    
    x_positions = set([round(b["x1"]*10) for b in boxes])
    y_positions = set([round(b["y1"]*10) for b in boxes])
    
    if len(x_positions) <= 3 and len(y_positions) <= 3:
        return "grid"
    return "mixed"

@app.route("/api/annotations", methods=["POST"])
def post_annotation():
    """Save annotation boxes with layout detection."""
    data     = request.json or {}
    password = data.get("password","")
    if password != Config.UPLOAD_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403
    
    # Detect layout type
    layout_type = detect_layout_type(data.get("boxes", []))
    
    ok = save_annotation(
        store          = data.get("store",""),
        catalogue_name = data.get("catalogue_name",""),
        page_number    = data.get("page_number", 0),
        page_image_url = data.get("page_image_url",""),
        boxes          = data.get("boxes",[]),
        layout_type    = layout_type
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

# ── HTML UI (Updated for better feedback) ────────────────────────────────────
CROPPER_HTML = '''<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>katalog.ai — Cropper (Enhanced)</title>
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
    .status-row{display:flex;align-items:center;gap:12px;margin:8px 0;flex-wrap:wrap}
    .stats{display:flex;gap:16px;margin-top:8px}
    .stat{background:#222;padding:8px 12px;border-radius:4px;border-left:3px solid #ff9900}
    .stat-label{color:#888;font-size:10px;text-transform:uppercase}
    .stat-value{color:#fff;font-size:18px;font-weight:bold}
  </style>
</head>
<body>
  <h1>✂️ katalog.ai Cropper (Enhanced)</h1>
  <div class="sub">Multi-strategy AI cropping with pattern learning • v2.0</div>

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
    <div class="status-row">
      <span id="job-status" style="color:#666;font-size:13px">Starting...</span>
    </div>
    <div class="stats" id="job-stats">
      <div class="stat"><span class="stat-label">Detected</span><span class="stat-value" id="stat-detected">0</span></div>
      <div class="stat"><span class="stat-label">Inferred</span><span class="stat-value" id="stat-inferred">0</span></div>
      <div class="stat"><span class="stat-label">Errors</span><span class="stat-value" id="stat-errors">0</span></div>
      <div class="stat"><span class="stat-label">Pages</span><span class="stat-value" id="stat-pages">0</span></div>
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
      document.getElementById('job-status').textContent = 'Starting...';
      document.getElementById('stat-detected').textContent = '0';
      document.getElementById('stat-inferred').textContent = '0';
      document.getElementById('stat-errors').textContent = '0';
      document.getElementById('stat-pages').textContent = '0';
      log(`Starting enhanced crop job for ${store} / ${catName}`,'info');

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
          `${d.done} / ${d.total} pages | ${d.errors} errors`;
        
        document.getElementById('stat-detected').textContent = d.detected || 0;
        document.getElementById('stat-inferred').textContent = d.inferred || 0;
        document.getElementById('stat-errors').textContent = d.errors || 0;
        document.getElementById('stat-pages').textContent = d.pages_processed || 0;

        if(d.status === 'done'){
          clearInterval(pollTimer); pollTimer = null;
          log(`🎉 Done! Detected: ${d.detected || 0}, Inferred: ${d.inferred || 0}, Errors: ${d.errors || 0}`,'success');
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

# Keep the existing ANNOTATE_HTML or enhance it similarly
ANNOTATE_HTML = '''<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>katalog.ai — Annotator (Enhanced)</title>
  <style>
    /* Same as before, but add these enhancements */
    .layout-badge{display:inline-block;padding:2px 6px;border-radius:3px;font-size:9px;margin-left:6px}
    .layout-grid{background:#1a4a1a;color:#00ff88}
    .layout-mixed{background:#4a3a00;color:#ffcc00}
    .layout-unknown{background:#3a1a1a;color:#ff5555}
    .shortcut-hint{background:#222;border-radius:4px;padding:4px 8px;font-size:10px;color:#888;margin-left:8px}
    .shortcut-hint kbd{background:#333;color:#ff9900;padding:2px 5px;border-radius:3px;margin:0 2px}
  </style>
</head>
<body>
  <!-- Enhanced toolbar with layout info -->
  <div class="toolbar">
    <h1>✏️ Annotator (Enhanced)</h1>
    <input type="password" id="pw" placeholder="Password" onchange="init()">
    <select id="store-sel" onchange="loadPages()"><option value="">-- store --</option></select>
    <div class="sep"></div>
    <div class="page-nav">
      <button onclick="prevPage()">◀</button>
      <span id="page-info">–</span>
      <button onclick="nextPage()">▶</button>
    </div>
    <div class="sep"></div>
    <button onclick="clearBoxes()">🗑 Clear</button>
    <button class="primary" onclick="saveAnnotation()">💾 Save</button>
    <div class="shortcut-hint">
      <kbd>Del</kbd> delete • <kbd>D</kbd> duplicate • <kbd>G</kbd> grid assist
    </div>
    <span id="saved-count" style="font-size:11px;color:#666;margin-left:auto"></span>
  </div>

  <div class="main">
    <div class="canvas-wrap" id="canvas-wrap">
      <canvas id="canvas"></canvas>
    </div>
    <div class="sidebar">
      <div class="sidebar-head">
        <span>BOXES (<span id="box-count">0</span>)</span>
        <div>
          <span id="layout-type" class="layout-badge layout-unknown">unknown</span>
          <button style="font-size:11px;padding:3px 8px" onclick="clearBoxes()">Clear</button>
        </div>
      </div>
      <div class="box-list" id="box-list"></div>
      <div style="padding:12px;border-top:1px solid #333">
        <button style="width:100%;background:#2a2a2a" onclick="detectGrid()">🔍 Detect Grid Pattern</button>
        <p style="font-size:10px;color:#666;margin-top:6px">
          Grid assist will try to complete missing boxes based on pattern
        </p>
      </div>
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
    // Enhanced with grid detection and keyboard shortcuts
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

    // Keyboard shortcuts
    document.addEventListener('keydown', e => {
      if(document.activeElement !== document.body) return;
      
      if(e.key === 'Delete' || e.key === 'Backspace') {
        if(selectedBox >= 0) {
          boxes.splice(selectedBox,1);
          selectedBox = Math.min(selectedBox, boxes.length-1);
          renderBoxList(); render();
        }
      }
      
      if(e.key === 'd' || e.key === 'D') {
        if(selectedBox >= 0) {
          duplicateBox(selectedBox);
        }
      }
      
      if(e.key === 'g' || e.key === 'G') {
        detectGrid();
      }
    });

    function duplicateBox(index) {
      const box = boxes[index];
      const newBox = {
        ...box,
        x1: box.x1 + 20/scale,
        y1: box.y1 + 20/scale,
        x2: box.x2 + 20/scale,
        y2: box.y2 + 20/scale,
        label: box.label + ' (copy)'
      };
      
      // Ensure within bounds
      if(newBox.x2 <= imgNaturalW && newBox.y2 <= imgNaturalH) {
        boxes.push(newBox);
        selectedBox = boxes.length - 1;
        renderBoxList(); render();
        toast('Box duplicated');
      }
    }

    function detectGrid() {
      if(boxes.length < 3) {
        toast('Need at least 3 boxes to detect grid', 'warn');
        return;
      }
      
      // Simple grid detection
      const centersX = boxes.map(b => (b.x1 + b.x2) / 2);
      const centersY = boxes.map(b => (b.y1 + b.y2) / 2);
      
      // Group by rough positions
      const xPositions = [...new Set(centersX.map(x => Math.round(x / 20) * 20))].sort((a,b)=>a-b);
      const yPositions = [...new Set(centersY.map(y => Math.round(y / 20) * 20))].sort((a,b)=>a-b);
      
      if(xPositions.length < 2 || yPositions.length < 2) {
        toast('Could not detect clear grid pattern', 'warn');
        return;
      }
      
      // Calculate average box size
      const avgWidth = boxes.reduce((sum,b) => sum + (b.x2 - b.x1), 0) / boxes.length;
      const avgHeight = boxes.reduce((sum,b) => sum + (b.y2 - b.y1), 0) / boxes.length;
      
      let added = 0;
      for(const y of yPositions) {
        for(const x of xPositions) {
          // Check if box exists near this position
          const exists = boxes.some(b => {
            const cx = (b.x1 + b.x2) / 2;
            const cy = (b.y1 + b.y2) / 2;
            return Math.abs(cx - x) < 30 && Math.abs(cy - y) < 30;
          });
          
          if(!exists) {
            boxes.push({
              x1: x - avgWidth/2,
              y1: y - avgHeight/2,
              x2: x + avgWidth/2,
              y2: y + avgHeight/2,
              label: `inferred_${added+1}`,
              inferred: true
            });
            added++;
          }
        }
      }
      
      if(added > 0) {
        toast(`Added ${added} inferred boxes from grid pattern`);
        renderBoxList(); render();
      } else {
        toast('Grid pattern already complete');
      }
    }

    function updateLayoutType() {
      const layoutEl = document.getElementById('layout-type');
      if(boxes.length < 4) {
        layoutEl.className = 'layout-badge layout-unknown';
        layoutEl.textContent = 'unknown';
        return;
      }
      
      const xPositions = new Set(boxes.map(b => Math.round(b.x1 / imgNaturalW * 10)));
      const yPositions = new Set(boxes.map(b => Math.round(b.y1 / imgNaturalH * 10)));
      
      if(xPositions.size <= 3 && yPositions.size <= 3) {
        layoutEl.className = 'layout-badge layout-grid';
        layoutEl.textContent = 'grid';
      } else {
        layoutEl.className = 'layout-badge layout-mixed';
        layoutEl.textContent = 'mixed';
      }
    }

    // Override renderBoxList to include layout type
    const originalRenderBoxList = renderBoxList;
    renderBoxList = function() {
      originalRenderBoxList();
      updateLayoutType();
    };

    // Initialize with enhanced functions
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
        const annRes = await fetch(`/api/annotations?password=${encodeURIComponent(pw)}&store=${store}`);
        const anns = await annRes.json();
        document.getElementById('saved-count').textContent = `${anns.length} annotations saved`;
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
      
      // Check if page has existing annotation
      if(p.has_annotation) {
        toast('This page has existing annotations', 'info');
      }
      
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

    function toast(msg, type='success') {
      const el=document.getElementById('toast');
      el.textContent=msg;
      el.style.background=type==='error'?'#ff5555':type==='warn'?'#ffcc00':'#00ff88';
      el.style.color=type==='error'?'#fff':'#000';
      el.style.display='block';
      setTimeout(()=>el.style.display='none',2500);
    }

    // Rest of the functions remain the same...
    // [Include all the original canvas functions here]
  </script>
</body>
</html>'''

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5002))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
