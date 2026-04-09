#!/usr/bin/env python3
APP = 'backend/app.py'

NEW_ROUTE = '''
@app.route("/api/image/<ean>")
def product_image(ean):
    """Return cached product image URL. Fetches from Open Food Facts on first request, caches in Supabase Storage."""
    storage_path = f"products/{ean}.jpg"
    public_url = f"{SUPABASE_URL}/storage/v1/object/public/katalog-images/{storage_path}"

    try:
        check = requests.head(public_url, timeout=5)
        if check.status_code == 200:
            return jsonify({"url": public_url, "cached": True})
    except Exception:
        pass

    image_bytes = None
    off_url = None
    ean_padded = ean.zfill(13)
    off_path = f"{ean_padded[0:3]}/{ean_padded[3:6]}/{ean_padded[6:9]}/{ean_padded[9:]}"
    candidates = [
        f"https://images.openfoodfacts.org/images/products/{off_path}/front_en.400.jpg",
        f"https://images.openfoodfacts.org/images/products/{off_path}/front.400.jpg",
        f"https://images.openfoodfacts.org/images/products/{ean}/front_en.400.jpg",
        f"https://images.openfoodfacts.org/images/products/{ean}/front.400.jpg",
    ]

    for url in candidates:
        try:
            r = requests.get(url, timeout=8, headers={"User-Agent": "Stedko/1.0"})
            if r.status_code == 200 and len(r.content) > 2000:
                image_bytes = r.content
                off_url = url
                break
        except Exception:
            continue

    if not image_bytes:
        return jsonify({"url": None, "cached": False}), 404

    try:
        upload_r = requests.put(
            f"{SUPABASE_URL}/storage/v1/object/katalog-images/{storage_path}",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "image/jpeg",
                "x-upsert": "true"
            },
            data=image_bytes,
            timeout=15
        )
        if upload_r.status_code in [200, 201]:
            logger.info(f"Cached product image for EAN {ean}")
            return jsonify({"url": public_url, "cached": False})
    except Exception as e:
        logger.error(f"Failed to cache image for {ean}: {e}")

    return jsonify({"url": off_url, "cached": False})

'''

ANCHOR = '@app.route("/manifest.json")'

with open(APP, 'r') as f:
    content = f.read()

if '/api/image/<ean>' in content:
    print('SKIP: /api/image already present')
else:
    content = content.replace(ANCHOR, NEW_ROUTE + ANCHOR)
    with open(APP, 'w') as f:
        f.write(content)
    print('OK: /api/image/<ean> added to app.py')
