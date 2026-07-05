/**
 * Tests for the account pool manager — model-name canonicalization, quota-aware
 * account selection, exhaustion marking, and the concurrent-selection penalty.
 */
import { describe, it, expect, afterEach } from "bun:test";
import { writeFileSync, mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { PoolManager } from "./pool.js";
import type { PoolConfig } from "../config/types.js";

const TMP = join(tmpdir(), `zcode-proxy-pool-test-${Date.now()}`);

function writePool(entries: Array<Record<string, unknown>>): string {
  mkdirSync(TMP, { recursive: true });
  const p = join(TMP, `pool-${Math.random().toString(36).slice(2)}.json`);
  writeFileSync(p, JSON.stringify(entries), "utf-8");
  return p;
}

afterEach(() => {
  rmSync(TMP, { recursive: true, force: true });
});

interface BalanceEntry {
  show_name: string;
  remaining_units: number;
  total_units: number;
}

/** Mock fetchImpl returning a billing/balance response keyed by JWT. */
function mockFetch(balancesByJwt: Record<string, BalanceEntry[]>): typeof fetch {
  return (async (_url: string | URL | Request, init?: RequestInit) => {
    const headers = (init?.headers ?? {}) as Record<string, string>;
    const auth = headers.authorization ?? "";
    const jwt = auth.replace(/^Bearer /, "");
    const balances = balancesByJwt[jwt] ?? [];
    return new Response(JSON.stringify({ code: 0, data: { balances } }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  }) as typeof fetch;
}

/** Flush the fire-and-forget initial quota refresh kicked off by start(). */
async function settle(): Promise<void> {
  await new Promise((r) => setTimeout(r, 0));
}

function poolConfig(path: string, accountProxies?: string[]): PoolConfig {
  return {
    poolPath: path,
    refreshIntervalMs: 300_000,
    ...(accountProxies ? { accountProxies } : {}),
  };
}

describe("PoolManager", () => {
  it("throws when pool.json does not exist", async () => {
    const pm = new PoolManager(poolConfig(join(TMP, "missing.json")));
    await expect(pm.start()).rejects.toThrow(/pool\.json not found/);
  });

  it("throws when pool.json is an empty array", async () => {
    const path = writePool([]);
    const pm = new PoolManager(poolConfig(path));
    await expect(pm.start()).rejects.toThrow(/pool\.json is empty/);
  });

  it("returns null when the pool has no accounts loaded", () => {
    const pm = new PoolManager(poolConfig(join(TMP, "unused.json")));
    expect(pm.getBestCredential("glm-4.6")).toBeNull();
  });

  it("canonicalizes model names for all config.yaml models (case/punctuation-insensitive)", async () => {
    const models: Array<[string, string]> = [
      ["glm-4.5-air", "GLM-4.5-Air"],
      ["glm-4.6", "GLM-4.6"],
      ["glm-4.6v", "GLM-4.6V"],
      ["glm-4.7", "GLM-4.7"],
      ["glm-5", "GLM-5"],
      ["glm-5-turbo", "GLM-5-Turbo"],
      ["glm-5v-turbo", "GLM-5V-Turbo"],
      ["glm-5.1", "GLM-5.1"],
      ["glm-5.2", "GLM-5.2"],
    ];

    for (const [requestModel, showName] of models) {
      const path = writePool([
        { email: "a@x.com", apiKey: "keyA", provider: "zai", jwt: "jwtA" },
        { email: "b@x.com", apiKey: "keyB", provider: "zai", jwt: "jwtB" },
      ]);
      const pm = new PoolManager(
        poolConfig(path),
        mockFetch({
          jwtA: [{ show_name: showName, remaining_units: 0, total_units: 100 }],
          jwtB: [{ show_name: showName, remaining_units: 100, total_units: 100 }],
        }),
      );
      await pm.start();
      await settle();
      const cred = pm.getBestCredential(requestModel);
      expect(cred?.apiKey).toBe("keyB"); // only B has quota for this model
      pm.stop();
    }
  });

  it("prefers the account with more remaining quota for the requested model", async () => {
    const path = writePool([
      { email: "a@x.com", apiKey: "keyA", provider: "zai", jwt: "jwtA" },
      { email: "b@x.com", apiKey: "keyB", provider: "zai", jwt: "jwtB" },
    ]);
    const pm = new PoolManager(
      poolConfig(path),
      mockFetch({
        jwtA: [{ show_name: "GLM-4.6", remaining_units: 0, total_units: 1000 }],
        jwtB: [{ show_name: "GLM-4.6", remaining_units: 1000, total_units: 1000 }],
      }),
    );
    await pm.start();
    await settle();
    const cred = pm.getBestCredential("glm-4.6");
    expect(cred?.apiKey).toBe("keyB");
    pm.stop();
  });

  it("markExhausted zeroes only the given account+model, not other models or accounts", async () => {
    const path = writePool([
      { email: "a@x.com", apiKey: "keyA", provider: "zai", jwt: "jwtA" },
      { email: "b@x.com", apiKey: "keyB", provider: "zai", jwt: "jwtB" },
    ]);
    const pm = new PoolManager(
      poolConfig(path),
      mockFetch({
        jwtA: [
          { show_name: "GLM-4.6", remaining_units: 1000, total_units: 1000 },
          { show_name: "GLM-5.2", remaining_units: 1000, total_units: 1000 },
        ],
        jwtB: [
          // Tiny but nonzero so B is still selectable once A is exhausted,
          // while A's overwhelming weight keeps the initial pick effectively
          // deterministic (weighted-random pick — not literally guaranteed).
          { show_name: "GLM-4.6", remaining_units: 1, total_units: 1000 },
          // Zero for glm-5.2 so it's excluded from selection entirely —
          // keeps the "other model unaffected" assertion deterministic.
          { show_name: "GLM-5.2", remaining_units: 0, total_units: 1000 },
        ],
      }),
    );
    await pm.start();
    await settle();

    const credA = pm.getBestCredential("glm-4.6");
    expect(credA?.apiKey).toBe("keyA"); // A has overwhelmingly more quota

    pm.markExhausted(credA!, "glm-4.6");

    // A is now exhausted for glm-4.6 — B should be picked instead.
    const next = pm.getBestCredential("glm-4.6");
    expect(next?.apiKey).toBe("keyB");

    // A should still be preferred for a DIFFERENT model (glm-5.2 untouched;
    // B has zero quota for it regardless of the glm-4.6 exhaustion above).
    const otherModel = pm.getBestCredential("glm-5.2");
    expect(otherModel?.apiKey).toBe("keyA");

    pm.stop();
  });

  it("falls back to any account when all accounts have zero quota for the model", async () => {
    const path = writePool([
      { email: "a@x.com", apiKey: "keyA", provider: "zai", jwt: "jwtA" },
      { email: "b@x.com", apiKey: "keyB", provider: "zai", jwt: "jwtB" },
    ]);
    const pm = new PoolManager(
      poolConfig(path),
      mockFetch({
        jwtA: [{ show_name: "GLM-4.6", remaining_units: 0, total_units: 1000 }],
        jwtB: [{ show_name: "GLM-4.6", remaining_units: 0, total_units: 1000 }],
      }),
    );
    await pm.start();
    await settle();
    const cred = pm.getBestCredential("glm-4.6");
    expect(cred).not.toBeNull();
    pm.stop();
  });

  it("penalizes an account's score after repeated concurrent-like selections, spreading picks to other accounts", async () => {
    const path = writePool([
      { email: "a@x.com", apiKey: "keyA", provider: "zai", jwt: "jwtA" },
      { email: "b@x.com", apiKey: "keyB", provider: "zai", jwt: "jwtB" },
    ]);
    // A modest quota edge (600 vs 500) so a handful of repeated selections of
    // A (each recorded as a "recent selection") should push its penalized
    // score below B's within just a few picks.
    const pm = new PoolManager(
      poolConfig(path),
      mockFetch({
        jwtA: [{ show_name: "GLM-4.6", remaining_units: 600, total_units: 1000 }],
        jwtB: [{ show_name: "GLM-4.6", remaining_units: 500, total_units: 1000 }],
      }),
    );
    await pm.start();
    await settle();

    let bPicked = false;
    for (let i = 0; i < 30; i++) {
      const cred = pm.getBestCredential("glm-4.6");
      if (cred?.apiKey === "keyB") {
        bPicked = true;
        break;
      }
    }
    // Soft mitigation, not a hard guarantee — but with a modest initial edge
    // and an accumulating penalty on repeated picks, B should get selected
    // well within 30 tries.
    expect(bPicked).toBe(true);

    pm.stop();
  });

  it("assigns a sticky proxy per account (round-robin by index over accountProxies)", async () => {
    const accountProxies = [
      "http://user:pass@proxy1:1000",
      "http://user:pass@proxy2:2000",
    ];

    // Give exactly one account positive quota per call so selection is
    // deterministic (top has exactly one candidate), then read back its
    // assigned proxyUrl.
    async function proxyForAccount(
      onlyPositiveJwt: string,
    ): Promise<string | undefined> {
      const path = writePool([
        { email: "a@x.com", apiKey: "keyA", provider: "zai", jwt: "jwtA" },
        { email: "b@x.com", apiKey: "keyB", provider: "zai", jwt: "jwtB" },
        { email: "c@x.com", apiKey: "keyC", provider: "zai", jwt: "jwtC" },
      ]);
      const zero = { show_name: "GLM-4.6", remaining_units: 0, total_units: 1000 };
      const positive = { show_name: "GLM-4.6", remaining_units: 1000, total_units: 1000 };
      const balances: Record<string, BalanceEntry[]> = {
        jwtA: [onlyPositiveJwt === "jwtA" ? positive : zero],
        jwtB: [onlyPositiveJwt === "jwtB" ? positive : zero],
        jwtC: [onlyPositiveJwt === "jwtC" ? positive : zero],
      };
      const pm = new PoolManager(
        poolConfig(path, accountProxies),
        mockFetch(balances),
      );
      await pm.start();
      await settle();
      const cred = pm.getBestCredential("glm-4.6");
      pm.stop();
      return cred?.proxyUrl;
    }

    // 3 accounts, 2 proxies: index 0→proxy0, 1→proxy1, 2→proxy0 (wraps).
    expect(await proxyForAccount("jwtA")).toBe(accountProxies[0]);
    expect(await proxyForAccount("jwtB")).toBe(accountProxies[1]);
    expect(await proxyForAccount("jwtC")).toBe(accountProxies[0]);
  });

  it("leaves proxyUrl undefined when accountProxies is not configured", async () => {
    const path = writePool([
      { email: "a@x.com", apiKey: "keyA", provider: "zai", jwt: "jwtA" },
    ]);
    const pm = new PoolManager(
      poolConfig(path),
      mockFetch({
        jwtA: [{ show_name: "GLM-4.6", remaining_units: 1000, total_units: 1000 }],
      }),
    );
    await pm.start();
    await settle();
    const cred = pm.getBestCredential("glm-4.6");
    expect(cred?.proxyUrl).toBeUndefined();
    pm.stop();
  });
});
