"""
katalog.ai - Multi-Country Shopping Assistant
Brand new implementation - Croatia first, then expandable
"""

import os
import json
import base64
import uuid
import threading
import logging
from datetime import datetime, date, timedelta
from urllib.parse import quote, urlparse, urlunparse
import re
from functools import wraps


from flask import Flask, request, jsonify, send_from_directory, g, session
from flask_socketio import SocketIO, emit, join_room, leave_room
import requests
from dotenv import load_dotenv

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__, static_folder='static', static_url_path='/static')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max file size

# SocketIO for real-time chat
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# ============================================================================
# CONFIGURATION
# ============================================================================

class Config:
    """Base configuration"""
    GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
    SUPABASE_URL = os.environ.get('SUPABASE_URL')
    SUPABASE_KEY = os.environ.get('SUPABASE_KEY')
    STORAGE_BUCKET = 'katalog-images'
    SUPABASE_URL = os.environ.get('SUPABASE_URL', 'https://172.64.149.246')
    SUPABASE_HOST = os.environ.get('SUPABASE_HOST', 'jwuifezafytihgzepylq.supabase.co')
    
    # Validate required variables
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY environment variable not set")
        raise ValueError("GEMINI_API_KEY environment variable not set")
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.error("Supabase credentials not set")
        raise ValueError("Supabase credentials not set")


class CroatiaConfig(Config):
    """Croatia-specific configuration"""
    COUNTRY_CODE = 'hr'
    COUNTRY_NAME = 'croatia'
    LANGUAGE = 'hr'
    CURRENCY = '€'
    DATE_FORMAT = '%d.%m.%Y.'
    
    # Default stores for Croatia
    STORES = [
        {'id': 'lidl', 'name': 'Lidl', 'color': '#0050aa'},
        {'id': 'kaufland', 'name': 'Kaufland', 'color': '#e30613'},
        {'id': 'spar', 'name': 'Spar', 'color': '#1e6b3b'},
        {'id': 'konzum', 'name': 'Konzum', 'color': '#ed1c24'},
        {'id': 'dm', 'name': 'dm', 'color': '#e31837'},
        {'id': 'plodine', 'name': 'Plodine', 'color': '#009640'},
    ]


class SloveniaConfig(Config):
    """Slovenia-specific configuration (for future)"""
    COUNTRY_CODE = 'si'
    COUNTRY_NAME = 'slovenia'
    LANGUAGE = 'sl'
    CURRENCY = '€'
    DATE_FORMAT = '%d. %m. %Y'
    
    STORES = [
        {'id': 'lidl', 'name': 'Lidl', 'color': '#0050aa'},
        {'id': 'hofer', 'name': 'Hofer', 'color': '#e30613'},
        {'id': 'spar', 'name': 'Spar', 'color': '#1e6b3b'},
        {'id': 'mercator', 'name': 'Mercator', 'color': '#ed1c24'},
        {'id': 'dm', 'name': 'dm', 'color': '#e31837'},
        {'id': 'tus', 'name': 'Tuš', 'color': '#009640'},
    ]


# Map country codes to configs
COUNTRY_CONFIGS = {
    'hr': CroatiaConfig,
    'si': SloveniaConfig,
}

# Default to Croatia
DEFAULT_COUNTRY = 'hr'


# ============================================================================
# TRANSLATIONS
# ============================================================================

TRANSLATIONS = {
    'hr': {
        # App
        'app_name': 'katalog.hr',
        'app_tagline': 'Pametni pomoćnik za kupovinu',
        
        # Navigation
        'nav_home': 'DOMA',
        'nav_favorites': 'MOJ POPIS',
        'nav_chat': 'CHAT',
        'nav_catalogues': 'KATALOZI',
        'nav_more': 'VIŠE',
        
        # Product page
        'today_deals': 'DANAŠNJE PONUDE',
        'valid_until': 'Vrijedi do',
        'add': 'DODAJ',
        'added': 'DODANO',
        'view_page': 'Pogledaj stranicu',
        'page': 'Stranica',
        'page_abbr': 'str.',
        
        # Chat
        'chat_greeting': 'Bok! Ja sam tvoj pomoćnik za kupovinu. Kako ti mogu pomoći?',
        'chat_placeholder': 'Napiši poruku...',
        'chat_suggestions': 'Možeš me pitati za:',
        'chat_suggestion_items': [
            'cijene proizvoda (npr. "koliko stoji mlijeko")',
            'akcije u trgovinama (npr. "akcije u Lidlu")',
            'gdje naći određeni proizvod (npr. "gdje ima Nutella")',
            'današnje ponude (npr. "što je danas na akciji")'
        ],
        'chat_typing': 'tipka...',
        
        # Search
        'search_placeholder': '🔍 Pretraži proizvode...',
        
        # Filters
        'all_stores': 'Sve trgovine',
        'today': 'Danas',
        'tomorrow': 'Sutra',
        'this_week': 'Ovaj tjedan',
        
        # Buttons
        'btn_prev_page': '◀ Prethodna',
        'btn_next_page': 'Sljedeća ▶',
        'btn_close': '✕',
        'btn_share': 'Podijeli',
        'btn_save': 'Spremi',
        
        # Messages
        'msg_no_products': 'Nema proizvoda za prikaz',
        'msg_no_image': 'Slika nije dostupna',
        'msg_error': 'Došlo je do greške',
        'msg_loading': 'Učitavanje...',
        'msg_welcome': 'Dobrodošao u katalog.hr!',
        
        # Numbers
        'products_found': 'pronađeno proizvoda',
        'page_info': 'Stranica {page} od {total}',
        
        # Time
        'today': 'danas',
        'tomorrow': 'sutra',
        'yesterday': 'jučer',
    },
    
    'si': {
        # Slovenian translations (for future)
        'app_name': 'katalog.si',
        'app_tagline': 'Pametni pomočnik za nakupovanje',
        'nav_home': 'DOMOV',
        'nav_favorites': 'MOJ SEZNAM',
        'nav_chat': 'KLEPET',
        'nav_catalogues': 'KATALOGI',
        'nav_more': 'VEČ',
        'today_deals': 'DANAŠNJE PONUDBE',
        'valid_until': 'Velja do',
        'add': 'DODAJ',
        'added': 'DODANO',
        'view_page': 'Poglej stran',
        'page': 'Stran',
        'search_placeholder': '🔍 Išči izdelke...',
    }
}


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================
# Custom session that handles IP-based URLs with correct Host header
def create_supabase_session():
    session = requests.Session()
    
    # Add retries
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504]
    )
    session.mount('https://', HTTPAdapter(max_retries=retries))
    
    # Set the Host header for IP-based requests
    session.headers.update({
        'Host': 'jwuifezafytihgzepylq.supabase.co'
    })
    
    return session

# Use this session for Supabase requests
supabase_session = create_supabase_session()

# Update your db_headers() function
def db_headers():
    return {
        "apikey": Config.SUPABASE_KEY,
        "Authorization": f"Bearer {Config.SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

# Create a new function for Supabase requests
def supabase_request(method, path, **kwargs):
    url = f"{Config.SUPABASE_URL}{path}"
    
    # If using IP address, ensure we have the Host header
    if '172.64.' in Config.SUPABASE_URL or '104.18.' in Config.SUPABASE_URL:
        headers = kwargs.get('headers', {})
        headers['Host'] = 'jwuifezafytihgzepylq.supabase.co'
        kwargs['headers'] = headers
    
    return supabase_session.request(method, url, **kwargs)


def get_country_config():
    """Get country configuration based on subdomain or request"""
    # Check subdomain (hr.katalog.ai, si.katalog.ai)
    host = request.host.split('.')[0] if request else ''
    
    if host in COUNTRY_CONFIGS:
        country_code = host
    else:
        # Check header or default
        country_code = request.headers.get('X-Country', DEFAULT_COUNTRY) if request else DEFAULT_COUNTRY
    
    config_class = COUNTRY_CONFIGS.get(country_code, CroatiaConfig)
    return config_class()


def get_translations(country_code=None):
    """Get translations for country"""
    if not country_code:
        config = get_country_config()
        country_code = config.COUNTRY_CODE
    
    return TRANSLATIONS.get(country_code, TRANSLATIONS['hr'])


def db_headers():
    """Supabase database headers"""
    return {
        "apikey": Config.SUPABASE_KEY,
        "Authorization": f"Bearer {Config.SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }


def storage_headers(content_type='application/octet-stream'):
    """Supabase storage headers"""
    return {
        "apikey": Config.SUPABASE_KEY,
        "Authorization": f"Bearer {Config.SUPABASE_KEY}",
        "Content-Type": content_type,
        "x-upsert": "true"
    }


def sanitize_filename(filename):
    """Remove special characters from filename"""
    # Replace spaces and special chars with underscore
    filename = re.sub(r'[^a-zA-Z0-9.-]', '_', filename)
    # Remove multiple underscores
    filename = re.sub(r'_+', '_', filename)
    return filename


def encode_url(url):
    """Properly encode URL for WhatsApp/PWA"""
    if not url:
        return url
    
    try:
        parsed = urlparse(url)
        path_parts = parsed.path.split('/')
        encoded_path = '/'.join(quote(part) for part in path_parts)
        
        encoded = urlunparse((
            parsed.scheme,
            parsed.netloc,
            encoded_path,
            parsed.params,
            parsed.query,
            parsed.fragment
        ))
        return encoded
    except Exception as e:
        logger.error(f"URL encoding error: {e}")
        return url


def format_date(date_str, country_code='hr'):
    """Format date according to country preferences"""
    if not date_str:
        return ''
    
    try:
        date_obj = datetime.strptime(date_str, '%Y-%m-%d')
        config = COUNTRY_CONFIGS.get(country_code, CroatiaConfig)
        return date_obj.strftime(config.DATE_FORMAT)
    except:
        return date_str


# ============================================================================
# DATABASE FUNCTIONS
# ============================================================================

def ensure_bucket_exists():
    """Ensure storage bucket exists and is public"""
    headers = {
        "apikey": Config.SUPABASE_KEY,
        "Authorization": f"Bearer {Config.SUPABASE_KEY}"
    }
    
    try:
        # Check if bucket exists
        response = requests.get(
            f"{Config.SUPABASE_URL}/storage/v1/bucket/{Config.STORAGE_BUCKET}",
            headers=headers,
            timeout=10
        )
        
        if response.status_code == 404:
            logger.info(f"Creating storage bucket: {Config.STORAGE_BUCKET}")
            
            # Create bucket
            create_response = requests.post(
                f"{Config.SUPABASE_URL}/storage/v1/bucket",
                headers={**headers, "Content-Type": "application/json"},
                json={
                    "name": Config.STORAGE_BUCKET,
                    "public": True,
                    "file_size_limit": 10485760,  # 10MB
                    "allowed_mime_types": ["image/jpeg", "image/jpg", "image/png"]
                },
                timeout=10
            )
            
            if create_response.status_code in [200, 201]:
                logger.info("✅ Storage bucket created")
                return True
            else:
                logger.error(f"Failed to create bucket: {create_response.text}")
                return False
        else:
            logger.info("✅ Storage bucket exists")
            return True
            
    except Exception as e:
        logger.error(f"Error checking bucket: {e}")
        return False


def init_database():
    """Initialize database tables if they don't exist"""
    # This would be done through Supabase migrations
    # But we'll check and warn
    logger.info("Checking database setup...")
    
    # Check products table
    try:
        response = requests.get(
            f"{Config.SUPABASE_URL}/rest/v1/products?limit=1",
            headers=db_headers(),
            timeout=10
        )
        
        if response.status_code == 200:
            logger.info("✅ Products table exists")
        else:
            logger.warning("⚠️ Products table may not exist. Run migrations first.")
            
    except Exception as e:
        logger.error(f"Database check failed: {e}")


# Call at startup
ensure_bucket_exists()
init_database()


# ============================================================================
# PRODUCT FUNCTIONS
# ============================================================================

def get_products(country_code=None, store=None, query=None, limit=100):
    """Get products from database"""
    if not country_code:
        config = get_country_config()
        country_code = config.COUNTRY_CODE
    
    today = date.today().strftime('%Y-%m-%d')
    
    # Build query params
    params = {
        "country": f"eq.{country_code}",
        "valid_from": f"lte.{today}",
        "valid_until": f"gte.{today}",
        "is_expired": "eq.false",
        "limit": limit,
        "order": "store,product"
    }
    
    if store:
        params["store"] = f"eq.{store}"
    
    if query:
        # Search in product name, brand, category
        params["product"] = f"ilike.*{query}*"
    
    try:
        response = requests.get(
            f"{Config.SUPABASE_URL}/rest/v1/products",
            headers=db_headers(),
            params=params,
            timeout=30
        )
        
        if response.status_code == 200:
            products = response.json()
            logger.info(f"Found {len(products)} products for {country_code}")
            return products
        else:
            logger.error(f"Error fetching products: {response.status_code}")
            return []
            
    except Exception as e:
        logger.error(f"Exception fetching products: {e}")
        return []


def get_product_by_id(product_id):
    """Get single product by ID"""
    try:
        response = requests.get(
            f"{Config.SUPABASE_URL}/rest/v1/products?id=eq.{product_id}",
            headers=db_headers(),
            timeout=10
        )
        
        if response.status_code == 200 and response.json():
            return response.json()[0]
        return None
        
    except Exception as e:
        logger.error(f"Error fetching product {product_id}: {e}")
        return None


def search_products(query, country_code=None, limit=50):
    """Search products by text query"""
    if not country_code:
        config = get_country_config()
        country_code = config.COUNTRY_CODE
    
    try:
        # Search in multiple fields
        response = requests.get(
            f"{Config.SUPABASE_URL}/rest/v1/products",
            headers=db_headers(),
            params={
                "country": f"eq.{country_code}",
                "or": f"(product.ilike.*{query}*,brand.ilike.*{query}*,category.ilike.*{query}*)",
                "limit": limit,
                "order": "store,product"
            },
            timeout=30
        )
        
        if response.status_code == 200:
            return response.json()
        return []
        
    except Exception as e:
        logger.error(f"Search error: {e}")
        return []


def save_products(products, country_code, store, page_num, page_url, catalogue_name, valid_from, valid_until):
    """Save products to database"""
    if not products:
        return 0
    
    records = []
    for p in products:
        # Skip if no sale price
        if not p.get('sale_price') or p.get('sale_price') in [None, 'null', '']:
            continue
        
        # Parse dates
        vu = p.get('valid_until') or valid_until
        vf = p.get('valid_from') or valid_from
        
        records.append({
            "country": country_code,
            "store": store,
            "product": p.get('product', ''),
            "brand": p.get('brand') if p.get('brand') not in [None, 'null'] else None,
            "quantity": p.get('quantity') if p.get('quantity') not in [None, 'null'] else None,
            "original_price": p.get('original_price') if p.get('original_price') not in [None, 'null'] else None,
            "sale_price": p.get('sale_price', ''),
            "discount_percent": p.get('discount_percent') if p.get('discount_percent') not in [None, 'null'] else None,
            "category": p.get('category', 'Other'),
            "subcategory": p.get('subcategory'),
            "valid_from": vf,
            "valid_until": vu,
            "is_expired": False,
            "page_image_url": page_url,
            "page_number": page_num,
            "catalogue_name": catalogue_name,
            "catalogue_week": datetime.now().strftime('%Y-W%V')
        })
    
    if not records:
        return 0
    
    try:
        response = requests.post(
            f"{Config.SUPABASE_URL}/rest/v1/products",
            headers={**db_headers(), "Prefer": "return=minimal"},
            json=records,
            timeout=30
        )
        
        if response.status_code in [200, 201]:
            logger.info(f"Saved {len(records)} products")
            return len(records)
        else:
            logger.error(f"Failed to save products: {response.status_code} - {response.text[:200]}")
            return 0
            
    except Exception as e:
        logger.error(f"Exception saving products: {e}")
        return 0


def save_catalogue(country_code, store, catalogue_name, valid_from, valid_until, fine_print, pages, products_count):
    """Save catalogue metadata"""
    try:
        response = requests.post(
            f"{Config.SUPABASE_URL}/rest/v1/catalogues",
            headers={**db_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
            json={
                "country": country_code,
                "store": store,
                "catalogue_name": catalogue_name,
                "valid_from": valid_from,
                "valid_until": valid_until,
                "fine_print": fine_print,
                "pages": pages,
                "products_count": products_count
            },
            timeout=30
        )
        
        if response.status_code in [200, 201]:
            logger.info(f"Catalogue saved: {catalogue_name}")
        else:
            logger.error(f"Failed to save catalogue: {response.status_code}")
            
    except Exception as e:
        logger.error(f"Exception saving catalogue: {e}")

# ============================================================================
# DEBUG ENDPOINTS - Add these temporarily
# ============================================================================

@app.route('/debug/health')
def debug_health():
    """Basic health check"""
    return jsonify({
        "status": "ok",
        "time": datetime.now().isoformat()
    })

@app.route('/debug/env')
def debug_env():
    """Check environment variables (without exposing values)"""
    import os
    return jsonify({
        "supabase_url_set": bool(os.environ.get('SUPABASE_URL')),
        "supabase_key_set": bool(os.environ.get('SUPABASE_KEY')),
        "gemini_key_set": bool(os.environ.get('GEMINI_API_KEY')),
        "supabase_url_prefix": str(os.environ.get('SUPABASE_URL', ''))[:20] + "..." if os.environ.get('SUPABASE_URL') else None,
        "python_version": sys.version,
        "cwd": os.getcwd(),
        "files_in_cwd": os.listdir('.')[:10]  # First 10 files
    })

@app.route('/debug/pymupdf')
def debug_pymupdf():
    """Test PyMuPDF installation"""
    try:
        import fitz
        return jsonify({
            "status": "success",
            "version": fitz.version,
            "fitz_path": fitz.__file__
        })
    except ImportError as e:
        return jsonify({
            "status": "error",
            "error": str(e),
            "type": "ImportError"
        }), 500
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e),
            "type": type(e).__name__
        }), 500

@app.route('/debug/supabase')
def debug_supabase():
    """Test Supabase connection"""
    try:
        headers = db_headers()
        response = requests.get(
            f"{Config.SUPABASE_URL}/rest/v1/",
            headers=headers,
            timeout=5
        )
        return jsonify({
            "status": "connected" if response.status_code < 500 else "error",
            "status_code": response.status_code,
            "response_preview": response.text[:200] if response.text else None
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e),
            "type": type(e).__name__
        }), 500

@app.route('/debug/temp')
def debug_temp():
    """Test temporary directory access"""
    import tempfile
    try:
        # Test creating a temp file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write('test')
            temp_path = f.name
        
        # Test reading it back
        with open(temp_path, 'r') as f:
            content = f.read()
        
        # Clean up
        import os
        os.unlink(temp_path)
        
        return jsonify({
            "status": "success",
            "temp_dir": tempfile.gettempdir(),
            "writable": True,
            "content": content
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e),
            "temp_dir": tempfile.gettempdir()
        }), 500


# ============================================================================
# STORAGE FUNCTIONS
# ============================================================================

def upload_image(img_bytes, storage_path):
    """Upload image to Supabase storage"""
    headers = storage_headers('image/jpeg')
    
    try:
        response = requests.put(
            f"{Config.SUPABASE_URL}/storage/v1/object/{Config.STORAGE_BUCKET}/{storage_path}",
            headers=headers,
            data=img_bytes,
            timeout=30
        )
        
        if response.status_code in [200, 201]:
            public_url = f"{Config.SUPABASE_URL}/storage/v1/object/public/{Config.STORAGE_BUCKET}/{storage_path}"
            logger.info(f"✅ Image uploaded: {public_url}")
            return public_url
        else:
            logger.warning(f"Upload failed: {response.status_code}")
            
    except Exception as e:
        logger.error(f"Upload exception: {e}")
    
    return None


def get_page_image_url(store, page_num, country_code=None):
    """Get image URL for a specific page"""
    if not country_code:
        config = get_country_config()
        country_code = config.COUNTRY_CODE
    
    try:
        # Find product with this page number
        response = requests.get(
            f"{Config.SUPABASE_URL}/rest/v1/products",
            headers=db_headers(),
            params={
                "country": f"eq.{country_code}",
                "store": f"eq.{store}",
                "page_number": f"eq.{page_num}",
                "select": "page_image_url",
                "limit": 1
            },
            timeout=10
        )
        
        if response.status_code == 200 and response.json():
            return response.json()[0].get('page_image_url')
        
        # Try without store filter
        response2 = requests.get(
            f"{Config.SUPABASE_URL}/rest/v1/products",
            headers=db_headers(),
            params={
                "country": f"eq.{country_code}",
                "page_number": f"eq.{page_num}",
                "select": "page_image_url",
                "limit": 1
            },
            timeout=10
        )
        
        if response2.status_code == 200 and response2.json():
            return response2.json()[0].get('page_image_url')
            
    except Exception as e:
        logger.error(f"Error getting page image: {e}")
    
    return None


# ============================================================================
# GEMINI AI FUNCTIONS
# ============================================================================

def extract_products_from_image(img_b64, store, page_num, country_code):
    """Use Gemini to extract products from catalog page image"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={Config.GEMINI_API_KEY}"
    
    config = COUNTRY_CONFIGS.get(country_code, CroatiaConfig)
    translations = get_translations(country_code)
    
    prompt = f"""
    Extract ALL products from this catalog page.
    
    Store: {store}
    Page: {page_num}
    Country: {config.COUNTRY_NAME}
    Currency: {config.CURRENCY}
    
    RULES:
    1. Extract ONLY products with visible prices
    2. Translate product names to English (keep original in 'original_name' field)
    3. Include brand, quantity, prices
    4. Convert dates to YYYY-MM-DD format
    5. Skip promotional items without prices
    
    Return as JSON array:
    [
        {{
            "original_name": "original product name",
            "product": "English translation",
            "brand": "brand or null",
            "quantity": "250g or null",
            "original_price": "2.99 or null",
            "sale_price": "1.99",
            "discount_percent": "33% or null",
            "valid_from": "2026-03-02 or null",
            "valid_until": "2026-03-08 or null",
            "category": "Category in English",
            "subcategory": "Subcategory in English"
        }}
    ]
    
    Categories: Meat and Fish, Dairy, Bread and Bakery, Fruit and Vegetables, Drinks,
    Snacks and Sweets, Canned Food, Cosmetics and Hygiene, Household and Cleaning,
    Tools and Construction, Home and Garden, Electronics, Clothing and Shoes,
    Pet Food, Health and Pharmacy, Other.
    
    If no valid products, return: []
    """
    
    body = {
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}},
                {"text": prompt}
            ]
        }],
        "generationConfig": {
            "maxOutputTokens": 8192,
            "temperature": 0.1
        }
    }
    
    for attempt in range(3):
        try:
            response = requests.post(url, json=body, timeout=45)
            result = response.json()
            
            if 'candidates' not in result:
                logger.error(f"Gemini error: {result}")
                continue
            
            text = result['candidates'][0]['content']['parts'][0]['text']
            text = text.replace('```json', '').replace('```', '').strip()
            
            products = json.loads(text)
            if not isinstance(products, list):
                return [], None
            
            # Extract fine print if any
            fine_print = None
            for p in products:
                if p.get('fine_print') and p.get('fine_print') not in [None, 'null']:
                    fine_print = p.get('fine_print')
                    break
            
            logger.info(f"Extracted {len(products)} products from page {page_num}")
            return products, fine_print
            
        except Exception as e:
            logger.error(f"Gemini attempt {attempt+1} failed: {e}")
            if attempt == 2:
                return [], None
            continue
    
    return [], None


def ask_gemini(message, context, country_code):
    """Ask Gemini a question with context"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={Config.GEMINI_API_KEY}"
    
    config = COUNTRY_CONFIGS.get(country_code, CroatiaConfig)
    translations = get_translations(country_code)
    today = date.today().strftime('%d.%m.%Y.')
    
    prompt = f"""
    You are a friendly shopping assistant for {config.COUNTRY_NAME}.
    Today is {today}.
    Language: Respond in {config.LANGUAGE} (Croatian for hr, Slovenian for si).
    
    CONTEXT:
    {context}
    
    USER QUESTION: {message}
    
    INSTRUCTIONS:
    - Be helpful and friendly
    - Use the local language
    - Mention store names and page numbers when available
    - Be concise but informative
    - If products are mentioned, always include page numbers
    - End with "Stranice: X, Y, Z" or similar
    - Use emojis to be friendly 🛒
    
    RESPOND IN {config.LANGUAGE.upper()}:
    """
    
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 1024,
            "temperature": 0.3
        }
    }
    
    try:
        response = requests.post(url, json=body, timeout=30)
        result = response.json()
        
        if 'candidates' in result:
            reply = result['candidates'][0]['content']['parts'][0]['text']
            return reply
        else:
            logger.error(f"Gemini error: {result}")
            return translations.get('msg_error', 'Došlo je do greške.')
            
    except Exception as e:
        logger.error(f"Gemini request failed: {e}")
        return translations.get('msg_error', 'Došlo je do greške.')


# ============================================================================
# USER FUNCTIONS
# ============================================================================

def get_or_create_user(device_id, country_code):
    """Get or create user in database"""
    headers = db_headers()
    
    try:
        # Check if user exists
        response = requests.get(
            f"{Config.SUPABASE_URL}/rest/v1/users?device_id=eq.{device_id}",
            headers=headers,
            timeout=10
        )
        
        if response.status_code == 200 and response.json():
            return response.json()[0]
        
        # Create new user
        new_user = {
            "device_id": device_id,
            "country": country_code,
            "language": country_code,
            "created_at": datetime.now().isoformat(),
            "last_active": datetime.now().isoformat(),
            "total_searches": 0,
            "favorites": [],
            "conversation": []
        }
        
        create_response = requests.post(
            f"{Config.SUPABASE_URL}/rest/v1/users",
            headers={**headers, "Prefer": "return=representation"},
            json=new_user,
            timeout=10
        )
        
        if create_response.status_code in [200, 201]:
            return create_response.json()[0]
        else:
            return new_user
            
    except Exception as e:
        logger.error(f"Error in get_or_create_user: {e}")
        return {"device_id": device_id, "country": country_code}


def update_user(device_id, updates):
    """Update user data"""
    headers = db_headers()
    
    try:
        response = requests.patch(
            f"{Config.SUPABASE_URL}/rest/v1/users?device_id=eq.{device_id}",
            headers={**headers, "Prefer": "return=minimal"},
            json={**updates, "last_active": datetime.now().isoformat()},
            timeout=10
        )
        
        if response.status_code not in [200, 201, 204]:
            logger.error(f"Update user failed: {response.status_code}")
            
    except Exception as e:
        logger.error(f"Update user exception: {e}")


def add_to_favorites(device_id, product_id):
    """Add product to user's favorites"""
    user = get_or_create_user(device_id, 'hr')
    favorites = user.get('favorites', [])
    
    if product_id not in favorites:
        favorites.append(product_id)
        update_user(device_id, {"favorites": favorites})
        return True
    return False


def remove_from_favorites(device_id, product_id):
    """Remove product from user's favorites"""
    user = get_or_create_user(device_id, 'hr')
    favorites = user.get('favorites', [])
    
    if product_id in favorites:
        favorites.remove(product_id)
        update_user(device_id, {"favorites": favorites})
        return True
    return False


# ============================================================================
# API ROUTES
# ============================================================================

@app.route('/')
def index():
    """Serve PWA frontend"""
    return send_from_directory('static', 'index.html')


@app.route('/api/country')
def get_country():
    """Get current country configuration"""
    config = get_country_config()
    translations = get_translations(config.COUNTRY_CODE)
    
    return jsonify({
        "code": config.COUNTRY_CODE,
        "name": config.COUNTRY_NAME,
        "language": config.LANGUAGE,
        "currency": config.CURRENCY,
        "date_format": config.DATE_FORMAT,
        "stores": config.STORES,
        "translations": translations
    })


@app.route('/api/products', methods=['GET'])
def api_get_products():
    """Get products with filters"""
    config = get_country_config()
    
    store = request.args.get('store')
    query = request.args.get('q')
    page = request.args.get('page', type=int)
    limit = request.args.get('limit', 50, type=int)
    
    if page:
        # Get products for specific page
        products = get_products_by_page(config.COUNTRY_CODE, store, page)
    else:
        # Search or get all
        if query:
            products = search_products(query, config.COUNTRY_CODE, limit)
        else:
            products = get_products(config.COUNTRY_CODE, store, limit=limit)
    
    # Format for frontend
    for p in products:
        p['valid_until_display'] = format_date(p.get('valid_until'), config.COUNTRY_CODE)
    
    return jsonify(products)


@app.route('/api/products/<product_id>')
def api_get_product(product_id):
    """Get single product by ID"""
    product = get_product_by_id(product_id)
    
    if product:
        config = get_country_config()
        product['valid_until_display'] = format_date(product.get('valid_until'), config.COUNTRY_CODE)
        return jsonify(product)
    
    return jsonify({"error": "Product not found"}), 404


@app.route('/api/page-image/<int:page_num>')
def api_page_image(page_num):
    """Get image URL for a page"""
    store = request.args.get('store')
    config = get_country_config()
    
    image_url = get_page_image_url(store, page_num, config.COUNTRY_CODE)
    
    if image_url:
        return jsonify({
            "page": page_num,
            "store": store,
            "image_url": image_url
        })
    
    return jsonify({"error": "Image not found"}), 404


@app.route('/api/favorites', methods=['GET', 'POST', 'DELETE'])
def api_favorites():
    """Manage user favorites"""
    device_id = request.headers.get('X-Device-ID', request.remote_addr)
    config = get_country_config()
    
    if request.method == 'GET':
        user = get_or_create_user(device_id, config.COUNTRY_CODE)
        favorites = user.get('favorites', [])
        
        if not favorites:
            return jsonify([])
        
        # Get favorite products
        products = []
        for fav_id in favorites[:50]:  # Limit to 50
            product = get_product_by_id(fav_id)
            if product:
                products.append(product)
        
        return jsonify(products)
    
    elif request.method == 'POST':
        data = request.json
        product_id = data.get('product_id')
        
        if not product_id:
            return jsonify({"error": "product_id required"}), 400
        
        added = add_to_favorites(device_id, product_id)
        return jsonify({"added": added})
    
    elif request.method == 'DELETE':
        data = request.json
        product_id = data.get('product_id')
        
        if not product_id:
            return jsonify({"error": "product_id required"}), 400
        
        removed = remove_from_favorites(device_id, product_id)
        return jsonify({"removed": removed})


@app.route('/api/chat', methods=['POST'])
def api_chat():
    """Chat endpoint"""
    data = request.json
    message = data.get('message', '').strip()
    device_id = request.headers.get('X-Device-ID', request.remote_addr)
    
    if not message:
        return jsonify({"error": "Message required"}), 400
    
    config = get_country_config()
    translations = get_translations(config.COUNTRY_CODE)
    
    # Search for products
    products = search_products(message, config.COUNTRY_CODE, 10)
    
    # Prepare context for Gemini
    context = "PRODUCTS FOUND:\n"
    if products:
        for p in products[:5]:
            context += f"- {p.get('store')}: {p.get('product')} - {p.get('sale_price')}{config.CURRENCY} (str. {p.get('page_number')})\n"
    else:
        context += "No products found matching the query.\n"
    
    # Ask Gemini
    reply = ask_gemini(message, context, config.COUNTRY_CODE)
    
    # Extract page numbers from reply
    page_numbers = re.findall(r'stranic[ea] (\d+)', reply, re.IGNORECASE)
    page_numbers = [int(p) for p in page_numbers if 1 <= int(p) <= 500]
    
    # Update user stats
    user = get_or_create_user(device_id, config.COUNTRY_CODE)
    update_user(device_id, {
        "total_searches": (user.get('total_searches', 0) + 1),
        "last_query": message
    })
    
    return jsonify({
        "reply": reply,
        "products": products[:5],
        "page_numbers": page_numbers[:3],
        "suggestions": translations.get('chat_suggestion_items', [])[:3]
    })


# ============================================================================
# UPLOAD TOOL ROUTES
# ============================================================================

@app.route('/upload-tool')
def upload_tool():
    """Serve the upload tool HTML"""
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>katalog.ai - Upload</title>
        <style>
            * {
                margin: 0;
                padding: 0;
                box-sizing: border-box;
                font-family: 'Segoe UI', monospace;
            }
            body {
                background: #111;
                color: #eee;
                padding: 20px;
                max-width: 800px;
                margin: 0 auto;
            }
            h1 {
                color: #00ff88;
                font-size: 28px;
                margin-bottom: 30px;
                border-bottom: 1px solid #333;
                padding-bottom: 10px;
            }
            .card {
                background: #1a1a1a;
                border-radius: 10px;
                padding: 20px;
                margin-bottom: 20px;
                border: 1px solid #333;
            }
            label {
                display: block;
                color: #aaa;
                font-size: 12px;
                text-transform: uppercase;
                letter-spacing: 1px;
                margin-bottom: 5px;
            }
            input, select {
                width: 100%;
                padding: 12px;
                background: #222;
                border: 1px solid #444;
                color: #eee;
                border-radius: 5px;
                margin-bottom: 15px;
                font-size: 16px;
            }
            input:focus, select:focus {
                outline: 1px solid #00ff88;
                border-color: #00ff88;
            }
            button {
                background: #00ff88;
                color: #000;
                border: none;
                padding: 15px 30px;
                font-size: 16px;
                font-weight: bold;
                border-radius: 5px;
                cursor: pointer;
                width: 100%;
                transition: 0.3s;
            }
            button:hover {
                background: #00cc66;
            }
            button:disabled {
                background: #444;
                color: #888;
                cursor: not-allowed;
            }
            button.secondary {
                background: #333;
                color: #eee;
                border: 1px solid #555;
            }
            button.secondary:hover {
                background: #444;
            }
            .progress-bar {
                background: #222;
                height: 30px;
                border-radius: 5px;
                margin: 20px 0;
                overflow: hidden;
                display: none;
            }
            .progress-fill {
                background: #00ff88;
                height: 100%;
                width: 0%;
                transition: width 0.3s;
                display: flex;
                align-items: center;
                justify-content: center;
                font-weight: bold;
                color: #000;
            }
            #log {
                background: #000;
                padding: 20px;
                border-radius: 5px;
                font-size: 13px;
                line-height: 1.6;
                white-space: pre-wrap;
                max-height: 400px;
                overflow-y: auto;
                border: 1px solid #333;
            }
            .success { color: #00ff88; }
            .error { color: #ff5555; }
            .info { color: #66ccff; }
            .warning { color: #ffaa00; }
            .flex {
                display: flex;
                gap: 10px;
            }
            .flex button {
                flex: 1;
            }
            .job-id {
                background: #1a3a1a;
                color: #00ff88;
                padding: 2px 8px;
                border-radius: 3px;
                font-family: monospace;
            }
            #restart-section {
                display: none;
                background: #2a1a1a;
                border-left: 3px solid #ffaa00;
                padding: 15px;
                margin-bottom: 20px;
                border-radius: 5px;
            }
        </style>
    </head>
    <body>
        <h1>📤 katalog.ai - Upload Catalog</h1>
        
        <div id="restart-section">
            <p style="color: #ffaa00; margin-bottom: 10px;">⚠️ Job already exists for this ID</p>
            <div class="flex">
                <button class="secondary" onclick="startNewUpload()">🆕 Start New</button>
                <button class="secondary" onclick="resumeJob()">🔄 Resume</button>
            </div>
        </div>
        
        <div class="card">
            <label>Country</label>
            <select id="country">
                <option value="hr">Croatia (Hrvatska)</option>
                <option value="si">Slovenia (Slovenija)</option>
            </select>
            
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
            <input type="text" id="validFrom" placeholder="2026-03-02" value="2026-03-02">
            
            <label>Valid Until (empty = 14 days auto)</label>
            <input type="text" id="validUntil" placeholder="2026-03-16">
            
            <label>Resume Job ID (optional)</label>
            <div class="flex">
                <input type="text" id="resumeJob" placeholder="Leave empty for new upload">
                <button class="secondary" onclick="checkJob()" style="width: auto;">Check</button>
            </div>
            <div id="jobStatus" style="margin-top: 5px; font-size: 12px;"></div>
            
            <button id="uploadBtn" onclick="upload()">Process Catalog</button>
        </div>
        
        <div class="progress-bar" id="progressBar">
            <div class="progress-fill" id="progressFill">0%</div>
        </div>
        
        <div id="log">Ready.</div>
        
        <script>
            let pollInterval = null;
            let lastPage = 0;
            let lastProducts = 0;
            let totalPages = 0;
            let currentJobId = null;
            
            function log(message, type = 'info') {
                const logDiv = document.getElementById('log');
                const color = {
                    'success': '#00ff88',
                    'error': '#ff5555',
                    'warning': '#ffaa00',
                    'info': '#66ccff'
                }[type] || '#eee';
                
                logDiv.innerHTML += `<span style="color: ${color}">${message}</span><br>`;
                logDiv.scrollTop = logDiv.scrollHeight;
            }
            
            function validateForm() {
                const file = document.getElementById('file');
                const store = document.getElementById('store').value;
                const validFrom = document.getElementById('validFrom').value;
                const resumeJob = document.getElementById('resumeJob').value;
                
                if (!resumeJob && (!file.files || file.files.length === 0)) {
                    log('❌ Please select a PDF file', 'error');
                    return false;
                }
                
                if (!store) {
                    log('❌ Please select a store', 'error');
                    return false;
                }
                
                if (!validFrom) {
                    log('❌ Please enter valid from date', 'error');
                    return false;
                }
                
                const dateRegex = /^\\d{4}-\\d{2}-\\d{2}$/;
                if (!dateRegex.test(validFrom)) {
                    log('❌ Valid From must be YYYY-MM-DD', 'error');
                    return false;
                }
                
                return true;
            }
            
            async function checkJob() {
                const jobId = document.getElementById('resumeJob').value;
                if (!jobId) return;
                
                try {
                    const response = await fetch(`/status/${jobId}`);
                    if (!response.ok) throw new Error('Job not found');
                    
                    const data = await response.json();
                    const status = document.getElementById('jobStatus');
                    
                    if (data.status === 'processing') {
                        status.innerHTML = `<span class="job-id">${jobId}</span> <span style="color: #ffaa00">🔄 In progress</span>`;
                        document.getElementById('restart-section').style.display = 'block';
                    } else if (data.status === 'done') {
                        status.innerHTML = `<span class="job-id">${jobId}</span> <span style="color: #00ff88">✅ Completed</span>`;
                        document.getElementById('restart-section').style.display = 'block';
                    } else if (data.status === 'error') {
                        status.innerHTML = `<span class="job-id">${jobId}</span> <span style="color: #ff5555">❌ Failed</span>`;
                        document.getElementById('restart-section').style.display = 'block';
                    }
                } catch (error) {
                    document.getElementById('jobStatus').innerHTML = '<span style="color: #ff5555">❌ Job not found</span>';
                }
            }
            
            function startNewUpload() {
                document.getElementById('resumeJob').value = '';
                document.getElementById('restart-section').style.display = 'none';
                document.getElementById('jobStatus').innerHTML = '';
                upload();
            }
            
            function resumeJob() {
                document.getElementById('restart-section').style.display = 'none';
                upload();
            }
            
            async function upload() {
                if (!validateForm()) return;
                
                // Clear log for new upload
                if (!currentJobId) {
                    document.getElementById('log').innerHTML = '';
                }
                
                const file = document.getElementById('file').files[0];
                const country = document.getElementById('country').value;
                const store = document.getElementById('store').value;
                const validFrom = document.getElementById('validFrom').value;
                let validUntil = document.getElementById('validUntil').value;
                const resumeJob = document.getElementById('resumeJob').value;
                
                // Auto-calculate validUntil if empty
                if (!validUntil) {
                    const d = new Date(validFrom);
                    d.setDate(d.getDate() + 14);
                    validUntil = d.toISOString().split('T')[0];
                    log(`📅 Auto-set valid until: ${validUntil}`, 'info');
                }
                
                const btn = document.getElementById('uploadBtn');
                btn.disabled = true;
                btn.textContent = resumeJob ? 'Resuming...' : 'Processing...';
                
                document.getElementById('progressBar').style.display = 'block';
                document.getElementById('progressFill').style.width = '0%';
                document.getElementById('progressFill').textContent = '0%';
                
                lastPage = 0;
                lastProducts = 0;
                
                const formData = new FormData();
                if (file) formData.append('file', file);
                formData.append('country', country);
                formData.append('store', store);
                formData.append('valid_from', validFrom);
                formData.append('valid_until', validUntil);
                if (resumeJob) formData.append('resume_job_id', resumeJob);
                
                try {
                    const response = await fetch('/upload', {
                        method: 'POST',
                        body: formData
                    });
                    
                    if (!response.ok) throw new Error(`HTTP ${response.status}`);
                    
                    const data = await response.json();
                    
                    if (data.error) {
                        log(`❌ ${data.error}`, 'error');
                        btn.disabled = false;
                        btn.textContent = 'Process Catalog';
                        return;
                    }
                    
                    currentJobId = data.job_id;
                    totalPages = data.total_pages;
                    
                    log(`✅ Job started! Total pages: ${data.total_pages}`, 'success');
                    if (data.start_page > 0) {
                        log(`🔄 Resuming from page ${data.start_page}`, 'info');
                    }
                    log(`🆔 Job ID: <span class="job-id">${data.job_id}</span>`, 'success');
                    log('─────────────────────────────', 'info');
                    
                    // Start polling
                    if (pollInterval) clearInterval(pollInterval);
                    pollInterval = setInterval(() => poll(data.job_id), 2000);
                    
                } catch (error) {
                    log(`❌ Upload failed: ${error.message}`, 'error');
                    btn.disabled = false;
                    btn.textContent = 'Process Catalog';
                }
            }
            
            async function poll(jobId) {
                try {
                    const response = await fetch(`/status/${jobId}`);
                    if (!response.ok) throw new Error('Poll failed');
                    
                    const data = await response.json();
                    
                    const currentPage = data.current_page || 0;
                    const currentProducts = data.total_products || 0;
                    
                    // Update progress
                    if (currentPage > lastPage) {
                        for (let i = lastPage + 1; i <= currentPage; i++) {
                            let line = `📄 Page ${String(i).padStart(3, '0')} / ${totalPages}`;
                            if (i === currentPage) {
                                const newProducts = currentProducts - lastProducts;
                                line += `  |  +${newProducts} products  |  total: ${currentProducts}`;
                            }
                            log(line, 'success');
                        }
                        
                        lastPage = currentPage;
                        lastProducts = currentProducts;
                        
                        const percent = Math.round((currentPage / totalPages) * 100);
                        document.getElementById('progressFill').style.width = percent + '%';
                        document.getElementById('progressFill').textContent = percent + '%';
                    }
                    
                    // Check status
                    if (data.status === 'done') {
                        clearInterval(pollInterval);
                        log('─────────────────────────────', 'info');
                        log(`✅ DONE! ${data.total_products} products saved!`, 'success');
                        document.getElementById('progressFill').style.width = '100%';
                        document.getElementById('progressFill').textContent = '100% COMPLETE';
                        
                        const btn = document.getElementById('uploadBtn');
                        btn.disabled = false;
                        btn.textContent = 'Process Another';
                        
                    } else if (data.status === 'error') {
                        clearInterval(pollInterval);
                        log('❌ ERROR - Check server logs', 'error');
                        
                        const btn = document.getElementById('uploadBtn');
                        btn.disabled = false;
                        btn.textContent = 'Retry';
                    }
                    
                } catch (error) {
                    console.log('Poll error:', error);
                }
            }
            
            // Check job ID on blur
            document.getElementById('resumeJob').addEventListener('blur', checkJob);
            
            // Set default date to today
            const today = new Date().toISOString().split('T')[0];
            document.getElementById('validFrom').value = today;
        </script>
    </body>
    </html>
    '''


@app.route('/upload', methods=['POST'])
def upload_catalog():
    """Start catalog upload processing"""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return jsonify({"error": "PyMuPDF not installed"}), 500
    
    # Get form data
    country_code = request.form.get('country', 'hr')
    store = request.form.get('store', '').strip()
    valid_from = request.form.get('valid_from', '').strip()
    valid_until = request.form.get('valid_until', '').strip()
    file = request.files.get('file')
    resume_job_id = request.form.get('resume_job_id', '').strip()
    
    # Validate
    if not store or not valid_from:
        return jsonify({"error": "Missing required fields"}), 400
    
    if not valid_until:
        d = datetime.strptime(valid_from, "%Y-%m-%d")
        valid_until = (d + timedelta(days=14)).strftime("%Y-%m-%d")
    
    # Get PDF if not resuming or if file provided
    pdf_bytes = None
    total_pages = 0
    catalogue_name = ''
    
    if file:
        pdf_bytes = file.read()
        catalogue_name = file.filename.replace('.pdf', '')
        
        # Count pages
        try:
            import fitz
            tmp_path = f"/tmp/{uuid.uuid4()}.pdf"
            with open(tmp_path, "wb") as f:
                f.write(pdf_bytes)
            doc = fitz.open(tmp_path)
            total_pages = len(doc)
            doc.close()
            os.remove(tmp_path)
        except Exception as e:
            return jsonify({"error": f"Could not read PDF: {e}"}), 500
    
    # Check for existing job
    if resume_job_id:
        response = requests.get(
            f"{Config.SUPABASE_URL}/rest/v1/jobs?id=eq.{resume_job_id}",
            headers=db_headers()
        )
        
        if response.status_code == 200 and response.json():
            job = response.json()[0]
            job_id = resume_job_id
            start_page = job.get('current_page', 0)
            total_products_so_far = job.get('total_products', 0)
            total_pages = job.get('total_pages', total_pages)
            
            # Update job status
            requests.patch(
                f"{Config.SUPABASE_URL}/rest/v1/jobs?id=eq.{job_id}",
                headers={**db_headers(), "Prefer": "return=minimal"},
                json={"status": "processing"}
            )
        else:
            return jsonify({"error": "Job ID not found"}), 404
    else:
        # Create new job
        job_id = str(uuid.uuid4())[:8]
        start_page = 0
        total_products_so_far = 0
        
        job_data = {
            "id": job_id,
            "country": country_code,
            "store": store,
            "catalogue_name": catalogue_name,
            "valid_from": valid_from,
            "valid_until": valid_until,
            "total_pages": total_pages,
            "current_page": 0,
            "total_products": 0,
            "status": "processing",
            "created_at": datetime.now().isoformat()
        }
        
        requests.post(
            f"{Config.SUPABASE_URL}/rest/v1/jobs",
            headers={**db_headers(), "Prefer": "return=minimal"},
            json=job_data
        )
    
    # Start processing thread
    if pdf_bytes:
        def process():
            try:
                import fitz
                tmp_path = f"/tmp/{job_id}.pdf"
                with open(tmp_path, "wb") as f:
                    f.write(pdf_bytes)
                
                doc = fitz.open(tmp_path)
                total_products = total_products_so_far
                fine_print = None
                
                for page_num in range(start_page, total_pages):
                    try:
                        page = doc[page_num]
                        pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
                        img_bytes = pix.tobytes("jpeg")
                        img_b64 = base64.b64encode(img_bytes).decode()
                        
                        # Upload image
                        safe_store = store.lower().replace(' ', '_')
                        safe_name = catalogue_name.lower().replace(' ', '_')
                        filename = f"{safe_store}_{safe_name}_page_{str(page_num+1).zfill(3)}.jpg"
                        storage_path = f"{country_code}/{safe_store}/{valid_from}/{filename}"
                        
                        page_url = upload_image(img_bytes, storage_path)
                        
                        # Extract products
                        products, fp = extract_products_from_image(img_b64, store, page_num+1, country_code)
                        
                        if fp:
                            fine_print = (fine_print + " " + fp) if fine_print else fp
                        
                        # Save products
                        saved = save_products(
                            products, country_code, store, page_num+1,
                            page_url, catalogue_name, valid_from, valid_until
                        )
                        
                        total_products += saved
                        
                        # Update job progress
                        requests.patch(
                            f"{Config.SUPABASE_URL}/rest/v1/jobs?id=eq.{job_id}",
                            headers={**db_headers(), "Prefer": "return=minimal"},
                            json={
                                "current_page": page_num + 1,
                                "total_products": total_products,
                                "fine_print": fine_print
                            }
                        )
                        
                        logger.info(f"Page {page_num+1}/{total_pages} processed: {saved} products")
                        
                    except Exception as e:
                        logger.error(f"Error on page {page_num+1}: {e}")
                        continue
                
                doc.close()
                os.remove(tmp_path)
                
                # Save catalogue
                save_catalogue(
                    country_code, store, catalogue_name,
                    valid_from, valid_until, fine_print,
                    total_pages, total_products
                )
                
                # Mark job as done
                requests.patch(
                    f"{Config.SUPABASE_URL}/rest/v1/jobs?id=eq.{job_id}",
                    headers={**db_headers(), "Prefer": "return=minimal"},
                    json={"status": "done"}
                )
                
                logger.info(f"Job {job_id} completed: {total_products} products")
                
            except Exception as e:
                logger.error(f"Job {job_id} failed: {e}")
                requests.patch(
                    f"{Config.SUPABASE_URL}/rest/v1/jobs?id=eq.{job_id}",
                    headers={**db_headers(), "Prefer": "return=minimal"},
                    json={"status": "error"}
                )
        
        thread = threading.Thread(target=process)
        thread.daemon = True
        thread.start()
    
    return jsonify({
        "job_id": job_id,
        "total_pages": total_pages,
        "start_page": start_page
    })


@app.route('/status/<job_id>')
def job_status(job_id):
    """Get job status"""
    response = requests.get(
        f"{Config.SUPABASE_URL}/rest/v1/jobs?id=eq.{job_id}",
        headers=db_headers()
    )
    
    if response.status_code == 200 and response.json():
        return jsonify(response.json()[0])
    
    return jsonify({"error": "Job not found"}), 404


# ============================================================================
# WEBSOCKET FOR REAL-TIME CHAT
# ============================================================================

@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    device_id = request.args.get('device_id', request.remote_addr)
    country_code = request.args.get('country', 'hr')
    
    join_room(f"user_{device_id}")
    logger.info(f"Client connected: {device_id} ({country_code})")
    
    # Send welcome message
    translations = get_translations(country_code)
    emit('message', {
        'type': 'system',
        'message': translations['chat_greeting'],
        'suggestions': translations['chat_suggestion_items'][:3]
    })


@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection"""
    device_id = request.args.get('device_id', request.remote_addr)
    leave_room(f"user_{device_id}")
    logger.info(f"Client disconnected: {device_id}")


@socketio.on('message')
def handle_message(data):
    """Handle incoming chat message"""
    device_id = request.args.get('device_id', request.remote_addr)
    country_code = request.args.get('country', 'hr')
    
    message = data.get('text', '').strip()
    
    if not message:
        return
    
    logger.info(f"Chat message from {device_id}: {message}")
    
    # Show typing indicator
    emit('typing', {'status': True}, room=f"user_{device_id}")
    
    try:
        # Search for products
        products = search_products(message, country_code, 5)
        
        # Prepare context
        context = "PRODUCTS:\n"
        if products:
            for p in products:
                context += f"- {p.get('store')}: {p.get('product')} - {p.get('sale_price')}€ (str. {p.get('page_number')})\n"
        else:
            context += "No products found.\n"
        
        # Ask Gemini
        reply = ask_gemini(message, context, country_code)
        
        # Extract page numbers
        page_numbers = re.findall(r'stranic[ea] (\d+)', reply, re.IGNORECASE)
        page_numbers = [int(p) for p in page_numbers if 1 <= int(p) <= 500]
        
        # Send response
        emit('message', {
            'type': 'bot',
            'message': reply,
            'products': products,
            'page_numbers': page_numbers[:3]
        }, room=f"user_{device_id}")
        
        # Update user
        user = get_or_create_user(device_id, country_code)
        update_user(device_id, {"total_searches": (user.get('total_searches', 0) + 1)})
        
    except Exception as e:
        logger.error(f"Chat error: {e}")
        translations = get_translations(country_code)
        emit('message', {
            'type': 'error',
            'message': translations['msg_error']
        }, room=f"user_{device_id}")
    
    finally:
        emit('typing', {'status': False}, room=f"user_{device_id}")


# ============================================================================
# STATIC FILES
# ============================================================================

@app.route('/static/<path:path>')
def serve_static(path):
    """Serve static files"""
    return send_from_directory('static', path)


@app.route('/manifest.json')
def serve_manifest():
    """Serve PWA manifest"""
    config = get_country_config()
    
    manifest = {
        "name": f"katalog.{config.COUNTRY_CODE}",
        "short_name": f"katalog.{config.COUNTRY_CODE}",
        "description": "Pametni pomoćnik za kupovinu",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#111111",
        "theme_color": "#00ff88",
        "orientation": "portrait",
        "icons": [
            {
                "src": "/static/icons/icon-72.png",
                "sizes": "72x72",
                "type": "image/png"
            },
            {
                "src": "/static/icons/icon-96.png",
                "sizes": "96x96",
                "type": "image/png"
            },
            {
                "src": "/static/icons/icon-128.png",
                "sizes": "128x128",
                "type": "image/png"
            },
            {
                "src": "/static/icons/icon-144.png",
                "sizes": "144x144",
                "type": "image/png"
            },
            {
                "src": "/static/icons/icon-152.png",
                "sizes": "152x152",
                "type": "image/png"
            },
            {
                "src": "/static/icons/icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any maskable"
            },
            {
                "src": "/static/icons/icon-384.png",
                "sizes": "384x384",
                "type": "image/png"
            },
            {
                "src": "/static/icons/icon-512.png",
                "sizes": "512x512",
                "type": "image/png"
            }
        ]
    }
    
    return jsonify(manifest)


@app.route('/service-worker.js')
def serve_service_worker():
    """Serve service worker"""
    return send_from_directory('static', 'service-worker.js')


# ============================================================================
# ERROR HANDLERS
# ============================================================================

@app.errorhandler(404)
def not_found(error):
    """Handle 404 errors"""
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def server_error(error):
    """Handle 500 errors"""
    logger.error(f"Server error: {error}")
    return jsonify({"error": "Internal server error"}), 500


# ============================================================================
# MAIN
# ============================================================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=True)
