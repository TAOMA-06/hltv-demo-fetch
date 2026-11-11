"""Anti-bot transport for HLTV.

HLTV sits behind Cloudflare's managed challenge.  This module implements a
layered transport instead of pretending that a normal ``curl`` is a browser:

1. **TLS / HTTP2 impersonation** with :mod:`curl_cffi` (Chrome JA3/JA4 + HTTP/2
   settings).  This is the fast path and works for most HLTV pages when the
   egress IP is not already burned.
2. **A real Chrome** driven by Playwright when a challenge page appears.  The
   browser is only used to clear the challenge and mint ``cf_clearance``; the
   resulting cookies are copied back to the HTTP session, so the rest of the
   crawl stays fast.
3. **CDP attach** to an already-open Chrome (``--cdp-url``).  This is the most
   reliable mode when the user is logged in and has already passed the
   challenge in their normal browser.
4. The caller (``sources/hltv.py``) still keeps the Jina Reader fallback for
   page discovery, and the open ``r2-demos.hltv.org`` CDN for large files.

Important boundary: TLS impersonation and headless browsers do not beat a hard
IP ban (HTTP 403 "Sorry, you have been blocked").  That needs a clean egress
(usually a residential proxy) or a browser session on an unblocked network.
"""

from __future__ import annotations

import atexit
import json
import os
import platform
import random
import re
import sys
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlsplit

from .errors import (
    BlockedError,
    BrowserUnavailable,
    ChallengeError,
    StealthError,
)
from .net import (
    DownloadError,
    DownloadResult,
    HttpError,
    download_file,
    request_text,
)
from .util import read_netscape_cookies

HLTV_BASE = "https://www.hltv.org"
CDN_HOST_MARKERS = ("r2-demos.hltv.org",)
DOWNLOAD_SUFFIXES = (
    ".rar",
    ".zip",
    ".7z",
    ".dem",
    ".dem.gz",
    ".dem.bz2",
    ".dem.zst",
)

# Cloudflare challenge pages.  Keep these lowercase: matching is case-insensitive.
_CHALLENGE_MARKERS = (
    "just a moment",
    "cf-challenge",
    "cf_chl_opt",
    "checking your browser",
    "enable javascript and cookies",
    "verifying you are human",
    "verification required",
    "managed challenge",
    "ddos protection by cloudflare",
)
# A hard WAF block cannot be solved by JavaScript or a different browser on the
# same IP.  Do not waste a browser launch on it.
_BLOCK_MARKERS = (
    "you have been blocked",
    "sorry, you have been blocked",
    "unable to access hltv.org",
    "error 1020",
    "access denied",
)

_JS_STEALTH = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def classify_page(status: int, headers: Mapping[str, Any], text: str) -> str:
    """Return ``challenge`` / ``blocked`` / ``rate_limited`` / ``""``.

    ``headers`` is a normal mapping (case-insensitive access is not required;
    callers pass a lower-cased dict when possible).
    """

    lowered = (text or "")[:40000].lower()
    mitigated = str(headers.get("cf-mitigated") or headers.get("CF-Mitigated") or "")
    if any(marker in lowered for marker in _BLOCK_MARKERS):
        return "blocked"
    if "challenge" in mitigated.lower():
        return "challenge"
    if any(marker in lowered for marker in _CHALLENGE_MARKERS):
        return "challenge"
    if status == 429:
        return "rate_limited"
    return ""


def is_cdn_url(url: str) -> bool:
    lowered = (url or "").lower()
    if any(marker in lowered for marker in CDN_HOST_MARKERS):
        return True
    try:
        path = urlsplit(lowered).path
    except ValueError:
        return False
    return path.endswith(DOWNLOAD_SUFFIXES)


def extract_cdn_url(text: str) -> Optional[str]:
    """Pick a direct demo/CDN URL out of HTML or Jina markdown."""

    for candidate in re.findall(r"https?://[^\s'\"<>)\]]+", text or ""):
        if is_cdn_url(candidate):
            return candidate
    return None


def default_state_path() -> Path:
    """Where cookies (including ``cf_clearance``) are cached between runs."""

    override = os.environ.get("HLTV_STATE_FILE")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        root = Path.home() / "Library" / "Caches" / "hltv-demo-fetch"
    elif os.name == "nt":
        root = (
            Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
            / "hltv-demo-fetch"
        )
    else:
        root = (
            Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
            / "hltv-demo-fetch"
        )
    return root / "hltv-state.json"


def load_state(path: Path | str) -> dict[str, Any]:
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        return {}
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path | str, cookies: Iterable[Mapping[str, Any]]) -> None:
    """Persist cookies atomically with ``0600`` permissions.

    The file can contain ``cf_clearance`` (and, if the user logged in, HLTV
    session cookies), so it must never be committed to a repository.
    """

    file_path = Path(path).expanduser()
    file_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": int(time.time()),
        "cookies": [dict(cookie) for cookie in cookies],
    }
    temp = file_path.with_name(file_path.name + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    temp.replace(file_path)


def cookie_header_to_cookies(
    header: str, *, domain: str = ".hltv.org"
) -> list[dict[str, Any]]:
    """Turn ``a=b; c=d`` into the dict shape used by state files."""

    cookies: list[dict[str, Any]] = []
    for part in (header or "").split(";"):
        name, separator, value = part.strip().partition("=")
        if not separator or not name.strip():
            continue
        cookies.append(
            {
                "name": name.strip(),
                "value": value.strip(),
                "domain": domain,
                "path": "/",
                "secure": True,
                "httpOnly": False,
                "expires": -1,
            }
        )
    return cookies


def _cookie_dict(cookie: Any) -> dict[str, Any]:
    """Normalise a curl_cffi / Playwright cookie object into a plain dict."""

    if isinstance(cookie, Mapping):
        data = dict(cookie)
    else:
        data = {
            key: getattr(cookie, key, None)
            for key in (
                "name",
                "value",
                "domain",
                "path",
                "secure",
                "httpOnly",
                "expires",
            )
        }
    out: dict[str, Any] = {
        "name": str(data.get("name") or ""),
        "value": str(data.get("value") or ""),
        "domain": str(data.get("domain") or ""),
        "path": str(data.get("path") or "/"),
        "secure": bool(data.get("secure", False)),
        "httpOnly": bool(data.get("httpOnly", False)),
    }
    expires = data.get("expires", data.get("expiry"))
    try:
        out["expires"] = float(expires) if expires is not None else -1
    except (TypeError, ValueError):
        out["expires"] = -1
    if data.get("sameSite"):
        out["sameSite"] = data["sameSite"]
    return out


def _is_hltv_cookie(cookie: Mapping[str, Any]) -> bool:
    domain = str(cookie.get("domain") or "").lstrip(".").lower()
    return domain == "hltv.org" or domain.endswith(".hltv.org")


def _dedupe_cookies(cookies: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for raw in cookies:
        cookie = _cookie_dict(raw)
        if not cookie["name"] or not _is_hltv_cookie(cookie):
            continue
        key = (cookie["name"], cookie["domain"], cookie["path"])
        merged[key] = cookie
    return list(merged.values())


def _looks_like_html(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            head = handle.read(512).lstrip().lower()
    except OSError:
        return False
    return (
        head.startswith(b"<!doctype html")
        or head.startswith(b"<html")
        or b"cloudflare" in head
        or b"just a moment" in head
    )


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class StealthConfig:
    """Knobs shared by the HTTP and browser layers."""

    engine: str = "auto"  # auto | curl | browser | cdp | stdlib
    impersonate: str = "chrome"
    proxy: Optional[str] = None
    cookie: Optional[str] = None
    cookie_file: Optional[str] = None
    state_file: Optional[str] = None
    headless: bool = True
    # A headless Chrome can mint cf_clearance but still fail to start the file
    # download on r2-demos.  Retry once with a visible window unless disabled.
    auto_headed: bool = True
    browser_channel: str = "auto"  # auto | chrome | msedge | chromium
    cdp_url: Optional[str] = None
    timeout: float = 45.0
    challenge_timeout: float = 40.0
    retries: int = 2
    request_delay: float = 0.6
    quiet: bool = False

    def resolved_engine(self) -> str:
        if self.cdp_url and self.engine in ("auto", "browser", "cdp"):
            return "cdp"
        return self.engine

    def resolved_state_file(self) -> Path:
        return (
            Path(self.state_file).expanduser()
            if self.state_file
            else default_state_path()
        )


# ---------------------------------------------------------------------------
# Browser layer
# ---------------------------------------------------------------------------


class BrowserSession:
    """A real Chrome used only to clear Cloudflare challenges."""

    def __init__(
        self, config: StealthConfig, *, log: Optional[Callable[[str], None]] = None
    ):
        self.config = config
        self._log = log or (lambda message: None)
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._owns_browser = False
        self._owns_context = False
        self._last_click = 0.0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._browser is not None:
            return
        try:
            # patchright is a stealth-patched drop-in for Playwright.  Prefer it
            # when the user installed it; plain Playwright is the fallback.
            from patchright.sync_api import sync_playwright

            self._driver_name = "patchright"
        except ImportError:
            try:
                from playwright.sync_api import sync_playwright

                self._driver_name = "playwright"
            except ImportError as error:
                raise BrowserUnavailable(
                    "浏览器模式需要 Playwright（推荐再装 patchright 增强隐蔽性）："
                    "`uv pip install --python learn/.venv/bin/python playwright patchright`，"
                    "然后 `learn/.venv/bin/python -m playwright install chrome`"
                    "（本机已装 Google Chrome 可跳过第二步）。"
                ) from error

        self._pw = sync_playwright().start()
        try:
            if self.config.cdp_url:
                self._start_cdp()
            else:
                self._start_launch()
        except BrowserUnavailable:
            self.close()
            raise
        except Exception as error:
            self.close()
            raise BrowserUnavailable(f"无法启动浏览器: {error}") from error

        # In CDP mode the user's live browser already has fresh cookies; do
        # not overwrite them with a stale local state file.
        if not self.config.cdp_url:
            self._load_cookies()

    def _start_cdp(self) -> None:
        assert self._pw is not None
        self._browser = self._pw.chromium.connect_over_cdp(self.config.cdp_url)
        self._owns_browser = False
        contexts = list(self._browser.contexts)
        if contexts:
            self._context = contexts[0]
            self._owns_context = False
        else:
            self._context = self._browser.new_context(**self._context_kwargs())
            self._owns_context = True
        self._log(f"已接管 Chrome CDP: {self.config.cdp_url}")

    def _start_launch(self) -> None:
        assert self._pw is not None
        channels = self._channel_candidates()
        last_error: Optional[Exception] = None
        launch_kwargs: dict[str, Any] = {}
        if self.config.proxy:
            launch_kwargs["proxy"] = {"server": self.config.proxy}
        for channel in channels:
            try:
                self._browser = self._pw.chromium.launch(
                    channel=channel,
                    headless=self.config.headless,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-dev-shm-usage",
                    ],
                    **launch_kwargs,
                )
                self._owns_browser = True
                label = channel or "bundled chromium"
                self._log(
                    f"浏览器已启动: {label} ({'headless' if self.config.headless else 'headed'})"
                )
                break
            except Exception as error:  # missing channel, sandbox failure, ...
                last_error = error
                continue
        if self._browser is None:
            raise BrowserUnavailable(
                "找不到可用的 Chrome。安装 Google Chrome，或运行 "
                "`learn/.venv/bin/python -m playwright install chrome`。"
                f" 最后一个错误: {last_error}"
            )
        self._context = self._browser.new_context(**self._context_kwargs())
        self._owns_context = True

    def _context_kwargs(self) -> dict[str, Any]:
        return {
            "locale": "en-US",
            "timezone_id": "Europe/Berlin",
            "viewport": {"width": 1440, "height": 900},
            "accept_downloads": True,
        }

    def _channel_candidates(self) -> list[Optional[str]]:
        preferred = (self.config.browser_channel or "auto").strip()
        if preferred and preferred != "auto":
            return [preferred]
        # Prefer a real Google Chrome: bundled Chromium is a much stronger bot
        # signal for Cloudflare.
        return ["chrome", "msedge", None]

    def _load_cookies(self) -> None:
        if self._context is None:
            return
        cookies = self._state_cookies()
        if not cookies:
            return
        payload = []
        for cookie in cookies:
            domain = cookie.get("domain") or ""
            if not domain and not cookie.get("url"):
                continue
            entry = {
                "name": cookie["name"],
                "value": cookie["value"],
                "domain": domain,
                "path": cookie.get("path") or "/",
                "secure": bool(cookie.get("secure", False)),
                "httpOnly": bool(cookie.get("httpOnly", False)),
            }
            expires = cookie.get("expires")
            if expires is not None and float(expires) > 0:
                entry["expires"] = float(expires)
            if cookie.get("sameSite"):
                entry["sameSite"] = cookie["sameSite"]
            payload.append(entry)
        try:
            self._context.add_cookies(payload)
        except Exception:
            # Bad/expired cookies must not prevent a fresh challenge attempt.
            pass

    def _state_cookies(self) -> list[dict[str, Any]]:
        data = load_state(self.config.resolved_state_file())
        cookies = [
            _cookie_dict(cookie)
            for cookie in data.get("cookies", [])
            if isinstance(cookie, Mapping)
        ]
        if self.config.cookie:
            cookies.extend(cookie_header_to_cookies(self.config.cookie))
        if self.config.cookie_file:
            header = read_netscape_cookies(self.config.cookie_file)
            cookies.extend(cookie_header_to_cookies(header))
        return _dedupe_cookies(cookies)

    # -- fetching ----------------------------------------------------------

    def fetch_text(
        self,
        url: str,
        *,
        timeout: Optional[float] = None,
        challenge_timeout: Optional[float] = None,
    ) -> str:
        page = self._new_page()
        try:
            try:
                response = page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=int((timeout or self.config.timeout) * 1000),
                )
            except Exception as error:
                # A challenge script may abort the initial navigation and
                # replace it with its own; read whatever is on the page now.
                if "ERR_ABORTED" not in str(error):
                    raise
                response = None
            html = self._settle(
                page,
                timeout=challenge_timeout or self.config.challenge_timeout,
            )
            if not html.strip():
                raise ChallengeError(
                    "浏览器没有拿到页面内容（可能仍在验证/跳转）。"
                    " 用 --headed 看实际页面，或换代理/--cdp-url。"
                )
            status = response.status if response is not None else 200
            headers = response.headers if response is not None else {}
            state = classify_page(status, headers, html)
            if state == "blocked":
                raise BlockedError(self._blocked_message())
            if state == "challenge":
                raise ChallengeError(self._challenge_message(challenge_timeout))
            return html
        finally:
            self._close_page(page)

    def resolve_download(self, url: str, *, timeout: Optional[float] = None) -> str:
        """Open an HLTV download page and capture the CDN URL it redirects to."""

        page = self._new_page()
        seen: list[str] = []

        def on_response(response: Any) -> None:
            try:
                candidate = response.url
            except Exception:
                return
            if is_cdn_url(candidate):
                seen.append(candidate)

        def on_download(download: Any) -> None:
            try:
                if is_cdn_url(download.url):
                    seen.append(download.url)
            except Exception:
                pass
            # The real file is streamed by download_file()/curl later; cancel
            # the browser copy so we do not download the same demo twice.
            try:
                download.cancel()
            except Exception:
                pass

        page.on("response", on_response)
        page.on("download", on_download)
        try:
            try:
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=int((timeout or self.config.timeout) * 1000),
                )
            except Exception as error:
                # A navigation that starts a download is reported as
                # net::ERR_ABORTED by Playwright; that is expected.
                if "ERR_ABORTED" not in str(error):
                    raise

            deadline = time.monotonic() + max(10.0, self.config.challenge_timeout)
            while time.monotonic() < deadline:
                for candidate in [page.url, *seen]:
                    if is_cdn_url(candidate):
                        return candidate
                try:
                    html = page.content()
                except Exception:
                    html = ""
                candidate = extract_cdn_url(html)
                if candidate:
                    return candidate
                state = classify_page(200, {}, html)
                if state == "blocked":
                    raise BlockedError(self._blocked_message())
                if state == "challenge":
                    self._try_click_challenge(page)
                page.wait_for_timeout(700)
            raise ChallengeError(
                "浏览器打开了 demo 页，但没有捕获到 CDN 下载地址。"
                " 可能是需要登录（请用 --cookie 或 --cdp-url 接管已登录浏览器），"
                "或页面结构已变化。"
            )
        finally:
            self._close_page(page)

    def download(
        self, url: str, destination: Path, *, timeout: Optional[float] = None
    ) -> DownloadResult:
        """Download through the browser as a last-resort fallback.

        The CDN can challenge even for a direct ``.rar`` URL.  A real browser
        clears that challenge, but the download only starts after the Turnstile
        button is clicked, so this method cannot simply block on
        ``expect_download``: it polls, nudges the challenge, then saves the file.
        """

        page = self._new_page()
        destination.parent.mkdir(parents=True, exist_ok=True)
        downloads: list[Any] = []

        def on_download(download: Any) -> None:
            downloads.append(download)

        page.on("download", on_download)
        try:
            try:
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=int((timeout or self.config.timeout) * 1000),
                )
            except Exception as error:
                if "ERR_ABORTED" not in str(error):
                    raise

            deadline = time.monotonic() + max(
                10.0, self.config.challenge_timeout + 10.0
            )
            while time.monotonic() < deadline:
                if downloads:
                    break
                try:
                    html = page.content()
                    title = page.title()
                except Exception:
                    html, title = "", ""
                state = classify_page(200, {}, f"{title}\n{html}")
                if state == "blocked":
                    raise BlockedError(self._blocked_message())
                if state == "challenge":
                    self._try_click_challenge(page)
                else:
                    # A normal landing page may expose a direct download link.
                    try:
                        link = page.locator("a").first
                        if link.count():
                            link.click(timeout=800, no_wait_after=True)
                    except Exception:
                        pass
                page.wait_for_timeout(1000)

            if not downloads:
                raise DownloadError(
                    url,
                    "浏览器等待下载超时（可能是验证未通过，或需要登录）。"
                    " 用 --headed 手动完成验证，或 --cdp-url 接管已登录 Chrome。",
                )
            download = downloads[0]
            try:
                download.save_as(str(destination))
            except Exception as error:
                raise DownloadError(url, f"浏览器保存文件失败: {error}") from error
            if not destination.exists() or destination.stat().st_size == 0:
                raise DownloadError(url, "浏览器下载完成但文件为空")
            if _looks_like_html(destination):
                snippet = destination.read_text("utf-8", errors="replace")[:200]
                destination.unlink(missing_ok=True)
                raise DownloadError(url, f"浏览器返回了 HTML: {snippet}")
            return DownloadResult(
                path=destination,
                url=url,
                final_url=getattr(download, "url", url) or url,
                size=destination.stat().st_size,
                content_type="",
            )
        finally:
            self._close_page(page)

    # -- helpers -----------------------------------------------------------

    def _new_page(self) -> Any:
        self.start()
        if self._context is None:
            raise BrowserUnavailable("浏览器上下文未初始化")
        page = self._context.new_page()
        try:
            page.add_init_script(_JS_STEALTH)
        except Exception:
            pass
        return page

    def _close_page(self, page: Any) -> None:
        try:
            page.close()
        except Exception:
            pass

    def _settle(self, page: Any, *, timeout: float) -> str:
        deadline = time.monotonic() + max(1.0, timeout)
        html = ""
        while time.monotonic() < deadline:
            try:
                html = page.content()
                title = page.title()
            except Exception:
                # The page may be mid-navigation; retry instead of treating an
                # empty string as a successful fetch.
                try:
                    page.wait_for_timeout(500)
                except Exception:
                    break
                continue
            combined = f"{title}\n{html}"
            state = classify_page(200, {}, combined)
            if state == "blocked":
                raise BlockedError(self._blocked_message())
            if state != "challenge":
                return html
            self._try_click_challenge(page)
            try:
                page.wait_for_timeout(1000)
            except Exception:
                break
        return html

    def _try_click_challenge(self, page: Any) -> None:
        """Best-effort click on a Turnstile checkbox/iframe.

        Cloudflare often resolves the managed challenge by itself; clicking is
        only a nudge.  Click at most once every few seconds: hammering the
        widget can make the challenge harder to pass.
        """

        now = time.monotonic()
        if now - self._last_click < 2.5:
            return
        self._last_click = now
        try:
            for frame in page.frames:
                if "challenges.cloudflare.com" not in (frame.url or ""):
                    continue
                box = frame.locator("body").bounding_box()
                if box:
                    page.mouse.click(box["x"] + 28, box["y"] + 28)
                    return
        except Exception:
            pass
        try:
            checkbox = page.locator("input[type=checkbox]").first
            if checkbox.count():
                checkbox.click(timeout=800, force=True)
        except Exception:
            pass

    def cookies(self) -> list[dict[str, Any]]:
        if self._context is None:
            return []
        try:
            cookies = [_cookie_dict(cookie) for cookie in self._context.cookies()]
        except Exception:
            return []
        return [cookie for cookie in cookies if _is_hltv_cookie(cookie)]

    def _blocked_message(self) -> str:
        return (
            "HLTV 对当前出口 IP 返回了硬封锁（Cloudflare 403），"
            "这不是可自动点掉的验证页。TLS 指纹/无头浏览器都救不了硬封锁："
            "请换干净出口（--proxy http://127.0.0.1:7890，最好是住宅代理），"
            "或在本机能打开 HLTV 的 Chrome 里用 --cdp-url http://127.0.0.1:9222 复用会话。"
        )

    def _challenge_message(self, challenge_timeout: Optional[float]) -> str:
        return (
            f"Cloudflare 验证在 {challenge_timeout or self.config.challenge_timeout:.0f}s 内未通过。"
            " 用 --headed 观察浏览器实际页面；如果出现人机验证请手动点一下；"
            " 或在已登录 Chrome 上用 --cdp-url 接管；换代理通常也能解决。"
        )

    def close(self) -> None:
        if self._owns_context and self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
        self._context = None
        if self._owns_browser and self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
        self._browser = None
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._pw = None


# ---------------------------------------------------------------------------
# HTTP + orchestration layer
# ---------------------------------------------------------------------------


class HltvFetcher:
    """A reusable HLTV session with cookie persistence and browser fallback."""

    def __init__(
        self,
        config: Optional[StealthConfig] = None,
        *,
        log: Optional[Callable[[str], None]] = None,
    ):
        self.config = config or StealthConfig()
        self._log_fn = log
        self._http: Any = None
        self._http_impl = ""  # curl_cffi | stdlib | none
        self._browser: Optional[BrowserSession] = None
        self._resolved: dict[str, str] = {}
        self._cookies: list[dict[str, Any]] = []
        self._last_request = 0.0
        self._last_percent = -10
        self._closed = False
        # Once HLTV hard-blocks this egress IP, do not spend 40s per demo
        # launching a browser again; page discovery can fall back to Jina.
        self._hltv_blocked = False
        self._load_cookies()
        atexit.register(self.close)

    # -- public API --------------------------------------------------------

    @property
    def engine(self) -> str:
        return self.config.resolved_engine()

    def get_text(
        self,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        allow_browser: bool = True,
    ) -> str:
        engine = self.engine
        if self._hltv_blocked:
            raise BlockedError(self._blocked_message())
        if engine in ("browser", "cdp"):
            try:
                html = self._browser_text(url)
            except BlockedError:
                self._hltv_blocked = True
                raise
            self._sync_browser_cookies()
            return html

        try:
            status, response_headers, text = self._http_text(url, headers=headers)
        except HttpError as error:
            state = classify_page(error.status, {}, error.body)
            if state == "blocked":
                self._hltv_blocked = True
                raise BlockedError(self._blocked_message()) from error
            if state == "challenge" and allow_browser and engine != "curl":
                return self._challenge_with_browser(url)
            raise

        state = classify_page(status, response_headers, text)
        if state == "blocked":
            self._hltv_blocked = True
            raise BlockedError(self._blocked_message())
        if state == "rate_limited":
            raise StealthError(
                "HLTV 返回 429（请求太快）。加大 --delay（例如 --delay 10）后重试。"
            )
        if state == "challenge":
            if engine == "curl":
                raise ChallengeError(
                    "HLTV 返回 Cloudflare 验证页；--engine curl 不允许浏览器兜底。"
                    " 去掉 --engine curl，或提供 --cookie/--cdp-url。"
                )
            if not allow_browser:
                raise ChallengeError(
                    "HLTV 返回 Cloudflare 验证页，且当前调用禁止浏览器兜底。"
                )
            return self._challenge_with_browser(url)
        return text

    def resolve_demo_url(self, demo_id: str) -> str:
        """Resolve ``/download/demo/<id>`` into the direct CDN URL."""

        demo_id = str(demo_id).strip()
        if demo_id in self._resolved:
            return self._resolved[demo_id]
        if self._hltv_blocked:
            raise BlockedError(self._blocked_message())

        url = f"{HLTV_BASE}/download/demo/{demo_id}"
        engine = self.engine

        if engine in ("browser", "cdp"):
            try:
                cdn = self._browser_resolve(url)
            except BlockedError:
                self._hltv_blocked = True
                raise
            self._resolved[demo_id] = cdn
            self._sync_browser_cookies()
            return cdn

        try:
            cdn = self._http_resolve(url)
        except BlockedError:
            self._hltv_blocked = True
            # A hard IP block is not solved by a fresh headless browser on the
            # same egress either.  Only the explicit browser / CDP modes get a
            # chance; auto goes straight to the Jina + open-CDN fallback.
            if engine not in ("browser", "cdp"):
                raise
            cdn = self._browser_resolve(url)
            self._hltv_blocked = False
            self._sync_browser_cookies()
        except ChallengeError:
            if engine == "curl":
                raise
            try:
                cdn = self._browser_resolve(url)
            except BlockedError:
                self._hltv_blocked = True
                raise
            self._sync_browser_cookies()
        except Exception as error:
            if engine == "curl" or self._http_impl == "none":
                raise StealthError(f"无法解析 HLTV demo {demo_id}: {error}") from error
            try:
                cdn = self._browser_resolve(url)
                self._sync_browser_cookies()
            except Exception as browser_error:
                raise StealthError(
                    f"无法解析 HLTV demo {demo_id}: {error}; 浏览器兜底也失败: {browser_error}"
                ) from browser_error
        self._resolved[demo_id] = cdn
        return cdn

    def download_demo(
        self,
        demo_id: str,
        *,
        destination: Path | str,
        timeout: Optional[float] = None,
        force: bool = False,
    ) -> DownloadResult:
        cdn = self.resolve_demo_url(demo_id)
        return self.download_file(
            cdn,
            destination,
            headers={"Referer": HLTV_BASE + "/"},
            timeout=timeout,
            force=force,
        )

    def download_file(
        self,
        url: str,
        destination: Path | str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
        force: bool = False,
    ) -> DownloadResult:
        dest = Path(destination).expanduser()
        effective_timeout = timeout or self.config.timeout
        self._ensure_http()
        if self._http_impl == "curl":
            try:
                return self._download_with_curl_cffi(
                    url, dest, headers=headers, timeout=effective_timeout, force=force
                )
            except DownloadError as error:
                # The CDN can itself show a Cloudflare challenge.  A real
                # browser that has cleared it is the last resort.
                if self.engine in ("auto", "browser", "cdp"):
                    self._log(f"HTTP 下载失败，改用浏览器: {error}")
                    return self._download_via_browser(
                        url, dest, timeout=effective_timeout
                    )
                raise
        if self._http_impl == "stdlib":
            try:
                return download_file(
                    url,
                    dest,
                    headers=headers,
                    cookie=self.cookie_header(),
                    proxy=self.config.proxy,
                    timeout=effective_timeout,
                    retries=self.config.retries,
                    resume=not force,
                )
            except DownloadError as error:
                if self.engine in ("auto", "browser", "cdp"):
                    self._log(f"HTTP 下载失败，改用浏览器: {error}")
                    return self._download_via_browser(
                        url, dest, timeout=effective_timeout
                    )
                raise
        return self._download_via_browser(url, dest, timeout=effective_timeout)

    def _download_via_browser(
        self, url: str, destination: Path, *, timeout: float
    ) -> DownloadResult:
        browser = self._ensure_browser()
        try:
            result = browser.download(url, destination, timeout=timeout)
        except DownloadError:
            # Keep the cf_clearance minted by the headless attempt; the headed
            # retry can then skip most of the challenge.
            self._sync_browser_cookies()
            if (
                self.config.headless
                and self.config.auto_headed
                and self.engine != "cdp"
            ):
                self._log(
                    "无头 Chrome 没下载成功，改用有头 Chrome 重试（会短暂弹出窗口）…"
                )
                self._restart_browser(headless=False)
                result = self._browser.download(url, destination, timeout=timeout)  # type: ignore[union-attr]
            else:
                raise
        self._sync_browser_cookies()
        return result

    def _restart_browser(self, *, headless: bool) -> None:
        if self._browser is not None:
            self._browser.close()
        config = replace(self.config, headless=headless, auto_headed=False)
        self._browser = BrowserSession(config, log=self._log)
        self._browser.start()

    def cookie_header(self) -> str:
        pairs = []
        for cookie in self._cookies:
            if not cookie.get("name"):
                continue
            pairs.append(f"{cookie['name']}={cookie['value']}")
        return "; ".join(pairs)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._http is not None:
            try:
                self._http.close()
            except Exception:
                pass
            self._http = None
        self._save_cookies()

    def __enter__(self) -> "HltvFetcher":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- HTTP layer --------------------------------------------------------

    def _load_cookies(self) -> None:
        state = load_state(self.config.resolved_state_file())
        cookies: list[dict[str, Any]] = [
            _cookie_dict(cookie)
            for cookie in state.get("cookies", [])
            if isinstance(cookie, Mapping)
        ]
        if self.config.cookie:
            cookies.extend(cookie_header_to_cookies(self.config.cookie))
        if self.config.cookie_file:
            try:
                header = read_netscape_cookies(self.config.cookie_file)
            except OSError as error:
                raise StealthError(f"读取 Cookie 文件失败: {error}") from error
            cookies.extend(cookie_header_to_cookies(header))
        self._cookies = _dedupe_cookies(cookies)

    def _ensure_http(self) -> None:
        if self._http_impl:
            return
        if self.engine == "stdlib":
            self._http_impl = "stdlib"
            return
        try:
            from curl_cffi import requests as curl_requests
        except ImportError:
            if self.engine == "curl":
                raise StealthError(
                    "curl_cffi 未安装。安装："
                    "`uv pip install --python learn/.venv/bin/python curl_cffi`。"
                )
            self._http_impl = "stdlib"
            if not self.config.quiet:
                self._log(
                    "未安装 curl_cffi，已降级为标准库/curl（Cloudflare 验证会更容易触发）。"
                    " 建议安装：uv pip install --python learn/.venv/bin/python curl_cffi"
                )
            return

        kwargs: dict[str, Any] = {
            "impersonate": self.config.impersonate or "chrome",
            "timeout": self.config.timeout,
        }
        if self.config.proxy:
            kwargs["proxy"] = self.config.proxy
        self._http = curl_requests.Session(**kwargs)
        self._http_impl = "curl"
        self._apply_http_cookies()

    def _apply_http_cookies(self) -> None:
        if self._http is None:
            return
        for cookie in self._cookies:
            try:
                self._http.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie.get("domain") or ".hltv.org",
                    path=cookie.get("path") or "/",
                    secure=bool(cookie.get("secure", False)),
                )
            except Exception:
                continue

    def _http_text(
        self,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
    ) -> tuple[int, dict[str, str], str]:
        self._ensure_http()
        self._throttle()
        if self._http_impl == "stdlib":
            text = request_text(
                url,
                headers=dict(headers or {}),
                cookie=self.cookie_header() or None,
                proxy=self.config.proxy,
                timeout=self.config.timeout,
                retries=self.config.retries,
            )
            return 200, {}, text

        last_error: Optional[Exception] = None
        for attempt in range(self.config.retries + 1):
            try:
                response = self._http.get(
                    url,
                    headers=self._request_headers(url, headers),
                    allow_redirects=True,
                    timeout=self.config.timeout,
                )
            except Exception as error:
                last_error = error
                if attempt < self.config.retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise StealthError(f"请求失败 {url}: {error}") from error

            status = int(response.status_code or 0)
            text = response.text or ""
            response_headers = {
                str(key).lower(): str(value) for key, value in response.headers.items()
            }
            try:
                response.close()
            except Exception:
                pass
            state = classify_page(status, response_headers, text)
            if (
                status in (408, 425, 500, 502, 503, 504)
                and attempt < self.config.retries
            ):
                time.sleep(1.5 * (attempt + 1))
                continue
            if state == "rate_limited" and attempt < self.config.retries:
                time.sleep(3.0 * (attempt + 1))
                continue
            return status, response_headers, text
        raise StealthError(f"请求失败 {url}: {last_error}")

    def _http_resolve(self, url: str) -> str:
        self._ensure_http()
        if self._http_impl == "stdlib":
            raise ChallengeError("标准库模式无法可靠解析 HLTV demo 重定向")

        self._throttle()
        response = self._http.get(
            url,
            headers={"Referer": HLTV_BASE + "/"},
            allow_redirects=False,
            timeout=self.config.timeout,
        )
        status = int(response.status_code or 0)
        headers = {
            str(key).lower(): str(value) for key, value in response.headers.items()
        }
        location = headers.get("location") or ""
        text = response.text or ""
        try:
            response.close()
        except Exception:
            pass

        if status in (301, 302, 303, 307, 308) and location:
            candidate = urljoin(url, location)
            if is_cdn_url(candidate):
                return candidate
            # A redirect to the challenge platform: let the browser handle it.
            lowered_location = candidate.lower()
            if "cloudflare" in lowered_location or "challenge" in lowered_location:
                raise ChallengeError("HLTV demo 页被重定向到 Cloudflare 验证")
            if "login" in lowered_location or "signin" in lowered_location:
                raise ChallengeError(
                    "HLTV demo 页要求登录。请用 --cookie-file 导入浏览器 Cookie，"
                    "或 --cdp-url 接管已登录的 Chrome。"
                )

        state = classify_page(status, headers, text)
        if status == 200:
            candidate = extract_cdn_url(text)
            if candidate:
                return candidate
            if state == "blocked":
                raise BlockedError(self._blocked_message())
            if state == "challenge":
                raise ChallengeError("HLTV demo 页返回 Cloudflare 验证")
            if "login" in text.lower() and "demo" in text.lower():
                raise ChallengeError(
                    "HLTV demo 页要求登录。请用 --cookie-file 导入浏览器 Cookie，"
                    "或 --cdp-url 接管已登录的 Chrome。"
                )

        if state == "blocked":
            raise BlockedError(self._blocked_message())
        if state == "challenge":
            raise ChallengeError("HLTV demo 页返回 Cloudflare 验证")
        if status in (401, 403):
            raise BlockedError(self._blocked_message())
        raise StealthError(f"HLTV demo 页没有给出 CDN 地址（HTTP {status}）")

    # -- browser layer -----------------------------------------------------

    def _ensure_browser(self) -> BrowserSession:
        if self._browser is None:
            self._browser = BrowserSession(self.config, log=self._log)
            self._browser.start()
        return self._browser

    def _browser_text(self, url: str) -> str:
        browser = self._ensure_browser()
        html = browser.fetch_text(
            url,
            timeout=self.config.timeout,
            challenge_timeout=self.config.challenge_timeout,
        )
        self._sync_browser_cookies()
        return html

    def _browser_resolve(self, url: str) -> str:
        browser = self._ensure_browser()
        return browser.resolve_download(url, timeout=self.config.timeout)

    def _challenge_with_browser(self, url: str) -> str:
        self._log("检测到 Cloudflare 验证，启动真实 Chrome 兜底…")
        try:
            html = self._browser_text(url)
        except BlockedError:
            self._hltv_blocked = True
            raise
        state = classify_page(200, {}, html)
        if state == "blocked":
            self._hltv_blocked = True
            raise BlockedError(self._blocked_message())
        if state == "challenge":
            raise ChallengeError(
                "真实 Chrome 也没有通过 Cloudflare 验证。"
                " 用 --headed 观察页面并手动完成验证，或 --cdp-url 接管已通过验证的 Chrome。"
            )
        return html

    def _sync_browser_cookies(self) -> None:
        if self._browser is None:
            return
        cookies = self._browser.cookies()
        if not cookies:
            return
        self._cookies = _dedupe_cookies([*self._cookies, *cookies])
        if self._http is not None:
            self._apply_http_cookies()
        self._save_cookies()

    def _save_cookies(self) -> None:
        if not self._cookies:
            return
        try:
            save_state(self.config.resolved_state_file(), self._cookies)
        except OSError as error:
            if not self.config.quiet:
                self._log(f"! 无法写入 Cookie 缓存: {error}")

    # -- download ----------------------------------------------------------

    def _download_with_curl_cffi(
        self,
        url: str,
        destination: Path,
        *,
        headers: Optional[Mapping[str, str]] = None,
        timeout: float,
        force: bool,
    ) -> DownloadResult:
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_name(destination.name + ".part")
        if force:
            part.unlink(missing_ok=True)
        offset = part.stat().st_size if part.exists() and not force else 0

        request_headers = dict(headers or {})
        if offset:
            request_headers["Range"] = f"bytes={offset}-"

        try:
            response = self._http.get(
                url,
                headers=request_headers,
                allow_redirects=True,
                stream=True,
                timeout=timeout,
            )
        except Exception as error:
            raise DownloadError(url, f"连接失败: {error}") from error

        status = int(response.status_code or 0)
        final_url = getattr(response, "url", url) or url
        content_type = response.headers.get("content-type", "")
        try:
            if status == 416 and offset:
                # The partial file is already complete.
                response.close()
                part.replace(destination)
                return DownloadResult(
                    path=destination,
                    url=url,
                    final_url=final_url,
                    size=destination.stat().st_size,
                    content_type=content_type,
                )
            if status not in (200, 206):
                body = response.text[:200] if status < 400 else ""
                response.close()
                if status in (401, 403):
                    host = urlsplit(url).netloc.lower()
                    if host in ("hltv.org", "www.hltv.org"):
                        raise BlockedError(self._blocked_message())
                    raise DownloadError(url, f"CDN 拒绝下载（HTTP {status}）")
                raise DownloadError(url, f"HTTP {status}: {body}")

            mode = "ab" if offset and status == 206 else "wb"
            if mode == "wb" and offset:
                offset = 0
            total = 0
            try:
                total = int(response.headers.get("content-length") or 0)
            except ValueError:
                total = 0
            written = offset
            with part.open(mode) as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    written += len(chunk)
                    self._progress(url, written, offset + total if total else 0)
        finally:
            try:
                response.close()
            except Exception:
                pass

        if not part.exists() or part.stat().st_size == 0:
            raise DownloadError(url, "下载完成但文件为空")
        if _looks_like_html(part):
            snippet = part.read_text("utf-8", errors="replace")[:200].replace("\n", " ")
            part.unlink(missing_ok=True)
            raise DownloadError(url, f"服务器返回了 HTML 而不是 demo: {snippet}")
        part.replace(destination)
        self._progress_done()
        return DownloadResult(
            path=destination,
            url=url,
            final_url=final_url,
            size=destination.stat().st_size,
            content_type=content_type,
        )

    # -- logging / helpers -------------------------------------------------

    def _request_headers(
        self, url: str, headers: Optional[Mapping[str, str]] = None
    ) -> dict[str, str]:
        merged = {str(key): str(value) for key, value in (headers or {}).items()}
        if "hltv.org" in url and not any(key.lower() == "referer" for key in merged):
            merged["Referer"] = HLTV_BASE + "/"
        return merged

    def _throttle(self) -> None:
        delay = max(0.0, float(self.config.request_delay or 0))
        if delay and self._last_request:
            remaining = delay - (time.monotonic() - self._last_request)
            if remaining > 0:
                time.sleep(remaining + random.uniform(0, 0.2))
        self._last_request = time.monotonic()

    def _progress(self, url: str, written: int, total: int) -> None:
        if self.config.quiet or not sys.stderr.isatty():
            return
        if total:
            percent = min(100, int(written * 100 / total))
            if percent // 10 == self._last_percent // 10:
                return
            self._last_percent = percent
            print(
                f"\r    ↓ {percent:3d}%  {written / 1024 / 1024:.1f}/{total / 1024 / 1024:.1f} MiB",
                end="",
                file=sys.stderr,
                flush=True,
            )
        elif self._last_percent < 0:
            self._last_percent = 0
            print(
                f"\r    ↓ {written / 1024 / 1024:.1f} MiB",
                end="",
                file=sys.stderr,
                flush=True,
            )

    def _progress_done(self) -> None:
        if self.config.quiet or not sys.stderr.isatty():
            return
        if self._last_percent >= 0:
            print(file=sys.stderr)
            self._last_percent = -10

    def _log(self, message: str) -> None:
        if self._log_fn is not None:
            self._log_fn(message)
        elif not self.config.quiet:
            print(message, flush=True)

    def _blocked_message(self) -> str:
        return (
            "HLTV 对当前出口 IP 返回了硬封锁（Cloudflare 403）。"
            " 这不是能靠 TLS 指纹或换浏览器解决的验证页：请换干净出口"
            "（--proxy，最好住宅代理），或在可正常打开 HLTV 的 Chrome 上用 "
            "--cdp-url http://127.0.0.1:9222 复用会话。"
        )


def create_fetcher(config: Optional[StealthConfig] = None) -> HltvFetcher:
    return HltvFetcher(config)


# Keep ``environment`` importable on platforms where ``platform`` is useful for
# diagnostics without pulling in psutil.
def describe_environment() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "state_file": str(default_state_path()),
    }
