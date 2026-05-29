"""
Fyers API V3 - Automated Login
ORB Strategy V2 | Balfund Trading Private Limited
================================================
Uses the proven working login flow from orb_option_seller.py
"""

import time
import requests
import pyotp
import urllib.parse
from fyers_apiv3 import fyersModel

# ============================================================
# CREDENTIALS — FILL THESE FOR VSCODE TESTING
# ============================================================
APP_ID = ""
APP_TYPE = "200"
SECRET_KEY = ""
FYERS_ID = ""
TOTP_SECRET = ""
PIN = ""
REDIRECT_URL = "https://trade.fyers.in/api-login/redirect-uri/index.html"
CLIENT_ID = f"{APP_ID}-{APP_TYPE}"

# Aliases for backward compat
FY_ID = FYERS_ID
TOTP_KEY = TOTP_SECRET

# Browser-like headers required by Fyers auth endpoints
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36",
    "Content-Type": "application/json",
}


def auto_login():
    """
    Automated Fyers login using TOTP — returns access_token string.
    Proven working flow: send_login_otp → verify_otp → verify_pin → token → generate_token
    """
    print("\n" + "=" * 60)
    print("  FYERS API V3 - Automated Login")
    print("  ORB Strategy V2 | Balfund Trading Pvt. Ltd.")
    print("=" * 60)

    if not APP_ID or not SECRET_KEY:
        print("  ✗ APP_ID / SECRET_KEY not set.")
        return None

    # Use runtime CLIENT_ID (may have been patched by GUI)
    client_id = CLIENT_ID or f"{APP_ID}-{APP_TYPE}"
    fyers_id = FYERS_ID or FY_ID
    totp_secret = TOTP_SECRET or TOTP_KEY
    pin = PIN

    print(f"\n  Client ID: {client_id}")
    print(f"  Fyers ID:  {fyers_id}")

    # ── Step 1: Create session for later token exchange ──
    session = fyersModel.SessionModel(
        client_id=client_id,
        secret_key=SECRET_KEY,
        redirect_uri=REDIRECT_URL,
        response_type="code",
        grant_type="authorization_code",
    )
    session.generate_authcode()
    print(f"\n[1/6] Session created.")

    # ── Step 2: Send Login OTP ──
    res = requests.post(
        "https://api-t2.fyers.in/vagator/v2/send_login_otp",
        json={"fy_id": fyers_id, "app_id": "2"},
        headers=HEADERS,
    )
    data = res.json()
    if "request_key" not in data:
        print(f"  ✗ Send OTP failed: {data}")
        return None
    request_key = data["request_key"]
    print(f"[2/6] OTP request sent. ✓")

    # ── Step 3: Verify TOTP (with retry) ──
    totp = pyotp.TOTP(totp_secret).now()
    request_key_2 = None
    for attempt in range(1, 4):
        res = requests.post(
            "https://api-t2.fyers.in/vagator/v2/verify_otp",
            json={"request_key": request_key, "otp": totp},
            headers=HEADERS,
        )
        data = res.json()
        if "request_key" in data:
            request_key_2 = data["request_key"]
            break
        print(f"  Attempt {attempt} failed: {data}")
        time.sleep(1)
        totp = pyotp.TOTP(totp_secret).now()

    if not request_key_2:
        print("  ✗ TOTP verification failed after 3 attempts")
        return None
    print(f"[3/6] TOTP verified. ✓")

    # ── Step 4: Verify PIN ──
    res = requests.post(
        "https://api-t2.fyers.in/vagator/v2/verify_pin",
        json={
            "request_key": request_key_2,
            "identity_type": "pin",
            "identifier": pin,
        },
        headers=HEADERS,
    )
    data = res.json()
    if "data" not in data or "access_token" not in data.get("data", {}):
        print(f"  ✗ Verify PIN failed: {data}")
        return None
    trade_token = data["data"]["access_token"]
    print(f"[4/6] PIN verified. ✓")

    # ── Step 5: Get auth code ──
    res = requests.post(
        "https://api-t1.fyers.in/api/v3/token",
        json={
            "fyers_id": fyers_id,
            "app_id": APP_ID,
            "redirect_uri": REDIRECT_URL,
            "appType": APP_TYPE,
            "code_challenge": "",
            "state": "sample_state",
            "scope": "",
            "nonce": "",
            "response_type": "code",
            "create_cookie": True,
        },
        headers={**HEADERS, "Authorization": f"Bearer {trade_token}"},
    )
    data = res.json()
    url_str = data.get("Url", "") or data.get("url", "")
    parsed = urllib.parse.urlparse(url_str)
    auth_code = urllib.parse.parse_qs(parsed.query).get("auth_code", [""])[0]
    if not auth_code:
        print(f"  ✗ Auth code not found: {data}")
        return None
    print(f"[5/6] Auth code obtained. ✓")

    # ── Step 6: Exchange auth code for final access token ──
    session.set_token(auth_code)
    response = session.generate_token()
    access_token = response.get("access_token", "")
    if not access_token:
        print(f"  ✗ Token generation failed: {response}")
        return None

    print(f"[6/6] ✅ Login successful!\n")
    return access_token


if __name__ == "__main__":
    token = auto_login()
    if token:
        print(f"Token: {token[:20]}...")
