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

import proxy_utils

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
# Outbound HTTP/SOCKS proxy (--proxy <url> or config.json "proxies"), if any.
SESSION = proxy_utils.create_session(config)

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

# Account pool (project root, plain JSON array)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
POOL_FILE = os.path.join(_SCRIPT_DIR, "..", "pool.json")


# ──────────────────────────────────────────────────────────────
# CALLBACK SERVER (catches the authCode redirect from chat.z.ai)
# ──────────────────────────────────────────────────────────────
class CallbackHandler(BaseHTTPRequestHandler):
    result: dict = None  # set by the factory

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query) if parsed.query else {}
        code = (qs.get("authCode") or qs.get("code") or [""])[0]
        error = (qs.get("error") or [""])[0]

        if code:
            handler = self.__class__
            if handler.result is not None:
                handler.result["code"] = code
                handler.result["received"] = True
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Authorization successful! You may close this window.")
        else:
            if handler.result is not None:
                handler.result["error"] = error or "no code"
            self.send_response(400)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress logs


def start_callback_server(result: dict):
    """Start HTTP server on a random port. Returns (server, port, thread)."""
    # Create a per-request handler class sharing the result dict
    handler_cls = type("Callback", (CallbackHandler,), {"result": result})
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def _url_host_path(url: str) -> str:
    """Return 'hostname + path' only (no query string).

    Query strings can carry values like `app_domain=chat.z.ai` or a
    `continue=` param embedding another URL — a naive substring check on the
    full URL (e.g. `"chat.z.ai" in page.url`) can false-positive on those
    values while still genuinely on a different page (e.g.
    accounts.google.com with `chat.z.ai` only appearing as a query value).
    Use this for all "which page are we on" checks instead.
    """
    p = urlparse(url)
    return f"{p.hostname or ''}{p.path or ''}"


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
    resp = SESSION.post(
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
    resp = SESSION.post(
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
    resp = SESSION.get(
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
        resp = SESSION.get(
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
    resp = SESSION.post(
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
    resp = SESSION.get(
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


import asyncio

# Async lock for pool.json writes (parallel registrations)
_pool_lock = asyncio.Lock()


async def append_to_pool(cred: dict, email: str) -> None:
    """Append credential to pool.json with locking (safe for parallel calls)."""
    async with _pool_lock:
        pool = []
        if os.path.exists(POOL_FILE):
            with open(POOL_FILE, "r", encoding="utf-8") as f:
                pool = json.load(f)
        existing = next((a for a in pool if a.get("email") == email), None)
        cred_copy = {k: v for k, v in cred.items()}
        cred_copy["email"] = email
        if existing:
            existing.update(cred_copy)
        else:
            pool.append(cred_copy)
        with open(POOL_FILE, "w", encoding="utf-8") as f:
            json.dump(pool, f, indent=2, ensure_ascii=False)
    print(f"  ├─ Appended to pool.json ({len(pool)} accounts)")


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
async def _handle_zai_consent_checkbox(page) -> bool:
    """Tick the checkbox + click Continue on chat.z.ai's consent page
    (https://chat.z.ai/auth/oauth/authorize). Returns True if handled.

    Extracted so both Step 6 (first arrival) and Step 7 (a repeat arrival,
    e.g. after Google detours through a TOS/challenge page) can reuse it.
    """
    try:
        print(f"  ├─ On z.ai consent page, ticking checkbox...")
        cb = page.locator('input[type="checkbox"]').first
        await cb.wait_for(state="attached", timeout=8000)
        await cb.click(force=True, timeout=5000)
        await asyncio.sleep(1)
        await page.locator('button:has-text("Continue")').last.click(timeout=5000)
        print(f"  ├─ Continue clicked")
        await asyncio.sleep(3)
        return True
    except Exception as e:
        print(f"  ├─ z.ai consent checkbox failed: {e}")
        return False


async def _handle_google_challenge(page, password: str) -> None:
    """Handle Google security challenge page (signin/challenge/pwd).

    Google may ask to re-enter the password or confirm identity after
    detecting unusual activity (headless browser, multiple logins).
    """
    try:
        # Re-enter password if prompt is shown
        pw = page.locator('input[type="password"]')
        if await pw.is_visible(timeout=3000):
            await pw.click()
            await pw.fill("")
            await pw.type(password, delay=60)
            await asyncio.sleep(1)
            await page.keyboard.press("Enter")
            await asyncio.sleep(3)
            return
    except:
        pass
    try:
        # Click "Next" or "Confirm" if no password prompt
        for btn_text in ["Next", "Confirm", "Verify", "Continue", "Lanjutkan"]:
            btn = page.locator(
                f'button:has-text("{btn_text}"), span:has-text("{btn_text}")'
            ).first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                await asyncio.sleep(3)
                return
    except:
        pass
    # Just wait — might redirect automatically
    print(f"  ├─ Challenge page, waiting for redirect...")
    await asyncio.sleep(5)


async def register_zcode(email: str, password: str) -> dict | None:
    """Register/log into zcode via Google OAuth. Returns credential dict or None."""
    result: dict = {"code": None, "error": None, "received": False, "state": None}
    server, port = start_callback_server(result)
    authorize_url, state, redirect_uri = build_authorize_url(port)
    result["state"] = state
    print(f"  Callback : http://127.0.0.1:{port}/...")
    print(f"  State    : {state[:16]}...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=not HEADFUL, proxy=proxy_utils.playwright_proxy(config)
        )
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
                # Might be a security challenge or direct redirect
                cur = page.url
                if "chat.z.ai" in cur or "z.ai" in cur:
                    print(f"  ├─ Already logged in, skipping password...")
                elif "challenge" in cur or "captcha" in cur:
                    print(f"  ├─ Google security challenge detected, trying to pass...")
                    await _handle_google_challenge(page, password)
                else:
                    raise RuntimeError(f"Could not find password input at {cur[:80]}")
            await asyncio.sleep(1)

            # Try password input again (may have been replaced by challenge)
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
            for _ in range(8):
                await asyncio.sleep(2)
                try:
                    cur_url = _url_host_path(page.url)

                    # Already past Google — let Step 6 handle chat.z.ai consent
                    if "chat.z.ai" in cur_url or "127.0.0.1" in cur_url:
                        break

                    # Google security challenge (signin/challenge)
                    if "challenge" in cur_url:
                        print(
                            f"  ├─ Google security challenge, re-entering password..."
                        )
                        await _handle_google_challenge(page, password)
                        continue

                    # Google account picker ("Choose an account to continue")
                    # URL: accounts.google.com/v3/signin/accountchooser
                    # NOTE: /signin/oauth/id is NOT the picker — it's the consent
                    # page ("Login dengan Google / Login ke z.ai"). Treating it
                    # as a picker causes an infinite loop.
                    if "accountchooser" in cur_url:
                        print(f"  ├─ On Google account picker, selecting account...")
                        # Click the first account entry (data-identifier or account list item)
                        account = page.locator(
                            "div[data-identifier], div[data-email], li[data-identifier], "
                            "a[data-identifier], div.account"
                        ).first
                        if await account.is_visible(timeout=5000):
                            await account.click(timeout=5000)
                            await asyncio.sleep(3)
                            continue
                        # Fallback: click any visible account row
                        row = (
                            page.locator('div[role="link"], div[role="button"]')
                            .filter(has_text=email)
                            .first
                        )
                        if await row.is_visible(timeout=3000):
                            await row.click(timeout=5000)
                            await asyncio.sleep(3)
                            continue

                    # Workspace terms of service
                    if "workspacetermsofservice" in cur_url:
                        print(f"  ├─ Handling TOS agreement...")
                        await page.evaluate(
                            "window.scrollTo(0, document.body.scrollHeight)"
                        )
                        await page.locator(
                            "text=I understand"
                        ).first.click(timeout=5000)
                        continue

                    # Only check Google consent buttons when on a Google domain.
                    # Without this guard, the disabled "Continue" button on
                    # chat.z.ai's consent page matches and burns iterations.
                    if "google" not in cur_url and "accounts." not in cur_url:
                        continue

                    # Google consent / Allow / Lanjutkan button
                    consent = page.locator(
                        'button:has-text("Continue"), button:has-text("Allow"), '
                        'button:has-text("Agree"), button:has-text("Lanjutkan"), '
                        'button:has-text("Lanjut"), button:has-text("Izinkan"), '
                        'button:has-text("Setuju"), div[role="button"]:has-text("Continue"), '
                        'div[role="button"]:has-text("Lanjutkan"), '
                        '#submit_approve_access, button[name="submit_approve_access"]'
                    )
                    if await consent.first.is_visible(timeout=3000):
                        await consent.first.click()
                        continue

                    # Fallback: Google consent form submit button (may be hidden)
                    submit_btn = page.locator(
                        '#submit_approve_access, input[name="submit_approve_access"]'
                    )
                    if await submit_btn.first.count() > 0:
                        print(f"  ├─ Submitting Google consent form...")
                        await submit_btn.first.click(force=True, timeout=5000)
                        continue

                    # Safety check ("Not now")
                    safety = page.locator(
                        'a:has-text("Not now"), button:has-text("Not now")'
                    )
                    if await safety.first.is_visible(timeout=3000):
                        await safety.first.click()
                        continue

                    # Recovery / confirm
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
            # Retry for up to 20s since Google may take time to redirect here
            try:
                for _ in range(10):
                    await asyncio.sleep(2)
                    if "chat.z.ai/auth/oauth/authorize" in page.url:
                        await _handle_zai_consent_checkbox(page)
                        break
                    if result["received"]:
                        break
            except:
                pass

            # Step 7: Handle post-consent Google pages that may appear with
            # unpredictable timing: workspace ToS, signin/oauth/id confirmation,
            # or a repeat visit to the chat.z.ai consent checkbox (can happen if
            # Google detours through a TOS/challenge page after Step 6 already
            # finished polling).
            print(f"  ├─ Waiting for Google post-consent pages...")

            for _ in range(15):
                await asyncio.sleep(3)
                if result["received"]:
                    break

                # Wait for any in-flight navigation to commit
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=2000)
                except:
                    pass

                cur_url = _url_host_path(page.url)

                # Reached the real OAuth callback target — we're done here
                if "127.0.0.1" in cur_url:
                    print(f"  ├─ Step 7: redirected to {cur_url[:80]}")
                    break

                # Repeat (or first, if Step 6 missed it) visit to chat.z.ai's
                # consent checkbox page — tick + Continue, then keep polling
                # for the actual redirect to the 127.0.0.1 callback.
                if "chat.z.ai/auth/oauth/authorize" in cur_url:
                    await _handle_zai_consent_checkbox(page)
                    continue

                # Workspace TOS agreement page
                if "workspacetermsofservice" in cur_url:
                    print(f"  ├─ Handling TOS agreement...")
                    try:
                        await page.evaluate(
                            "window.scrollTo(0, document.body.scrollHeight)"
                        )
                        await asyncio.sleep(1)
                        # Google's "I understand" is a Material span, not a
                        # <button>. Unquoted text= is case-insensitive substring
                        # match — the real text is lowercase "I understand".
                        await page.locator(
                            "text=I understand"
                        ).first.click(timeout=5000)
                        print(f"  ├─ TOS I Understand clicked")
                        await asyncio.sleep(3)
                    except Exception as e:
                        print(f"  ├─ TOS click failed: {e}")
                    continue

                # Google security challenge
                if "challenge" in cur_url:
                    print(f"  ├─ Google security challenge, re-entering password...")
                    await _handle_google_challenge(page, password)
                    continue

                # signin/oauth/id — "You're signed in as..." confirmation
                if "signin/oauth/id" in cur_url:
                    print(f"  ├─ On Google signin confirmation, clicking Continue...")
                    try:
                        btn = page.locator(
                            'button:has-text("Continue"), div[role="button"]:has-text("Continue"), '
                            'button:has-text("Lanjutkan"), div[role="button"]:has-text("Lanjutkan"), '
                            'button[type="submit"]'
                        ).first
                        if await btn.is_visible(timeout=5000):
                            await btn.click(timeout=5000)
                            await asyncio.sleep(3)
                            print(f"  ├─ Continue clicked, waiting for redirect...")
                            continue
                        else:
                            await page.keyboard.press("Enter")
                            await asyncio.sleep(3)
                    except:
                        await page.keyboard.press("Enter")
                        await asyncio.sleep(3)
                    continue

                # Debug: show current URL when nothing matched
                print(f"  ├─ Step 7: waiting... ({cur_url[:100]})")
                continue

            # Step 8: Wait for callback to receive auth code
            print(f"  ├─ Waiting for OAuth callback...")
            for _ in range(30):
                if result["received"]:
                    break
                await asyncio.sleep(1)

            if not result["received"]:
                print(f"  ├─ Callback not received. Current URL: {page.url[:120]}")
                # Try to extract authCode from URL as fallback
                if "authCode=" in page.url or "code=" in page.url:
                    from urllib.parse import parse_qs, urlparse

                    qs = parse_qs(urlparse(page.url).query)
                    code = (qs.get("authCode") or qs.get("code") or [""])[0]
                    if code and code not in (result.get("state") or ""):
                        result["code"] = code
                        result["received"] = True
                    else:
                        # The callback might have been intercepted — check if there was a network response
                        pass

            if not result["code"]:
                print(f"  └─ Failed: no auth code received")
                print(f"     URL: {page.url[:150]}")
                return None

            auth_code = result["code"]
            print(f"  ├─ Auth code received (len {len(auth_code)})")

            # Step 9: Visit chat.z.ai to trigger account initialization
            # The ZCode Electron renderer loads chat.z.ai with User-Agent:
            # ZCode/3.2.2 + HTTP-Referer: https://zcode.z.ai. The server
            # checks this UA to activate the start-plan on first login.
            # Copy OAuth cookies to a new context with the ZCode UA.
            print(f"  ├─ Visiting chat.z.ai to activate account...")
            try:
                cookies = await context.cookies()
                zcode_ctx = await browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent="ZCode/3.2.4",
                    locale="en-US",
                )
                await zcode_ctx.add_cookies(cookies)
                zp = await zcode_ctx.new_page()
                await zp.set_extra_http_headers({"HTTP-Referer": "https://zcode.z.ai"})
                await zp.goto(
                    "https://chat.z.ai/", wait_until="domcontentloaded", timeout=30000
                )
                await asyncio.sleep(5)
                print(f"  ├─ chat.z.ai visited with ZCode UA")
                await zcode_ctx.close()
                print(f"  ├─ chat.z.ai session established")
            except Exception as e:
                print(f"  ├─ chat.z.ai visit warning: {e}")

            # Step 10: Exchange code for token + JWT
            print(f"  ├─ Exchanging code for token...")
            token_data = exchange_code(auth_code, redirect_uri, state)
            print(f"  ├─ Token received")

            # Step 11: Resolve coding-plan API key
            print(f"  ├─ Resolving API key...")
            api_cred = resolve_coding_plan_credential(
                token_data["accessToken"], token_data.get("userId")
            )
            api_cred["jwt"] = token_data.get("jwt")
            api_cred["provider"] = "zai"
            print(f"  ├─ API key: {api_cred['apiKey'][:12]}...")

            # Step 12: Activate start-plan (billing/balance triggers creation)
            # billing/balance is the call that activates the plan server-side;
            # billing/current is read-only. Must include ZCode desktop headers.
            print(f"  ├─ Activating start-plan...")
            plan_found = False
            try:
                r = SESSION.get(
                    "https://zcode.z.ai/api/v1/zcode-plan/billing/balance?app_version=3.2.4",
                    headers={
                        "Authorization": f"Bearer {api_cred['jwt']}",
                        "User-Agent": "ZCode/3.2.4",
                        "X-Title": "Z Code@electron",
                        "X-ZCode-Agent": "glm",
                        "X-ZCode-App-Version": "3.2.4",
                        "HTTP-Referer": "https://zcode.z.ai",
                    },
                    timeout=15,
                )
                billing = r.json()
                if billing.get("code") == 0:
                    plans = billing.get("data", {}).get("plans", [])
                    if plans:
                        plan = plans[0]
                        print(
                            f"  ├─ Plan active: {plan.get('name', '?')} (status: {plan.get('status', '?')})"
                        )
                        for ent in plan.get("entitlements", []):
                            print(
                                f"  │  • {ent.get('show_name', '?')}: {ent.get('grant_units', '?')} {ent.get('unit_type', '?')}/{ent.get('period', '?')}"
                            )
                        plan_found = True
            except Exception as e:
                print(f"  ├─ Billing error: {e}")
            if not plan_found:
                print(f"  ├─ No plans found (may require desktop app login)")

            # Step 13: Save
            print(f"  ├─ Saving credentials...")
            save_credential(api_cred)
            await append_to_pool(
                {
                    "apiKey": api_cred["apiKey"],
                    "secret": api_cred.get("secret", ""),
                    "provider": api_cred["provider"],
                    "jwt": api_cred.get("jwt", ""),
                    "userId": api_cred.get("userId"),
                },
                email,
            )
            print(f"  └─ Done! {email} registered successfully")

            return api_cred
        finally:
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
    batch_size = config.get("batch_size", 1)
    successful = 0
    failed_emails = set()
    for i in range(0, len(emails), batch_size):
        batch = emails[i : i + batch_size]
        print(
            f"\nBatch [{i + 1}-{i + len(batch)}/{len(emails)}] ({len(batch)} parallel)..."
        )

        # Run registrations in parallel
        tasks = [register_zcode(email, PASSWORD) for email in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for email, res in zip(batch, results):
            if isinstance(res, Exception):
                print(f"  ✖ {email}: {res}")
                failed_emails.add(email)
            elif res:
                print(f"  ✓ {email}: {res['apiKey'][:12]}...")
                successful += 1
            else:
                print(f"  ✖ {email}: failed")
                failed_emails.add(email)

    # Remove successfully registered emails from email.txt.
    # Failed emails stay so they can be retried on next run.
    if successful > 0:
        with open(EMAIL_FILE, "r") as f:
            lines = f.readlines()
        keep = [l for l in lines if l.strip() and l.strip() in failed_emails]
        with open(EMAIL_FILE, "w") as f:
            f.writelines(keep)
        print(f"\n  {successful} removed from {EMAIL_FILE}, {len(keep)} remaining")

    print(f"\nDone! {successful}/{len(emails)} registered successfully")


if __name__ == "__main__":
    asyncio.run(main())
