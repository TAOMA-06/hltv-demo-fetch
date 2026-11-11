"""HTTP helpers for hltv-demo-fetch.

The module deliberately keeps the dependency surface small:

* JSON/API calls use the Python standard library (``urllib``).
* File downloads prefer ``curl`` when it is available because it handles large
  files, redirects, proxies, cookies, and resume far better than urllib.
* ``urllib`` is used as a portable fallback.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from .util import read_netscape_cookies

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
    # Asking for identity avoids having to decompress API responses manually.
    "Accept-Encoding": "identity",
}


class HttpError(RuntimeError):
    """An HTTP request failed."""

    def __init__(self, url: str, status: int, message: str, body: str = "") -> None:
        super().__init__(f"HTTP {status} for {url}: {message}")
        self.url = url
        self.status = status
        self.body = body


class DownloadError(RuntimeError):
    """A demo/archive download failed."""

    def __init__(self, url: str, message: str, *, stderr: str = "") -> None:
        super().__init__(f"下载失败 {url}: {message}")
        self.url = url
        self.stderr = stderr


@dataclass
class DownloadResult:
    path: Path
    url: str
    final_url: str
    size: int
    content_type: str


def _build_opener(proxy: Optional[str]) -> Any:
    handlers = []
    if proxy:
        handlers.append(ProxyHandler({"http": proxy, "https": proxy}))
    return build_opener(*handlers)


def _request_headers(
    headers: Optional[Mapping[str, str]] = None,
    cookie: Optional[str] = None,
    cookie_file: Optional[str] = None,
) -> dict[str, str]:
    merged = dict(DEFAULT_HEADERS)
    if headers:
        merged.update({str(key): str(value) for key, value in headers.items()})
    parts: list[str] = []
    if cookie:
        parts.append(cookie.strip())
    if cookie_file:
        try:
            from_file = read_netscape_cookies(cookie_file)
        except OSError:
            from_file = ""
        if from_file:
            parts.append(from_file)
    if parts:
        merged["Cookie"] = "; ".join(parts)
    return merged


def request_text(
    url: str,
    *,
    headers: Optional[Mapping[str, str]] = None,
    cookie: Optional[str] = None,
    cookie_file: Optional[str] = None,
    proxy: Optional[str] = None,
    timeout: float = 30.0,
    retries: int = 2,
    retry_delay: float = 2.0,
) -> str:
    """GET *url* and return decoded text."""

    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        request = Request(
            url, headers=_request_headers(headers, cookie, cookie_file), method="GET"
        )
        try:
            with _build_opener(proxy).open(request, timeout=timeout) as response:
                raw = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, errors="replace")
        except HTTPError as error:
            body = error.read().decode("utf-8", errors="replace") if error.fp else ""
            last_error = HttpError(url, error.code, error.reason or "", body)
            if error.code in (408, 425, 429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(retry_delay * (attempt + 1))
                continue
            raise last_error from error
        except (URLError, TimeoutError, OSError) as error:
            last_error = error
            # Some Python/OpenSSL builds fail against certain CDNs while curl's
            # TLS stack works.  Try curl once before giving up.
            if curl_available():
                try:
                    return curl_get_text(
                        url,
                        headers=headers,
                        cookie=cookie,
                        cookie_file=cookie_file,
                        proxy=proxy,
                        timeout=timeout,
                    )
                except Exception:
                    pass
            if attempt < retries:
                time.sleep(retry_delay * (attempt + 1))
                continue
            raise HttpError(url, 0, str(error)) from error
    raise HttpError(url, 0, str(last_error))


def request_json(
    url: str,
    *,
    headers: Optional[Mapping[str, str]] = None,
    cookie: Optional[str] = None,
    cookie_file: Optional[str] = None,
    proxy: Optional[str] = None,
    timeout: float = 30.0,
    retries: int = 2,
) -> Any:
    text = request_text(
        url,
        headers={"Accept": "application/json", **(headers or {})},
        cookie=cookie,
        cookie_file=cookie_file,
        proxy=proxy,
        timeout=timeout,
        retries=retries,
    )
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        snippet = text.strip().replace("\n", " ")[:200]
        raise HttpError(url, 200, f"响应不是 JSON: {snippet}", text) from error


def post_json(
    url: str,
    payload: Any,
    *,
    headers: Optional[Mapping[str, str]] = None,
    cookie: Optional[str] = None,
    cookie_file: Optional[str] = None,
    proxy: Optional[str] = None,
    timeout: float = 30.0,
    retries: int = 2,
) -> Any:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        merged = _request_headers(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
                **(headers or {}),
            },
            cookie,
            cookie_file,
        )
        request = Request(url, data=body, headers=merged, method="POST")
        try:
            with _build_opener(proxy).open(request, timeout=timeout) as response:
                text = response.read().decode(
                    response.headers.get_content_charset() or "utf-8",
                    errors="replace",
                )
                return json.loads(text)
        except HTTPError as error:
            error_body = (
                error.read().decode("utf-8", errors="replace") if error.fp else ""
            )
            last_error = HttpError(url, error.code, error.reason or "", error_body)
            if error.code in (408, 425, 429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise last_error from error
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            last_error = error
            if attempt < retries:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise HttpError(url, 0, str(error)) from error
    raise HttpError(url, 0, str(last_error))


def curl_available() -> bool:
    return shutil.which("curl") is not None


def curl_get_text(
    url: str,
    *,
    headers: Optional[Mapping[str, str]] = None,
    cookie: Optional[str] = None,
    cookie_file: Optional[str] = None,
    proxy: Optional[str] = None,
    timeout: float = 60.0,
) -> str:
    """GET text through curl (useful for hosts where urllib's TLS fails)."""

    if not curl_available():
        return request_text(
            url,
            headers=headers,
            cookie=cookie,
            proxy=proxy,
            timeout=timeout,
        )
    args = [
        "curl",
        "-sS",
        "-L",
        "--connect-timeout",
        str(min(max(timeout, 5.0), 60.0)),
        "--max-time",
        str(max(int(timeout), 30)),
        "-A",
        DEFAULT_UA,
        *_header_args(headers),
        *_cookie_args(cookie, cookie_file),
    ]
    if proxy:
        args.extend(["-x", proxy])
    args.append(url)
    try:
        completed = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout + 30
        )
    except subprocess.TimeoutExpired as error:
        raise HttpError(url, 0, f"curl 超时 ({timeout}s)") from error
    if completed.returncode != 0:
        raise HttpError(
            url,
            0,
            f"curl 退出码 {completed.returncode}: {(completed.stderr or '').strip()[-300:]}",
        )
    return completed.stdout


def _cookie_args(cookie: Optional[str], cookie_file: Optional[str]) -> list[str]:
    args: list[str] = []
    if cookie:
        # curl accepts a raw "a=b; c=d" string here.
        args.extend(["-b", cookie])
    if cookie_file:
        args.extend(["-b", cookie_file])
    return args


def _header_args(headers: Optional[Mapping[str, str]]) -> list[str]:
    args: list[str] = []
    for key, value in (headers or {}).items():
        args.extend(["-H", f"{key}: {value}"])
    return args


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
    )


def download_file(
    url: str,
    dest: Path | str,
    *,
    headers: Optional[Mapping[str, str]] = None,
    cookie: Optional[str] = None,
    cookie_file: Optional[str] = None,
    proxy: Optional[str] = None,
    timeout: float = 180.0,
    retries: int = 3,
    resume: bool = True,
) -> DownloadResult:
    """Download *url* to *dest* and return metadata.

    ``curl`` is used when present.  If it is not, urllib is used (without resume).
    Cloudflare-protected hosts such as HLTV may return 403; pass ``proxy`` or a
    browser cookie to work around that.
    """

    destination = Path(dest).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_size > 0 and not resume:
        destination.unlink()

    if curl_available():
        return _download_with_curl(
            url,
            destination,
            headers=headers,
            cookie=cookie,
            cookie_file=cookie_file,
            proxy=proxy,
            timeout=timeout,
            retries=retries,
            resume=resume,
        )
    return _download_with_urllib(
        url,
        destination,
        headers=headers,
        cookie=cookie,
        cookie_file=cookie_file,
        proxy=proxy,
        timeout=timeout,
        retries=retries,
        resume=resume,
    )


def _download_with_curl(
    url: str,
    destination: Path,
    *,
    headers: Optional[Mapping[str, str]],
    cookie: Optional[str],
    cookie_file: Optional[str],
    proxy: Optional[str],
    timeout: float,
    retries: int,
    resume: bool,
) -> DownloadResult:
    base_args = [
        "curl",
        "-sSfL",
        "--retry",
        str(max(0, retries)),
        "--retry-delay",
        "2",
        "--connect-timeout",
        str(min(max(timeout, 5.0), 60.0)),
        "--max-time",
        str(max(int(timeout), 30)),
        "-A",
        DEFAULT_UA,
        "-o",
        str(destination),
        "-w",
        "%{http_code}\t%{size_download}\t%{url_effective}\t%{content_type}",
        *_header_args(headers),
        *_cookie_args(cookie, cookie_file),
    ]
    if proxy:
        base_args.extend(["-x", proxy])
    if resume and destination.exists() and destination.stat().st_size > 0:
        base_args.extend(["-C", "-"])
    base_args.append(url)

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args, capture_output=True, text=True, timeout=timeout + 60
        )

    try:
        completed = run(base_args)
        # Some servers do not support Range requests.  Retry once from scratch.
        if completed.returncode != 0 and "-C" in base_args:
            if destination.exists():
                destination.unlink()
            retry_args = [arg for arg in base_args if arg != "-C" and arg != "-"]
            completed = run(retry_args)
    except subprocess.TimeoutExpired as error:
        raise DownloadError(url, f"curl 超时 ({timeout}s)") from error

    stdout = (completed.stdout or "").strip().split("\t")
    stderr = (completed.stderr or "").strip()
    if completed.returncode != 0:
        raise DownloadError(
            url,
            f"curl 退出码 {completed.returncode}: {stderr[-500:]}",
            stderr=stderr,
        )
    if len(stdout) >= 4:
        status_text, size_text, final_url, content_type = (
            stdout[0],
            stdout[1],
            stdout[2],
            stdout[3],
        )
    elif len(stdout) == 3:
        status_text, size_text, final_url = stdout
        content_type = ""
    else:
        status_text, _size_text, final_url, content_type = "", "", url, ""
    try:
        status = int(status_text) if status_text else 0
    except ValueError:
        status = 0
    if status and not (200 <= status < 300):
        raise DownloadError(url, f"HTTP {status}", stderr=stderr)
    if not destination.exists() or destination.stat().st_size == 0:
        raise DownloadError(url, "下载完成但文件为空", stderr=stderr)
    if _looks_like_html(destination):
        snippet = destination.read_text("utf-8", errors="replace")[:200].replace(
            "\n", " "
        )
        destination.unlink(missing_ok=True)
        raise DownloadError(
            url, f"服务器返回了 HTML 而不是文件: {snippet}", stderr=stderr
        )
    return DownloadResult(
        path=destination,
        url=url,
        final_url=final_url or url,
        size=destination.stat().st_size,
        content_type=content_type,
    )


def _download_with_urllib(
    url: str,
    destination: Path,
    *,
    headers: Optional[Mapping[str, str]],
    cookie: Optional[str],
    cookie_file: Optional[str],
    proxy: Optional[str],
    timeout: float,
    retries: int,
    resume: bool,
) -> DownloadResult:
    part = destination.with_name(destination.name + ".part")
    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        offset = part.stat().st_size if resume and part.exists() else 0
        request_headers = _request_headers(headers, cookie, cookie_file)
        if offset:
            request_headers["Range"] = f"bytes={offset}-"
        request = Request(url, headers=request_headers, method="GET")
        try:
            with _build_opener(proxy).open(request, timeout=timeout) as response:
                mode = "ab" if offset and response.status == 206 else "wb"
                with part.open(mode) as handle:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
            part.replace(destination)
            if _looks_like_html(destination):
                destination.unlink(missing_ok=True)
                raise DownloadError(url, "服务器返回了 HTML 而不是文件")
            return DownloadResult(
                path=destination,
                url=url,
                final_url=response.geturl() if "response" in locals() else url,
                size=destination.stat().st_size,
                content_type=response.headers.get_content_type()
                if "response" in locals()
                else "",
            )
        except HTTPError as error:
            last_error = DownloadError(url, f"HTTP {error.code}")
            if error.code in (408, 425, 429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise last_error from error
        except (URLError, TimeoutError, OSError) as error:
            last_error = error
            if attempt < retries:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise DownloadError(url, str(error)) from error
    raise DownloadError(url, str(last_error))


def resolve_final_url(
    url: str,
    *,
    headers: Optional[Mapping[str, str]] = None,
    cookie: Optional[str] = None,
    cookie_file: Optional[str] = None,
    proxy: Optional[str] = None,
    timeout: float = 30.0,
) -> str:
    """Follow redirects and return the effective URL (without saving the body)."""

    if curl_available():
        common = [
            "curl",
            "-sS",
            "-L",
            "--connect-timeout",
            str(min(max(timeout, 5.0), 60.0)),
            "--max-time",
            str(max(int(timeout), 30)),
            "-A",
            DEFAULT_UA,
            *_header_args(headers),
            *_cookie_args(cookie, cookie_file),
        ]
        if proxy:
            common.extend(["-x", proxy])
        # Prefer HEAD: it avoids downloading a large body just to learn the
        # final URL.  Some CDNs reject HEAD, so fall back to a one-byte range.
        completed: Optional[subprocess.CompletedProcess[str]] = None
        for extra in (["-I"], ["-o", os.devnull, "-r", "0-0"]):
            args = [*common, *extra, "-w", "%{url_effective}", url]
            completed = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout + 30
            )
            effective = (completed.stdout or "").strip()
            if completed.returncode == 0 and effective:
                return effective
        raise DownloadError(
            url,
            f"解析重定向失败: {((completed.stderr if completed else '') or '').strip()[-300:]}",
        )
    request = Request(
        url, headers=_request_headers(headers, cookie, cookie_file), method="HEAD"
    )
    try:
        with _build_opener(proxy).open(request, timeout=timeout) as response:
            return response.geturl()
    except (HTTPError, URLError) as error:
        raise DownloadError(url, str(error)) from error
