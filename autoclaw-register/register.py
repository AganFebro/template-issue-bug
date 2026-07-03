"""
╔══════════════════════════════════════════════════════════╗
║             AutoClaw Auto Register Tool                  ║
║         Google OAuth | LLM Proxy | Free 2000 pts        ║
╚══════════════════════════════════════════════════════════╝

Features:
- Auto register via Google OAuth (first login = register)
- New accounts get 2000 bonus points
- Test API key / chat completion
- Check wallet balance
- OpenAI-compatible LLM proxy

GitHub: https://github.com/xxx/autoclaw-tools
"""

import asyncio
import hashlib
import json
import os
import random
import sys
import time
import uuid

import requests
from playwright.async_api import async_playwright

# ═══════════════════════════════════════════════════════════
# CONFIG (loaded from config.json)
# ═══════════════════════════════════════════════════════════
CONFIG_FILE = "config.json"


def load_config():
    defaults = {
        "password": "masuk123",
        "batch_size": 1,
        "email_file": "email.txt",
        "accounts_file": "autoclaw_accounts.json",
        "tokens_file": "accesstoken.txt",
        "proxies": [],
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                cfg = json.load(f)
            defaults.update(cfg)
        except:
            pass
    return defaults


def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


config = load_config()
PASSWORD = config["password"]
EMAIL_FILE = config["email_file"]
ACCOUNTS_FILE = config["accounts_file"]
TOKENS_FILE = config["tokens_file"]
BATCH_SIZE = config["batch_size"]

# ═══════════════════════════════════════════════════════════
# PROXY CONFIG
# ═══════════════════════════════════════════════════════════
PROXY_LIST = config.get("proxies", [])


def parse_proxy(proxy_str):
    """Parse proxy string host:port:username:password into dicts for requests and playwright"""
    parts = proxy_str.split(":")
    if len(parts) == 4:
        host, port, user, pwd = parts
        return {
            "requests": {
                "http": f"http://{user}:{pwd}@{host}:{port}",
                "https": f"http://{user}:{pwd}@{host}:{port}",
            },
            "playwright": {
                "server": f"http://{host}:{port}",
                "username": user,
                "password": pwd,
            },
        }
    elif len(parts) == 2:
        host, port = parts
        return {
            "requests": {
                "http": f"http://{host}:{port}",
                "https": f"http://{host}:{port}",
            },
            "playwright": {"server": f"http://{host}:{port}"},
        }
    return None


def get_proxy():
    """Pick a random proxy. Returns (requests_proxy_dict, playwright_proxy_dict) or (None, None)"""
    if not PROXY_LIST:
        return None, None
    proxy_str = random.choice(PROXY_LIST)
    cfg = parse_proxy(proxy_str)
    if cfg:
        return cfg["requests"], cfg["playwright"]
    return None, None


# ═══════════════════════════════════════════════════════════
# AUTOCLAW API CONSTANTS
# ═══════════════════════════════════════════════════════════
APP_ID = "100003"
APP_KEY = "38d2391985e2369a5fb8227d8e6cd5e5"
BASE_URL = "https://autoglm-api.autoglm.ai"
PROXY_URL = f"{BASE_URL}/autoclaw-proxy/proxy/autoclaw"
REDIRECT_URI = f"{BASE_URL}/userapi/oauth/google/callback"

# Available models
MODELS = {
    "1": {"id": "openrouter_glm-5.2", "name": "GLM-5.2 (Best)", "cost": "~3 pts"},
    "2": {"id": "zai_glm-5-turbo", "name": "GLM-5-Turbo (Cheapest)", "cost": "1 pt"},
    "3": {"id": "zai_auto", "name": "Auto/DeepSeek-V4 (Expensive)", "cost": "~7 pts"},
}


# ═══════════════════════════════════════════════════════════
# UTILS
# ═══════════════════════════════════════════════════════════
def clear():
    os.system("cls" if os.name == "nt" else "clear")


def banner():
    print("""
\033[36m╔══════════════════════════════════════════════════════════╗
║           ⚡ AutoClaw Auto Register Tool ⚡              ║
╚══════════════════════════════════════════════════════════╝\033[0m
    """)


def generate_sign(timestamp):
    raw = f"{APP_ID}&{timestamp}&{APP_KEY}"
    return hashlib.md5(raw.encode()).hexdigest()


def get_auth_headers():
    ts = str(int(time.time()))
    return {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": "https://autoclaw.z.ai",
        "referer": "https://autoclaw.z.ai/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        "x-auth-appid": APP_ID,
        "x-auth-timestamp": ts,
        "x-auth-sign": generate_sign(ts),
        "x-product": "autoclaw",
        "x-version": "1.10.0",
        "x-tm": "web",
        "x-channel": "official",
        "x-client-type": "web",
        "x-trace-id": str(uuid.uuid4()),
        "x-lang": "zh-CN",
    }


def load_accounts():
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []


def save_accounts(accounts):
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(accounts, f, indent=2, ensure_ascii=False)
    # Also save access tokens to txt (one per line)
    with open(TOKENS_FILE, "w", encoding="utf-8") as f:
        for acc in accounts:
            token = acc.get("access_token", "")
            if token:
                f.write(f"{token}\n")


# ═══════════════════════════════════════════════════════════
# API FUNCTIONS
# ═══════════════════════════════════════════════════════════
def get_google_oauth_url(device_id, proxies=None):
    """Get Google OAuth URL from AutoClaw API"""
    url = f"{BASE_URL}/userapi/overseasv1/google-oauth-url"
    body = {
        "device_id": device_id,
        "source_id": "web",
        "navigate_uri": REDIRECT_URI,
        "client_type": "web",
    }
    response = requests.post(
        url, json=body, headers=get_auth_headers(), proxies=proxies
    )
    data = response.json()
    if data.get("code") == 0:
        return data["data"]["oauth_url"], data["data"]["state"]
    return None, None


def get_wallet_balance(access_token, proxies=None):
    """Get wallet balance"""
    url = f"{BASE_URL}/agent-assetmgr/api/v2/wallets?biz_app_id=autoclaw"
    headers = get_auth_headers()
    headers["authorization"] = access_token
    try:
        response = requests.get(url, headers=headers, proxies=proxies)
        data = response.json()
        if data.get("code") == 0:
            return data["data"].get("total_balance", "N/A")
    except:
        pass
    return "N/A"


def test_chat(
    access_token,
    model="openrouter_glm-5.2",
    prompt="Hello! What model are you? Reply in 1 sentence.",
    proxies=None,
):
    """Test chat completion"""
    ts = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-Authorization": access_token,
        "X-Request-Id": str(uuid.uuid4()),
        "X-Request-Model": model,
        "X-Auth-Appid": APP_ID,
        "X-Auth-Timestamp": ts,
        "X-Auth-Sign": generate_sign(ts),
        "X-Product": "autoclaw",
        "X-Version": "1.10.0",
        "X-Tm": "web",
        "X-Trace-Id": str(uuid.uuid4()),
    }
    body = {
        "model": "x",
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "temperature": 0.7,
    }

    resp = requests.post(
        f"{PROXY_URL}/chat/completions",
        json=body,
        headers=headers,
        stream=True,
        proxies=proxies,
    )
    if resp.status_code != 200:
        return None, resp.text

    full_response = ""
    for line in resp.iter_lines():
        if line:
            line = line.decode("utf-8")
            if line.startswith("data: "):
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    delta = chunk["choices"][0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        print(content, end="", flush=True)
                        full_response += content
                except:
                    pass
    print()
    return full_response, None


def test_chat_silent(
    access_token, model="openrouter_glm-5.2", prompt="Hello", proxies=None
):
    """Test chat completion silently (no streaming output)"""
    ts = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-Authorization": access_token,
        "X-Request-Id": str(uuid.uuid4()),
        "X-Request-Model": model,
        "X-Auth-Appid": APP_ID,
        "X-Auth-Timestamp": ts,
        "X-Auth-Sign": generate_sign(ts),
        "X-Product": "autoclaw",
        "X-Version": "1.10.0",
        "X-Tm": "web",
        "X-Trace-Id": str(uuid.uuid4()),
    }
    body = {
        "model": "x",
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "temperature": 0.7,
    }
    try:
        resp = requests.post(
            f"{PROXY_URL}/chat/completions",
            json=body,
            headers=headers,
            stream=True,
            timeout=15,
            proxies=proxies,
        )
        if resp.status_code != 200:
            return None, resp.text
        full = ""
        for line in resp.iter_lines():
            if line:
                line = line.decode("utf-8")
                if line.startswith("data: "):
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0].get("delta", {})
                        full += delta.get("content", "")
                    except:
                        pass
        return full if full else None, None
    except Exception as e:
        return None, str(e)


# ═══════════════════════════════════════════════════════════
# REGISTER FUNCTION
# ═══════════════════════════════════════════════════════════
async def register_autoclaw(
    email, password, browser, req_proxies=None, playwright_proxy=None
):
    """Register AutoClaw account via Google OAuth"""
    device_id = str(uuid.uuid4())

    context_kwargs = {
        "viewport": {"width": 1280, "height": 800},
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    }
    if playwright_proxy:
        context_kwargs["proxy"] = playwright_proxy

    context = await browser.new_context(**context_kwargs)
    page = await context.new_page()

    tokens = {"access_token": None, "refresh_token": None}

    async def handle_response(response):
        try:
            url = response.url
            if (
                "/userapi/v1/refresh" in url
                or "/userapi/overseasv1/google-oauth-login" in url
            ):
                body = await response.json()
                if body.get("code") == 0 and body.get("data", {}).get("access_token"):
                    tokens["access_token"] = body["data"]["access_token"]
                    tokens["refresh_token"] = body["data"].get("refresh_token", "")
                    print(f"\033[32m  [✓] Token intercepted!\033[0m")
        except:
            pass

    page.on("response", handle_response)

    try:
        print(f"\n\033[36m  ┌─ Processing: {email}\033[0m")
        print(f"  │  Device ID: {device_id[:20]}...")

        # Step 1: Get OAuth URL
        print(f"  ├─ Getting OAuth URL...")
        oauth_url, state = get_google_oauth_url(device_id, proxies=req_proxies)
        if not oauth_url:
            print(f"\033[31m  └─ ✖ Failed to get OAuth URL\033[0m")
            return None

        # Step 2: Google OAuth
        print(f"  ├─ Opening Google login...")
        await page.goto(oauth_url)
        await asyncio.sleep(2)

        # Step 3: Enter email
        print(f"  ├─ Entering email...")
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(2)
        email_input = page.locator("#identifierId")
        if not await email_input.is_visible(timeout=3000):
            email_input = page.locator('input[type="email"]')
        if not await email_input.is_visible(timeout=3000):
            email_input = page.locator('input[name="identifier"]')
        await email_input.click()
        await email_input.type(email, delay=50)
        await asyncio.sleep(1)
        next_btn = page.locator("#identifierNext")
        if await next_btn.is_visible(timeout=3000):
            await next_btn.click()
        else:
            await page.keyboard.press("Enter")
        await asyncio.sleep(3)

        # Step 4: Enter password
        print(f"  ├─ Entering password...")
        await page.wait_for_selector('input[type="password"]', timeout=10000)
        await asyncio.sleep(1)
        password_input = page.locator('input[type="password"]')
        await password_input.click()
        await password_input.type(password, delay=50)
        await asyncio.sleep(1)
        next_btn = page.locator("#passwordNext")
        if await next_btn.is_visible(timeout=3000):
            await next_btn.click()
        else:
            await page.keyboard.press("Enter")
        await asyncio.sleep(3)

        # Step 5: Handle workspace terms
        await asyncio.sleep(2)
        if "workspacetermsofservice" in page.url or "speedbump" in page.url:
            print(f"  ├─ Accepting terms...")
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1)
            try:
                btn = page.locator(
                    'button:has-text("I understand"), button:has-text("Saya mengerti"), input[type="submit"]'
                ).first
                await btn.wait_for(state="visible", timeout=5000)
                await btn.click()
                await asyncio.sleep(3)
            except:
                pass

        # Step 6: Handle consent
        try:
            continue_btn = page.locator(
                'button:has-text("Lanjutkan"), button:has-text("Continue"), button:has-text("Allow")'
            )
            await continue_btn.first.wait_for(state="visible", timeout=15000)
            print(f"  ├─ Clicking consent...")
            await continue_btn.first.click()
            await asyncio.sleep(3)
        except:
            pass

        # Step 7: Wait redirect
        print(f"  ├─ Waiting for redirect...")
        try:
            await page.wait_for_url("**/autoclaw.z.ai/**", timeout=30000)
        except:
            pass

        # Step 8: Wait for token
        print(f"  ├─ Waiting for token...")
        for _ in range(15):
            if tokens["access_token"]:
                break
            await asyncio.sleep(1)

        # Fallback: localStorage
        if not tokens["access_token"]:
            print(f"  ├─ Trying localStorage...")
            await asyncio.sleep(3)
            storage_data = await page.evaluate("""() => {
                let result = {};
                for (let i = 0; i < localStorage.length; i++) {
                    let key = localStorage.key(i);
                    let val = localStorage.getItem(key);
                    if (val && (val.includes("eyJ") || key.toLowerCase().includes("token"))) {
                        result[key] = val;
                    }
                }
                return result;
            }""")
            if storage_data:
                for key, val in storage_data.items():
                    if "Bearer" in val or val.startswith("eyJ"):
                        if "refresh" in key.lower():
                            tokens["refresh_token"] = val
                        else:
                            tokens["access_token"] = val

        if tokens["access_token"]:
            access_token = tokens["access_token"]
            refresh_tok = tokens["refresh_token"] or ""
            balance = get_wallet_balance(access_token, proxies=req_proxies)

            print(f"\033[32m  ├─ ✓ Registered successfully!\033[0m")
            print(f"\033[32m  ├─ ✓ Balance: {balance} points\033[0m")

            # Save to accounts
            accounts = load_accounts()
            accounts.append(
                {
                    "email": email,
                    "device_id": device_id,
                    "access_token": access_token,
                    "refresh_token": refresh_tok,
                    "balance": balance,
                    "registered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            save_accounts(accounts)
            print(f"\033[32m  └─ ✓ Saved!\033[0m")

            # Remove from email.txt
            if os.path.exists(EMAIL_FILE):
                with open(EMAIL_FILE, "r") as f:
                    lines = f.readlines()
                with open(EMAIL_FILE, "w") as f:
                    for line in lines:
                        if line.strip() != email:
                            f.write(line)

            return access_token
        else:
            print(f"\033[31m  └─ ✖ Failed to get tokens\033[0m")
            print(f"       URL: {page.url}")
            return None

    except Exception as e:
        print(f"\033[31m  └─ ✖ Error: {e}\033[0m")
        return None
    finally:
        await context.close()


# ═══════════════════════════════════════════════════════════
# MENU HANDLERS
# ═══════════════════════════════════════════════════════════
async def menu_register():
    """Register accounts from email.txt"""
    print(f"\n\033[36m{'─' * 50}\033[0m")
    print(f"\033[36m  📝 AUTO REGISTER\033[0m")
    print(f"\033[36m{'─' * 50}\033[0m")

    if not os.path.exists(EMAIL_FILE):
        print(f"\n\033[31m  [!] {EMAIL_FILE} not found!\033[0m")
        print(f"  [*] Create {EMAIL_FILE} with one email per line")
        return

    with open(EMAIL_FILE, "r") as f:
        emails = [line.strip() for line in f if line.strip() and "@" in line]

    if not emails:
        print(f"\n\033[31m  [!] No emails in {EMAIL_FILE}\033[0m")
        return

    print(f"\n  Emails found  : \033[33m{len(emails)}\033[0m")
    print(f"  Password      : \033[33m{PASSWORD}\033[0m")
    print(f"  Batch size    : \033[33m{BATCH_SIZE}\033[0m")
    print(f"  Output        : \033[33m{ACCOUNTS_FILE} + {TOKENS_FILE}\033[0m")
    if PROXY_LIST:
        print(f"  Proxies       : \033[33m{len(PROXY_LIST)} configured\033[0m")

    total_input = input(f"\n  How many accounts to register? [{len(emails)}]: ").strip()
    total = int(total_input) if total_input.isdigit() else len(emails)
    total = min(total, len(emails))
    emails = emails[:total]

    print(f"  Registering   : \033[33m{total}\033[0m accounts\n")

    async with async_playwright() as p:
        for i in range(0, len(emails), BATCH_SIZE):
            batch = emails[i : i + BATCH_SIZE]
            print(
                f"\n\033[36m  ═══ Batch {i // BATCH_SIZE + 1}/{(len(emails) + BATCH_SIZE - 1) // BATCH_SIZE} ═══\033[0m"
            )

            tasks = []
            for email in batch:
                req_proxy, pw_proxy = get_proxy()
                browser = await p.chromium.launch(
                    headless=False,
                    channel="chrome",
                    args=["--disable-blink-features=AutomationControlled"],
                )
                task = register_autoclaw(email, PASSWORD, browser, req_proxy, pw_proxy)
                tasks.append(task)

            await asyncio.gather(*tasks)

            if i + BATCH_SIZE < len(emails):
                await asyncio.sleep(3)

    accounts = load_accounts()
    print(f"\n\033[32m  ✓ Done! Total accounts: {len(accounts)}\033[0m")


def menu_test_api():
    """Test API key with chat completion"""
    print(f"\n\033[36m{'─' * 50}\033[0m")
    print(f"\033[36m  🧪 TEST API KEY\033[0m")
    print(f"\033[36m{'─' * 50}\033[0m")

    accounts = load_accounts()

    if accounts:
        print(f"\n  Found {len(accounts)} saved accounts:")
        for i, acc in enumerate(accounts[:10]):
            print(
                f"    [{i + 1}] {acc.get('email', 'N/A')} | Balance: {acc.get('balance', '?')}"
            )
        if len(accounts) > 10:
            print(f"    ... and {len(accounts) - 10} more")
        print(f"    [A] Test ALL accounts")
        print()
        choice = input("  Select account (number/A) or paste token: ").strip()

        if choice.lower() == "a":
            # Test all accounts
            print(f"\n  Select model:")
            for k, v in MODELS.items():
                print(f"    [{k}] {v['name']} ({v['cost']})")
            model_choice = input(f"\n  Select model [1]: ").strip() or "1"
            model = MODELS.get(model_choice, MODELS["1"])["id"]
            prompt = "Hello! Reply with just 'OK' to confirm you work."

            print(
                f"\n\033[36m  ─── Testing all {len(accounts)} accounts ({model}) ───\033[0m\n"
            )

            import concurrent.futures

            def test_one(index, acc):
                token = acc.get("access_token", "")
                req_proxy, _ = get_proxy()
                result, error = test_chat_silent(
                    token, model=model, prompt=prompt, proxies=req_proxy
                )
                return index, acc.get("email", "N/A")[:30], bool(result)

            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                futures = [
                    executor.submit(test_one, i, acc) for i, acc in enumerate(accounts)
                ]
                success = 0
                failed = 0
                for future in concurrent.futures.as_completed(futures):
                    idx, email, ok = future.result()
                    if ok:
                        status = f"\033[32m✓ OK\033[0m"
                        success += 1
                    else:
                        status = f"\033[31m✖ FAIL\033[0m"
                        failed += 1
                    print(f"  [{idx + 1}/{len(accounts)}] {email:<32} {status}")

            print(f"\n  {'─' * 50}")
            print(
                f"  \033[32m✓ Success: {success}\033[0m | \033[31m✖ Failed: {failed}\033[0m | Total: {len(accounts)}"
            )
            return

        elif choice.isdigit() and 1 <= int(choice) <= len(accounts):
            token = accounts[int(choice) - 1]["access_token"]
        else:
            token = choice
    else:
        token = input("\n  Paste access_token: ").strip()

    if not token:
        print("\033[31m  [!] No token provided\033[0m")
        return

    # Select model
    print(f"\n  Available models:")
    for k, v in MODELS.items():
        print(f"    [{k}] {v['name']} ({v['cost']})")
    model_choice = input(f"\n  Select model [1]: ").strip() or "1"
    model = MODELS.get(model_choice, MODELS["1"])["id"]

    # Custom prompt or default
    prompt = input(f"  Prompt [Hello! What model are you?]: ").strip()
    if not prompt:
        prompt = "Hello! What model are you? Reply in 1 sentence."

    print(f"\n\033[36m  ─── Response ({model}) ───\033[0m\n")

    req_proxy, _ = get_proxy()
    result, error = test_chat(token, model=model, prompt=prompt, proxies=req_proxy)

    if error:
        print(f"\n\033[31m  [!] Error: {error[:200]}\033[0m")
    else:
        print(f"\n\033[32m  [✓] Success!\033[0m")


def menu_check_balance():
    """Check balance for all accounts"""
    print(f"\n\033[36m{'─' * 50}\033[0m")
    print(f"\033[36m  💰 CHECK BALANCE\033[0m")
    print(f"\033[36m{'─' * 50}\033[0m")

    accounts = load_accounts()
    if not accounts:
        print(f"\n\033[31m  [!] No accounts found in {ACCOUNTS_FILE}\033[0m")
        return

    print(f"\n  {'Email':<35} {'Balance':<10} {'Status'}")
    print(f"  {'─' * 60}")

    total = 0
    for i, acc in enumerate(accounts):
        req_proxy, _ = get_proxy()
        token = acc.get("access_token", "")
        balance = get_wallet_balance(token, proxies=req_proxy)
        acc["balance"] = balance
        if i < 10:
            status = "\033[32m●\033[0m" if balance != "N/A" else "\033[31m●\033[0m"
            email = acc.get("email", "N/A")[:33]
            print(f"  {email:<35} {str(balance):<10} {status}")
        if isinstance(balance, (int, float)):
            total += balance

    if len(accounts) > 10:
        print(f"  ... and {len(accounts) - 10} more accounts")

    print(f"  {'─' * 60}")
    print(f"  \033[33mTotal: {total} points ({len(accounts)} accounts)\033[0m")

    save_accounts(accounts)


def menu_show_accounts():
    """Show saved accounts"""
    print(f"\n\033[36m{'─' * 50}\033[0m")
    print(f"\033[36m  📋 SAVED ACCOUNTS\033[0m")
    print(f"\033[36m{'─' * 50}\033[0m")

    accounts = load_accounts()
    if not accounts:
        print(f"\n  No accounts yet. Run Register first!")
        return

    print(f"\n  Total: \033[33m{len(accounts)}\033[0m accounts\n")
    print(f"  {'#':<4} {'Email':<35} {'Balance':<10} {'Date'}")
    print(f"  {'─' * 65}")
    for i, acc in enumerate(accounts[:10]):
        email = acc.get("email", "N/A")[:33]
        balance = acc.get("balance", "?")
        date = acc.get("registered_at", "?")[:10]
        print(f"  {i + 1:<4} {email:<35} {str(balance):<10} {date}")

    if len(accounts) > 10:
        print(f"  ... and {len(accounts) - 10} more accounts")

    print(f"\n  Files:")
    print(f"    • {ACCOUNTS_FILE} (full data)")
    print(f"    • {TOKENS_FILE} (tokens only, one per line)")


def menu_settings():
    """Edit settings (saved to config.json)"""
    global PASSWORD, BATCH_SIZE, EMAIL_FILE
    print(f"\n\033[36m{'─' * 50}\033[0m")
    print(f"\033[36m  ⚙️  SETTINGS (config.json)\033[0m")
    print(f"\033[36m{'─' * 50}\033[0m")

    print(f"\n  [1] Password    : {PASSWORD}")
    print(f"  [2] Batch Size  : {BATCH_SIZE}")
    print(f"  [3] Email File  : {EMAIL_FILE}")
    print(f"  [4] Proxies     : {len(PROXY_LIST)} configured")
    print()

    choice = input("  Edit (1/2/3/4) or Enter to go back: ").strip()
    if choice == "1":
        new_pw = input(f"  New password [{PASSWORD}]: ").strip()
        if new_pw:
            PASSWORD = new_pw
            config["password"] = PASSWORD
            save_config(config)
            print(f"\033[32m  ✓ Saved to config.json\033[0m")
    elif choice == "2":
        new_bs = input(f"  New batch size [{BATCH_SIZE}]: ").strip()
        if new_bs.isdigit():
            BATCH_SIZE = int(new_bs)
            config["batch_size"] = BATCH_SIZE
            save_config(config)
            print(f"\033[32m  ✓ Saved to config.json\033[0m")
    elif choice == "3":
        new_ef = input(f"  New email file [{EMAIL_FILE}]: ").strip()
        if new_ef:
            EMAIL_FILE = new_ef
            config["email_file"] = EMAIL_FILE
            save_config(config)
            print(f"\033[32m  ✓ Saved to config.json\033[0m")
    elif choice == "4":
        print(f"  \033[90mProxies are managed in config.json directly.\033[0m")
        print(f"  Format: host:port:username:password")
        for i, p in enumerate(PROXY_LIST):
            print(f"    [{i}] {p}")


# ═══════════════════════════════════════════════════════════
# MAIN MENU
# ═══════════════════════════════════════════════════════════
def main():
    while True:
        clear()
        banner()

        accounts = load_accounts()
        print(f"  \033[90mAccounts: {len(accounts)} | Tokens: {TOKENS_FILE}\033[0m")
        if PROXY_LIST:
            print(f"  \033[90mProxies : {len(PROXY_LIST)} configured\033[0m")
        print()

        print("  \033[33m[1]\033[0m 📝 Register Accounts")
        print("  \033[33m[2]\033[0m 🧪 Test API Key")
        print("  \033[33m[3]\033[0m 💰 Check Balance")
        print("  \033[33m[4]\033[0m 📋 Show Accounts")
        print("  \033[33m[5]\033[0m ⚙️  Settings")
        print("  \033[33m[0]\033[0m 🚪 Exit")

        choice = input(f"\n  \033[36m❯\033[0m Select: ").strip()

        if choice == "1":
            asyncio.run(menu_register())
            input("\n  Press Enter to continue...")
        elif choice == "2":
            menu_test_api()
            input("\n  Press Enter to continue...")
        elif choice == "3":
            menu_check_balance()
            input("\n  Press Enter to continue...")
        elif choice == "4":
            menu_show_accounts()
            input("\n  Press Enter to continue...")
        elif choice == "5":
            menu_settings()
            input("\n  Press Enter to continue...")
        elif choice == "0":
            print("\n  \033[36mBye! 👋\033[0m\n")
            sys.exit(0)
        else:
            pass


if __name__ == "__main__":
    main()
