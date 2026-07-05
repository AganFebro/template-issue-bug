/**
 * Configuration types for zcode-proxy.
 * @see .omo/plans/zcode-proxy.md Task 2
 */

/** Provider endpoint configuration (one per upstream provider). */
export interface ProviderEndpoints {
  /** Base URL for Anthropic-format API, e.g. "https://api.z.ai/api/anthropic". */
  anthropicBase: string;
  /** Base URL for OpenAI-format API, e.g. "https://api.z.ai/api/coding/paas/v4". */
  openaiBase: string;
  /** Provider-specific credential override. If absent, uses the global `auth.apiKey`. */
  credential?: string;
}

/** Auth section of the proxy configuration. */
interface AuthConfig {
  /**
   * Key that clients must provide to use the proxy (via `Authorization: Bearer {proxyApiKey}`).
   * If unset, the proxy does not require client auth.
   */
  proxyApiKey?: string;
  /** How the proxy obtains the upstream credential. */
  mode: "apikey" | "oauth" | "pool";
  /** Direct credential for `apikey` mode. Format: `{apiKey}` or `{apiKey}.{secret}` (Z.AI). */
  apiKey?: string;
  /** Path to stored OAuth credentials (for `oauth` mode). */
  oauthCredentialsPath?: string;
}

/** Pool-mode configuration for multi-account quota rotation. */
export interface PoolConfig {
  /** Path to pool.json relative to CWD. Default: "pool.json". */
  poolPath: string;
  /** How often to refresh quotas from the billing API (ms). Default: 300000 (5 min). */
  refreshIntervalMs: number;
  /**
   * Optional list of outbound proxy URLs for sticky per-account assignment.
   * Each pool account is assigned one proxy (round-robin by account index,
   * `accountProxies[index % accountProxies.length]`) so it always egresses
   * from the same IP across requests and restarts. Omit to use the global
   * `outboundProxy` (or no proxy) for all pool accounts.
   */
  accountProxies?: string[];
}

/**
 * Identity headers injected on every upstream request to mimic the ZCode
 * desktop client. Mirrors the `pio` builder in the reverse-engineered bundle
 * (`_reverse/zcode.cjs`); see `_reverse/NOTEPAD.md` "How Credential is Used".
 *
 * Resolution: env var (matches ZCode's own convention) → YAML override → default.
 * `appVersion` must be printable ASCII (`/^[\x20-\x7e]+$/`); non-conforming
 * values are silently dropped and fall back to the default (current ZCode
 * release), exactly like `fio` in the bundle.
 */
export interface ProxyIdentity {
  appVersion: string;
  sourceTitle: string;
  refererOrigin: string;
}

/** Local client-session inference mode for upstream session affinity. */
export interface ClientIdentityConfig {
  /** "observe" logs/instruments only; "enforce" reuses upstream x-session-id; "off" disables inference. */
  mode: "off" | "observe" | "enforce";
  /** In-memory session TTL in seconds. */
  ttlSeconds: number;
  /** Maximum number of inferred sessions retained in memory. */
  maxSessions: number;
}

/** Top-level proxy configuration. */
export interface ProxyConfig {
  server: {
    port: number;
    host: string;
  };
  auth: AuthConfig;
  /** Active upstream provider. */
  provider: "zai" | "bigmodel";
  /** Which plan tier to use. "coding-plan" (default) uses direct upstream endpoints; "start-plan" routes through zcode.z.ai with JWT auth. */
  plan: "coding-plan" | "start-plan";
  /** Per-provider endpoint overrides. */
  providers: {
    zai: ProviderEndpoints;
    bigmodel: ProviderEndpoints;
  };
  /** Default model id used when client request omits `model`. */
  defaultModel: string;
  /** Whitelist of allowed model ids. */
  models: string[];
  /**
   * Identity headers injected upstream. Always present after `loadConfig`;
   * defaults mirror the production ZCode desktop client.
   */
  identity: ProxyIdentity;
  /** Local client session inference for cache-affinity experiments. */
  clientIdentity: ClientIdentityConfig;
  logging: {
    level: "debug" | "info" | "warn" | "error";
  };
  /** Account pool config (only used when auth.mode is "pool"). */
  pool?: PoolConfig;
  /** Outbound HTTP/SOCKS proxy for all upstream requests. */
  outboundProxy?: OutboundProxyConfig;
}

/** Outbound proxy configuration. Supports http://, https://, socks5:// URLs. */
export interface OutboundProxyConfig {
  /** Proxy URL (e.g. "socks5://127.0.0.1:1080", "http://proxy:8080"). */
  url: string;
}
