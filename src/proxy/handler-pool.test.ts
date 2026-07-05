/**
 * Regression test for pool-mode account rotation on gateway 1005 (quota
 * exhausted) responses.
 *
 * Reproduces the bug where `peekUpstreamJsonError` intercepted the 1005
 * response *before* the pool-rotation block ran for Anthropic-format clients
 * on `plan: start-plan` (both translate flags false for that combination),
 * making `auth.markExhausted` + retry-with-next-account unreachable. Also
 * covers the stale-body bug where a successful rotation retry's response
 * body was overwritten with the original exhausted-account error text.
 */
import { describe, it, expect } from "bun:test";
import { writeFileSync, mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { proxyRequest } from "./handler.js";
import { AuthManager } from "../auth/manager.js";
import type { ProxyConfig, ProxyIdentity } from "../config/types.js";

const TMP = join(tmpdir(), `zcode-proxy-handler-pool-test-${Date.now()}`);

function writePool(entries: Array<Record<string, unknown>>): string {
  mkdirSync(TMP, { recursive: true });
  const p = join(TMP, "pool.json");
  writeFileSync(p, JSON.stringify(entries), "utf-8");
  return p;
}

const IDENTITY: ProxyIdentity = {
  appVersion: "test-1.0.0",
  sourceTitle: "cli",
  refererOrigin: "https://zcode.z.ai",
};

const BASE_CONFIG: Omit<ProxyConfig, "auth" | "plan"> = {
  server: { port: 8080, host: "0.0.0.0" },
  provider: "zai",
  providers: {
    zai: {
      anthropicBase: "https://api.z.ai/api/anthropic",
      openaiBase: "https://api.z.ai/api/coding/paas/v4",
    },
    bigmodel: {
      anthropicBase: "https://open.bigmodel.cn/api/anthropic",
      openaiBase: "https://open.bigmodel.cn/api/coding/paas/v4",
    },
  },
  defaultModel: "glm-4.6",
  models: ["glm-4.6"],
  identity: IDENTITY,
  clientIdentity: { mode: "observe", ttlSeconds: 900, maxSessions: 1024 },
  logging: { level: "info" },
};

async function settle(): Promise<void> {
  await new Promise((r) => setTimeout(r, 0));
}

describe("pool rotation on gateway 1005 (Anthropic-format client + start-plan)", () => {
  it("rotates to the next account and returns its real response, not the stale 1005 error", async () => {
    const poolPath = writePool([
      { email: "a@x.com", apiKey: "keyA", provider: "zai", jwt: "jwtA" },
      { email: "b@x.com", apiKey: "keyB", provider: "zai", jwt: "jwtB" },
    ]);

    // Pool quota refresh — give A the quota so it's picked first.
    const poolFetch = (async (
      _url: string | URL | Request,
    ): Promise<Response> => {
      return new Response(
        JSON.stringify({
          code: 0,
          data: {
            balances: [
              { show_name: "GLM-4.6", remaining_units: 1000, total_units: 1000 },
            ],
          },
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }) as typeof fetch;

    const auth = new AuthManager({
      mode: "pool",
      provider: "zai",
      pool: { poolPath, refreshIntervalMs: 300_000 },
      fetchImpl: poolFetch,
    });
    await auth.start();
    await settle();

    const config: ProxyConfig = {
      ...BASE_CONFIG,
      auth: { mode: "pool" },
      plan: "start-plan",
    };

    const originalFetch = globalThis.fetch;
    globalThis.fetch = (async (req: RequestInfo | URL): Promise<Response> => {
      const url =
        typeof req === "string"
          ? req
          : req instanceof URL
            ? req.toString()
            : req.url;
      if (url.includes("/client/configs")) {
        return new Response(
          JSON.stringify({ data: { configs: { captcha: { enabled: false } } } }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      throw new Error(`unexpected global fetch in test: ${url}`);
    }) as typeof fetch;

    try {
      // Upstream mock: account A (jwtA) gets a 1005 quota-exhausted response;
      // account B (jwtB), picked after rotation, gets a real success reply.
      const upstreamFetch: typeof fetch = (async (
        req: RequestInfo | URL,
      ): Promise<Response> => {
        if (!(req instanceof Request)) throw new Error("expected Request");
        const authHeader = req.headers.get("authorization") ?? "";
        if (authHeader.includes("jwtA")) {
          return new Response(
            JSON.stringify({ code: 1005, msg: "quota exhausted" }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        if (authHeader.includes("jwtB")) {
          return new Response(
            JSON.stringify({
              id: "msg_b",
              type: "message",
              role: "assistant",
              content: [{ type: "text", text: "reply from account B" }],
              model: "glm-4.6",
              stop_reason: "end_turn",
              stop_sequence: null,
              usage: { input_tokens: 5, output_tokens: 3 },
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        throw new Error(`unexpected authorization header: ${authHeader}`);
      }) as typeof fetch;

      const clientReq = new Request("http://localhost:8080/v1/messages", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: '{"model":"glm-4.6","max_tokens":1024,"messages":[{"role":"user","content":"hi"}]}',
      });

      const resp = await proxyRequest(clientReq, "anthropic", {
        config,
        auth,
        fetchImpl: upstreamFetch,
      });

      expect(resp.status).toBe(200);
      const body = await resp.json();
      // Must be account B's real reply — not the stale 1005 error text, and
      // not a 402 (which is what peekUpstreamJsonError would have returned
      // before the pool-rotation block was moved ahead of it).
      expect(body.content[0].text).toBe("reply from account B");
    } finally {
      globalThis.fetch = originalFetch;
      auth.stop();
      rmSync(TMP, { recursive: true, force: true });
    }
  });

  it("rotates through multiple exhausted accounts (multi-hop) before finding one with quota", async () => {
    const poolPath = writePool([
      { email: "a@x.com", apiKey: "keyA", provider: "zai", jwt: "jwtA" },
      { email: "b@x.com", apiKey: "keyB", provider: "zai", jwt: "jwtB" },
      { email: "c@x.com", apiKey: "keyC", provider: "zai", jwt: "jwtC" },
      { email: "d@x.com", apiKey: "keyD", provider: "zai", jwt: "jwtD" },
    ]);

    const poolFetch = (async (): Promise<Response> => {
      return new Response(
        JSON.stringify({
          code: 0,
          data: {
            balances: [
              { show_name: "GLM-4.6", remaining_units: 1000, total_units: 1000 },
            ],
          },
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }) as typeof fetch;

    const auth = new AuthManager({
      mode: "pool",
      provider: "zai",
      pool: { poolPath, refreshIntervalMs: 300_000 },
      fetchImpl: poolFetch,
    });
    await auth.start();
    await settle();

    const config: ProxyConfig = {
      ...BASE_CONFIG,
      auth: { mode: "pool" },
      plan: "start-plan",
    };

    const originalFetch = globalThis.fetch;
    globalThis.fetch = (async (req: RequestInfo | URL): Promise<Response> => {
      const url =
        typeof req === "string"
          ? req
          : req instanceof URL
            ? req.toString()
            : req.url;
      if (url.includes("/client/configs")) {
        return new Response(
          JSON.stringify({ data: { configs: { captcha: { enabled: false } } } }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      throw new Error(`unexpected global fetch in test: ${url}`);
    }) as typeof fetch;

    try {
      // A and B exhausted (1005); C and D both have real quota. Whichever of
      // C/D the (weighted-random) rotation lands on first should succeed
      // within POOL_ROTATION_MAX_HOPS (3) hops.
      const upstreamFetch: typeof fetch = (async (
        req: RequestInfo | URL,
      ): Promise<Response> => {
        if (!(req instanceof Request)) throw new Error("expected Request");
        const authHeader = req.headers.get("authorization") ?? "";
        if (authHeader.includes("jwtA") || authHeader.includes("jwtB")) {
          return new Response(
            JSON.stringify({ code: 1005, msg: "quota exhausted" }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        if (authHeader.includes("jwtC") || authHeader.includes("jwtD")) {
          const who = authHeader.includes("jwtC") ? "C" : "D";
          return new Response(
            JSON.stringify({
              id: `msg_${who.toLowerCase()}`,
              type: "message",
              role: "assistant",
              content: [{ type: "text", text: `reply from account ${who}` }],
              model: "glm-4.6",
              stop_reason: "end_turn",
              stop_sequence: null,
              usage: { input_tokens: 5, output_tokens: 3 },
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        throw new Error(`unexpected authorization header: ${authHeader}`);
      }) as typeof fetch;

      const clientReq = new Request("http://localhost:8080/v1/messages", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: '{"model":"glm-4.6","max_tokens":1024,"messages":[{"role":"user","content":"hi"}]}',
      });

      const resp = await proxyRequest(clientReq, "anthropic", {
        config,
        auth,
        fetchImpl: upstreamFetch,
      });

      expect(resp.status).toBe(200);
      const body = await resp.json();
      expect(body.content[0].text).toMatch(/^reply from account [CD]$/);
    } finally {
      globalThis.fetch = originalFetch;
      auth.stop();
      rmSync(TMP, { recursive: true, force: true });
    }
  });

  it("gives up with a 402 after POOL_ROTATION_MAX_HOPS accounts are all exhausted", async () => {
    const poolPath = writePool([
      { email: "a@x.com", apiKey: "keyA", provider: "zai", jwt: "jwtA" },
      { email: "b@x.com", apiKey: "keyB", provider: "zai", jwt: "jwtB" },
      { email: "c@x.com", apiKey: "keyC", provider: "zai", jwt: "jwtC" },
      { email: "d@x.com", apiKey: "keyD", provider: "zai", jwt: "jwtD" },
      { email: "e@x.com", apiKey: "keyE", provider: "zai", jwt: "jwtE" },
    ]);

    const poolFetch = (async (): Promise<Response> => {
      return new Response(
        JSON.stringify({
          code: 0,
          data: {
            balances: [
              { show_name: "GLM-4.6", remaining_units: 1000, total_units: 1000 },
            ],
          },
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }) as typeof fetch;

    const auth = new AuthManager({
      mode: "pool",
      provider: "zai",
      pool: { poolPath, refreshIntervalMs: 300_000 },
      fetchImpl: poolFetch,
    });
    await auth.start();
    await settle();

    const config: ProxyConfig = {
      ...BASE_CONFIG,
      auth: { mode: "pool" },
      plan: "start-plan",
    };

    const originalFetch = globalThis.fetch;
    globalThis.fetch = (async (req: RequestInfo | URL): Promise<Response> => {
      const url =
        typeof req === "string"
          ? req
          : req instanceof URL
            ? req.toString()
            : req.url;
      if (url.includes("/client/configs")) {
        return new Response(
          JSON.stringify({ data: { configs: { captcha: { enabled: false } } } }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      throw new Error(`unexpected global fetch in test: ${url}`);
    }) as typeof fetch;

    try {
      // Every account (5, more than POOL_ROTATION_MAX_HOPS=3) returns 1005 —
      // the loop must give up after the hop budget instead of retrying
      // forever or hanging, and surface a proper 402 to the client.
      let callCount = 0;
      const upstreamFetch: typeof fetch = (async (): Promise<Response> => {
        callCount++;
        return new Response(
          JSON.stringify({ code: 1005, msg: "quota exhausted" }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }) as typeof fetch;

      const clientReq = new Request("http://localhost:8080/v1/messages", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: '{"model":"glm-4.6","max_tokens":1024,"messages":[{"role":"user","content":"hi"}]}',
      });

      const resp = await proxyRequest(clientReq, "anthropic", {
        config,
        auth,
        fetchImpl: upstreamFetch,
      });

      expect(resp.status).toBe(402);
      // Initial attempt + POOL_ROTATION_MAX_HOPS (3) retries = 4 total calls.
      expect(callCount).toBe(4);
    } finally {
      globalThis.fetch = originalFetch;
      auth.stop();
      rmSync(TMP, { recursive: true, force: true });
    }
  });
});
