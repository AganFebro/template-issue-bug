/**
 * Account pool manager — multi-account quota rotation for start-plan.
 *
 * Reads plain JSON credentials from pool.json (written by zcode_register.py),
 * refreshes per-model quotas via billing/balance every `refreshIntervalMs`,
 * and picks the best account for each request.
 *
 * On a 402/1005 upstream rejection, the current account's relevant model
 * quota is marked as exhausted immediately (set to 0 remaining) so the
 * next request picks a different account.
 *
 * Model names are matched via `canonicalizeModelName` (lowercase, strip all
 * non-alphanumeric characters) so lookups are case/punctuation-insensitive
 * between client-requested model ids (e.g. "glm-4.5-air") and the billing
 * API's `show_name` values (e.g. "GLM-4.5-Air") — no hardcoded per-model map
 * to maintain as new models are added.
 *
 * Concurrent requests for the same model can independently pick the same
 * account before quota state catches up (quota only truly updates via
 * `refreshQuota()` every `refreshIntervalMs`, or via `markExhausted()` after
 * a 1005 already happened). `getBestCredential` mitigates this with a
 * short-lived per-account "recent selections" penalty — see `SELECTION_WINDOW_MS`.
 */

import type { Credential } from "./types.js";
import type { PoolConfig } from "../config/types.js";
import { credentialString } from "./types.js";
import { readFileSync, existsSync } from "node:fs";

interface PoolEntry {
  email: string;
  apiKey: string;
  secret?: string;
  provider: string;
  jwt: string;
  userId?: string;
}

interface QuotaInfo {
  lastRefreshed: number;
  balances: Record<string /* canonicalized model name */, { remaining: number; total: number }>;
}

interface PoolAccount extends PoolEntry {
  quota: QuotaInfo;
  /**
   * Recent selection timestamps per canonicalized model key. Used only to
   * bias weighted-random selection away from accounts multiple concurrent
   * requests just picked — not a hard reservation system, entries just age
   * out of `SELECTION_WINDOW_MS`.
   */
  recentSelections: Record<string, number[]>;
  /**
   * Sticky outbound proxy URL assigned to this account (round-robin over
   * `PoolConfig.accountProxies` by account index), or undefined when no
   * per-account proxies are configured.
   */
  proxyUrl?: string;
}

/**
 * Canonicalize a model name for quota lookup: lowercase, strip everything but
 * letters/digits. Makes matching insensitive to casing/hyphens/dots between
 * client-requested model ids and the billing API's `show_name` values.
 */
function canonicalizeModelName(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]/g, "");
}

/**
 * Selection-timestamp window (ms) used to penalize an account's score when
 * scoring for the *same* model if it was recently picked by another request.
 * Heuristic mitigation for concurrent-request clustering, not a hard limiter
 * — very long-running requests can outlast this window (acceptable; real
 * quota state still self-corrects via `refreshQuota` and `markExhausted`).
 */
const SELECTION_WINDOW_MS = 15_000;

/**
 * Loads pool.json, periodically refreshes quotas, and picks the best account
 * for each request based on remaining quota for the requested model.
 */
export class PoolManager {
  private accounts: PoolAccount[] = [];
  private refreshTimer: ReturnType<typeof setInterval> | null = null;
  private readonly poolPath: string;
  private readonly refreshIntervalMs: number;
  private readonly accountProxies: string[] | undefined;
  private fetchImpl: typeof fetch;

  constructor(config: PoolConfig, fetchImpl: typeof fetch = fetch) {
    this.poolPath = config.poolPath;
    this.refreshIntervalMs = config.refreshIntervalMs;
    this.accountProxies = config.accountProxies;
    this.fetchImpl = fetchImpl;
  }

  /** Load pool.json and start background quota refresh. */
  async start(): Promise<void> {
    if (!existsSync(this.poolPath)) {
      throw new Error(`pool.json not found at ${this.poolPath}. Run: python zcode-register/zcode_register.py`);
    }
    const raw = readFileSync(this.poolPath, "utf-8");
    const entries: PoolEntry[] = JSON.parse(raw);
    if (!Array.isArray(entries) || entries.length === 0) {
      throw new Error("pool.json is empty — register accounts first");
    }
    this.accounts = entries.map((e, idx) => ({
      ...e,
      quota: { lastRefreshed: 0, balances: {} },
      recentSelections: {},
      proxyUrl: this.accountProxies?.length
        ? this.accountProxies[idx % this.accountProxies.length]
        : undefined,
    }));
    // Initial refresh — don't block startup, run in background
    this.refreshAllQuotas().catch((err) =>
      console.error("[pool] initial quota refresh failed:", err?.message ?? err)
    );
    // Periodic refresh
    this.refreshTimer = setInterval(
      () => this.refreshAllQuotas().catch((e) => console.error("[pool] quota refresh:", e?.message ?? e)),
      this.refreshIntervalMs,
    );
  }

  /** Stop the background refresh timer. */
  stop(): void {
    if (this.refreshTimer) {
      clearInterval(this.refreshTimer);
      this.refreshTimer = null;
    }
  }

  /** Number of accounts in the pool. */
  get size(): number { return this.accounts.length; }

  /**
   * Get the credential for the best account for a given model.
   * Prioritizes accounts with highest remaining quota for the model.
   * Falls back to first account with any quota if model-specific data
   * is unavailable.
   */
  getBestCredential(model: string): Credential | null {
    if (this.accounts.length === 0) return null;

    const modelKey = canonicalizeModelName(model);

    // Refresh if we have no quota data yet
    const allStale = this.accounts.every((a) => a.quota.lastRefreshed === 0);
    if (allStale) return this.select(0, modelKey);

    const now = Date.now();

    // Score accounts by remaining quota for this model, penalized by how many
    // requests recently picked this same account+model — mitigates concurrent
    // requests clustering on the same account before quota state catches up.
    const scored = this.accounts.map((a, idx) => {
      const bal = a.quota.balances[modelKey];
      if (!bal || bal.remaining <= 0) return { idx, score: 0 };
      const recentCount = this.pruneAndCountRecentSelections(a, modelKey, now);
      const baseScore = bal.remaining / (bal.total || 1);
      return { idx, score: baseScore / (1 + recentCount) };
    });

    // Sort descending by score
    scored.sort((a, b) => b.score - a.score);

    // Pick from top-scoring accounts with randomness proportional to score
    const top = scored.filter((s) => s.score > 0);
    if (top.length === 0) {
      // All accounts have 0 quota — pick any (first fallback)
      return this.select(0, modelKey);
    }

    // Weighted random pick among accounts with quota
    const totalWeight = top.reduce((sum, s) => sum + s.score, 0);
    let rand = Math.random() * totalWeight;
    for (const s of top) {
      rand -= s.score;
      if (rand <= 0) return this.select(s.idx, modelKey);
    }
    return this.select(top[top.length - 1].idx, modelKey);
  }

  /**
   * Mark the current account's quota for a model as exhausted (0 remaining).
   * Called when upstream returns 1005 or 402 for this account + model.
   */
  markExhausted(cred: Credential, model: string): void {
    const modelKey = canonicalizeModelName(model);
    for (const a of this.accounts) {
      if (a.jwt === cred.jwt || a.apiKey === cred.apiKey) {
        if (!a.quota.balances[modelKey]) {
          a.quota.balances[modelKey] = { remaining: 0, total: 1 };
        } else {
          a.quota.balances[modelKey].remaining = 0;
        }
        break;
      }
    }
  }

  private toCredential(entry: PoolAccount): Credential {
    return {
      apiKey: entry.apiKey,
      secret: entry.secret,
      provider: entry.provider as any,
      jwt: entry.jwt,
      userId: entry.userId,
      proxyUrl: entry.proxyUrl,
    };
  }

  /** Record a selection timestamp for account+model and return its credential. */
  private select(idx: number, modelKey: string): Credential {
    const account = this.accounts[idx];
    const list = account.recentSelections[modelKey];
    if (list) list.push(Date.now());
    else account.recentSelections[modelKey] = [Date.now()];
    return this.toCredential(account);
  }

  /**
   * Prune selection timestamps outside `SELECTION_WINDOW_MS` and return the
   * remaining (recent) count for this account+model.
   */
  private pruneAndCountRecentSelections(
    account: PoolAccount,
    modelKey: string,
    now: number,
  ): number {
    const list = account.recentSelections[modelKey];
    if (!list || list.length === 0) return 0;
    const cutoff = now - SELECTION_WINDOW_MS;
    const kept = list.filter((t) => t > cutoff);
    account.recentSelections[modelKey] = kept;
    return kept.length;
  }

  private async refreshAllQuotas(): Promise<void> {
    for (const account of this.accounts) {
      try {
        await this.refreshQuota(account);
      } catch (err) {
        // Individual account refresh failure shouldn't kill the pool
      }
    }
  }

  private async refreshQuota(account: PoolAccount): Promise<void> {
    if (!account.jwt) return;

    const resp = await this.fetchImpl(
      "https://zcode.z.ai/api/v1/zcode-plan/billing/balance?app_version=3.2.4",
      {
        headers: {
          authorization: `Bearer ${account.jwt}`,
          "content-type": "application/json",
          "user-agent": "ZCode/3.2.4",
          "x-title": "Z Code@electron",
          "x-zcode-agent": "glm",
          "x-zcode-app-version": "3.2.4",
          "http-referer": "https://zcode.z.ai",
        },
      },
    );

    if (!resp.ok) return;

    const data = (await resp.json()) as {
      code?: number;
      data?: { balances?: Array<{ show_name?: string; remaining_units?: number; total_units?: number }> };
    };
    if (data.code !== 0 || !data.data?.balances) return;

    const balances: Record<string, { remaining: number; total: number }> = {};
    for (const b of data.data.balances) {
      if (b.show_name) {
        balances[canonicalizeModelName(b.show_name)] = {
          remaining: b.remaining_units ?? 0,
          total: b.total_units ?? 1,
        };
      }
    }
    account.quota = { lastRefreshed: Date.now(), balances };
  }
}
