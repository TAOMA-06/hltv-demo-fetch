"""HLTV source: professional match demos.

HLTV sits behind Cloudflare.  Page discovery and demo downloads therefore use
the layered transport in :mod:`hltv_demo_fetch.stealth`:

1. ``curl_cffi`` Chrome TLS/JA3 + HTTP/2 fingerprint (fast path);
2. a real Chrome via Playwright/patchright for managed challenges;
3. ``--cdp-url`` to reuse an already verified / logged-in Chrome;
4. ``r.jina.ai`` as a page-discovery fallback when the local IP is hard-blocked.

The public match page exposes ``/download/demo/<id>``, which usually redirects
to ``r2-demos.hltv.org``.  That CDN is normally open, but can also show a
challenge; the browser fallback clears it before streaming the archive.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Iterable, Optional


from .errors import SourceError
from .net import (
    DownloadError,
    HttpError,
    curl_get_text,
    download_file,
    request_text,
    resolve_final_url,
)
from .pipeline import Job
from .stealth import HltvFetcher, StealthConfig, create_fetcher, is_cdn_url
from .util import parse_date_argument, parse_human_date

HLTV_BASE = "https://www.hltv.org"
JINA_PREFIX = "https://r.jina.ai/"
MATCH_RE = re.compile(r"https?://(?:www\.)?hltv\.org/matches/(\d+)/([A-Za-z0-9-]+)")
REL_MATCH_RE = re.compile(r'(?:href=["\'])?/matches/(\d+)/([A-Za-z0-9-]+)')
DEMO_RE = re.compile(r"(?:hltv\.org)?/download/demo/(\d+)")
DATE_HEADING_RE = re.compile(r"Results for\s+(.+)", re.IGNORECASE)
JINA_CHALLENGE_MARKERS = (
    "just a moment",
    "performing security verification",
    "attention required",
    "enable javascript and cookies",
    "you have been blocked",
    "sorry, you have been blocked",
)


@dataclass
class HltvMatch:
    match_id: str
    slug: str
    url: str
    date: str = ""

    @property
    def teams(self) -> str:
        # The slug is the only reliable, server-rendered name on every page.
        # Team names can contain hyphens, so do not try to split "A-vs-B-event"
        # into exact teams; use the full slug for filenames instead.
        return self.slug


def _challenge(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in JINA_CHALLENGE_MARKERS)


def fetch_hltv_text(
    url: str,
    *,
    fetcher: Optional[HltvFetcher] = None,
    use_jina: bool = True,
    cookie: Optional[str] = None,
    cookie_file: Optional[str] = None,
    proxy: Optional[str] = None,
    timeout: float = 45.0,
    retries: int = 2,
) -> str:
    """Fetch an HLTV page, falling back to the Jina Reader proxy.

    When ``fetcher`` is provided it uses the modern anti-bot transport
    (curl_cffi TLS fingerprint + real Chrome challenge fallback).  Jina remains
    useful for *page discovery* even when the local IP is hard-blocked, because
    the actual demo archives live on the open ``r2-demos.hltv.org`` CDN.
    """

    direct_error: Optional[Exception] = None
    if fetcher is not None:
        try:
            text = fetcher.get_text(url)
            if text and not _challenge(text):
                return text
            direct_error = SourceError("HLTV 返回了 Cloudflare 验证页")
        except Exception as error:  # noqa: BLE001 - fall through to Jina
            direct_error = error
    else:
        try:
            text = request_text(
                url,
                cookie=cookie,
                cookie_file=cookie_file,
                proxy=proxy,
                timeout=timeout,
                retries=0,
            )
            if text and not _challenge(text):
                return text
            direct_error = SourceError("HLTV 返回了 Cloudflare 验证页")
        except HttpError as error:
            direct_error = error

    if not use_jina:
        detail = str(direct_error).rstrip("。.;； ")
        raise SourceError(
            f"HLTV 直连失败: {detail}。"
            " 可尝试 --proxy <http(s)://...>、--cookie/--cookie-file，"
            "或 --engine browser --headed / --cdp-url。"
        )

    last_error: Optional[Exception] = None
    for attempt in range(retries + 2):
        headers = {"X-Return-Format": "markdown"}
        if attempt:
            headers["X-No-Cache"] = "true"
        try:
            text = curl_get_text(
                JINA_PREFIX + url,
                headers=headers,
                proxy=proxy,
                timeout=max(timeout, 60.0),
            )
        except HttpError as error:
            last_error = error
            time.sleep(1.5 * (attempt + 1))
            continue
        if text and not _challenge(text):
            return text
        last_error = SourceError("Jina Reader 也返回了 Cloudflare 验证页")
        time.sleep(1.5 * (attempt + 1))
    detail = str(last_error or direct_error).rstrip("。.;； ")
    raise SourceError(
        f"HLTV 页面抓取失败: {detail}。"
        " 请检查网络；HLTV 下载还需要可用的代理或浏览器 cookie。"
    )


def parse_match_links(text: str) -> list[HltvMatch]:
    """Extract match links from HLTV results/event HTML or Jina markdown."""

    found: list[HltvMatch] = []
    seen: set[str] = set()
    for regex in (MATCH_RE, REL_MATCH_RE):
        for match in regex.finditer(text):
            match_id, slug = match.group(1), match.group(2)
            if match_id in seen:
                continue
            seen.add(match_id)
            found.append(
                HltvMatch(
                    match_id=match_id,
                    slug=slug,
                    url=f"{HLTV_BASE}/matches/{match_id}/{slug}",
                )
            )
    return found


def parse_results_page(text: str) -> list[HltvMatch]:
    """Parse a results page and attach the date heading to each match."""

    matches: list[HltvMatch] = []
    current_date = ""
    # Both the HTML and the Jina markdown contain the match links and date
    # headings in document order, so a single line scan is enough.
    #
    # Jina also keeps HLTV's sidebar ("latest matches" / forum widget), whose
    # links are not results.  Real result rows always carry a ``bo1``..``bo5``
    # marker, so use that as a cheap structural filter.
    for line in text.splitlines():
        heading = DATE_HEADING_RE.search(line)
        if heading:
            parsed = parse_human_date(heading.group(1))
            current_date = parsed.isoformat() if parsed else ""
        if not re.search(r"\bbo[1-5]\b", line, re.IGNORECASE):
            continue
        for match in parse_match_links(line):
            match.date = current_date
            matches.append(match)
    # Some markdown tables put multiple links on one line without a heading;
    # the regex above still handles them, but deduplicate the final list.
    deduped: list[HltvMatch] = []
    seen: set[str] = set()
    for match in matches:
        if match.match_id in seen:
            continue
        seen.add(match.match_id)
        deduped.append(match)
    # Featured results are rendered before the first "Results for <date>"
    # heading.  Give them the first real date on the page so --since can still
    # reason about them.
    first_date = next((match.date for match in deduped if match.date), "")
    if first_date:
        for match in deduped:
            if not match.date:
                match.date = first_date
    return deduped


def parse_demo_ids(text: str) -> list[str]:
    """Return unique demo ids from a match page (HTML or markdown)."""

    seen: set[str] = set()
    demo_ids: list[str] = []
    for match in DEMO_RE.finditer(text):
        demo_id = match.group(1)
        if demo_id not in seen:
            seen.add(demo_id)
            demo_ids.append(demo_id)
    return demo_ids


def demo_page_url(demo_id: str) -> str:
    return f"{HLTV_BASE}/download/demo/{demo_id}"


def extract_cdn_url(text: str) -> Optional[str]:
    """Pick the R2/demo URL out of a Jina reader response."""

    for candidate in re.findall(r"https?://[^\s'\"<>)\]]+", text or ""):
        if is_cdn_url(candidate):
            return candidate
    return None


def resolve_demo_url(
    demo_id: str,
    *,
    fetcher: Optional[HltvFetcher] = None,
    use_jina: bool = True,
    cookie: Optional[str] = None,
    cookie_file: Optional[str] = None,
    proxy: Optional[str] = None,
    timeout: float = 45.0,
) -> str:
    """Resolve ``/download/demo/<id>`` to the final CDN URL."""

    url = demo_page_url(demo_id)

    if fetcher is not None:
        try:
            return fetcher.resolve_demo_url(demo_id)
        except Exception as error:  # noqa: BLE001 - Jina is the last page resolver
            if not use_jina:
                raise SourceError(
                    f"无法解析 HLTV demo {demo_id} 的下载地址: {error}"
                ) from error

    if fetcher is None:
        try:
            final_url = resolve_final_url(
                url,
                cookie=cookie,
                cookie_file=cookie_file,
                proxy=proxy,
                timeout=timeout,
            )
            if is_cdn_url(final_url):
                return final_url
        except Exception:
            pass

    if not use_jina:
        raise SourceError(f"无法解析 HLTV demo {demo_id} 的下载地址")
    try:
        # The demo page redirect ends at the CDN, but the CDN itself may return
        # a Cloudflare block page.  Jina still reports the effective URL in its
        # "Final URL:" line, so accept the text even if it looks like a
        # challenge page.
        text = curl_get_text(
            JINA_PREFIX + url,
            headers={"X-Return-Format": "markdown", "X-No-Cache": "true"},
            proxy=proxy,
            timeout=max(timeout, 60.0),
        )
    except Exception as error:
        raise SourceError(f"无法解析 HLTV demo {demo_id}: {error}") from error
    candidate = extract_cdn_url(text)
    if candidate:
        return candidate
    raise SourceError(f"HLTV demo {demo_id} 页面没有找到 CDN 下载链接")


def _matches_from_event(event_url: str, **kwargs) -> list[HltvMatch]:
    text = fetch_hltv_text(event_url, **kwargs)
    matches = parse_match_links(text)
    if not matches:
        raise SourceError(f"事件页面没有找到比赛链接: {event_url}")
    return matches


def _make_downloader(
    fetcher: HltvFetcher,
    demo_id: str,
    *,
    use_jina: bool,
    cookie: Optional[str],
    cookie_file: Optional[str],
    proxy: Optional[str],
    timeout: float,
):
    """Download one demo, with an open-CDN + Jina fallback.

    The stealth path resolves ``/download/demo/<id>`` and streams the archive
    from ``r2-demos.hltv.org``.  If the local IP is hard-blocked, Jina can still
    reveal the CDN URL; the CDN itself is usually not challenged.
    """

    def download(*, destination, timeout, force=False):
        try:
            return fetcher.download_demo(
                demo_id,
                destination=destination,
                timeout=timeout,
                force=force,
            )
        except Exception as primary:  # noqa: BLE001 - try the last-resort path
            if not use_jina:
                raise
            try:
                cdn_url = resolve_demo_url(
                    demo_id,
                    fetcher=None,
                    use_jina=True,
                    cookie=cookie,
                    cookie_file=cookie_file,
                    proxy=proxy,
                    timeout=timeout,
                )
                # In auto/browser/cdp modes the CDN may challenge and need the
                # browser fallback.  For the explicit curl-only engine keep the
                # old system-curl path (a different TLS stack occasionally
                # succeeds where curl_cffi is blocked).
                if fetcher.engine in ("auto", "browser", "cdp"):
                    return fetcher.download_file(
                        cdn_url,
                        destination,
                        headers={"Referer": "https://www.hltv.org/"},
                        timeout=timeout,
                        force=force,
                    )
                return download_file(
                    cdn_url,
                    destination,
                    headers={"Referer": "https://www.hltv.org/"},
                    cookie=cookie,
                    cookie_file=cookie_file,
                    proxy=proxy,
                    timeout=timeout,
                    retries=3,
                    resume=not force,
                )
            except Exception as secondary:  # noqa: BLE001
                raise DownloadError(
                    demo_page_url(demo_id),
                    f"反爬下载失败: {primary}; Jina/CDN 兜底失败: {secondary}",
                ) from secondary

    return download


def _job_for_demo(
    demo_id: str,
    *,
    match_id: str = "",
    slug: str = "",
    date: str = "",
    meta: Optional[dict] = None,
    fetcher: Optional[HltvFetcher] = None,
    use_jina: bool = True,
    cookie: Optional[str] = None,
    cookie_file: Optional[str] = None,
    proxy: Optional[str] = None,
    timeout: float = 45.0,
) -> Job:
    job_meta = {"demo_id": demo_id, **(meta or {})}
    job = Job(
        source="hltv",
        match_id=match_id,
        demo_id=demo_id,
        url=demo_page_url(demo_id),
        date=date,
        headers={"Referer": "https://www.hltv.org/"},
        suggested_name=f"hltv_{demo_id}_{slug or match_id or 'demo'}",
        meta=job_meta,
        fallback_url_resolver=(
            lambda demo_id=demo_id: resolve_demo_url(
                demo_id,
                fetcher=fetcher,
                use_jina=use_jina,
                cookie=cookie,
                cookie_file=cookie_file,
                proxy=proxy,
                timeout=timeout,
            )
        ),
    )
    if fetcher is not None:
        job.downloader = _make_downloader(
            fetcher,
            demo_id,
            use_jina=use_jina,
            cookie=cookie,
            cookie_file=cookie_file,
            proxy=proxy,
            timeout=timeout,
        )
        job.closer = fetcher.close
    return job


def _jobs_for_match(
    match: HltvMatch,
    *,
    fetcher: Optional[HltvFetcher] = None,
    use_jina: bool,
    cookie: Optional[str],
    cookie_file: Optional[str],
    proxy: Optional[str],
    timeout: float,
) -> list[Job]:
    text = fetch_hltv_text(
        match.url,
        fetcher=fetcher,
        use_jina=use_jina,
        cookie=cookie,
        cookie_file=cookie_file,
        proxy=proxy,
        timeout=timeout,
    )
    demo_ids = parse_demo_ids(text)
    if not demo_ids:
        return []
    return [
        _job_for_demo(
            demo_id,
            match_id=match.match_id,
            slug=match.slug,
            date=match.date,
            meta={"match_url": match.url, "slug": match.slug},
            fetcher=fetcher,
            use_jina=use_jina,
            cookie=cookie,
            cookie_file=cookie_file,
            proxy=proxy,
            timeout=timeout,
        )
        for demo_id in demo_ids
    ]


def jobs(
    *,
    match_urls: Iterable[str] = (),
    event_urls: Iterable[str] = (),
    demo_ids: Iterable[str] = (),
    limit: Optional[int] = None,
    pages: int = 1,
    stars: Optional[int] = None,
    since: Optional[str] = None,
    use_jina: bool = True,
    cookie: Optional[str] = None,
    cookie_file: Optional[str] = None,
    proxy: Optional[str] = None,
    timeout: float = 45.0,
    quiet: bool = False,
    engine: str = "auto",
    impersonate: str = "chrome",
    browser_channel: str = "auto",
    cdp_url: Optional[str] = None,
    state_file: Optional[str] = None,
    headless: bool = True,
    auto_headed: bool = True,
    challenge_timeout: float = 40.0,
) -> list[Job]:
    """Build HLTV download jobs from explicit URLs or recent results."""

    since_date = parse_date_argument(since)
    jobs_out: list[Job] = []

    fetcher: Optional[HltvFetcher] = None
    if engine != "stdlib":
        fetcher = create_fetcher(
            StealthConfig(
                engine=engine,
                impersonate=impersonate,
                proxy=proxy,
                cookie=cookie,
                cookie_file=cookie_file,
                state_file=state_file,
                headless=headless,
                auto_headed=auto_headed,
                browser_channel=browser_channel,
                cdp_url=cdp_url,
                timeout=timeout,
                challenge_timeout=challenge_timeout,
                quiet=quiet,
            )
        )

    if demo_ids:
        for demo_id in demo_ids:
            demo_id = str(demo_id).strip()
            if not demo_id:
                continue
            jobs_out.append(
                _job_for_demo(
                    demo_id,
                    fetcher=fetcher,
                    use_jina=use_jina,
                    cookie=cookie,
                    cookie_file=cookie_file,
                    proxy=proxy,
                    timeout=timeout,
                )
            )
        if not jobs_out and fetcher is not None:
            fetcher.close()
        return jobs_out[: limit or None]

    matches: list[HltvMatch] = []
    for event_url in event_urls:
        matches.extend(
            _matches_from_event(
                event_url,
                fetcher=fetcher,
                use_jina=use_jina,
                cookie=cookie,
                cookie_file=cookie_file,
                proxy=proxy,
                timeout=timeout,
            )
        )
    for match_url in match_urls:
        parsed = parse_match_links(match_url)
        if parsed:
            matches.extend(parsed)
        else:
            # Accept a bare match id as a convenience.
            match_id = re.search(r"(\d{4,})", match_url)
            if match_id:
                mid = match_id.group(1)
                matches.append(
                    HltvMatch(
                        match_id=mid,
                        slug=f"match-{mid}",
                        url=match_url
                        if "://" in match_url
                        else f"{HLTV_BASE}/matches/{mid}",
                    )
                )

    if not matches:
        offset = 0
        page_size = 100
        for page in range(max(1, pages)):
            query = f"?content=demo&offset={offset}"
            if stars:
                query += f"&stars={stars}"
            page_url = f"{HLTV_BASE}/results{query}"
            try:
                text = fetch_hltv_text(
                    page_url,
                    fetcher=fetcher,
                    use_jina=use_jina,
                    cookie=cookie,
                    cookie_file=cookie_file,
                    proxy=proxy,
                    timeout=timeout,
                )
            except SourceError:
                # ``content=demo`` occasionally hits a stricter challenge.
                # Fall back to the normal results page; matches without a demo
                # are filtered later when their match page is fetched.
                text = fetch_hltv_text(
                    f"{HLTV_BASE}/results?offset={offset}",
                    fetcher=fetcher,
                    use_jina=use_jina,
                    cookie=cookie,
                    cookie_file=cookie_file,
                    proxy=proxy,
                    timeout=timeout,
                )
            page_matches = parse_results_page(text)
            if not page_matches:
                break
            if since_date:
                page_matches = [
                    match
                    for match in page_matches
                    if not match.date or parse_human_date(match.date) >= since_date
                ]
            matches.extend(page_matches)
            offset += page_size
            if limit and len(matches) >= limit:
                break

    # Deduplicate by match id, preserving order.
    deduped: list[HltvMatch] = []
    seen: set[str] = set()
    for match in matches:
        if match.match_id in seen:
            continue
        seen.add(match.match_id)
        deduped.append(match)
    # Do not truncate to `limit` matches: some matches have no demo, and one
    # match can yield several demos.  Check a bounded number of candidate
    # matches instead, stopping as soon as we have enough demos.
    if limit:
        deduped = deduped[: max(limit * 3, limit)]

    for match in deduped:
        if since_date and match.date and parse_human_date(match.date) < since_date:
            continue
        match_jobs = _jobs_for_match(
            match,
            fetcher=fetcher,
            use_jina=use_jina,
            cookie=cookie,
            cookie_file=cookie_file,
            proxy=proxy,
            timeout=timeout,
        )
        if not match_jobs and not quiet:
            print(f"  - HLTV {match.match_id} 没有 demo 链接，跳过", flush=True)
        jobs_out.extend(match_jobs)
        if limit and len(jobs_out) >= limit:
            break
        # Be polite to HLTV / the Jina reader between match pages.  Downloads
        # have their own delay in run_jobs.
        time.sleep(0.8)
    if not jobs_out and fetcher is not None:
        fetcher.close()
    return jobs_out
