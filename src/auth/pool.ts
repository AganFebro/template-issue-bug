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
  balances: Record<string /* model name */, { remaining: number; total: number }>;
}

interface PoolAccount extends PoolEntry {
  quota: QuotaInfo;
}

/**
 * Loads pool.json, periodically refreshes quotas, and picks the best account
 * for each request based on remaining quota for the requested model.
 */
export class PoolManager {
  private accounts: PoolAccount[] = [];
  private refreshTimer: ReturnType<typeof setInterval> | null = null;
  private readonly poolPath: string;
  private readonly refreshIntervalMs: number;
  private fetchImpl: typeof fetch;

  constructor(config: PoolConfig, fetchImpl: typeof fetch = fetch) {
    this.poolPath = config.poolPath;
    this.refreshIntervalMs = config.refreshIntervalMs;
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
    this.accounts = entries.map((e) => ({
      ...e,
      quota: { lastRefreshed: 0, balances: {} },
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

    // Refresh if we have no quota data yet
    const allStale = this.accounts.every((a) => a.quota.lastRefreshed === 0);
    if (allStale) return this.toCredential(this.accounts[0]);

    // Normalize model name for quota lookup
    const modelKey = this.normalizeModelName(model);

    // Score accounts by remaining quota for this model
    const scored = this.accounts.map((a, idx) => {
      const bal = a.quota.balances[modelKey];
      if (!bal || bal.remaining <= 0) return { idx, score: 0 };
      // Weight: remaining / total gives a 0-1 score
      return { idx, score: bal.remaining / (bal.total || 1) };
    });

    // Sort descending by score
    scored.sort((a, b) => b.score - a.score);

    // Pick from top-scoring accounts with randomness proportional to score
    const top = scored.filter((s) => s.score > 0);
    if (top.length === 0) {
      // All accounts have 0 quota — pick any (first fallback)
      return this.toCredential(this.accounts[0]);
    }

    // Weighted random pick among accounts with quota
    const totalWeight = top.reduce((sum, s) => sum + s.score, 0);
    let rand = Math.random() * totalWeight;
    for (const s of top) {
      rand -= s.score;
      if (rand <= 0) return this.toCredential(this.accounts[s.idx]);
    }
    return this.toCredential(this.accounts[top[top.length - 1].idx]);
  }

  /**
   * Mark the current account's quota for a model as exhausted (0 remaining).
   * Called when upstream returns 1005 or 402 for this account + model.
   */
  markExhausted(cred: Credential, model: string): void {
    const modelKey = this.normalizeModelName(model);
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

  private toCredential(entry: PoolEntry): Credential {
    return {
      apiKey: entry.apiKey,
      secret: entry.secret,
      provider: entry.provider as any,
      jwt: entry.jwt,
      userId: entry.userId,
    };
  }

  private normalizeModelName(model: string): string {
    const lower = model.toLowerCase().replace(/[_-]/g, "");
    const map: Record<string, string> = {
      "glm52": "GLM-5.2",
      "glm5.2": "GLM-5.2",
      "glm5turbo": "GLM-5-Turbo",
      "glm5.turbo": "GLM-5-Turbo",
      "glm5": "GLM-5",
      "glm46": "GLM-4.6",
      "glm4.6": "GLM-4.6",
    };
    return map[lower] ?? lower;
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
        balances[b.show_name] = {
          remaining: b.remaining_units ?? 0,
          total: b.total_units ?? 1,
        };
      }
    }
    account.quota = { lastRefreshed: Date.now(), balances };
  }
}
