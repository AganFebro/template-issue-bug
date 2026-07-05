/**
 * Aliyun Captcha V3 solver — Playwright (headless Chromium).
 *
 * The solver spawns `captcha_solver.py` (Python + Playwright), which launches
 * a real headless Chromium browser, injects the bundled AliyunCaptcha SDK,
 * and waits for the traceless verification to produce a `verifyParam` token.
 *
 * Why Playwright over the previous jsdom solver: Aliyun's FeiLin device-
 * fingerprint SDK (loaded by AliyunCaptcha) detects the jsdom environment
 * via leaked Node/Bun globals (`Bun.version`, `process`, `Buffer` accessible
 * through `Function("return this")()`), producing a fingerprint that Aliyun
 * rejects with `verifyCode: F001`. A real Chromium engine produces a real
 * fingerprint that passes — this mirrors what the ZCode desktop client does
 * (it uses Electron/Chromium internally).
 *
 * The AliyunCaptcha SDK is bundled as a text import (`AliyunCaptcha.js.txt`)
 * and passed to the Python script, which injects it via
 * `page.add_script_tag()` — no CDN dependency at runtime.
 *
 * Solve attempts are retried (`SOLVE_RETRIES`), and the resulting token is
 * cached for `TOKEN_TTL_MS` (45s) — Aliyun tokens are short-lived.
 */
import ALIYUN_SDK_LOCAL from "./AliyunCaptcha.js.txt" with { type: "text" };
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import type { OutboundProxyConfig } from "../config/types.js";
import { createProxiedFetch } from "./outbound-proxy.js";

const CAPTCHA_HEADER = "x-aliyun-captcha-verify-param";
const REGION_HEADER = "x-aliyun-captcha-verify-region";
const CONFIGS_API = "https://zcode.z.ai/api/v1/client/configs";

/** How many times to retry a single captcha solve. Overridable via env. */
const SOLVE_RETRIES = Number(process.env.ZCODE_CAPTCHA_RETRIES || 3);
/** Per-attempt solve timeout (ms), passed through to the Python script. */
const SOLVE_TIMEOUT_MS = Number(process.env.ZCODE_CAPTCHA_TIMEOUT_MS || 40_000);

/** Path to the Python solver script (sibling of this file). */
const SOLVER_SCRIPT_PATH = join(
  dirname(fileURLToPath(import.meta.url)),
  "captcha_solver.py",
);
/** Path to the bundled SDK (sibling of this file). */
const SDK_PATH = join(
  dirname(fileURLToPath(import.meta.url)),
  "AliyunCaptcha.js.txt",
);

/**
 * Python binary for captcha solver subprocess.
 * Ubunt/Debian install Python as `python3`, not `python`.
 * Set ZCODE_CAPTCHA_PYTHON to override the auto-detection.
 */
const PYTHON_BIN =
  process.env.ZCODE_CAPTCHA_PYTHON ??
  (() => {
    for (const c of ["python3", "python"]) {
      try {
        Bun.spawnSync([c, "--version"]);
        return c;
      } catch {}
    }
    return "python3"; // best-effort default
  })();

interface FetchedCaptchaConfig {
  enabled: boolean;
  prefix: string;
  sceneId: string;
  region: string;
}
let cachedConfig: { value: FetchedCaptchaConfig | null; expiresAt: number } = {
  value: null,
  expiresAt: 0,
};

export function detectCaptchaChallenge(resp: Response): string | null {
  const v = resp.headers.get(CAPTCHA_HEADER);
  return v && v.trim().length > 0 ? v.trim() : null;
}

/**
 * Check if an upstream response body is a 3007 "captcha verify failed" rejection.
 * The zcode.z.ai gateway returns this as JSON `{"code":3007,"msg":"captcha verify failed"}`
 * WITHOUT the `x-aliyun-captcha-verify-param` response header, so
 * `detectCaptchaChallenge` alone won't catch it.
 *
 * Reads the response body (consuming it). Returns:
 * - `{ isRejection: true, bodyText }` when it IS a 3007 — caller should re-solve
 * - `{ isRejection: false, bodyText }` when it is NOT — caller must reconstruct
 *   the response with `bodyText` since the stream is consumed
 * - `{ isRejection: false, bodyText: null }` when the response cannot be a 3007
 *   (wrong status/content-type) — body is NOT consumed in this case
 *
 * Only call this on a non-SSE 403 response.
 */
export async function detectCaptchaRejection(
  resp: Response,
): Promise<{ isRejection: boolean; bodyText: string | null }> {
  if (resp.status !== 403) return { isRejection: false, bodyText: null };
  const ct = resp.headers.get("content-type") ?? "";
  if (!ct.includes("application/json"))
    return { isRejection: false, bodyText: null };
  try {
    const text = await resp.text();
    const parsed = JSON.parse(text) as { code?: number; msg?: string };
    return { isRejection: parsed.code === 3007, bodyText: text };
  } catch {
    return { isRejection: false, bodyText: null };
  }
}

export function invalidateCaptchaToken(): void {
  // No-op: tokens are not cached (single-use). Kept for handler.ts compatibility.
}

async function fetchCaptchaConfig(
  appVersion: string,
  outboundProxy: OutboundProxyConfig | undefined,
): Promise<FetchedCaptchaConfig | null> {
  if (cachedConfig.value && cachedConfig.expiresAt > Date.now())
    return cachedConfig.value;
  try {
    const fetchImpl = createProxiedFetch(outboundProxy);
    const resp = await fetchImpl(
      `${CONFIGS_API}?app_version=${encodeURIComponent(appVersion)}&platform=win32-x64`,
    );
    const json = (await resp.json()) as {
      data?: { configs?: { captcha?: FetchedCaptchaConfig } };
    };
    const cfg = json?.data?.configs?.captcha ?? null;
    cachedConfig = { value: cfg, expiresAt: Date.now() + 60000 };
    return cfg;
  } catch {
    return null;
  }
}

export async function getCaptchaToken(
  appVersion: string,
  outboundProxy?: OutboundProxyConfig,
): Promise<{ verifyParam: string; region: string }> {
  // No token caching: Aliyun captcha tokens are single-use. Reusing a cached
  // token causes 3007 "captcha verify failed" on every subsequent request.
  // Each call solves fresh via Playwright (~10s). The handler's pre-solve +
  // 3007-retry covers the rare case where a fresh token is rejected.
  const cfg = await fetchCaptchaConfig(appVersion, outboundProxy);
  if (!cfg || !cfg.enabled || !cfg.prefix || !cfg.sceneId)
    throw new Error("Captcha config unavailable");
  const verifyParam = await solveWithPlaywrightRetry(cfg, outboundProxy);
  return { verifyParam, region: cfg.region };
}

async function solveWithPlaywrightRetry(
  cfg: FetchedCaptchaConfig,
  outboundProxy: OutboundProxyConfig | undefined,
): Promise<string> {
  let lastErr: Error | null = null;
  for (let attempt = 1; attempt <= SOLVE_RETRIES; attempt++) {
    try {
      return await solveWithPlaywright(cfg, outboundProxy);
    } catch (err) {
      lastErr = err as Error;
      console.error(
        `[captcha] solve attempt ${attempt}/${SOLVE_RETRIES} failed: ${lastErr.message}`,
      );
    }
  }
  throw new Error(
    `captcha solve failed after ${SOLVE_RETRIES} attempts: ${lastErr?.message ?? "unknown"}`,
  );
}

/**
 * Spawn the Python Playwright solver and return the verifyParam token.
 *
 * The Python script reads JSON config from stdin and writes JSON result to
 * stdout. A single attempt — retries are handled by the caller.
 */
async function solveWithPlaywright(
  cfg: FetchedCaptchaConfig,
  outboundProxy: OutboundProxyConfig | undefined,
): Promise<string> {
  const input = JSON.stringify({
    sceneId: cfg.sceneId,
    prefix: cfg.prefix,
    region: cfg.region,
    sdkPath: SDK_PATH,
    timeout: SOLVE_TIMEOUT_MS,
    proxy: outboundProxy?.url,
  });

  const proc = Bun.spawn([PYTHON_BIN, SOLVER_SCRIPT_PATH], {
    stdin: new Blob([input]),
    stdout: "pipe",
    stderr: "pipe",
    env: {
      ...process.env,
      // Silence CloakBrowser's non-fatal "no Windows fonts" advisory — it's
      // printed to stderr on every launch and would otherwise mask the real
      // error below (stderr is preferred over stdout when both are present).
      CLOAKBROWSER_SUPPRESS_FONT_WARNING: "1",
    },
  });

  const [stdout, stderr, exitCode] = await Promise.all([
    new Response(proc.stdout).text(),
    new Response(proc.stderr).text(),
    proc.exited,
  ]);

  if (exitCode !== 0) {
    // Prefer stdout (our own structured `{"success":false,"error":...}` goes
    // there) but include stderr too — a stray warning on stderr should never
    // silently hide the real failure reason from stdout.
    const stdoutTrim = stdout.trim();
    const stderrTrim = stderr.trim();
    const detail =
      stdoutTrim && stderrTrim
        ? `${stdoutTrim} | stderr: ${stderrTrim}`
        : stdoutTrim || stderrTrim || `(exit ${exitCode})`;
    throw new Error(
      `python solver exited ${exitCode}: ${detail.slice(0, 500)}`,
    );
  }

  let result: { success: boolean; verifyParam?: string; error?: string };
  try {
    result = JSON.parse(stdout);
  } catch {
    throw new Error(
      `python solver returned non-JSON output: ${stdout.slice(0, 200)}`,
    );
  }

  if (!result.success || !result.verifyParam) {
    throw new Error(
      result.error ?? "python solver returned success=false without error",
    );
  }
  return result.verifyParam;
}

export const RETRY_HEADERS = { PARAM: CAPTCHA_HEADER, REGION: REGION_HEADER };
