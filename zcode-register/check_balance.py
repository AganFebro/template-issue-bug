#!/usr/bin/env python3
"""ZCode Pool Balance — aggregated quota across all accounts in pool.json.

Reads pool.json (plain JSON, same format as zcode_register.py output),
calls billing/balance for each account, and displays totals.

Usage:
  python check_balance.py                     # aggregate all accounts
  python check_balance.py --detail            # show per-account breakdown too
  python check_balance.py --exhausted          # accounts with zero quota on any model
  python check_balance.py --exhausted all      # accounts with zero quota on EVERY model
  python check_balance.py --exhausted glm-5-turbo  # accounts exhausted on this model only
"""

import json
import os
import platform
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import proxy_utils

# ──────────────────────────────────────────────────────────────
POOL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pool.json")
CONFIG_FILE = "config.json"
API = "https://zcode.z.ai/api/v1/zcode-plan/billing/balance?app_version=3.2.4"

HEADERS_TEMPLATE = {
    "User-Agent": "ZCode/3.2.4",
    "X-Title": "Z Code@electron",
    "X-ZCode-Agent": "glm",
    "X-ZCode-App-Version": "3.2.4",
    "HTTP-Referer": "https://zcode.z.ai",
}


def _load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


# Outbound HTTP/SOCKS proxy (--proxy <url> or config.json "proxies"), if any.
SESSION = proxy_utils.create_session(_load_config())


def fetch_balance(account: dict, retries: int = 1) -> dict | None:
    """Fetch billing/balance for one account. Returns {name: {remaining, total}}."""
    jwt = account.get("jwt", "")
    if not jwt:
        return None
    for attempt in range(retries):
        try:
            r = SESSION.get(
                API,
                headers={"Authorization": f"Bearer {jwt}", **HEADERS_TEMPLATE},
                timeout=15,
            )
            data = r.json()
            if data.get("code") == 0:
                balances = {}
                for b in data.get("data", {}).get("balances", []):
                    name = b.get("show_name", "")
                    if name:
                        balances[name] = {
                            "remaining": b.get("remaining_units", 0),
                            "total": b.get("total_units", 0),
                        }
                return balances
        except:
            pass
        if attempt < retries - 1:
            time.sleep(1)
    return None


def fmt(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def canon(name: str) -> str:
    """Canonicalize a model name for loose matching: lowercase, strip everything
    but letters/digits. Mirrors src/auth/pool.ts's canonicalizeModelName so
    `--exhausted glm-5-turbo` matches the billing API's "GLM-5-Turbo".
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


def flag_value(flag: str) -> str | None:
    """Return the value following `flag` in sys.argv, or None if omitted
    (e.g. bare `--exhausted`, or immediately followed by another `--flag`)."""
    if flag not in sys.argv:
        return None
    i = sys.argv.index(flag)
    if i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith("--"):
        return sys.argv[i + 1]
    return None


def bar(pct: float, width: int = 20) -> str:
    """Draw a colored progress bar."""
    filled = int(width * pct)
    if pct >= 0.9:
        color = "\033[31m"
    elif pct >= 0.7:
        color = "\033[33m"
    else:
        color = "\033[32m"
    return f"{color}{'█' * filled}{'░' * (width - filled)}\033[0m"


def main():
    if not os.path.exists(POOL_FILE):
        print(f"\033[31m  pool.json not found at {POOL_FILE}\033[0m")
        sys.exit(1)

    with open(POOL_FILE, "r", encoding="utf-8") as f:
        pool = json.load(f)

    if not pool:
        print("\033[33m  Pool is empty.\033[0m")
        sys.exit(0)

    show_detail = "--detail" in sys.argv
    show_failed = "--failed" in sys.argv or show_detail
    show_exhausted = "--exhausted" in sys.argv
    exhausted_filter = flag_value("--exhausted")  # None (any) | "all" | "<model>"

    print()
    print(f"  \033[36m╔{'═' * 42}╗\033[0m")
    print(
        f"  \033[36m║\033[0m  \033[1mZCode Pool Balance\033[0m{' ' * 29}\033[36m║\033[0m"
    )
    print(f"  \033[36m╚{'═' * 42}╝\033[0m")
    print()

    # Fetch all balances in parallel
    print(f"  Fetching quotas for {len(pool)} accounts...")
    t0 = time.time()
    results: list[tuple[dict, dict]] = []
    errors = 0
    failed_accounts: list[dict] = []
    idx = 0
    # First pass: parallel, single attempt per account
    with ThreadPoolExecutor(max_workers=len(pool)) as ex:
        futures = {ex.submit(fetch_balance, acc, 1): acc for acc in pool}
        for future in as_completed(futures):
            acc = futures[future]
            idx += 1
            try:
                bal = future.result()
                if bal:
                    results.append((acc, bal))
                    if show_detail:
                        remaining = sum(v["remaining"] for v in bal.values())
                        print(f"    \033[2m#{idx}: {fmt(remaining)} available\033[0m")
                else:
                    failed_accounts.append(acc)
                    if show_detail:
                        print(f"    \033[90m#{idx}: retrying...\033[0m")
            except:
                failed_accounts.append(acc)
                if show_detail:
                    print(f"    \033[90m#{idx}: retrying...\033[0m")

    # Second pass: sequential, 5 retries with 1s spacing for failed accounts
    if failed_accounts:
        if show_detail:
            print(
                f"\n  Retrying {len(failed_accounts)} failed accounts (sequential, 5 attempts)..."
            )
        still_failed = []
        for acc in failed_accounts:
            bal = fetch_balance(acc, retries=5)
            email = acc.get("email", "?")
            if bal:
                results.append((acc, bal))
                if show_detail:
                    remaining = sum(v["remaining"] for v in bal.values())
                    print(f"    \033[32m✓ {email}: {fmt(remaining)} available\033[0m")
            else:
                still_failed.append(acc)
                if show_detail:
                    print(f"    \033[31m✗ {email}: still failing\033[0m")
        recovered = len(failed_accounts) - len(still_failed)
        failed_accounts = still_failed
        if show_detail and recovered > 0:
            print(
                f"    \033[32mRecovered {recovered}/{recovered + len(still_failed)}\033[0m"
            )
    errors = len(failed_accounts)
    elapsed = time.time() - t0

    if not results:
        print(f"\n  \033[31m  All {len(pool)} accounts failed to fetch.\033[0m")
        sys.exit(1)

    # Aggregate
    agg: dict[str, dict] = {}
    for _, bal in results:
        for model, q in bal.items():
            if model not in agg:
                agg[model] = {"remaining": 0, "total": 0}
            agg[model]["remaining"] += q["remaining"]
            agg[model]["total"] += q["total"]

    total_remaining = sum(v["remaining"] for v in agg.values())
    total_capacity = sum(v["total"] for v in agg.values())

    # Find max model name length for alignment
    models = sorted(agg.keys())
    max_len = max(len(m) for m in models) if models else 10

    # Display
    print()
    print(
        f"  \033[1mAccounts\033[0m    : {len(results)} active  "
        + (f"\033[90m({errors} failed)\033[0m" if errors else "")
    )
    print(
        f"  \033[1mTotal Pool\033[0m : \033[1;36m{fmt(total_remaining)}\033[0m / {fmt(total_capacity)} tokens"
    )
    print()
    print(
        f"  {'Model':<{max_len}}  {'Remaining':>8}  {'Total':>8}  {'Usage':>7}  {'Bar'}"
    )
    print(f"  {'─' * max_len}  {'─' * 8}  {'─' * 8}  {'─' * 7}  {'─' * 20}")

    for model in models:
        q = agg[model]
        used = q["total"] - q["remaining"]
        pct = used / q["total"] if q["total"] > 0 else 0
        print(
            f"  \033[1m{model:<{max_len}}\033[0m  "
            f"\033[36m{fmt(q['remaining']):>8}\033[0m  "
            f"{fmt(q['total']):>8}  "
            f"{pct:>6.0%}   "
            f"{bar(pct)}"
        )

    print(f"\n  \033[90mFetched in {elapsed:.1f}s\033[0m")

    if show_exhausted:
        mode = (exhausted_filter or "any").lower()
        exhausted: list[tuple[str, list[str]]] = []
        for acc, bal in results:
            zeroed = sorted(
                model for model, q in bal.items() if q["remaining"] <= 0
            )
            if mode == "all":
                if bal and len(zeroed) == len(bal):
                    exhausted.append((acc.get("email", "?"), zeroed))
            elif mode == "any":
                if zeroed:
                    exhausted.append((acc.get("email", "?"), zeroed))
            else:
                target = canon(mode)
                matched = [m for m in zeroed if canon(m) == target]
                if matched:
                    exhausted.append((acc.get("email", "?"), matched))

        label = {
            "all": "have zero quota on ALL models",
            "any": "have zero quota on at least one model",
        }.get(mode, f"have zero quota on {exhausted_filter}")

        if exhausted:
            print(f"\n  \033[33m{len(exhausted)} account(s) {label}:\033[0m")
            for email, zeroed_models in exhausted:
                print(f"    \033[2m{email}\033[0m: {', '.join(zeroed_models)}")
        else:
            print(f"\n  \033[32mNo accounts {label}.\033[0m")

    if show_failed and failed_accounts:
        print(
            f"\n  \033[31m{len(failed_accounts)} accounts still failed — JWTs may be stale:\033[0m"
        )
        for acc in failed_accounts:
            print(f"    \033[2m{acc.get('email', '?')}\033[0m")
        print(f"\n  \033[90mRe-register with: python zcode_register.py\033[0m")


if __name__ == "__main__":
    main()
