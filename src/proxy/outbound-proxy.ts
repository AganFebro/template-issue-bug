/**
 * Outbound proxy support — routes all outbound `fetch()` calls (upstream LLM
 * requests, OAuth, key resolution, pool quota refresh, captcha config) through
 * a configured HTTP/HTTPS/SOCKS proxy.
 *
 * Uses Bun's native `fetch(url, { proxy })` support — no custom CONNECT/SOCKS
 * client needed.
 *
 * @see https://bun.sh/docs/api/fetch#proxying-requests
 */
import type { OutboundProxyConfig } from "../config/types.js";

/**
 * Wrap `fetch` so every request is routed through the configured proxy.
 * Returns `baseFetch` unchanged when no proxy is configured.
 */
export function createProxiedFetch(
  proxyConfig: OutboundProxyConfig | undefined,
  baseFetch: typeof fetch = fetch,
): typeof fetch {
  if (!proxyConfig?.url) return baseFetch;
  const proxy = proxyConfig.url;

  const proxiedFetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    return baseFetch(input as string | URL | Request, {
      ...init,
      proxy,
    } as RequestInit);
  }) as typeof fetch;

  return proxiedFetch;
}
