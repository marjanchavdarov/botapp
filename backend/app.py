"""
katalog.ai backend
Plain requests to Supabase REST API — no SDK, no special library.
SSL fix: verify=False on every call (Render's system CA bundle is stale).

Install: pip install pymupdf flask requests certifi
Render start command: gunicorn app:app --worker-class gthread -w 1 --threads 4 --bind 0.0.0.0:$PORT
"""

import os
import json
import uuid
import base64
import logging
import threading
import tempfile
import time
import re
from datetime import datetime, date, timedelta

import requests
import fitz
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

# ----------------------------------------------------------------------------
# CONFIG & LOGGING
# ----------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("katalog")

app = Flask(__name__, static_folder="static", static_url_path="/static")
CORS(app)  # Allow all origins — required for frontend/Supabase to call this API


class Config:
    GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY")
    SUPABASE_URL    = os.environ.get("SUPABASE_URL")
    SUPABASE_KEY    = os.environ.get("SUPABASE_KEY")
    STORAGE_BUCKET  = "pages"
    BASE_URL        = os.environ.get("BASE_URL", "https://botapp-u7qa.onrender.com")


CROATIA_STORES = [
    {"id": "lidl",     "name": "Lidl",     "color": "#0050aa"},
    {"id": "kaufland", "name": "Kaufland", "color": "#e30613"},
    {"id": "spar",     "name": "Spar",     "color": "#1e6b3b"},
    {"id": "konzum",   "name": "Konzum",   "color": "#ed1c24"},
    {"id": "dm",       "name": "dm",       "color": "#e31837"},
    {"id": "plodine",  "name": "Plodine",  "color": "#009640"},
]

# ----------------------------------------------------------------------------
# SUPABASE — plain requests
# verify=False fixes SSL handshake failures on Render (stale CA bundle).
# ----------------------------------------------------------------------------

def _db_headers():
    return {
        "apikey":        Config.SUPABASE_KEY,
        "Authorization": f"Bearer {Config.SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal",
    }

def _sb_get(path, params=None):
    r = requests.get(
        f"{Config.SUPABASE_URL}{path}",
        headers=_db_headers(), params=params,
        timeout=20, verify=False
    )
    r.raise_for_status()
    return r.json()

def _sb_post(path, data):
    r = requests.post(
        f"{Config.SUPABASE_URL}{path}",
        headers=_db_headers(), json=data,
        timeout=20, verify=False
    )
    r.raise_for_status()
    return r

def _sb_patch(path, data):
    r = requests.patch(
        f"{Config.SUPABASE_URL}{path}",
        headers=_db_headers(), json=data,
        timeout=20, verify=False
    )
    r.raise_for_status()
    return r

def _sb_storage_put(path, img_bytes):
    r = requests.put(
        f"{Config.SUPABASE_URL}/storage/v1/object/{Config.STORAGE_BUCKET}/{path}",
        headers={
            "apikey":        Config.SUPABASE_KEY,
            "Authorization": f"Bearer {Config.SUPABASE_KEY}",
            "Content-Type":  "image/jpeg",
            "x-upsert":      "true",
        },
        data=img_bytes, timeout=30, verify=False
    )
    r.raise_for_status()
    return f"{Config.SUPABASE_URL}/storage/v1/object/public/{Config.STORAGE_BUCKET}/{path}"


# ----------------------------------------------------------------------------
# PRODUCTS
# ----------------------------------------------------------------------------

def get_products(store=None, query=None, limit=50):
    today = date.today().isoformat()
    try:
        params = {
            "valid_from":  f"lte.{today}",
            "valid_until": f"gte.{today}",
            "is_expired":  "is.false",
            "limit":       limit,
            "order":       "store,product",
        }
        if store:
            params["store"] = f"eq.{store}"
        if query:
            query = re.sub(r"[^a-zA-Z0-9\sčćšđžČĆŠĐŽ]", "", query)
            params["product"] = f"ilike.*{query}*"

        return _sb_get("/rest/v1/products", params) or []

    except Exception as e:
        logger.error(f"get_products failed: {e}")
        return []


def save_products(products, store, page_num, page_url, catalogue_name, valid_from, valid_until):
    """Bulk-insert products for one catalogue page."""
    if not products:
        return 0

    records = [
        {
            "store":            store,
            "product":          p.get("product", ""),
            "brand":            p.get("brand"),
            "quantity":         p.get("quantity"),
            "original_price":   p.get("original_price"),
            "sale_price":       p.get("sale_price"),
            "discount_percent": p.get("discount_percent"),
            "category":         p.get("category", "Other"),
            "valid_from":       valid_from,
            "valid_until":      valid_until,
            "page_image_url":   page_url,
            "page_number":      page_num,
            "catalogue_name":   catalogue_name,
        }
        for p in products
        if p.get("sale_price")
    ]

    if not records:
        return 0

    try:
        _sb_post("/rest/v1/products", records)
        logger.info(f"Saved {len(records)} products from page {page_num}")
        return len(records)
    except Exception as e:
        logger.error(f"save_products failed on page {page_num}: {e}")
        return 0


# ----------------------------------------------------------------------------
# JOBS
# ----------------------------------------------------------------------------

def create_job(job_id, store, catalogue_name, valid_from, valid_until, total_pages):
    try:
        _sb_post("/rest/v1/jobs", {
            "id":             job_id,
            "store":          store,
            "catalogue_name": catalogue_name,
            "valid_from":     valid_from,
            "valid_until":    valid_until,
            "total_pages":    total_pages,
            "current_page":   0,
            "total_products": 0,
            "status":         "processing",
            "created_at":     datetime.now().isoformat(),
        })
        return True
    except Exception as e:
        logger.error(f"create_job failed: {e}")
        return False


def update_job(job_id, **fields):
    try:
        _sb_patch(f"/rest/v1/jobs?id=eq.{job_id}", fields)
    except Exception as e:
        logger.error(f"update_job failed for {job_id}: {e}")


def get_job(job_id):
    try:
        data = _sb_get(f"/rest/v1/jobs?id=eq.{job_id}")
        return data[0] if data else None
    except Exception as e:
        logger.error(f"get_job failed for {job_id}: {e}")
        return None


# ----------------------------------------------------------------------------
# IMAGE STORAGE
# ----------------------------------------------------------------------------

def upload_image(img_bytes, path):
    try:
        public_url = _sb_storage_put(path, img_bytes)
        logger.info(f"Image uploaded: {public_url}")
        return public_url
    except Exception as e:
        logger.error(f"upload_image failed: {e}")
        return None


# ----------------------------------------------------------------------------
# GEMINI  (still uses requests — no official Python client for Gemini REST)
# API key is never logged; scrubbed from any exception messages.
# ----------------------------------------------------------------------------

_GEMINI_BASE = (
    "https://generativelanguage.googleapis.com"
    "/v1beta/models/gemini-2.5-flash:generateContent"
)


def _gemini_url():
    return f"{_GEMINI_BASE}?key={Config.GEMINI_API_KEY}"


def _scrub(text):
    """Remove the API key from any string before logging."""
    if Config.GEMINI_API_KEY and Config.GEMINI_API_KEY in text:
        return text.replace(Config.GEMINI_API_KEY, "***")
    return text


def extract_products(img_b64, store, page):
    if not Config.GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY not set")
        return []

    prompt = f"""
Extract ALL products from this catalog page.

Store: {store}
Page: {page}

Return a JSON array only — no markdown, no explanation. Each item must have:
- product: name (translate to English)
- brand: brand name or null
- sale_price: current price in euros
- original_price: original price or null
- quantity: size/weight or null
- discount_percent: discount % or null
- category: one of [Meat and Fish, Dairy, Bread and Bakery,
  Fruit and Vegetables, Drinks, Snacks and Sweets, Other]

Example:
[{{"product":"Milk 1L","brand":"Z'bregov","sale_price":"0.99",
   "original_price":"1.29","quantity":"1L","discount_percent":"23%",
   "category":"Dairy"}}]

If no products are visible, return [].
"""

    body = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}},
            {"text": prompt},
        ]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 4096},
    }

    for attempt in range(3):
        try:
            r = requests.post(_gemini_url(), json=body, timeout=90)

            if r.status_code != 200:
                logger.error(f"Gemini HTTP {r.status_code} on attempt {attempt+1}")
                time.sleep(2 ** attempt)
                continue

            result = r.json()

            if "candidates" not in result:
                logger.error(f"Gemini missing candidates on attempt {attempt+1}")
                time.sleep(2 ** attempt)
                continue

            text = result["candidates"][0]["content"]["parts"][0]["text"]
            match = re.search(r"\[.*\]", text, re.DOTALL)

            if not match:
                logger.error(f"No JSON array in Gemini response (page {page})")
                continue

            products = json.loads(match.group())
            if isinstance(products, list):
                logger.info(f"Extracted {len(products)} products from page {page}")
                return products

        except json.JSONDecodeError as e:
            logger.error(f"Gemini JSON parse error attempt {attempt+1}: {e}")
        except Exception as e:
            logger.error(f"Gemini attempt {attempt+1} failed: {_scrub(str(e))}")
            if attempt < 2:
                time.sleep(2 ** attempt)

    return []


def ask_ai(message, products):
    if not Config.GEMINI_API_KEY:
        return "Pronašao sam neke proizvode. Upiši broj stranice da vidiš sliku."

    context = "\n".join(
        f"- {p.get('store')}: {p.get('product')} - {p.get('sale_price')}€ "
        f"(str. {p.get('page_number')})"
        for p in products[:5]
    )

    prompt = f"""You are a helpful shopping assistant for Croatia.
Today is {date.today().strftime('%d.%m.%Y.')}

User question: {message}

Products found:
{context}

Instructions:
- Respond in Croatian
- Be friendly and helpful
- Mention store names and page numbers
- End with "Stranice: X, Y, Z" if products have page numbers
"""

    body = {"contents": [{"parts": [{"text": prompt}]}]}

    try:
        r = requests.post(_gemini_url(), json=body, timeout=60)
        result = r.json()
        return result["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        logger.error(f"ask_ai failed: {_scrub(str(e))}")
        return "Dogodila se greška."


# ----------------------------------------------------------------------------
# PDF PROCESSOR  (runs in background daemon thread)
# ----------------------------------------------------------------------------

def process_catalog(job_id, pdf_path, store, valid_from, valid_until, catalogue_name):
    doc = None
    try:
        doc = fitz.open(pdf_path)
        total_pages    = len(doc)
        total_products = 0

        for page_num in range(total_pages):
            try:
                logger.info(f"Processing page {page_num+1}/{total_pages}")

                page      = doc[page_num]
                pix       = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
                img_bytes = pix.tobytes("jpeg")
                img_b64   = base64.b64encode(img_bytes).decode()

                safe_store   = store.lower().replace(" ", "_")
                safe_name    = catalogue_name.lower().replace(" ", "_")
                filename     = f"{safe_store}_{safe_name}_page_{str(page_num+1).zfill(3)}.jpg"
                storage_path = f"{safe_store}/{valid_from}/{filename}"

                page_url       = upload_image(img_bytes, storage_path)
                products       = extract_products(img_b64, store, page_num + 1)
                saved          = save_products(
                    products, store, page_num + 1,
                    page_url, catalogue_name, valid_from, valid_until,
                )
                total_products += saved

                update_job(job_id, current_page=page_num + 1, total_products=total_products)
                logger.info(f"Page {page_num+1} done: {saved} products")

            except Exception:
                logger.exception(f"Page {page_num+1} failed — skipping")
                continue

        update_job(job_id, status="done")
        logger.info(f"Job {job_id} complete: {total_products} products")

    except Exception as e:
        logger.error(f"Job {job_id} crashed: {e}")
        update_job(job_id, status="error")

    finally:
        if doc:
            doc.close()
        try:
            os.remove(pdf_path)
            logger.info(f"Cleaned up: {pdf_path}")
        except Exception as e:
            logger.error(f"Failed to delete temp file: {e}")


# ----------------------------------------------------------------------------
# ROUTES
# ----------------------------------------------------------------------------

@app.route("/")
def home():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/country")
def get_country():
    return jsonify({
        "code":        "hr",
        "name":        "croatia",
        "language":    "hr",
        "currency":    "€",
        "date_format": "%d.%m.%Y.",
        "stores":      CROATIA_STORES,
    })


@app.route("/api/products")
def api_products():
    store = request.args.get("store")
    query = request.args.get("q")
    page  = request.args.get("page", type=int)

    if page:
        try:
            params = {"page_number": f"eq.{page}", "limit": 50}
            if store:
                params["store"] = f"eq.{store}"
            return jsonify(_sb_get("/rest/v1/products", params) or [])
        except Exception as e:
            logger.error(f"api_products page query failed: {e}")
            return jsonify([])

    return jsonify(get_products(store, query))


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data    = request.json or {}
    message = data.get("message", "").strip()

    if not message:
        return jsonify({"error": "Message required"}), 400

    products = get_products(query=message, limit=10)
    reply    = ask_ai(message, products)

    page_numbers = [
        int(p) for p in re.findall(r"stranic[ea] (\d+)", reply, re.IGNORECASE)
        if 1 <= int(p) <= 500
    ]

    enhanced = [
        {**p, "share_url": f"{Config.BASE_URL}/p/{p['id']}" if p.get("id") else None}
        for p in products[:5]
    ]

    return jsonify({"reply": reply, "products": enhanced, "page_numbers": page_numbers[:3]})


# ----------------------------------------------------------------------------
# UPLOAD TOOL
# ----------------------------------------------------------------------------

@app.route("/upload-tool")
def upload_tool():
    if os.path.exists("static/upload-tool.html"):
        return send_from_directory("static", "upload-tool.html")
    return UPLOAD_HTML


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
        button{background:#00ff88;color:#000;border:none;padding:15px;font-size:16px;font-weight:bold;border-radius:5px;cursor:pointer;width:100%}
        button:hover{background:#00cc66}
        button:disabled{background:#444;color:#888;cursor:not-allowed}
        .progress-bar{background:#222;height:30px;border-radius:5px;margin:20px 0;overflow:hidden;display:none}
        .progress-fill{background:#00ff88;height:100%;width:0%;transition:width 0.3s;display:flex;align-items:center;justify-content:center;font-weight:bold;color:#000}
        #log{background:#000;padding:20px;border-radius:5px;font-size:13px;line-height:1.6;max-height:400px;overflow-y:auto;border:1px solid #333}
    </style>
</head>
<body>
    <h1>📤 katalog.ai — Upload Catalog</h1>
    <div class="card">
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
        <input type="text" id="validFrom" placeholder="2026-03-02">
        <label>Valid Until (empty = 14 days auto)</label>
        <input type="text" id="validUntil" placeholder="2026-03-16">
        <button id="uploadBtn" onclick="upload()">Process Catalog</button>
    </div>
    <div class="progress-bar" id="progressBar">
        <div class="progress-fill" id="progressFill">0%</div>
    </div>
    <div id="log">Ready.</div>

    <script>
        let pollInterval = null, totalPages = 0;
        let lastPage = 0, lastProducts = 0;
        let failCount = 0, pollStartTime = null;
        const MAX_FAILS = 5, MAX_MINUTES = 15;

        function log(msg, type="info") {
            const colors = {success:"#00ff88", error:"#ff5555", info:"#66ccff"};
            const el = document.getElementById("log");
            el.innerHTML += `<span style="color:${colors[type]||"#eee"}">${msg}</span><br>`;
            el.scrollTop = el.scrollHeight;
        }

        function stopPolling(msg) {
            clearInterval(pollInterval); pollInterval = null;
            if (msg) log(msg, "error");
            const btn = document.getElementById("uploadBtn");
            btn.disabled = false; btn.textContent = "Process Catalog";
        }

        async function upload() {
            const file = document.getElementById("file").files?.[0];
            if (!file) { log("❌ Select a PDF file", "error"); return; }

            document.getElementById("log").innerHTML = "";
            failCount = 0; lastPage = 0; lastProducts = 0;

            let validFrom = document.getElementById("validFrom").value
                         || new Date().toISOString().split("T")[0];
            let validUntil = document.getElementById("validUntil").value;
            if (!validUntil) {
                const d = new Date(validFrom);
                d.setDate(d.getDate() + 14);
                validUntil = d.toISOString().split("T")[0];
                log(`📅 Auto valid until: ${validUntil}`, "info");
            }

            const btn = document.getElementById("uploadBtn");
            btn.disabled = true; btn.textContent = "Processing...";
            document.getElementById("progressBar").style.display = "block";
            document.getElementById("progressFill").style.width = "0%";
            document.getElementById("progressFill").textContent = "0%";

            const form = new FormData();
            form.append("file", file);
            form.append("store", document.getElementById("store").value);
            form.append("valid_from", validFrom);
            form.append("valid_until", validUntil);

            try {
                log(`📤 Uploading: ${file.name}`, "info");
                const res = await fetch("/upload", { method: "POST", body: form });
                if (!res.ok) {
                    const err = await res.json().catch(() => ({}));
                    throw new Error(err.error || `HTTP ${res.status}`);
                }
                const data = await res.json();
                totalPages = data.pages;
                pollStartTime = Date.now();
                log(`✅ Job started — ${totalPages} pages`, "success");
                log(`🆔 Job ID: ${data.job_id}`, "info");
                if (pollInterval) clearInterval(pollInterval);
                pollInterval = setInterval(() => poll(data.job_id), 2000);
            } catch(e) {
                log(`❌ Upload failed: ${e.message}`, "error");
                stopPolling();
            }
        }

        async function poll(jobId) {
            if (Date.now() - pollStartTime > MAX_MINUTES * 60000) {
                stopPolling(`⏱️ Timed out after ${MAX_MINUTES} minutes.`); return;
            }
            try {
                const res = await fetch(`/status/${jobId}`);
                if (res.status === 404) {
                    if (++failCount >= MAX_FAILS)
                        stopPolling("❌ Job not found — check server logs.");
                    return;
                }
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                failCount = 0;

                const data = await res.json();
                const cur = data.current_page || 0, prods = data.total_products || 0;

                // Pick up total_pages from the job record once background thread writes it
                if (data.total_pages && data.total_pages > 0) totalPages = data.total_pages;

                if (cur > lastPage) {
                    for (let i = lastPage + 1; i <= cur; i++) {
                        const total = totalPages > 0 ? totalPages : "?";
                        let line = `📄 Page ${String(i).padStart(3,"0")} / ${total}`;
                        if (i === cur) line += `  |  +${prods - lastProducts} products  |  total: ${prods}`;
                        log(line, "success");
                    }
                    lastPage = cur; lastProducts = prods;
                    if (totalPages > 0) {
                        const pct = Math.round(cur / totalPages * 100);
                        document.getElementById("progressFill").style.width = pct + "%";
                        document.getElementById("progressFill").textContent = pct + "%";
                    }
                }

                if (data.status === "done") {
                    clearInterval(pollInterval); pollInterval = null;
                    log(`✅ DONE! ${prods} products saved.`, "success");
                    document.getElementById("uploadBtn").disabled = false;
                    document.getElementById("uploadBtn").textContent = "Process Another";
                }
                if (data.status === "error") stopPolling("❌ Job failed — check server logs.");

            } catch(e) {
                if (++failCount >= MAX_FAILS)
                    stopPolling(`❌ Polling stopped: ${e.message}`);
            }
        }
    </script>
</body>
</html>'''


@app.route("/upload", methods=["POST"])
def upload():
    file        = request.files.get("file")
    store       = request.form.get("store")
    valid_from  = request.form.get("valid_from", date.today().isoformat())
    valid_until = request.form.get("valid_until")

    if not file or not store:
        return jsonify({"error": "file and store required"}), 400

    if not valid_until:
        d = datetime.strptime(valid_from, "%Y-%m-%d")
        valid_until = (d + timedelta(days=14)).strftime("%Y-%m-%d")

    # Save PDF — slow for large files. Everything else runs in background
    # so we respond before Render's 30s proxy timeout.
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        file.save(tmp.name)
        pdf_path       = tmp.name
        catalogue_name = file.filename.replace(".pdf", "")

    job_id = str(uuid.uuid4())[:8]

    def run():
        try:
            doc         = fitz.open(pdf_path)
            total_pages = len(doc)
            doc.close()
        except Exception as e:
            logger.error(f"Failed to open PDF: {e}")
            try: os.remove(pdf_path)
            except Exception: pass
            return

        if not create_job(job_id, store, catalogue_name, valid_from, valid_until, total_pages):
            try: os.remove(pdf_path)
            except Exception: pass
            return

        process_catalog(job_id, pdf_path, store, valid_from, valid_until, catalogue_name)

    threading.Thread(target=run, daemon=True).start()

    # Respond immediately — frontend polls /status which has total_pages once job is created
    return jsonify({"job_id": job_id, "pages": 0})


@app.route("/status/<job_id>")
def status(job_id):
    job = get_job(job_id)
    if job:
        return jsonify(job)
    return jsonify({"error": "not found"}), 404


@app.route("/debug/health")
def health():
    return jsonify({
        "status":   "ok",
        "time":     datetime.now().isoformat(),
        "supabase": bool(Config.SUPABASE_KEY),
        "gemini":   bool(Config.GEMINI_API_KEY),
    })


@app.route("/debug/supabase")
def debug_supabase():
    """Hit this in your browser to test Supabase connectivity."""
    results = {}

    # Check env vars are set
    results["SUPABASE_URL_set"]  = bool(Config.SUPABASE_URL)
    results["SUPABASE_KEY_set"]  = bool(Config.SUPABASE_KEY)
    results["SUPABASE_URL"]      = Config.SUPABASE_URL or "NOT SET"

    if not Config.SUPABASE_URL or not Config.SUPABASE_KEY:
        return jsonify(results), 500

    # Try a simple GET
    try:
        r = requests.get(
            f"{Config.SUPABASE_URL}/rest/v1/jobs?limit=1",
            headers={
                "apikey":        Config.SUPABASE_KEY,
                "Authorization": f"Bearer {Config.SUPABASE_KEY}",
            },
            timeout=10,
            verify=False,
        )
        results["status_code"] = r.status_code
        results["response"]    = r.text[:300]
        results["success"]     = r.status_code < 300
    except Exception as e:
        results["error"]   = str(e)
        results["success"] = False

    return jsonify(results)


@app.route("/p/<product_id>")
def product_page(product_id):
    try:
        data = _sb_get(f"/rest/v1/products?id=eq.{product_id}")
        if data:
            return jsonify(data[0])
    except Exception as e:
        logger.error(f"product_page failed: {e}")
    return jsonify({"error": "Product not found"}), 404


# ----------------------------------------------------------------------------
# MAIN
# On Render, use gunicorn — this block is never reached in production:
#   gunicorn katalog:app --bind 0.0.0.0:$PORT --workers 1 --threads 4
# ----------------------------------------------------------------------------

if __name__ == "__main__":
    port   = int(os.environ.get("PORT", 5000))
    is_dev = os.environ.get("FLASK_ENV") == "development"
    logger.info(f"Starting katalog.ai on port {port} (dev={is_dev})")
    app.run(host="0.0.0.0", port=port, debug=is_dev, use_reloader=False)
