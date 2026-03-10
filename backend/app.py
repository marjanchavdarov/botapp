"""
katalog.ai - Simplified working version
"""

import os
import json
import logging
from datetime import date, datetime, timedelta
from flask import Flask, request, jsonify
import requests

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ============================================================================
# CONFIGURATION
# ============================================================================

class Config:
    SUPABASE_URL = os.environ.get('SUPABASE_URL', 'https://172.64.149.246')
    SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')
    GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
    SUPABASE_HOST = os.environ.get('SUPABASE_HOST', 'jwuifezafytihgzepylq.supabase.co')

# ============================================================================
# DATABASE HELPERS
# ============================================================================

def db_headers():
    """Simple database headers"""
    return {
        "apikey": Config.SUPABASE_KEY,
        "Authorization": f"Bearer {Config.SUPABASE_KEY}",
        "Content-Type": "application/json"
    }

def supabase_get(path, params=None):
    """Make GET request to Supabase"""
    headers = db_headers()
    headers['Host'] = Config.SUPABASE_HOST
    
    url = f"{Config.SUPABASE_URL}{path}"
    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        return response
    except Exception as e:
        logger.error(f"Supabase GET failed: {e}")
        return None

# ============================================================================
# SIMPLE ROUTES
# ============================================================================

@app.route('/')
def home():
    return jsonify({
        "status": "ok",
        "message": "katalog.ai is running",
        "endpoints": [
            "/upload-tool",
            "/api/country",
            "/debug/health"
        ]
    })

@app.route('/debug/health')
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})

@app.route('/api/country')
def get_country():
    """Get Croatia configuration"""
    return jsonify({
        "code": "hr",
        "name": "croatia",
        "language": "hr",
        "currency": "€",
        "date_format": "%d.%m.%Y.",
        "stores": [
            {"id": "lidl", "name": "Lidl", "color": "#0050aa"},
            {"id": "kaufland", "name": "Kaufland", "color": "#e30613"},
            {"id": "spar", "name": "Spar", "color": "#1e6b3b"},
            {"id": "konzum", "name": "Konzum", "color": "#ed1c24"},
            {"id": "dm", "name": "dm", "color": "#e31837"},
            {"id": "plodine", "name": "Plodine", "color": "#009640"}
        ]
    })

@app.route('/upload-tool')
def upload_tool():
    return '''
    <!DOCTYPE html>
    <html>
    <head><title>katalog.ai Upload</title>
    <style>
        body{background:#111;color:#eee;font-family:monospace;padding:40px}
        h1{color:#00ff88}
        input,select{background:#222;border:1px solid #444;color:#eee;padding:8px;width:100%;margin:5px 0 15px}
        button{background:#00ff88;color:#000;border:none;padding:15px;font-weight:bold;width:100%;cursor:pointer}
        #log{background:#000;padding:20px;margin-top:20px;min-height:100px}
    </style>
    </head>
    <body>
        <h1>📤 katalog.ai Upload Tool</h1>
        <p>Upload tool is loading... (backend is working!)</p>
        <div id="log">Status: Connected to backend ✅</div>
        <script>
            fetch('/api/country')
                .then(r => r.json())
                .then(data => {
                    document.getElementById('log').innerHTML += '<br>✅ Loaded ' + data.stores.length + ' stores for ' + data.name;
                })
                .catch(e => {
                    document.getElementById('log').innerHTML += '<br>❌ Error: ' + e;
                });
        </script>
    </body>
    </html>
    '''

@app.route('/debug/supabase')
def debug_supabase():
    """Test Supabase connection"""
    try:
        # Test with IP + Host header
        headers = db_headers()
        headers['Host'] = Config.SUPABASE_HOST
        
        response = requests.get(
            f"{Config.SUPABASE_URL}/rest/v1/",
            headers=headers,
            timeout=5
        )
        
        return jsonify({
            "status": "connected" if response.status_code < 500 else "error",
            "status_code": response.status_code,
            "using_ip": Config.SUPABASE_URL,
            "using_host": Config.SUPABASE_HOST
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e),
            "type": type(e).__name__
        }), 500

# ============================================================================
# MAIN
# ============================================================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
