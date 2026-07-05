"""Outbound HTTP/SOCKS proxy helper shared by zcode_register.py and check_balance.py.

Resolves a proxy URL from (in precedence order) the `--proxy <url>` CLI flag,
then the `proxies` list in config.json (first entry), and builds a
`requests.Session` and/or Playwright proxy dict routed through it.

config.json format:
  "proxies": []                            # no proxy (default)
  "proxies": ["socks5://host:port"]        # single proxy, used for all requests
  "proxies": ["http://user:pass@host:port"]

Only the first entry is used — this project does not yet rotate outbound
proxies per-account (see "Further Considerations" in the proxy support plan).
SOCKS5 requires the `PySocks` package (see requirements.txt).
"""

import sys
from typing import Optional

import requests


def resolve_proxy_url(config: dict) -> Optional[str]:
    """Resolve the outbound proxy URL. Returns None when none is configured."""
    argv = sys.argv
    for i, arg in enumerate(argv):
        if arg == "--proxy" and i + 1 < len(argv):
            return argv[i + 1]
    proxies = config.get("proxies") or []
    return proxies[0] if proxies else None


def create_session(config: dict) -> requests.Session:
    """Build a `requests.Session` routed through the configured proxy, if any."""
    session = requests.Session()
    proxy_url = resolve_proxy_url(config)
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}
    return session


def playwright_proxy(config: dict) -> Optional[dict]:
    """Return a Playwright-compatible proxy dict (`{"server": url}`), or None."""
    proxy_url = resolve_proxy_url(config)
    return {"server": proxy_url} if proxy_url else None
