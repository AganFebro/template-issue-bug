"""
ZCode Auto Register — Playwright Google OAuth on chat.z.ai.

Flow:
  1. Start a localhost callback server on a random port
  2. Build chat.z.ai authorize URL
  3. Playwright navigates → clicks "Continue with Google" → fills email+password
  4. Google consent accepted automatically
  5. chat.z.ai redirects to localhost callback with authCode
  6. Exchange authCode at zcode.z.ai → access token + JWT
  7. Resolve coding-plan API key via biz API
  8. Save credentials to ~/.zcode-proxy/credentials.json (proxy-compatible format)

Usage:
  python zcode_register.py                      # read email.txt, use password from config.json
  python zcode_register.py email@gmail.com      # register a single email
  python zcode_register.py --headful            # show browser (debug mode)
"""

import asyncio
import hashlib
import json
import os
import platform
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlparse

import requests
from playwright.async_api import async_playwright

# ──────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────
CONFIG_FILE = "config.json"
DEFAULT_PASSWORD = "masuk123"

EMAIL_FILE = "email.txt"
HEADFUL = "--headful" in sys.argv
SINGLE_EMAIL = next((a for a in sys.argv[1:] if "@" in a), None)


def load_config():
    cfg = {"password": DEFAULT_PASSWORD}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                cfg.update(json.load(f))
        except:
            pass
    return cfg


config = load_config()
PASSWORD = config.get("password", DEFAULT_PASSWORD)

# ──────────────────────────────────────────────────────────────
# CONSTANTS (mirrors src/auth/oauth.ts and src/auth/resolver.ts)
# ──────────────────────────────────────────────────────────────
AUTHORIZE_URL = "https://chat.z.ai/api/oauth/authorize"
TOKEN_URL = "https://zcode.z.ai/api/v1/oauth/token"
APP_ID = "client_P8X5CMWmlaRO9gyO-KSqtg"

# ZAI biz API
ZAI_HOST = "https://api.z.ai"
ZAI_LOGIN = f"{ZAI_HOST}/api/auth/z/login"
API_KEY_NAME = "zcode-api-key"
DEFAULT_ORG_MARKER = "\u9ed8\u8ba4\u673a\u6784"  # 默认机构
DEFAULT_PROJECT_MARKER = "\u9ed8\u8ba4\u9879\u76ee"  # 默认项目

# Proxy credential store
STORE_DIR = os.path.join(os.path.expanduser("~"), ".zcode-proxy")
STORE_FILE = os.path.join(STORE_DIR, "credentials.json")

# ──────────────────────────────────────────────────────────────
# CALLBACK SERVER (catches the authCode redirect from chat.z.ai)
# ──────────────────────────────────────────────────────────────
CALLBACK_RESULT = {"code": None, "error": None, "received": False}


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query) if parsed.query else {}
        code = (qs.get("authCode") or qs.get("code") or [""])[0]
        state = (qs.get("state") or [""])[0]
        error = (qs.get("error") or [""])[0]

        if code:
            CALLBACK_RESULT["code"] = code
            CALLBACK_RESULT["received"] = True
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Authorization successful! You may close this window.")
        else:
            CALLBACK_RESULT["error"] = error or "no code"
            self.send_response(400)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress logs


def start_callback_server():
    """Start HTTP server on a random port. Returns (server, port)."""
    server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def build_authorize_url(callback_port: int) -> str:
    """Build the chat.z.ai authorize URL with localhost callback."""
    redirect_uri = f"http://127.0.0.1:{callback_port}/oauth/callback/zai"
    state = hashlib.sha256(os.urandom(32)).hexdigest()
    params = {
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "client_id": APP_ID,
        "state": state,
    }
    from urllib.parse import urlencode

    return f"{AUTHORIZE_URL}?{urlencode(params)}", state, redirect_uri


# ──────────────────────────────────────────────────────────────
# TOKEN EXCHANGE + API KEY RESOLUTION (mirrors oauth.ts + resolver.ts)
# ──────────────────────────────────────────────────────────────
def exchange_code(auth_code: str, redirect_uri: str, state: str) -> dict:
    """POST zcode.z.ai token exchange. Returns {accessToken, userId, jwt} or raises."""
    resp = requests.post(
        TOKEN_URL,
        json={
            "provider": "zai",
            "code": auth_code,
            "redirect_uri": redirect_uri,
            "state": state,
        },
        headers={"content-type": "application/json"},
        timeout=30,
    )
    data = resp.json()
    if resp.status_code != 200 or data.get("code", 0) != 0:
        raise RuntimeError(
            f"Token exchange failed: {resp.status_code} {data.get('msg', '')}"
        )
    result = {
        "accessToken": data["data"]["zai"]["access_token"],
        "userId": data["data"].get("user", {}).get("user_id"),
        "jwt": data["data"].get("token", "").strip() or None,
    }
    return result


def resolve_zai_biz_token(access_token: str) -> str:
    """Exchange Z.AI access token for biz token."""
    resp = requests.post(
        ZAI_LOGIN,
        json={"token": access_token},
        headers={"content-type": "application/json"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"z/login failed: {resp.status_code}")
    data = resp.json()
    return (
        data.get("access_token")
        or data.get("accessToken")
        or data.get("data", {}).get("access_token")
    )


def resolve_customer_info(host: str, authorization: str) -> tuple:
    """Get org + project. Returns (orgId, projectId)."""
    resp = requests.get(
        f"{host}/api/biz/customer/getCustomerInfo",
        headers={"Authorization": authorization, "Content-Type": "application/json"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"getCustomerInfo failed: {resp.status_code}")
    data = resp.json()
    orgs = data.get("data", data).get("organizations", data.get("orgs", []))
    if not orgs:
        raise RuntimeError("No organizations found")
    org = next(
        (
            o
            for o in orgs
            if DEFAULT_ORG_MARKER in (o.get("organizationName") or o.get("name", ""))
        ),
        orgs[0],
    )
    org_id = org.get("organizationId") or org.get("id") or org.get("orgId")
    projects = org.get("projects", [])
    if not projects:
        raise RuntimeError("No projects found")
    project = next(
        (
            p
            for p in projects
            if DEFAULT_PROJECT_MARKER in (p.get("projectName") or p.get("name", ""))
        ),
        projects[0],
    )
    project_id = project.get("projectId") or project.get("id")
    return org_id, project_id


def find_or_create_api_key(
    host: str, authorization: str, org_id: str, project_id: str
) -> str:
    """Find or create an API key. Returns apiKey."""
    list_url = f"{host}/api/biz/v1/organization/{org_id}/projects/{project_id}/api_keys"
    try:
        resp = requests.get(
            list_url,
            headers={
                "Authorization": authorization,
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        if resp.status_code == 200:
            items = resp.json().get("data", [])
            found = next((k for k in items if k.get("name") == API_KEY_NAME), None)
            if found and found.get("apiKey"):
                return found["apiKey"]
    except:
        pass
    resp = requests.post(
        list_url,
        json={"name": API_KEY_NAME},
        headers={"Authorization": authorization, "Content-Type": "application/json"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"API key creation failed: {resp.status_code}")
    return resp.json().get("data", {}).get("apiKey")


def get_secret_key(
    host: str, authorization: str, org_id: str, project_id: str, api_key: str
) -> str:
    """Get secret key for an API key."""
    from urllib.parse import quote

    url = f"{host}/api/biz/v1/organization/{org_id}/projects/{project_id}/api_keys/copy/{quote(api_key, safe='')}"
    resp = requests.get(
        url,
        headers={"Authorization": authorization, "Content-Type": "application/json"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"getSecretKey failed: {resp.status_code}")
    data = resp.json()
    return data.get("data", {}).get("secretKey") or data.get("data", {}).get(
        "secret_key", ""
    )


def resolve_coding_plan_credential(access_token: str, user_id: str | None) -> dict:
    """Full API key resolution. Returns {apiKey, secret, userId, jwt}."""
    biz_token = resolve_zai_biz_token(access_token)
    authorization = f"Bearer {biz_token}"
    org_id, project_id = resolve_customer_info(ZAI_HOST, authorization)
    api_key = find_or_create_api_key(ZAI_HOST, authorization, org_id, project_id)
    secret = ""
    try:
        secret = get_secret_key(ZAI_HOST, authorization, org_id, project_id, api_key)
    except:
        pass
    return {"apiKey": api_key, "secret": secret, "userId": user_id}


# ──────────────────────────────────────────────────────────────
# CREDENTIAL STORE (mirrors src/auth/store.ts)
# ──────────────────────────────────────────────────────────────
def encrypt_credential(cred: dict) -> dict:
    """AES-GCM encryption matching the proxy store.ts XOR-based key derivation."""
    import base64 as b64

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    # Mirror Bun's process.platform + process.arch
    _platform = sys.platform  # "win32" on Windows (matches Bun)
    _arch = (
        "x64"
        if platform.machine().lower() in ("amd64", "x86_64")
        else platform.machine()
    )
    seed = os.environ.get(
        "ZCODE_PROXY_CREDENTIAL_SECRET",
        f"{os.path.expanduser('~')}-{_platform}-{_arch}",
    )
    # Mirror store.ts: XOR each seed byte into a 32-byte buffer
    seed_bytes = seed.encode("utf-8")
    key = bytearray(32)
    for i, b in enumerate(seed_bytes):
        key[i % 32] ^= b
    key = bytes(key)
    iv = os.urandom(12)
    aesgcm = AESGCM(key)
    plaintext = json.dumps(cred, ensure_ascii=False).encode("utf-8")
    encrypted = aesgcm.encrypt(iv, plaintext, None)
    combined = iv + encrypted
    return {"encrypted": b64.b64encode(combined).decode("ascii")}


def save_credential(cred: dict) -> None:
    """Save credential in proxy-compatible encrypted format."""
    os.makedirs(STORE_DIR, exist_ok=True)
    encrypted = encrypt_credential(cred)
    with open(STORE_FILE, "w", encoding="utf-8") as f:
        json.dump(encrypted, f)
    os.chmod(STORE_FILE, 0o600)


# ──────────────────────────────────────────────────────────────
# PLAYWRIGHT AUTO-LOGIN
# ──────────────────────────────────────────────────────────────
async def register_zcode(email: str, password: str) -> dict | None:
    """Register/log into zcode via Google OAuth. Returns credential dict or None."""
    server, port = start_callback_server()
    authorize_url, state, redirect_uri = build_authorize_url(port)
    print(f"  Callback : http://127.0.0.1:{port}/...")
    print(f"  State    : {state[:16]}...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not HEADFUL)
        try:
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                locale="en-US",
            )
            page = await context.new_page()

            # Step 1: Navigate to authorize URL → chat.z.ai login page
            print(f"  ├─ Opening chat.z.ai authorize page...")
            await page.goto(authorize_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

            # Step 2: Click "Continue with Google"
            print(f"  ├─ Clicking Continue with Google...")
            google_btn = page.locator(
                'button:has-text("Google"), a:has-text("Google"), [data-provider="google"]'
            ).first
            try:
                await google_btn.wait_for(state="visible", timeout=10000)
                await google_btn.click()
            except:
                # Try alternative selectors
                btns = await page.locator("button, a.button, [role='button']").all()
                for btn in btns:
                    try:
                        text = (await btn.inner_text()).strip().lower()
                        if "google" in text:
                            await btn.click()
                            break
                    except:
                        pass
            await asyncio.sleep(3)

            # Step 3: Google login — fill email
            print(f"  ├─ Filling Google email: {email}")
            try:
                await page.wait_for_selector("#identifierId", timeout=15000)
            except:
                await page.wait_for_selector('input[type="email"]', timeout=15000)
            await asyncio.sleep(1)

            email_input = page.locator("#identifierId")
            if not await email_input.is_visible(timeout=3000):
                email_input = page.locator('input[type="email"]')
            await email_input.click()
            await email_input.fill("")
            await email_input.type(email, delay=60)
            await asyncio.sleep(1)
            next_btn = page.locator("#identifierNext")
            if await next_btn.is_visible(timeout=3000):
                await next_btn.click()
            else:
                await page.keyboard.press("Enter")
            await asyncio.sleep(3)

            # Step 4: Google login — fill password
            print(f"  ├─ Filling Google password...")
            try:
                await page.wait_for_selector('input[type="password"]', timeout=15000)
            except:
                # Might be "account not found" or direct redirect
                if "chat.z.ai" in page.url or "z.ai" in page.url:
                    print(f"  ├─ Already logged in, skipping password...")
                else:
                    raise RuntimeError("Could not find password input")
            await asyncio.sleep(1)

            password_input = page.locator('input[type="password"]')
            if await password_input.is_visible(timeout=5000):
                await password_input.click()
                await password_input.type(password, delay=60)
                await asyncio.sleep(1)
                next_btn = page.locator("#passwordNext")
                if await next_btn.is_visible(timeout=3000):
                    await next_btn.click()
                else:
                    await page.keyboard.press("Enter")
                await asyncio.sleep(3)

            # Step 5: Handle Google consent / terms of service
            print(f"  ├─ Handling consent screens...")
            for _ in range(5):
                await asyncio.sleep(2)
                try:
                    # Workspace terms of service
                    if "workspacetermsofservice" in page.url or "speedbump" in page.url:
                        await page.evaluate(
                            "window.scrollTo(0, document.body.scrollHeight)"
                        )
                        await page.locator(
                            'button:has-text("I understand"), button:has-text("Understand"), input[type="submit"]'
                        ).first.click(timeout=5000)
                        continue
                    # Consent / Allow button
                    consent = page.locator(
                        'button:has-text("Continue"), button:has-text("Allow"), button:has-text("Agree"), button:has-text("Lanjutkan")'
                    )
                    if await consent.first.is_visible(timeout=3000):
                        await consent.first.click()
                        continue
                    # Safety check
                    safety = page.locator(
                        'a:has-text("Not now"), button:has-text("Not now")'
                    )
                    if await safety.first.is_visible(timeout=3000):
                        await safety.first.click()
                        continue
                    # Recovery
                    recovery = page.locator(
                        'button:has-text("Confirm"), button:has-text("Yes")'
                    )
                    if await recovery.first.is_visible(timeout=3000):
                        await recovery.first.click()
                        continue
                    break
                except:
                    break

            # Step 6: chat.z.ai consent page (https://chat.z.ai/auth/oauth/authorize)
            # Shows a checkbox + disabled Continue button — tick to enable
            try:
                await asyncio.sleep(2)
                if "chat.z.ai/auth/oauth/authorize" in page.url:
                    print(f"  ├─ On z.ai consent page, ticking checkbox...")
                    cb = page.locator('input[type="checkbox"]').first
                    await cb.wait_for(state="attached", timeout=8000)
                    await cb.click(force=True, timeout=5000)
                    await asyncio.sleep(1)
                    await page.locator('button:has-text("Continue")').last.click(
                        timeout=5000
                    )
                    print(f"  ├─ Continue clicked")
                    await asyncio.sleep(3)
            except:
                pass

            # Step 7: Wait for callback to receive auth code
            print(f"  ├─ Waiting for OAuth callback...")
            for _ in range(30):
                if CALLBACK_RESULT["received"]:
                    break
                await asyncio.sleep(1)

            if not CALLBACK_RESULT["received"]:
                print(f"  ├─ Callback not received. Current URL: {page.url[:120]}")
                # Try to extract authCode from URL as fallback
                if "authCode=" in page.url or "code=" in page.url:
                    from urllib.parse import parse_qs, urlparse

                    qs = parse_qs(urlparse(page.url).query)
                    code = (qs.get("authCode") or qs.get("code") or [""])[0]
                    if code and code not in (CALLBACK_RESULT["state"] or ""):
                        CALLBACK_RESULT["code"] = code
                        CALLBACK_RESULT["received"] = True
                    else:
                        # The callback might have been intercepted — check if there was a network response
                        pass

            if not CALLBACK_RESULT["code"]:
                print(f"  └─ Failed: no auth code received")
                print(f"     URL: {page.url[:150]}")
                return None

            auth_code = CALLBACK_RESULT["code"]
            print(f"  ├─ Auth code received (len {len(auth_code)})")

            # Step 7: Exchange code for token + JWT
            print(f"  ├─ Exchanging code for token...")
            token_data = exchange_code(auth_code, redirect_uri, state)
            print(f"  ├─ Token received")

            # Step 8: Resolve coding-plan API key
            print(f"  ├─ Resolving API key...")
            api_cred = resolve_coding_plan_credential(
                token_data["accessToken"], token_data.get("userId")
            )
            api_cred["jwt"] = token_data.get("jwt")
            api_cred["provider"] = "zai"
            print(f"  ├─ API key: {api_cred['apiKey'][:12]}...")

            # Step 9: Save
            print(f"  ├─ Saving to {STORE_FILE}...")
            save_credential(api_cred)
            print(f"  └─ Done! {email} registered successfully")

            return api_cred
        finally:
            # Reset callback state for next run
            CALLBACK_RESULT["code"] = None
            CALLBACK_RESULT["received"] = False
            CALLBACK_RESULT["error"] = None
            await browser.close()
            server.shutdown()


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────
async def main():
    if SINGLE_EMAIL:
        print(f"Registering single email: {SINGLE_EMAIL}")
        result = await register_zcode(SINGLE_EMAIL, PASSWORD)
        if result:
            print(f"\nDone! API Key: {result['apiKey'][:12]}...")
        else:
            print("\nFailed!")
            sys.exit(1)
        return

    if not os.path.exists(EMAIL_FILE):
        print(f"  {EMAIL_FILE} not found. Create it with one Gmail address per line.")
        sys.exit(1)

    with open(EMAIL_FILE, "r") as f:
        emails = [line.strip() for line in f if line.strip() and "@" in line]

    if not emails:
        print(f"  No emails in {EMAIL_FILE}")
        sys.exit(1)

    print(f"Registering {len(emails)} accounts from {EMAIL_FILE}")
    for i, email in enumerate(emails):
        print(f"\n[{i + 1}/{len(emails)}] {email}")
        try:
            await register_zcode(email, PASSWORD)
        except Exception as e:
            print(f"  Error: {e}")
            traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
