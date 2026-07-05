# zcode-proxy

A reverse proxy for Z.AI / Bigmodel.cn coding-plan APIs that exposes both OpenAI-compatible and Anthropic-format endpoints.

## No http/socks proxy support yet! Still a very experimental codes, proceed with caution.

## Quick Start

```bash
# Install dependencies
bun install

# Copy and edit config
cp config.example.yaml config.yaml
# Edit config.yaml — set your API key

# Start the proxy
bun run src/index.ts

# Or specify a config path
bun run src/index.ts /path/to/config.yaml
```

## Authentication

### Option 1: Direct API Key (simplest)

1. Get an API key from [Z.AI](https://z.ai) or [Bigmodel](https://bigmodel.cn)
2. For Z.AI you need `{apiKey}.{secretKey}` format
3. For Bigmodel you need `{apiKey}` format
4. Set it in `config.yaml`:

```yaml
auth:
  mode: apikey
  apiKey: "yourApiKey.yourSecretKey"
provider: zai  # or bigmodel
```

### Option 2: OAuth Login (browser-based, both providers)

```bash
# Z.AI auth-code flow (chat.z.ai authorize → zcode.z.ai token exchange)
bun run src/index.ts auth login zai

# Bigmodel auth-code flow (bigmodel.cn authorize → zcode.z.ai token exchange)
bun run src/index.ts auth login bigmodel

# This will:
# 1. Print an authorize URL and open your browser
# 2. Exchange the auth code for upstream credentials
# 3. Resolve your coding-plan API key automatically
# 4. Save encrypted credentials to ~/.zcode-proxy/credentials.json

# Then set config.yaml:
auth:
  mode: oauth
provider: zai  # or bigmodel
```

### Option 3: Import from ZCode Config (skip OAuth)

If you already use the ZCode desktop app, import the API key directly:

```bash
bun run src/index.ts auth login bigmodel --import
```

### Option 4: Account Pool (multi-account quota rotation)

For start-plan, you can register multiple accounts and rotate between them automatically as quotas are exhausted. Uses `pool.json` for credential storage and `billing/balance` for real-time quota tracking.

**Setup:**

```bash
# Register accounts (auto-saves to pool.json)
cd zcode-register
pip install -r requirements.txt
python zcode_register.py

# Check aggregate pool balance
python check_balance.py
python check_balance.py --detail   # per-account
python check_balance.py --failed   # show stale JWTs
```

Then in `config.yaml`:

```yaml
auth:
  mode: pool
provider: zai
plan: start-plan

pool:
  poolPath: "pool.json"
  refreshIntervalMs: 300000  # 5 min
```

The pool manager picks the account with the most remaining quota for per model. On 1005 "exceed quota limit", the account is marked exhausted silently and the request retries with the next best account. Quotas are refreshed every 5 minutes via `billing/balance`.

## Outbound Proxy

Route all outbound requests the proxy makes upstream (LLM chat completions, OAuth,
key resolution, pool quota refresh, captcha config/solving) through an
HTTP/HTTPS/SOCKS5 proxy:

```yaml
outboundProxy:
  url: "socks5://user:pass@host:port"
```

Or via environment variable (overrides YAML, works for `auth login` too since
it runs before `config.yaml` is loaded):

```bash
export ZCODE_OUTBOUND_PROXY="socks5://127.0.0.1:1080"
```

When a proxy is configured, the client-session `enforce` mode's exact-header-
ordering transport (raw sockets) is automatically disabled in favor of the
regular fetch path, since raw sockets can't honor an HTTP/SOCKS proxy.

### Sticky Per-Account Proxy (pool mode)

When using the account pool (`auth.mode: pool`), you can assign each pool
account its own dedicated outbound proxy so it always egresses from the same
IP across requests and restarts:

```yaml
pool:
  poolPath: "pool.json"
  refreshIntervalMs: 300000
  accountProxies:
    - "http://user:pass@host1:port1"
    - "http://user:pass@host2:port2"
```

Accounts are assigned proxies round-robin by their position in `pool.json`
(`accountProxies[index % accountProxies.length]`), so with fewer proxies than
accounts, several accounts share a proxy — but the same account always uses
the same one. This is useful when a gateway rejection (e.g. `1005` "quota
exhausted") turns out to actually be a per-IP rate limit rather than genuine
per-account quota exhaustion: giving each account a distinct IP avoids
tripping shared rate limits from a single local IP hammering the gateway with
many accounts. Falls back to the global `outboundProxy` (or no proxy) for
accounts/requests without a `proxyUrl` assigned.

## Auto-Register Tools (`zcode-register/`)

| Script | Description |
|--------|-------------|
| `zcode_register.py` | Playwright Google OAuth → chat.z.ai → auto-activate start-plan → append to `../pool.json` |
| `check_balance.py` | Aggregate quota across all pool accounts, parallel fetch, retry on failure |

### Register Configuration (`config.json`)

```json
{
  "password": "your_gmail_password",
  "batch_size": 2
}
```

- `email.txt` — one Gmail address per line; successfully registered emails are auto-removed
- `batch_size` — how many accounts to register in parallel (2-3 recommended)

### Outbound Proxy (zcode-register)

Both `zcode_register.py` and `check_balance.py` can route all outbound requests
(Google OAuth, token exchange, API key resolution, billing checks) and the
Playwright browser through an HTTP/HTTPS/SOCKS5 proxy:

```json
{
  "proxies": ["socks5://user:pass@host:port"]
}
```

Or override per-run without editing `config.json`:

```bash
python zcode_register.py --proxy socks5://127.0.0.1:1080
python check_balance.py --proxy http://127.0.0.1:8080
```

Only the first `proxies` entry is used — this project does not yet rotate
outbound proxies per account. SOCKS5 requires `PySocks` (already in
`requirements.txt`).

The main proxy server has its own separate `outboundProxy` setting (see
[Outbound Proxy](#outbound-proxy) below) for routing upstream LLM requests
through a proxy.



The `start-plan` tier routes through zcode.z.ai with JWT auth + captcha verification. It requires OAuth login mode.

### Setup

```bash
# OAuth login (Z.AI)
bun run src/index.ts auth login zai

# Or import from ZCode desktop config
bun run src/index.ts auth login zai --import
```

Then in `config.yaml`:

```yaml
auth:
  mode: oauth
provider: zai
plan: start-plan
```

### Captcha Solver Requirement

Start-plan uses Aliyun intelligent captcha verification. The proxy spawns a Python + Playwright subprocess to solve it — headless Chromium runs the AliyunCaptcha SDK in a real browser to produce a valid device fingerprint.

**Prerequisites:**

- Python 3 with `playwright` package installed (`pip install playwright`)
- Chromium browsers installed (`playwright install chromium`)

The solver (`src/proxy/captcha_solver.py`) is launched automatically per-request. Solve time is ~8-12 seconds. Captcha tokens are single-use — every request gets a fresh solve.

### Available Models

Start-plan models are determined by the zcode.z.ai gateway, not the proxy. The coding-plan model list (below) is a guide — actual availability depends on your account:

- `glm-5-turbo` — confirmed working (200 status, proper streaming)
- `glm-5.2` — may return 529 (rate limited by z.ai backend, high demand for new model)

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/chat/completions` | OpenAI-compatible chat completions (streaming + non-streaming) |
| `POST` | `/v1/messages` | Anthropic-format messages (streaming + non-streaming) |
| `GET` | `/v1/models` | List available models |
| `GET` | `/health` | Health check |

## Usage Examples

### OpenAI Format

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer your-proxy-secret" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-4.6",
    "messages": [{"role": "user", "content": "Hello!"}],
    "stream": false
  }'
```

### Anthropic Format

```bash
curl http://localhost:8080/v1/messages \
  -H "x-api-key: your-proxy-secret" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-4.6",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### Streaming

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer your-proxy-secret" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-4.6",
    "messages": [{"role": "user", "content": "Write a poem"}],
    "stream": true
  }'
```

### List Models

```bash
curl http://localhost:8080/v1/models \
  -H "Authorization: Bearer your-proxy-secret"
```

## Configuration

| Field | Env Var | Default | Description |
|-------|---------|---------|-------------|
| `server.port` | `ZCODE_PROXY_PORT` | `8080` | Listen port |
| `auth.apiKey` | `ZCODE_API_KEY` | — | Upstream API key |
| `auth.mode` | — | `apikey` | `apikey`, `oauth`, or `pool` |
| `auth.proxyApiKey` | `ZCODE_PROXY_API_KEY` | — | Client auth key |
| `provider` | `ZCODE_PROVIDER` | `zai` | Upstream provider |
| `plan` | — | `coding-plan` | Plan tier: `coding-plan` (direct upstream) or `start-plan` (zcode.z.ai gateway + JWT + captcha) |
| `providers.<p>.credential` | — | — | Per-provider credential override (else uses `auth.apiKey`) |
| `pool.poolPath` | — | `pool.json` | Path to pool credentials JSON (pool mode only) |
| `pool.refreshIntervalMs` | — | `300000` | Quota refresh interval in ms (pool mode only) |
| `identity.appVersion` | `ZCODE_APP_VERSION` | `3.2.4` | `User-Agent: ZCode/{version}` |
| `identity.sourceTitle` | `ZCODE_SOURCE_TITLE` | `cli` | `X-Title: Z Code@{title}` |
| `identity.refererOrigin` | `ZCODE_REFERER_ORIGIN` | `https://zcode.z.ai` | `HTTP-Referer` URL |
| config file path | `ZCODE_PROXY_CONFIG` | `config.yaml` | Config file to load on `serve` |

Start-plan captcha tunables (env only): `ZCODE_CAPTCHA_RETRIES`, `ZCODE_CAPTCHA_TIMEOUT_MS`, `ZCODE_CAPTCHA_SDK_LOAD_MS`.

## Architecture

```
Client Request
      │
      ▼
Proxy API Key Auth (shared secret)
      │
      ▼
Route Detection + Plan-aware Routing
  /v1/chat/completions (OpenAI client format)
    ├─ coding-plan → passthrough to provider's OpenAI endpoint
    └─ start-plan  → TRANSLATE OpenAI→Anthropic → zcode.z.ai Anthropic gateway (JWT + captcha)
  /v1/messages     (Anthropic client format)
    ├─ coding-plan → TRANSLATE Anthropic→OpenAI → provider's OpenAI endpoint
    └─ start-plan  → passthrough to zcode.z.ai Anthropic gateway (JWT + captcha)
      │
      ▼
Body Transformation (ZCode-equivalent mutations)
  OpenAI format        → inject stream_options.include_usage (streaming only)
  start-plan           → prepend ZCode system blocks (Anthropic `system` field)
  Anthropic format     → convert all message content to array content blocks
                         (gateway rejects string content with 3001)
                         add cache_control {type:"ephemeral"} to last block
  Anthropic + OAuth    → inject metadata.user_id (coding-plan only)
      │
      ▼
[Translation mode] coding-plan A→OpenAI; start-plan OpenAI→Anthropic
      │
      ▼
Auth + Identity Header Injection
  coding-plan (OpenAI upstream):   Authorization: Bearer {credential}
  coding-plan (Anthropic upstream): x-api-key: {credential} + anthropic-version
  start-plan (Anthropic upstream):  Authorization: Bearer {jwt} + anthropic-version
  Both:                             User-Agent: ZCode/{version} + X-ZCode-* + trace headers
      │
      ▼
Captcha (start-plan only)
  Pre-solve: getCaptchaToken() → spawns captcha_solver.py (Playwright headless Chromium)
             → injects bundled AliyunCaptcha SDK → solves intelligent captcha
             → returns verifyParam token → injected as x-aliyun-captcha-verify-param header
  On 403:   detectCaptchaChallenge (header-based) OR detectCaptchaRejection (3007 body)
             → invalidate → re-solve → retry once
      │
      ▼
Upstream Forward (Bun.fetch)
  Translation mode:    decompress enabled (proxy reads + translates body)
  Passthrough:         decompress enabled for start-plan (gateway compresses SSE)
                       decompress disabled for coding-plan passthrough (raw bytes)
      │
      ▼
Response Handling
  Passthrough non-SSE:      forwarded as-is (content-encoding stripped if decompressed)
  Passthrough SSE:          tee'd — stats branch → observeStream; client branch → forwarded
  Translation batch:        Anthropic JSON ↔ OpenAI JSON → re-gzip if client accepts
  Translation SSE stream:   Anthropic SSE ↔ OpenAI SSE chunks → client
  Gateway error detection:  peekUpstreamJsonError converts 1005/3001/3012 JSON errors
                            (returned with 200 status) to proper HTTP 402/400/405
  Pool rotation (start-plan): on 1005 quota exhaustion, mark account exhausted →
                            rotate to next best account → silently retry
  Pool quota refresh:      background timer calls billing/balance per account every
                            `refreshIntervalMs` to keep quota data current
```

## Development

```bash
# Run tests
bun test

# Type check
bun x tsc --noEmit

# Run in dev mode
bun run src/index.ts config.yaml

# Compile a single-file binary (→ zcode-proxy.exe, gitignored)
bun run build
```

## Docker

Pull the multi-arch image from GitHub Packages (ghcr.io):

```bash
docker pull ghcr.io/tridefender/zcode-proxy:latest
```

Run with env-var configuration (no config file needed):

```bash
docker run --rm -p 8080:8080 \
  -e ZCODE_API_KEY="yourApiKey.yourSecretKey" \
  -e ZCODE_PROVIDER=zai \
  -e ZCODE_PROXY_API_KEY="your-proxy-secret" \
  ghcr.io/tridefender/zcode-proxy:latest
```

Or mount a config file:

```bash
docker run --rm -p 8080:8080 \
  -v "$(pwd)/config.yaml:/data/config.yaml:ro" \
  ghcr.io/tridefender/zcode-proxy:latest
```

> Note: `/health` and all routes sit behind the proxy-API-key check, so health probes must send `x-api-key: <ZCODE_PROXY_API_KEY>`.

Common environment variables (see the Configuration table above for the full list):

| Env Var | Description |
|---------|-------------|
| `ZCODE_API_KEY` | Upstream API key (`{apiKey}.{secretKey}` for Z.AI, `{apiKey}` for Bigmodel) |
| `ZCODE_PROVIDER` | `zai` or `bigmodel` |
| `ZCODE_PROXY_API_KEY` | Client auth shared secret |
| `ZCODE_PROXY_PORT` | Listen port (default `8080`) |

docker-compose:

```yaml
services:
  zcode-proxy:
    image: ghcr.io/tridefender/zcode-proxy:latest
    ports:
      - "8080:8080"
    environment:
      ZCODE_API_KEY: "yourApiKey.yourSecretKey"
      ZCODE_PROVIDER: zai
      ZCODE_PROXY_API_KEY: "your-proxy-secret"
    restart: unless-stopped
```

## Available Models

The proxy lists these models on `GET /v1/models` (pinned to the GLM coding-plan tier):

| Model | Context | Max Output |
|-------|---------|------------|
| `glm-4.5-air` | 200K | 128K |
| `glm-4.6` | 200K | 128K |
| `glm-4.6v` | 200K | 128K |
| `glm-4.7` | 200K | 128K |
| `glm-5` | 200K | 128K |
| `glm-5-turbo` | 200K | 128K |
| `glm-5v-turbo` | 200K | 128K |
| `glm-5.1` | 200K | 128K |
| `glm-5.2` | 1M | 128K |

Requests for models not in this list are still forwarded upstream — the listing is informational, not a gate.

## License

MIT
