"""
katalog.ai backend
FIXED version — all debug report issues resolved
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

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("katalog")

app = Flask(__name__, static_folder="static", static_url_path="/static")


class Config:
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
    SUPABASE_URL = os.environ.get("SUPABASE_URL")
    SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
    STORAGE_BUCKET = "katalog-images"
    BASE_URL = os.environ.get("BASE_URL", "https://botapp-u7qa.onrender.com")


CROATIA_STORES = [
    {"id": "lidl",     "name": "Lidl",     "color": "#0050aa"},
    {"id": "kaufland", "name": "Kaufland", "color": "#e30613"},
    {"id": "spar",     "name": "Spar",     "color": "#1e6b3b"},
    {"id": "konzum",   "name": "Konzum",   "color": "#ed1c24"},
    {"id": "dm",       "name": "dm",       "color": "#e31837"},
    {"id": "plodine",  "name": "Plodine",  "color": "#009640"},
]

# ----------------------------------------------------------------------------
# SUPABASE
# ----------------------------------------------------------------------------


def sb_headers():
    return {
        "apikey": Config.SUPABASE_KEY,
        "Authorization": f"Bearer {Config.SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal"
    }


def supabase_get(path, params=None):
    try:
        url = f"{Config.SUPABASE_URL}{path}"
        r = requests.get(url, headers=sb_headers(), params=params, timeout=20)
        return r
    except Exception as e:
        logger.error(f"Supabase GET failed {e}")
        return None


def supabase_post(path, data):
    try:
        url = f"{Config.SUPABASE_URL}{path}"
        r = requests.post(url, headers=sb_headers(), json=data, timeout=20)
        if r.status_code >= 300:
            logger.error(f"Supabase POST error {r.status_code}: {r.text[:200]}")
            return None
        return r
    except Exception as e:
        logger.error(f"Supabase POST failed {e}")
        return None


def supabase_patch(path, data):
    try:
        url = f"{Config.SUPABASE_URL}{path}"
        r = requests.patch(url, headers=sb_headers(), json=data, timeout=20)
        if r.status_code >= 300:
            logger.error(f"Supabase PATCH error {r.status_code}: {r.text[:200]}")
            return None
        return r
    except Exception as e:
        logger.error(f"Supabase PATCH failed {e}")
        return None


# ----------------------------------------------------------------------------
# PRODUCTS
# ----------------------------------------------------------------------------


def get_products(store=None, query=None, limit=50):
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
        query = re.sub(r"[^a-zA-Z0-9\sčćšđžČĆŠĐŽ]", "", query)
        params["product"] = f"ilike.*{query}*"

    r = supabase_get("/rest/v1/products", params)

    if r and r.status_code == 200:
        return r.json()

    return []


def save_products(products, store, page_num, page_url, catalogue_name, valid_from, valid_until):
    """Save products to database."""
    if not products:
        return 0

    records = []
    for p in products:
        if not p.get('sale_price'):
            continue
        records.append({
            "store": store,
            "product": p.get('product', ''),
            "brand": p.get('brand'),
            "quantity": p.get('quantity'),
            "original_price": p.get('original_price'),
            "sale_price": p.get('sale_price'),
            "discount_percent": p.get('discount_percent'),
            "category": p.get('category', 'Other'),
            "valid_from": valid_from,
            "valid_until": valid_until,
            "page_image_url": page_url,
            "page_number": page_num,
            "catalogue_name": catalogue_name
        })

    if not records:
        return 0

    r = supabase_post("/rest/v1/products", records)
    if r and r.status_code < 300:
        logger.info(f"Saved {len(records)} products from page {page_num}")
        return len(records)
    else:
        logger.error(f"Failed to save {len(records)} products from page {page_num}")
        return 0


# ----------------------------------------------------------------------------
# GEMINI
# ----------------------------------------------------------------------------

# FIX: Build the URL once, never log it (key would be visible in exceptions).
_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"


def _gemini_url():
    return f"{_GEMINI_BASE}?key={Config.GEMINI_API_KEY}"


def extract_products(img_b64, store, page):
    if not Config.GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY not set")
        return []

    prompt = f"""
Extract ALL products from this catalog page.

Store: {store}
Page: {page}

Return JSON array. Each product must have:
- product: name (translate to English)
- brand: brand name or null
- sale_price: current price in €
- original_price: original price or null
- quantity: size/weight or null
- discount_percent: discount % or null
- category: one of [Meat and Fish, Dairy, Bread and Bakery, Fruit and Vegetables, Drinks, Snacks and Sweets, Other]

Example:
[
  {{
    "product": "Milk 1L",
    "brand": "Z'bregov",
    "sale_price": "0.99",
    "original_price": "1.29",
    "quantity": "1L",
    "discount_percent": "23%",
    "category": "Dairy"
  }}
]

If no products, return [].
"""

    body = {
        "contents": [
            {
                "parts": [
                    {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}},
                    {"text": prompt},
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 4096
        }
    }

    for attempt in range(3):
        try:
            r = requests.post(_gemini_url(), json=body, timeout=90)

            if r.status_code != 200:
                # FIX: Log status only — never log the URL (contains API key)
                logger.error(f"Gemini API error on attempt {attempt+1}: status {r.status_code}")
                time.sleep(2 ** attempt)
                continue

            result = r.json()

            if 'candidates' not in result:
                logger.error(f"Gemini invalid response structure on attempt {attempt+1}")
                time.sleep(2 ** attempt)
                continue

            text = result["candidates"][0]["content"]["parts"][0]["text"]

            match = re.search(r'\[.*\]', text, re.DOTALL)
            if not match:
                logger.error(f"No JSON array found in Gemini response (page {page})")
                continue

            products = json.loads(match.group())

            if isinstance(products, list):
                logger.info(f"Extracted {len(products)} products from page {page}")
                return products

        except json.JSONDecodeError as e:
            logger.error(f"Gemini JSON parse failed on attempt {attempt+1}: {e}")
        except Exception as e:
            # FIX: Sanitize exception message in case URL leaks through
            err_msg = str(e)
            if Config.GEMINI_API_KEY and Config.GEMINI_API_KEY in err_msg:
                err_msg = err_msg.replace(Config.GEMINI_API_KEY, "***")
            logger.error(f"Gemini attempt {attempt+1} failed: {err_msg}")
            # FIX: Exponential backoff between retries
            if attempt < 2:
                time.sleep(2 ** attempt)

    return []


def ask_ai(message, products):
    if not Config.GEMINI_API_KEY:
        return "Pronašao sam neke proizvode. Upiši broj stranice da vidiš sliku."

    context = ""
    for p in products[:5]:
        context += f"- {p.get('store')}: {p.get('product')} - {p.get('sale_price')}€ (str. {p.get('page_number')})\n"

    prompt = f"""
You are a helpful shopping assistant for Croatia.
Today is {date.today().strftime('%d.%m.%Y.')}

User question: {message}

Products found:
{context}

Instructions:
- Respond in Croatian
- Be friendly and helpful
- Mention store names and page numbers
- End with "Stranice: X, Y, Z" if products have page numbers

Response:
"""

    body = {"contents": [{"parts": [{"text": prompt}]}]}

    try:
        r = requests.post(_gemini_url(), json=body, timeout=60)
        result = r.json()
        return result["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        err_msg = str(e)
        if Config.GEMINI_API_KEY and Config.GEMINI_API_KEY in err_msg:
            err_msg = err_msg.replace(Config.GEMINI_API_KEY, "***")
        logger.error(f"Chat error: {err_msg}")
        return "Dogodila se greška."


# ----------------------------------------------------------------------------
# IMAGE STORAGE
# ----------------------------------------------------------------------------


def upload_image(img_bytes, path):
    url = f"{Config.SUPABASE_URL}/storage/v1/object/{Config.STORAGE_BUCKET}/{path}"

    headers = {
        "apikey": Config.SUPABASE_KEY,
        "Authorization": f"Bearer {Config.SUPABASE_KEY}",
        "Content-Type": "image/jpeg",
        "x-upsert": "true",
    }

    try:
        r = requests.put(url, headers=headers, data=img_bytes, timeout=30)
        if r.status_code in [200, 201]:
            public_url = f"{Config.SUPABASE_URL}/storage/v1/object/public/{Config.STORAGE_BUCKET}/{path}"
            logger.info(f"Image uploaded: {public_url}")
            return public_url
        else:
            logger.error(f"Image upload failed: status {r.status_code}")
    except Exception as e:
        logger.error(f"Image upload exception: {e}")

    return None


# ----------------------------------------------------------------------------
# PDF PROCESSOR
# ----------------------------------------------------------------------------


def process_catalog(job_id, pdf_path, store, valid_from, valid_until, catalogue_name):
    doc = None
    try:
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        total_products = 0

        for page_num in range(total_pages):
            try:
                logger.info(f"Processing page {page_num+1}/{total_pages}")

                page = doc[page_num]
                pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
                img_bytes = pix.tobytes("jpeg")
                img_b64 = base64.b64encode(img_bytes).decode()

                safe_store = store.lower().replace(' ', '_')
                safe_name = catalogue_name.lower().replace(' ', '_')
                filename = f"{safe_store}_{safe_name}_page_{str(page_num+1).zfill(3)}.jpg"
                storage_path = f"{safe_store}/{valid_from}/{filename}"

                page_url = upload_image(img_bytes, storage_path)
                products = extract_products(img_b64, store, page_num + 1)
                saved = save_products(
                    products, store, page_num + 1,
                    page_url, catalogue_name, valid_from, valid_until
                )
                total_products += saved

                supabase_patch(
                    f"/rest/v1/jobs?id=eq.{job_id}",
                    {
                        "current_page": page_num + 1,
                        "total_products": total_products
                    }
                )

                logger.info(f"Page {page_num+1} done: {saved} products")

            except Exception:
                logger.exception(f"Page {page_num+1} failed — continuing")
                continue

        supabase_patch(f"/rest/v1/jobs?id=eq.{job_id}", {"status": "done"})
        logger.info(f"Job {job_id} completed: {total_products} total products")

    except Exception as e:
        logger.error(f"Job {job_id} failed: {e}")
        supabase_patch(f"/rest/v1/jobs?id=eq.{job_id}", {"status": "error"})

    finally:
        if doc:
            doc.close()
        try:
            os.remove(pdf_path)
            logger.info(f"Cleaned up temp file: {pdf_path}")
        except Exception as e:
            logger.error(f"Failed to delete temp file: {e}")


# ----------------------------------------------------------------------------
# API ROUTES
# ----------------------------------------------------------------------------


@app.route("/")
def home():
    return jsonify({
        "status": "ok",
        "service": "katalog.ai",
        "version": "1.0.0",
        "endpoints": ["/api/country", "/api/products", "/api/chat", "/upload-tool"]
    })


@app.route("/api/country")
def get_country():
    return jsonify({
        "code": "hr",
        "name": "croatia",
        "language": "hr",
        "currency": "€",
        "date_format": "%d.%m.%Y.",
        "stores": CROATIA_STORES
    })


@app.route("/api/products")
def api_products():
    store = request.args.get("store")
    query = request.args.get("q")
    page = request.args.get("page", type=int)

    if page:
        params = {"page_number": f"eq.{page}", "limit": 50}
        if store:
            params["store"] = f"eq.{store}"
        r = supabase_get("/rest/v1/products", params)
        if r and r.status_code == 200:
            return jsonify(r.json())
        return jsonify([])

    return jsonify(get_products(store, query))


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.json
    message = (data.get("message") or "").strip()

    if not message:
        return jsonify({"error": "Message required"}), 400

    products = get_products(query=message, limit=10)
    reply = ask_ai(message, products)

    page_numbers = re.findall(r'stranic[ea] (\d+)', reply, re.IGNORECASE)
    page_numbers = [int(p) for p in page_numbers if 1 <= int(p) <= 500]

    enhanced_products = []
    for p in products[:5]:
        product_copy = p.copy()
        product_copy['share_url'] = (
            f"{Config.BASE_URL}/p/{p['id']}" if p.get('id') else None
        )
        enhanced_products.append(product_copy)

    return jsonify({
        "reply": reply,
        "products": enhanced_products,
        "page_numbers": page_numbers[:3]
    })


# ----------------------------------------------------------------------------
# UPLOAD TOOL
# ----------------------------------------------------------------------------


@app.route('/upload-tool')
def upload_tool():
    if os.path.exists('static/upload-tool.html'):
        return send_from_directory('static', 'upload-tool.html')
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
        .success{color:#00ff88}
        .error{color:#ff5555}
        .info{color:#66ccff}
    </style>
</head>
<body>
    <h1>📤 katalog.ai - Upload Catalog</h1>

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
        let pollInterval = null;
        let totalPages = 0;
        let lastPage = 0;
        let lastProducts = 0;

        // FIX: Track consecutive failures so we can stop polling gracefully
        let failCount = 0;
        const MAX_POLL_FAILS = 5;
        const MAX_POLL_MINUTES = 15;
        let pollStartTime = null;

        function log(msg, type='info') {
            const colors = {success:'#00ff88', error:'#ff5555', info:'#66ccff'};
            const logDiv = document.getElementById('log');
            logDiv.innerHTML += `<span style="color:${colors[type]||'#eee'}">${msg}</span><br>`;
            logDiv.scrollTop = logDiv.scrollHeight;
        }

        function stopPolling(reason) {
            if (pollInterval) {
                clearInterval(pollInterval);
                pollInterval = null;
            }
            if (reason) log(reason, 'error');
            const btn = document.getElementById('uploadBtn');
            btn.disabled = false;
            btn.textContent = 'Process Catalog';
        }

        function validate() {
            const file = document.getElementById('file');
            if (!file.files || !file.files[0]) {
                log('❌ Select a PDF file', 'error');
                return false;
            }
            return true;
        }

        async function upload() {
            if (!validate()) return;

            document.getElementById('log').innerHTML = '';
            failCount = 0;
            lastPage = 0;
            lastProducts = 0;

            const file = document.getElementById('file').files[0];
            const store = document.getElementById('store').value;
            let validFrom = document.getElementById('validFrom').value;
            let validUntil = document.getElementById('validUntil').value;

            if (!validFrom) {
                validFrom = new Date().toISOString().split('T')[0];
                document.getElementById('validFrom').value = validFrom;
            }

            if (!validUntil) {
                const d = new Date(validFrom);
                d.setDate(d.getDate() + 14);
                validUntil = d.toISOString().split('T')[0];
                log(`📅 Auto valid until: ${validUntil}`, 'info');
            }

            const btn = document.getElementById('uploadBtn');
            btn.disabled = true;
            btn.textContent = 'Processing...';

            document.getElementById('progressBar').style.display = 'block';
            document.getElementById('progressFill').style.width = '0%';
            document.getElementById('progressFill').textContent = '0%';

            const formData = new FormData();
            formData.append('file', file);
            formData.append('store', store);
            formData.append('valid_from', validFrom);
            formData.append('valid_until', validUntil);

            try {
                log(`📤 Uploading: ${file.name}`, 'info');

                const response = await fetch('/upload', {
                    method: 'POST',
                    body: formData
                });

                if (!response.ok) {
                    const err = await response.json().catch(() => ({}));
                    throw new Error(err.error || `HTTP ${response.status}`);
                }

                const data = await response.json();
                totalPages = data.pages;
                pollStartTime = Date.now();

                log(`✅ Job started: ${totalPages} pages`, 'success');
                log(`🆔 Job ID: ${data.job_id}`, 'info');

                if (pollInterval) clearInterval(pollInterval);
                pollInterval = setInterval(() => poll(data.job_id), 2000);

            } catch (e) {
                log(`❌ Upload failed: ${e.message}`, 'error');
                stopPolling();
            }
        }

        async function poll(jobId) {
            // FIX: Hard timeout — stop after MAX_POLL_MINUTES regardless
            if (pollStartTime && (Date.now() - pollStartTime) > MAX_POLL_MINUTES * 60 * 1000) {
                stopPolling(`⏱️ Timed out after ${MAX_POLL_MINUTES} minutes.`);
                return;
            }

            try {
                const response = await fetch(`/status/${jobId}`);

                // FIX: Handle 404 explicitly with retry limit instead of looping forever
                if (response.status === 404) {
                    failCount++;
                    if (failCount >= MAX_POLL_FAILS) {
                        stopPolling(`❌ Job not found after ${MAX_POLL_FAILS} attempts. The job may not have been created — check server logs.`);
                    }
                    return;
                }

                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }

                // Reset fail counter on a successful response
                failCount = 0;

                const data = await response.json();
                const currentPage = data.current_page || 0;
                const currentProducts = data.total_products || 0;

                if (currentPage > lastPage) {
                    for (let i = lastPage + 1; i <= currentPage; i++) {
                        let line = `📄 Page ${String(i).padStart(3,'0')} / ${totalPages}`;
                        if (i === currentPage) {
                            line += `  |  +${currentProducts - lastProducts} products  |  total: ${currentProducts}`;
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
                    pollInterval = null;
                    log(`✅ DONE! ${currentProducts} products saved.`, 'success');
                    const btn = document.getElementById('uploadBtn');
                    btn.disabled = false;
                    btn.textContent = 'Process Another';
                }

                if (data.status === 'error') {
                    stopPolling('❌ Job failed on the server. Check logs.');
                }

            } catch (e) {
                failCount++;
                if (failCount >= MAX_POLL_FAILS) {
                    stopPolling(`❌ Polling stopped after ${MAX_POLL_FAILS} errors: ${e.message}`);
                }
            }
        }
    </script>
</body>
</html>'''


@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("file")
    store = request.form.get("store")
    valid_from = request.form.get("valid_from", date.today().strftime("%Y-%m-%d"))
    valid_until = request.form.get("valid_until")

    if not file or not store:
        return jsonify({"error": "file and store required"}), 400

    if not valid_until:
        d = datetime.strptime(valid_from, "%Y-%m-%d")
        valid_until = (d + timedelta(days=14)).strftime("%Y-%m-%d")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        file.save(tmp.name)
        pdf_path = tmp.name
        catalogue_name = file.filename.replace('.pdf', '')

    doc = fitz.open(pdf_path)
    total_pages = len(doc)
    doc.close()

    job_id = str(uuid.uuid4())[:8]

    r = supabase_post("/rest/v1/jobs", {
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

    # FIX: Return 500 if job creation failed — don't hand out a job_id
    # the frontend can never poll successfully
    if not r or r.status_code >= 300:
        logger.error("Failed to create job in Supabase — aborting upload")
        try:
            os.remove(pdf_path)
        except Exception:
            pass
        return jsonify({"error": "Failed to create job. Check Supabase connection."}), 500

    # FIX: daemon=True so the thread doesn't block process shutdown on Render
    thread = threading.Thread(
        target=process_catalog,
        args=(job_id, pdf_path, store, valid_from, valid_until, catalogue_name),
        daemon=True
    )
    thread.start()

    return jsonify({"job_id": job_id, "pages": total_pages})


@app.route("/status/<job_id>")
def status(job_id):
    r = supabase_get(f"/rest/v1/jobs?id=eq.{job_id}")
    if r and r.status_code == 200:
        data = r.json()
        if data:
            return jsonify(data[0])
    return jsonify({"error": "not found"}), 404


@app.route("/debug/health")
def health():
    return jsonify({
        "status": "ok",
        "time": datetime.now().isoformat(),
        "supabase": bool(Config.SUPABASE_KEY),
        "gemini": bool(Config.GEMINI_API_KEY)
    })


@app.route("/p/<product_id>")
def product_page(product_id):
    """Shareable product page."""
    r = supabase_get(f"/rest/v1/products?id=eq.{product_id}")
    if r and r.status_code == 200 and r.json():
        return jsonify(r.json()[0])
    return jsonify({"error": "Product not found"}), 404


# ----------------------------------------------------------------------------
# MAIN
# FIX: debug=True removed — caused Werkzeug reloader to conflict with threading,
#      producing "maximum recursion depth exceeded" on every Supabase call.
#      On Render, use gunicorn as the start command instead of running this file:
#        gunicorn katalog:app --bind 0.0.0.0:$PORT --workers 1 --threads 4
# ----------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    is_dev = os.environ.get("FLASK_ENV") == "development"
    logger.info(f"Starting katalog.ai on port {port} (dev={is_dev})")
    # use_reloader=False is a safety net; on Render this block is never reached
    app.run(host="0.0.0.0", port=port, debug=is_dev, use_reloader=False)
