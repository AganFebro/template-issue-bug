#!/usr/bin/env python3
"""Quick smoke test for zcode-proxy — sends a simple prompt to both models."""

import sys

import requests

BASE = "http://127.0.0.1:8080"
API_KEY = sys.argv[1] if len(sys.argv) > 1 else "your-proxy-secret"
HEADERS = {
    "Content-Type": "application/json",
    "x-api-key": API_KEY,
}

MODELS = ["glm-5-turbo", "glm-5.2"]


def test(model: str) -> bool:
    payload = {
        "model": model,
        "max_tokens": 128,
        "messages": [{"role": "user", "content": "Say hello in exactly 10 words."}],
    }
    print(f"  {model:<16} ", end="", flush=True)
    try:
        r = requests.post(
            f"{BASE}/v1/messages", headers=HEADERS, json=payload, timeout=120
        )
        data = r.json()
        if r.ok:
            text = ""
            for block in data.get("content", []):
                text += block.get("text", "")
            safe = text.replace("\n", " ").strip()[:80]
            print(f"\033[32mOK\033[0m   → {safe}")
            return True
        else:
            err = data.get("error", {}).get("message", r.text)[:80]
            print(f"\033[31m{r.status_code}\033[0m → {err}")
            return False
    except Exception as e:
        print(f"\033[31mERR\033[0m  → {e}")
        return False


if __name__ == "__main__":
    print(f"zcode-proxy smoke test  (key: {API_KEY[:4]}...{API_KEY[-4:]})\n")
    ok = 0
    for m in MODELS:
        if test(m):
            ok += 1
    print(f"\n{ok}/{len(MODELS)} passed")
