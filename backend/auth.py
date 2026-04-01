import os, requests, json
from flask import Blueprint, request, jsonify
from datetime import datetime

auth_bp = Blueprint('auth', __name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_VERIFY_SID = os.environ.get("TWILIO_VERIFY_SID", "")

def db_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

@auth_bp.route("/api/auth/send-otp", methods=["POST"])
def send_otp():
    data = request.json or {}
    phone = data.get("phone", "").strip()
    if not phone:
        return jsonify({"error": "Phone required"}), 400

    try:
        r = requests.post(
            f"https://verify.twilio.com/v2/Services/{TWILIO_VERIFY_SID}/Verifications",
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            data={"To": phone, "Channel": "sms"}
        )
        if r.status_code == 201:
            return jsonify({"ok": True})
        return jsonify({"error": r.json().get("message", "Failed")}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@auth_bp.route("/api/auth/verify-otp", methods=["POST"])
def verify_otp():
    data = request.json or {}
    phone = data.get("phone", "").strip()
    code = data.get("code", "").strip()

    if not phone or not code:
        return jsonify({"error": "Phone and code required"}), 400

    try:
        r = requests.post(
            f"https://verify.twilio.com/v2/Services/{TWILIO_VERIFY_SID}/VerificationCheck",
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            data={"To": phone, "Code": code}
        )
        result = r.json()
        print(f"TWILIO VERIFY RESPONSE: {result}")
        if result.get("status") == "approved":
            try:
                existing = requests.get(
                    f"{SUPABASE_URL}/rest/v1/users?phone=eq.{requests.utils.quote(phone)}&limit=1",
                    headers=db_headers()
                ).json()

                if existing and len(existing) > 0:
                    user_id = existing[0].get("id")
                else:
                    new_user = {
                        "phone": phone,
                        "total_searches": 0,
                        "last_active": datetime.now().isoformat()
                    }
                    created = requests.post(
                        f"{SUPABASE_URL}/rest/v1/users",
                        headers=db_headers(),
                        json=new_user
                    )
                    print(f"User creation: {created.status_code} {created.text[:200]}")
                    if created.status_code in (200, 201):
                        user_id = created.json()[0].get("id") if isinstance(created.json(), list) else None
                    else:
                        user_id = None
            except Exception as e:
                print(f"User creation error: {e}")
                user_id = None

            return jsonify({"ok": True, "user_id": user_id, "phone": phone})
        
        return jsonify({"error": "Invalid code"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@auth_bp.route("/api/auth/track-scan", methods=["POST"])
def track_scan():
    data = request.json or {}
    phone = data.get("phone")
    barcode = data.get("barcode")
    product_name = data.get("product_name")
    
    if not phone or not barcode:
        return jsonify({"ok": False}), 400

    try:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/users?phone=eq.{requests.utils.quote(phone)}",
            headers={**db_headers(), "Prefer": "return=minimal"},
            json={
                "total_searches": None,
                "last_active": datetime.now().isoformat()
            }
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False}), 500
