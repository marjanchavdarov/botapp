#!/usr/bin/env python3
APP = 'backend/app.py'

OLD = '@app.route("/api/image/<ean>")\ndef product_image(ean):\n    """Return cached product image URL. Fetches from Open Food Facts on first request, caches in Supabase Storage."""'

NEW_FUNC = '''@app.route("/api/image/<ean>")
def product_image(ean):
    storage_path = f"products/{ean}.jpg"
    public_url = f"{SUPABASE_URL}/storage/v1/object/public/katalog-images/{storage_path}"
    ua = "Stedko/1.0 (info@stedko.hr)"
    try:
        check = requests.head(public_url, timeout=5)
        if check.status_code == 200:
            return jsonify({"url": public_url, "cached": True})
    except Exception:
        pass
    image_url = None
    for base in ["https://world.openfoodfacts.org","https://world.openbeautyfacts.org"]:
        try:
            r = requests.get(f"{base}/api/v2/product/{ean}.json?fields=image_front_url,image_url", headers={"User-Agent": ua}, timeout=8)
            if r.status_code == 200 and r.json().get("status") == 1:
                p = r.json().get("product", {})
                image_url = p.get("image_front_url") or p.get("image_url")
                if image_url: break
        except Exception as e:
            logger.warning(f"Image API error {base} {ean}: {e}")
    if not image_url:
        return jsonify({"url": None, "cached": False}), 404
    try:
        img_r = requests.get(image_url, timeout=10, headers={"User-Agent": ua})
        if img_r.status_code == 200 and len(img_r.content) > 2000:
            up = requests.put(f"{SUPABASE_URL}/storage/v1/object/katalog-images/{storage_path}",
                headers={"apikey": SUPABASE_KEY,"Authorization": f"Bearer {SUPABASE_KEY}","Content-Type": "image/jpeg","x-upsert": "true"},
                data=img_r.content, timeout=15)
            if up.status_code in [200, 201]:
                logger.info(f"Cached image EAN {ean}")
                return jsonify({"url": public_url, "cached": False})
    except Exception as e:
        logger.error(f"Cache fail {ean}: {e}")
    return jsonify({"url": image_url, "cached": False})'''

with open(APP, 'r') as f:
    content = f.read()

# Find and replace the whole old function up to the next route
import re
pattern = r'@app\.route\("/api/image/<ean>"\)\ndef product_image.*?(?=\n\n@app\.route)'
match = re.search(pattern, content, re.DOTALL)
if not match:
    print('ERROR: function not found')
    exit(1)
content = content[:match.start()] + NEW_FUNC + content[match.end():]
with open(APP, 'w') as f:
    f.write(content)
print('OK: /api/image upgraded')
