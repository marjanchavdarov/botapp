"""
katalog.ai — Upload Tool (standalone)
Separate service from main app. Deploy on its own Render instance.

Requirements: flask flask-cors requests pymupdf gunicorn
Start command: gunicorn upload:app --worker-class gthread -w 1 --threads 4 --bind 0.0.0.0:$PORT
Root directory: backend

Env vars needed:
  SUPABASE_URL
  SUPABASE_KEY
  SUPABASE_SERVICE_KEY  (optional but recommended)
  GEMINI_API_KEY
  UPLOAD_PASSWORD       (protect the tool — set anything you want)
"""

import os, json, uuid, base64, logging, threading, tempfile, time, re, io
from datetime import datetime, date, timedelta
import requests, fitz
try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
from flask import Flask, request, jsonify, make_response
from flask_cors import CORS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("upload")

app = Flask(__name__)
CORS(app)

# ── CONFIG ──────────────────────────────────────────────────────────────────
class Config:
    SUPABASE_URL         = os.environ.get("SUPABASE_URL")
    SUPABASE_KEY         = os.environ.get("SUPABASE_KEY")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
    GEMINI_API_KEY       = os.environ.get("GEMINI_API_KEY")
    STORAGE_BUCKET       = "katalog-images"
    UPLOAD_PASSWORD      = os.environ.get("UPLOAD_PASSWORD", "katalog2026")

CROATIA_STORES = ["lidl","kaufland","spar","konzum","dm","plodine","tommy","ntl"]

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

def _sb_post(path, data):
    r = requests.post(f"{Config.SUPABASE_URL}{path}", headers=_headers(),
                      json=data, timeout=20, verify=False)
    r.raise_for_status()
    return r

def _sb_patch(path, data):
    r = requests.patch(f"{Config.SUPABASE_URL}{path}", headers=_headers(),
                       json=data, timeout=20, verify=False)
    r.raise_for_status()
    return r

def _sb_storage_put(path, img_bytes):
    key = Config.SUPABASE_SERVICE_KEY or Config.SUPABASE_KEY
    url = f"{Config.SUPABASE_URL}/storage/v1/object/{Config.STORAGE_BUCKET}/{path}"
    r = requests.put(url, headers={
        "apikey": key, "Authorization": f"Bearer {key}",
        "Content-Type": "image/jpeg", "x-upsert": "true",
    }, data=img_bytes, timeout=30, verify=False)
    logger.info(f"Storage {r.status_code} — {r.text[:200]}")
    if not r.ok:
        raise Exception(f"Storage {r.status_code}: {r.text[:300]}")
    return f"{Config.SUPABASE_URL}/storage/v1/object/public/{Config.STORAGE_BUCKET}/{path}"

# ── JOBS ─────────────────────────────────────────────────────────────────────
def create_job(job_id, store, catalogue_name, valid_from, valid_until, total_pages):
    try:
        _sb_post("/rest/v1/jobs", {
            "id": job_id, "store": store, "catalogue_name": catalogue_name,
            "valid_from": valid_from, "valid_until": valid_until,
            "total_pages": total_pages, "current_page": 0,
            "total_products": 0, "status": "processing",
            "created_at": datetime.now().isoformat(),
        })
        return True
    except Exception as e:
        logger.error(f"create_job failed: {e}")
        return False

def update_job(job_id, **fields):
    try:
        _sb_patch(f"/rest/v1/jobs?id=eq.{job_id}", fields)
    except Exception as e:
        logger.error(f"update_job failed: {e}")

def get_job(job_id):
    try:
        data = _sb_get(f"/rest/v1/jobs?id=eq.{job_id}")
        return data[0] if data else None
    except Exception as e:
        logger.error(f"get_job failed: {e}")
        return None

# ── GEMINI ───────────────────────────────────────────────────────────────────
_GEMINI_URL = (
    "https://generativelanguage.googleapis.com"
    "/v1beta/models/gemini-2.5-flash:generateContent"
)


def get_fewshot_examples(store, limit=3):
    """Fetch saved annotation examples for this store."""
    try:
        r = requests.get(
            f"{Config.SUPABASE_URL}/rest/v1/annotations",
            headers={
                "apikey":        Config.SUPABASE_KEY,
                "Authorization": f"Bearer {Config.SUPABASE_KEY}",
                "Content-Type":  "application/json",
            },
            params={"store": f"eq.{store}", "order": "created_at.desc", "limit": limit,
                    "select": "page_image_url,boxes,page_number"},
            timeout=10, verify=False
        )
        if r.ok:
            return r.json() or []
    except Exception as e:
        logger.error(f"get_fewshot_examples: {e}")
    return []


def crop_image(img_bytes, x1, y1, x2, y2, padding=12):
    """Crop a region from image bytes. Returns JPEG bytes or None."""
    if not PIL_AVAILABLE:
        return None
    try:
        img = Image.open(io.BytesIO(img_bytes))
        w, h = img.size
        x1 = max(0, int(x1 * w) - padding)
        y1 = max(0, int(y1 * h) - padding)
        x2 = min(w, int(x2 * w) + padding)
        y2 = min(h, int(y2 * h) + padding)
        if (x2 - x1) < 15 or (y2 - y1) < 15:
            return None
        cropped = img.crop((x1, y1, x2, y2))
        out = io.BytesIO()
        cropped.save(out, format='JPEG', quality=88)
        return out.getvalue()
    except Exception as e:
        logger.error(f"crop_image failed: {e}")
        return None


def extract_products(img_b64, store, page, examples=None):
    """
    Single Gemini call that returns product data + bounding boxes together.
    This replaces the separate upload + crop pipeline.
    """
    if not Config.GEMINI_API_KEY:
        return []

    # Build few-shot hint from annotations
    fewshot_hint = ""
    if examples:
        parts = []
        for ex in examples[:3]:
            boxes = ex.get("boxes", [])
            clean = [{"x1":b["x1"],"y1":b["y1"],"x2":b["x2"],"y2":b["y2"]} for b in boxes]
            parts.append(
                f"Page {ex.get('page_number','?')} ({len(clean)} products): "
                + json.dumps(clean[:3])  # show first 3 boxes as layout example
            )
        if parts:
            fewshot_hint = (
                "\n\nLayout examples from same store (bounding boxes normalized 0.0-1.0):\n"
                + "\n".join(parts)
                + "\nUse these to understand the page layout and find ALL products."
            )

    prompt = f"""Izvuci SVE proizvode s ove stranice kataloga.
Trgovina: {store} / Stranica: {page}

Vrati SAMO JSON niz bez markdowna. Svaki proizvod mora imati:
- product: naziv na HRVATSKOM
- brand: marka ili null
- sale_price: trenutna cijena u eurima (string, npr "2.99")
- original_price: originalna cijena ili null
- quantity: gramaza/komadi ili null
- discount_percent: popust % ili null
- category: jedna od [Meso i riba, Mliječni, Kruh i pekarski, Voće i povrće, Pića, Grickalice i slatkiši, Kućanstvo, Osobna njega, Ostalo]
- bbox: koordinate proizvoda na slici kao {{"x1":0.0,"y1":0.0,"x2":1.0,"y2":1.0}} (normalizirane 0.0-1.0, uključi sliku I cjenik)

Ako nema proizvoda vrati [].{fewshot_hint}

Primjer jednog proizvoda:
{{"product":"Gloria kava","brand":"Gloria","sale_price":"7.49","original_price":"9.49","quantity":"500g","discount_percent":"21%","category":"Pića","bbox":{{"x1":0.05,"y1":0.08,"x2":0.48,"y2":0.55}}}}
"""

    body = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}},
            {"text": prompt},
        ]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 8192},
    }

    for attempt in range(3):
        try:
            r = requests.post(f"{_GEMINI_URL}?key={Config.GEMINI_API_KEY}",
                              json=body, timeout=120)
            if r.status_code != 200:
                logger.error(f"Gemini {r.status_code} attempt {attempt+1}")
                time.sleep(2 ** attempt)
                continue
            result = r.json()
            if "candidates" not in result:
                time.sleep(2 ** attempt)
                continue
            text = result["candidates"][0]["content"]["parts"][0]["text"]
            text = re.sub(r"```json|```", "", text).strip()
            match = re.search(r"\[.*\]", text, re.DOTALL)
            if not match:
                logger.warning(f"No JSON on page {page}: {text[:150]}")
                continue
            products = json.loads(match.group())
            if isinstance(products, list):
                logger.info(f"Page {page}: {len(products)} products extracted")
                return products
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error attempt {attempt+1}: {e}")
        except Exception as e:
            logger.error(f"Gemini attempt {attempt+1}: {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)
    return []

# ── PRODUCTS ─────────────────────────────────────────────────────────────────
def save_products(products, store, page_num, page_url, catalogue_name, valid_from, valid_until):
    records = [
        {
            "store": store, "product": p.get("product",""),
            "brand": p.get("brand"), "quantity": p.get("quantity"),
            "original_price": p.get("original_price"),
            "sale_price": p.get("sale_price"),
            "discount_percent": p.get("discount_percent"),
            "category": p.get("category","Ostalo"),
            "valid_from": valid_from, "valid_until": valid_until,
            "page_image_url": page_url, "page_number": page_num,
            "catalogue_name": catalogue_name,
            "product_image_url": p.get("product_image_url"),  # cropped image
        }
        for p in products if p.get("sale_price")
    ]
    if not records:
        return 0
    try:
        _sb_post("/rest/v1/products", records)
        return len(records)
    except Exception as e:
        logger.error(f"save_products page {page_num}: {e}")
        return 0

# ── PROCESSOR ────────────────────────────────────────────────────────────────
def process_catalog(job_id, pdf_path, store, valid_from, valid_until, catalogue_name):
    doc = None
    try:
        doc = fitz.open(pdf_path)
        total = len(doc)
        total_products = 0

        # Fetch annotation examples once for this store
        fewshot_examples = get_fewshot_examples(store)
        if fewshot_examples:
            logger.info(f"Using {len(fewshot_examples)} annotation examples for {store}")

        for i in range(total):
            try:
                logger.info(f"Page {i+1}/{total}")
                page     = doc[i]
                pix      = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
                img_bytes = pix.tobytes("jpeg")
                img_b64   = base64.b64encode(img_bytes).decode()

                safe_store = store.lower().replace(" ","_")
                safe_name  = catalogue_name.lower().replace(" ","_")
                filename   = f"{safe_store}_{safe_name}_page_{str(i+1).zfill(3)}.jpg"
                path       = f"{safe_store}/{valid_from}/{filename}"

                page_url = None
                try:
                    page_url = _sb_storage_put(path, img_bytes)
                except Exception as e:
                    logger.error(f"Image upload failed page {i+1}: {e}")

                # One Gemini call: extract products + bounding boxes
                products = extract_products(img_b64, store, i+1, examples=fewshot_examples)

                # Crop each product immediately using bbox from Gemini
                safe_store = store.lower().replace(" ","_")
                safe_name  = catalogue_name.lower().replace(" ","_")
                for prod in products:
                    bbox = prod.get("bbox")
                    if not bbox or not prod.get("sale_price"):
                        continue
                    try:
                        x1 = float(bbox.get("x1", 0))
                        y1 = float(bbox.get("y1", 0))
                        x2 = float(bbox.get("x2", 1))
                        y2 = float(bbox.get("y2", 1))
                        if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
                            continue
                        cropped = crop_image(img_bytes, x1, y1, x2, y2)
                        if cropped:
                            crop_id   = str(uuid.uuid4())[:8]
                            crop_path = f"product-images/{safe_store}/{safe_name}/{crop_id}.jpg"
                            prod["product_image_url"] = _sb_storage_put(crop_path, cropped)
                    except Exception as e:
                        logger.error(f"Crop failed page {i+1}: {e}")

                saved = save_products(products, store, i+1, page_url,
                                      catalogue_name, valid_from, valid_until)
                total_products += saved
                update_job(job_id, current_page=i+1, total_products=total_products)
                logger.info(f"Page {i+1} done: {saved} products")

            except Exception:
                logger.exception(f"Page {i+1} failed — skipping")

        update_job(job_id, status="done")
        logger.info(f"Job {job_id} complete: {total_products} products")

    except Exception as e:
        logger.error(f"Job {job_id} crashed: {e}")
        update_job(job_id, status="error")
    finally:
        if doc: doc.close()
        try: os.remove(pdf_path)
        except Exception: pass

# ── ROUTES ───────────────────────────────────────────────────────────────────
@app.route("/")
def home():
    return make_response(UPLOAD_HTML, 200, {"Content-Type": "text/html"})

@app.route("/upload", methods=["POST"])
def upload():
    # Password check
    password = request.form.get("password","")
    if password != Config.UPLOAD_PASSWORD:
        return jsonify({"error": "Wrong password"}), 403

    file        = request.files.get("file")
    store       = request.form.get("store","").lower()
    valid_from  = request.form.get("valid_from", date.today().isoformat())
    valid_until = request.form.get("valid_until","")

    if not file or not store:
        return jsonify({"error": "file and store required"}), 400
    if not valid_until:
        d = datetime.strptime(valid_from, "%Y-%m-%d")
        valid_until = (d + timedelta(days=14)).strftime("%Y-%m-%d")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        file.save(tmp.name)
        pdf_path = tmp.name
        catalogue_name = file.filename.replace(".pdf","")

    job_id = str(uuid.uuid4())[:8]

    def run():
        try:
            doc = fitz.open(pdf_path)
            total_pages = len(doc)
            doc.close()
        except Exception as e:
            logger.error(f"PDF open failed: {e}")
            try: os.remove(pdf_path)
            except: pass
            return
        if not create_job(job_id, store, catalogue_name, valid_from, valid_until, total_pages):
            try: os.remove(pdf_path)
            except: pass
            return
        process_catalog(job_id, pdf_path, store, valid_from, valid_until, catalogue_name)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id, "pages": 0})

@app.route("/status/<job_id>")
def status(job_id):
    job = get_job(job_id)
    return jsonify(job) if job else (jsonify({"error": "not found"}), 404)

@app.route("/debug/health")
def health():
    return jsonify({"status": "ok", "service": "katalog-upload",
                    "time": datetime.now().isoformat()})

# ── HTML ─────────────────────────────────────────────────────────────────────
UPLOAD_HTML = '''<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>katalog.ai — Upload</title>
  <style>
    *{margin:0;padding:0;box-sizing:border-box;font-family:monospace}
    body{background:#111;color:#eee;padding:40px;max-width:800px;margin:0 auto}
    h1{color:#00ff88;margin-bottom:8px;font-size:24px}
    .sub{color:#666;margin-bottom:30px;font-size:13px}
    .card{background:#1a1a1a;border-radius:10px;padding:20px;margin-bottom:20px;border:1px solid #333}
    label{color:#aaa;font-size:11px;text-transform:uppercase;letter-spacing:1px;display:block;margin-bottom:5px}
    input,select{width:100%;padding:12px;background:#222;border:1px solid #444;color:#eee;border-radius:5px;margin-bottom:15px;font-size:15px;font-family:monospace}
    input[type=password]{letter-spacing:3px}
    button{background:#00ff88;color:#000;border:none;padding:15px;font-size:16px;font-weight:bold;border-radius:5px;cursor:pointer;width:100%;font-family:monospace}
    button:hover{background:#00cc66}
    button:disabled{background:#333;color:#666;cursor:not-allowed}
    .progress-bar{background:#222;height:28px;border-radius:5px;margin:16px 0;overflow:hidden;display:none;border:1px solid #333}
    .progress-fill{background:linear-gradient(90deg,#00ff88,#00cc66);height:100%;width:0%;transition:width .3s;display:flex;align-items:center;justify-content:center;font-weight:bold;color:#000;font-size:13px;font-family:monospace}
    #log{background:#000;padding:16px;border-radius:5px;font-size:12px;line-height:1.7;max-height:400px;overflow-y:auto;border:1px solid #222;margin-top:16px}
    .row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  </style>
</head>
<body>
  <h1>⚡ katalog.ai Upload</h1>
  <div class="sub">Admin panel — scan PDF catalogues into the database</div>

  <div class="card">
    <label>Password</label>
    <input type="password" id="password" placeholder="••••••••">

    <label>PDF File</label>
    <input type="file" id="file" accept=".pdf">

    <label>Store</label>
    <select id="store">
      <option value="konzum">Konzum</option>
      <option value="lidl">Lidl</option>
      <option value="kaufland">Kaufland</option>
      <option value="spar">Spar</option>
      <option value="dm">dm</option>
      <option value="plodine">Plodine</option>
      <option value="tommy">Tommy</option>
      <option value="ntl">NTL</option>
    </select>

    <div class="row">
      <div>
        <label>Valid From</label>
        <input type="date" id="validFrom">
      </div>
      <div>
        <label>Valid Until</label>
        <input type="date" id="validUntil">
      </div>
    </div>

    <button id="btn" onclick="upload()">▶ Start Processing</button>
  </div>

  <div class="progress-bar" id="bar"><div class="progress-fill" id="fill">0%</div></div>
  <div id="log">Ready. Enter password and select a PDF to begin.</div>

  <script>
    // Set default dates
    const today = new Date().toISOString().split('T')[0];
    document.getElementById('validFrom').value = today;
    const until = new Date(); until.setDate(until.getDate()+14);
    document.getElementById('validUntil').value = until.toISOString().split('T')[0];

    let poll, lastPage=0, lastProds=0, failCount=0, startTime=0, totalPages=0;

    function log(msg, type='info') {
      const colors = {success:'#00ff88', error:'#ff5555', info:'#66ccff', warn:'#ffcc00'};
      const el = document.getElementById('log');
      const time = new Date().toLocaleTimeString();
      el.innerHTML += `<span style="color:#555">[${time}]</span> <span style="color:${colors[type]||'#eee'}">${msg}</span>\n`;
      el.scrollTop = el.scrollHeight;
    }

    function stop(msg) {
      clearInterval(poll); poll=null;
      if(msg) log(msg,'error');
      const btn=document.getElementById('btn');
      btn.disabled=false; btn.textContent='▶ Start Processing';
    }

    async function upload() {
      const password = document.getElementById('password').value;
      if(!password){log('❌ Enter password','error');return;}
      const file = document.getElementById('file').files?.[0];
      if(!file){log('❌ Select a PDF','error');return;}

      document.getElementById('log').innerHTML='';
      lastPage=0; lastProds=0; failCount=0; totalPages=0;

      const btn=document.getElementById('btn');
      btn.disabled=true; btn.textContent='Processing...';
      document.getElementById('bar').style.display='block';
      document.getElementById('fill').style.width='0%';
      document.getElementById('fill').textContent='0%';

      const form=new FormData();
      form.append('file',file);
      form.append('store',document.getElementById('store').value);
      form.append('valid_from',document.getElementById('validFrom').value);
      form.append('valid_until',document.getElementById('validUntil').value);
      form.append('password',password);

      try {
        log(`📤 Uploading ${file.name} (${(file.size/1024/1024).toFixed(1)}MB)...`,'info');
        const res=await fetch('/upload',{method:'POST',body:form});
        if(!res.ok){const e=await res.json().catch(()=>({}));throw new Error(e.error||`HTTP ${res.status}`);}
        const data=await res.json();
        startTime=Date.now();
        log(`✅ Job started — ID: ${data.job_id}`,'success');
        if(poll) clearInterval(poll);
        poll=setInterval(()=>doPoll(data.job_id),2000);
      } catch(e){
        log(`❌ ${e.message}`,'error');
        stop();
      }
    }

    async function doPoll(jobId) {
      if(Date.now()-startTime > 20*60*1000){stop('⏱️ Timeout after 20 min');return;}
      try {
        const res=await fetch(`/status/${jobId}`);
        if(res.status===404){if(++failCount>=5)stop('❌ Job not found');return;}
        if(!res.ok) throw new Error(`HTTP ${res.status}`);
        failCount=0;
        const d=await res.json();
        const cur=d.current_page||0, prods=d.total_products||0;
        if(d.total_pages>0) totalPages=d.total_pages;

        if(cur>lastPage){
          for(let i=lastPage+1;i<=cur;i++){
            const total=totalPages||'?';
            log(`📄 Page ${String(i).padStart(3,'0')} / ${total}  |  products: ${prods}`,'success');
          }
          lastPage=cur; lastProds=prods;
          if(totalPages>0){
            const pct=Math.round(cur/totalPages*100);
            document.getElementById('fill').style.width=pct+'%';
            document.getElementById('fill').textContent=pct+'%';
          }
        }
        if(d.status==='done'){
          clearInterval(poll); poll=null;
          log(`🎉 DONE! ${prods} products saved.`,'success');
          document.getElementById('fill').style.width='100%';
          document.getElementById('fill').textContent='100%';
          document.getElementById('btn').disabled=false;
          document.getElementById('btn').textContent='▶ Process Another';
        }
        if(d.status==='error') stop('❌ Job failed — check server logs.');
      } catch(e){
        if(++failCount>=5) stop(`❌ Poll error: ${e.message}`);
      }
    }
  </script>
</body>
</html>'''

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
