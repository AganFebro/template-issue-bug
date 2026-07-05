#!/usr/bin/env python3
"""Aliyun captcha solver using Playwright (headless Chromium).

Reads JSON config from stdin:
  { "sceneId": "...", "prefix": "...", "region": "...", "sdkPath": "..." }

Writes JSON result to stdout:
  { "success": true, "verifyParam": "..." }
  { "success": false, "error": "..." }

The SDK source is read from `sdkPath` (bundled AliyunCaptcha.js.txt) and
injected via page.add_script_tag() — no CDN dependency, matching the
no-CDN property of the previous jsdom solver.

Why Playwright over jsdom: Aliyun's FeiLin device-fingerprint SDK detects
the jsdom environment (via Bun.version, process, Buffer globals that leak
through Function("return this")()), producing a fingerprint that Aliyun
rejects with verifyCode F001. A real Chromium engine produces a real
fingerprint that passes. This mirrors what the ZCode desktop client does
(it uses Electron/Chromium internally).
"""

import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

# Mirrors FAKE_UA in captcha.ts — must look like a real Windows Chrome UA
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

SOLVE_TIMEOUT_MS = 40_000
SDK_LOAD_TIMEOUT_MS = 20_000


async def solve(
    scene_id: str,
    prefix: str,
    region: str,
    sdk_path: str,
    timeout_ms: int = SOLVE_TIMEOUT_MS,
    proxy: str | None = None,
) -> str:
    """Launch Chromium, inject the Aliyun SDK, solve, return verifyParam.

    `proxy`, when set, is a proxy URL (http://, https://, or socks5://) that
    Chromium routes all traffic through — matches the outbound proxy the
    TypeScript proxy uses for its own upstream requests.
    """
    sdk_source = Path(sdk_path).read_text(encoding="utf-8")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            proxy={"server": proxy} if proxy else None,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-site-isolation-trials",
                "--enable-webgl",
                "--ignore-gpu-blocklist",
                "--use-gl=swiftshader",
            ],
        )
        try:
            context = await browser.new_context(
                user_agent=USER_AGENT,
                locale="en-US",
                timezone_id="America/Los_Angeles",
                viewport={"width": 1280, "height": 720},
            )
            page = await context.new_page()

            # Anti-detection: remove navigator.webdriver and spoof WebGL
            await page.add_init_script(
                """() => {
                    delete Object.getPrototypeOf(navigator).webdriver;
                    // Spoof WebGL vendor to look like a real GPU
                    const getParameter = WebGLRenderingContext.prototype.getParameter;
                    WebGLRenderingContext.prototype.getParameter = function(p) {
                        if (p === 37445) return 'Intel Inc.';
                        if (p === 37446) return 'Intel Iris OpenGL Engine';
                        return getParameter.call(this, p);
                    };
                }"""
            )

            # Set up the page shell, then inject the SDK via add_script_tag
            # (avoids </script> parsing issues that break inline injection)
            await page.set_content(
                """<!DOCTYPE html>
                <html><head></head><body>
                  <div id="captcha-element"></div>
                  <button id="captcha-button"></button>
                </body></html>"""
            )
            await page.add_script_tag(content=sdk_source)

            # Wait for the SDK to expose initAliyunCaptcha
            await page.wait_for_function(
                "typeof window.initAliyunCaptcha === 'function'",
                timeout=SDK_LOAD_TIMEOUT_MS,
            )

            # Set the AliyunCaptchaConfig (SDK reads this on init)
            await page.evaluate(
                f"""window.AliyunCaptchaConfig = {{ region: {json.dumps(region)}, prefix: {json.dumps(prefix)} }};"""
            )

            # Call initAliyunCaptcha and wait for success/fail/onError
            # The promise resolves with the verifyParam string on success
            # and rejects with an error object on failure.
            token = await page.evaluate(
                """async (cfg) => {
                    return await new Promise((resolve, reject) => {
                        const timeout = setTimeout(
                            () => reject(new Error("captcha solve timeout after " + cfg.timeout + "ms")),
                            cfg.timeout
                        );
                        window.initAliyunCaptcha({
                            SceneId: cfg.sceneId,
                            mode: "popup",
                            region: cfg.region,
                            prefix: cfg.prefix,
                            language: "en",
                            element: "#captcha-element",
                            button: "#captcha-button",
                            captchaLogoImg: "",
                            showErrorTip: false,
                            getInstance: (inst) => {
                                const fn = inst.startTracelessVerification || inst.show;
                                if (typeof fn === "function") {
                                    try { fn.call(inst); }
                                    catch (e) { clearTimeout(timeout); reject(e); }
                                }
                            },
                            success: (param) => {
                                clearTimeout(timeout);
                                // SDK may pass an object or the verifyParam directly.
                                // Extract the string token — page.evaluate needs a serializable return.
                                if (typeof param === 'string') {
                                    resolve(param);
                                } else if (param && param.verifyParam) {
                                    resolve(param.verifyParam);
                                } else if (param && typeof param.captchaVerifyParam === 'string') {
                                    resolve(param.captchaVerifyParam);
                                } else if (param && typeof param.toString === 'function') {
                                    const s = param.toString();
                                    if (s !== '[object Object]') resolve(s);
                                    else resolve(JSON.stringify(param));
                                } else {
                                    resolve(JSON.stringify(param));
                                }
                            },
                            fail: (err) => {
                                clearTimeout(timeout);
                                reject(typeof err === 'string' ? new Error(err) :
                                       err && err.message ? err : new Error(JSON.stringify(err)));
                            },
                            onError: (err) => {
                                clearTimeout(timeout);
                                reject(typeof err === 'string' ? new Error(err) :
                                       err && err.message ? err : new Error(JSON.stringify(err)));
                            },
                        });
                    });
                }""",
                {
                    "sceneId": scene_id,
                    "prefix": prefix,
                    "region": region,
                    "timeout": timeout_ms,
                },
            )

            return token
        finally:
            await browser.close()


async def main() -> None:
    try:
        raw = sys.stdin.read()
        config = json.loads(raw)
        token = await solve(
            scene_id=config["sceneId"],
            prefix=config["prefix"],
            region=config["region"],
            sdk_path=config["sdkPath"],
            timeout_ms=int(config.get("timeout", SOLVE_TIMEOUT_MS)),
            proxy=config.get("proxy") or None,
        )
        print(json.dumps({"success": True, "verifyParam": token}))
    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
