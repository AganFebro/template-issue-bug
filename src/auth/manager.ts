/**
 * Auth manager — picks the right credential source based on mode.
 * @see .omo/plans/zcode-proxy.md Task 4
 */
import type { Credential } from "./types.js";
import { createApiKeyCredential } from "./apikey.js";
import type { ProviderId } from "../provider/types.js";
import { PoolManager } from "./pool.js";
import type { PoolConfig } from "../config/types.js";

type AuthMode = "apikey" | "oauth" | "pool";

/** Options for constructing an `AuthManager`. */
interface AuthManagerOptions {
  mode: AuthMode;
  provider: ProviderId;
  /** Raw credential string for apikey mode (`{apiKey}` or `{apiKey}.{secret}`). */
  apiKey?: string;
  /** Pool config (only used in pool mode). */
  pool?: PoolConfig;
}

/**
 * Resolves the upstream credential to inject into proxied requests.
 *
 * In `apikey` mode: returns a static credential parsed from the config string.
 * In `oauth` mode: uses the stored OAuth credential.
 * In `pool` mode: picks the best account from pool.json based on quota.
 */
export class AuthManager {
  private mode: AuthMode;
  private provider: ProviderId;
  private cachedApiKeyCred: Credential | null = null;
  private oauthCred: Credential | null = null;
  private pool: PoolManager | null = null;

  constructor(opts: AuthManagerOptions) {
    this.mode = opts.mode;
    this.provider = opts.provider;
    if (opts.mode === "apikey" && opts.apiKey) {
      this.cachedApiKeyCred = createApiKeyCredential(
        this.provider,
        opts.apiKey,
      );
    }
    if (opts.mode === "pool" && opts.pool) {
      this.pool = new PoolManager(opts.pool);
    }
  }

  /** Start background services (pool quota refresh). */
  async start(): Promise<void> {
    if (this.pool) await this.pool.start();
  }

  /** Stop background services. */
  stop(): void {
    if (this.pool) this.pool.stop();
  }

  /**
   * Returns the current credential.
   *
   * In pool mode, `model` is used to pick the account with the most
   * remaining quota for that model. In other modes, `model` is ignored.
   */
  async getCredential(model?: string): Promise<Credential> {
    if (this.mode === "pool" && this.pool) {
      if (!model) {
        // Fallback: return first account if no model specified
        // (shouldn't happen in normal flow)
        const cred = await this.getAnyPoolCredential();
        if (cred) return cred;
        throw new Error("pool is empty");
      }
      const cred = this.pool.getBestCredential(model);
      if (cred) return cred;
      throw new Error("pool has no available accounts");
    }

    if (this.mode === "apikey") {
      if (this.cachedApiKeyCred) return this.cachedApiKeyCred;
      throw new Error("apikey mode configured but no credential was set");
    }

    // oauth mode
    if (this.oauthCred) {
      if (this.oauthCred.expiresAt && Date.now() >= this.oauthCred.expiresAt) {
        this.oauthCred = null;
        throw new Error("OAuth credential expired; re-authentication required");
      }
      return this.oauthCred;
    }
    throw new Error("OAuth credential not available — run login flow first");
  }

  private async getAnyPoolCredential(): Promise<Credential | null> {
    return this.pool?.getBestCredential("GLM-5.2") ?? null;
  }

  /**
   * Mark a credential as exhausted for a model.
   * In pool mode, sets that account's quota to 0 for the model so
   * subsequent requests pick a different account.
   */
  markExhausted(cred: Credential, model: string): void {
    if (this.pool) this.pool.markExhausted(cred, model);
  }

  /** Set the OAuth credential (used by OAuth login flow). */
  setOAuthCredential(cred: Credential): void {
    this.oauthCred = cred;
  }

  /** Current auth mode. */
  getMode(): AuthMode {
    return this.mode;
  }

  /** True if pool mode is active. */
  isPool(): boolean {
    return this.mode === "pool" && this.pool !== null;
  }

  /** Number of accounts in the pool. */
  get poolSize(): number {
    return this.pool?.size ?? 0;
  }
}
