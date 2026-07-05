#!/usr/bin/env python3
"""Aliyun captcha solver using CloakBrowser (stealth Chromium).

Reads JSON config from stdin:
  { "sceneId": "...", "prefix": "...", "region": "...", "sdkPath": "..." }

Writes JSON result to stdout:
  { "success": true, "verifyParam": "..." }
  { "success": false, "error": "..." }

The SDK source is read from `sdkPath` (bundled AliyunCaptcha.js.txt) and
injected via page.add_script_tag() — no CDN dependency, matching the
no-CDN property of the previous jsdom solver.

Why CloakBrowser over plain Playwright: Aliyun's FeiLin device-fingerprint
SDK scores the browser server-side (`verifyCode: F001` = risk engine
rejection, not a JS-detectable failure). Plain Playwright's fingerprint
looks synthetic even with manual `navigator.webdriver` deletion / WebGL
spoofing via `page.add_init_script()` — those are JS-level patches, easy
for risk engines to fingerprint themselves (inconsistent with other signals).
CloakBrowser patches Chromium at the C++ source level (canvas, WebGL, audio,
fonts, GPU, screen, automation signals) so the binary itself reports as a
normal browser — no JS injection needed, and `geoip=True` auto-matches
timezone/locale to the proxy's exit IP, avoiding a UTC+en-US-on-a-residential-
IP mismatch. IP reputation (datacenter vs. residential) still matters most —
see the `proxy` param and pool.accountProxies in the main config.
"""

import asyncio
import json
import os
import socket
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from cloakbrowser import launch_async

_T0 = time.monotonic()


def _log_ts(label: str) -> None:
    """Timestamped stderr breadcrumb (elapsed seconds since process start).

    Diagnostic-only: pinpoints whether a slow/stuck solve is spent in
    CloakBrowser's launch (binary start, geoip DB download/lookup — CPU and
    network bound, ~70 MB on first use) vs. the actual page/captcha steps.
    Cheap and always-on since it only writes a few short lines to stderr.
    """
    print(f"[timing] +{time.monotonic() - _T0:.2f}s {label}", file=sys.stderr, flush=True)

# geoip auto-detection (timezone/locale from the proxy's exit IP) needs the
# optional `cloakbrowser[geoip]` extra (geoip2 package). Degrade gracefully
# when it's not installed instead of failing every solve attempt — the
# fingerprint/proxy IP still helps even without matched timezone/locale.
try:
    import geoip2  # noqa: F401

    _HAS_GEOIP = True
except ImportError:
    _HAS_GEOIP = False

SOLVE_TIMEOUT_MS = 40_000
SDK_LOAD_TIMEOUT_MS = 20_000
PROXY_CHECK_TIMEOUT_S = 5.0


def _check_proxy_reachable(proxy_url: str, timeout: float = PROXY_CHECK_TIMEOUT_S) -> None:
    """Fast TCP reachability check for the proxy host:port before launching
    the browser. A dead/unreachable proxy otherwise surfaces as a vague
    ~30s Playwright navigation timeout inside set_content() with no hint of
    which proxy is actually broken — this fails fast (a few seconds) with a
    clear, identifying error instead.
    """
    parsed = urlparse(proxy_url)
    host, port = parsed.hostname, parsed.port
    if not host or not port:
        return  # unparseable — let the browser launch surface any real error
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError as e:
        raise RuntimeError(f"proxy unreachable ({host}:{port}): {e}") from e


async def solve(
    scene_id: str,
    prefix: str,
    region: str,
    sdk_path: str,
    timeout_ms: int = SOLVE_TIMEOUT_MS,
    proxy: str | None = None,
) -> str:
    """Launch stealth Chromium, inject the Aliyun SDK, solve, return verifyParam.

    `proxy`, when set, is a proxy URL (http://, https://, or socks5://) that
    Chromium routes all traffic through — matches the outbound proxy (or the
    pool account's sticky proxy) the TypeScript proxy uses for its own
    upstream requests, so the captcha-solve IP and the API-request IP match.
    """
    sdk_source = Path(sdk_path).read_text(encoding="utf-8")
    _log_ts("sdk read")
    if proxy:
        _check_proxy_reachable(proxy)
        _log_ts("proxy reachable")

    # Kill-switch for the geoip lookup (timezone/locale auto-match from the
    # proxy's exit IP): it downloads a ~70 MB GeoLite2 DB on first use and
    # does live network resolution through the proxy. On a constrained VPS
    # this can eat a large chunk of wall-clock time before the browser is
    # even up. Set ZCODE_CAPTCHA_GEOIP=0 to rule it out without a code change.
    geoip_enabled = bool(proxy) and _HAS_GEOIP and os.environ.get("ZCODE_CAPTCHA_GEOIP", "1") != "0"

    browser = await launch_async(
        headless=True,
        proxy=proxy,
        # Auto-match timezone/locale to the proxy's exit IP when one is set
        # and the optional geoip2 dependency is installed — avoids a
        # UTC/en-US-on-a-residential-IP mismatch signal. No proxy, or no
        # geoip2 installed → no geoip call, CloakBrowser's own defaults apply.
        geoip=geoip_enabled,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-web-security",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-site-isolation-trials",
        ],
    )
    _log_ts("browser launched")
    try:
        # No manual user-agent/locale/timezone override, no navigator.webdriver
        # deletion, no WebGL JS patching — CloakBrowser's C++-level patches
        # already produce a consistent, real-looking fingerprint. Overriding
        # pieces manually here would just reintroduce the inconsistencies
        # (mismatched UA vs. Client Hints, double-patched WebGL) that made the
        # plain-Playwright approach detectable.
        context = await browser.new_context(viewport={"width": 1280, "height": 720})
        page = await context.new_page()
        _log_ts("context+page ready")

        # Set up the page shell, then inject the SDK via add_script_tag
        # (avoids </script> parsing issues that break inline injection).
        #
        # page.set_content() is NOT a real navigation — it just injects HTML
        # into the current document via CDP. Confirmed by timing logs that
        # this hangs for the full 30s timeout specifically when the browser
        # was launched with a `proxy` configured (reproduced identically on
        # Windows and the VPS, with the proxy independently confirmed
        # reachable) — Chromium's proxy-aware navigation/readiness machinery
        # apparently never settles for a non-navigation content injection.
        # A `data:` URL goto() is a genuine navigation, which exercises the
        # same code path that already works fine for real proxied requests.
        html = """<!DOCTYPE html>
            <html><head></head><body>
              <div id="captcha-element"></div>
              <button id="captcha-button"></button>
            </body></html>"""
        await page.goto("data:text/html;charset=utf-8," + quote(html), wait_until="domcontentloaded")
        _log_ts("goto done")
        await page.add_script_tag(content=sdk_source)
        _log_ts("sdk script tag injected")

        # Wait for the SDK to expose initAliyunCaptcha
        await page.wait_for_function(
            "typeof window.initAliyunCaptcha === 'function'",
            timeout=SDK_LOAD_TIMEOUT_MS,
        )
        _log_ts("sdk ready")

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
