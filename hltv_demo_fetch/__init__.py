"""hltv-demo-fetch — download CS2 GOTV demos from HLTV.

The transport is designed for Cloudflare-protected pages:

* curl_cffi Chrome TLS/JA3 + HTTP/2 impersonation (fast path);
* a real Chrome via Playwright/patchright for managed challenges;
* optional CDP attach to an already verified / logged-in Chrome;
* Jina Reader page discovery when the egress IP is hard-blocked.

Usage:
    python3 -m hltv_demo_fetch --help
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
