"""Command line interface for the standalone HLTV demo fetcher."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from . import __version__
from . import hltv
from .errors import SourceError, StealthError
from .manifest import Manifest
from .pipeline import Job, close_jobs, run_jobs, safe_job_url
from .stealth import env_bool
from .util import ensure_dir, read_netscape_cookies

DEFAULT_OUT = "demos"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hltv-demo-fetch",
        description=(
            "Download CS2 GOTV demos from HLTV with a modern Cloudflare-resistant "
            "transport (curl_cffi TLS impersonation + real Chrome fallback)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  hltv-demo-fetch --limit 10 --since 2026-10-01\n"
            "  hltv-demo-fetch --match-url https://www.hltv.org/matches/2399724/...\n"
            "  hltv-demo-fetch --demo-id 113158 --engine browser --headed\n"
            "  hltv-demo-fetch --engine cdp --cdp-url http://127.0.0.1:9222\n"
            "  hltv-demo-fetch --limit 5 --proxy http://127.0.0.1:7890\n"
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"hltv-demo-fetch {__version__}"
    )
    parser.add_argument("--out", default=DEFAULT_OUT, help="输出目录（默认 ./demos）")
    parser.add_argument(
        "--limit", type=int, default=10, help="最多下载多少个 demo（默认 10）"
    )
    parser.add_argument("--dry-run", action="store_true", help="只列出计划，不下载")
    parser.add_argument("--force", action="store_true", help="覆盖已存在/已记录的文件")
    parser.add_argument("--keep-archive", action="store_true", help="保留下载的压缩包")

    source = parser.add_argument_group("HLTV 来源")
    source.add_argument(
        "--match-url", action="append", default=[], help="HLTV 比赛页 URL（可重复）"
    )
    source.add_argument(
        "--event-url", action="append", default=[], help="HLTV 事件页 URL（可重复）"
    )
    source.add_argument(
        "--demo-id", action="append", default=[], help="HLTV demo ID（可重复）"
    )
    source.add_argument(
        "--pages", type=int, default=1, help="最近结果页数（默认 1，每页约 100 场）"
    )
    source.add_argument(
        "--stars", type=int, default=None, help="只要 N 星及以上比赛，如 5"
    )
    source.add_argument("--since", help="最早比赛日期，如 2026-10-01")

    network = parser.add_argument_group("网络 / 反爬")
    network.add_argument(
        "--engine",
        choices=["auto", "curl", "browser", "cdp", "stdlib"],
        default=os.environ.get("HLTV_ENGINE", "auto"),
        help=(
            "auto=curl_cffi 优先、验证时升级真实 Chrome；curl=只用 TLS 指纹；"
            "browser=直接用 Chrome；cdp=接管已登录 Chrome（需 --cdp-url）；"
            "stdlib=旧实现（默认 auto）"
        ),
    )
    network.add_argument(
        "--impersonate",
        default=os.environ.get("HLTV_IMPERSONATE", "chrome"),
        help="curl_cffi 指纹，默认 chrome（可填 chrome131/firefox133 等）",
    )
    network.add_argument(
        "--browser-channel",
        default=os.environ.get("HLTV_BROWSER_CHANNEL", "auto"),
        help="Chrome 通道：auto/chrome/msedge/chromium（默认 auto，优先系统 Chrome）",
    )
    network.add_argument(
        "--cdp-url",
        default=os.environ.get("HLTV_CDP_URL"),
        help=("接管已打开的 Chrome，例如 http://127.0.0.1:9222；设置后默认 engine=cdp"),
    )
    network.add_argument(
        "--state-file",
        default=os.environ.get("HLTV_STATE_FILE"),
        help="Cookie 缓存文件（默认 ~/Library/Caches/hltv-demo-fetch/hltv-state.json）",
    )
    headless = network.add_mutually_exclusive_group()
    headless.add_argument(
        "--headed",
        dest="headless",
        action="store_false",
        help="显示浏览器窗口（遇到顽固验证时更容易过）",
    )
    headless.add_argument(
        "--headless",
        dest="headless",
        action="store_true",
        help="无头浏览器（默认；云端/无桌面环境用）",
    )
    network.set_defaults(headless=env_bool("HLTV_HEADLESS", True))
    network.add_argument(
        "--no-headed-fallback",
        action="store_true",
        help="无头 Chrome 下载失败时不自动弹出有头窗口重试",
    )
    network.add_argument(
        "--challenge-timeout",
        type=float,
        default=float(os.environ.get("HLTV_CHALLENGE_TIMEOUT", "40")),
        help="等待 Cloudflare 验证完成的秒数（默认 40）",
    )
    network.add_argument(
        "--no-jina", action="store_true", help="不通过 r.jina.ai 兜底抓页面"
    )
    network.add_argument("--proxy", help="HTTP(S) 代理，例如 http://127.0.0.1:7890")
    network.add_argument("--cookie", help="Cookie 字符串（HLTV 登录态）")
    network.add_argument(
        "--cookie-file", help="Netscape 格式 Cookie 文件（浏览器导出）"
    )

    runtime = parser.add_argument_group("执行")
    runtime.add_argument(
        "--timeout", type=float, default=180.0, help="单文件下载超时秒数（默认 180）"
    )
    runtime.add_argument(
        "--api-timeout", type=float, default=45.0, help="页面请求超时秒数（默认 45）"
    )
    runtime.add_argument(
        "--delay", type=float, default=5.0, help="demo 之间间隔秒数（默认 5，别调太小）"
    )
    runtime.add_argument(
        "--fail-fast", action="store_true", help="遇到第一个错误就停止"
    )
    runtime.add_argument("--quiet", action="store_true", help="减少输出")
    runtime.add_argument("--json", action="store_true", help="只输出 JSON 摘要")
    return parser


def _job_dict(job: Job) -> dict[str, Any]:
    return {
        "source": job.source,
        "match_id": job.match_id,
        "demo_id": job.demo_id,
        "map": job.map_name,
        "date": job.date,
        "url": safe_job_url(job),
    }


def _summary_dict(summary: Any) -> dict[str, Any]:
    return {
        "planned": summary.planned,
        "downloaded": summary.downloaded,
        "skipped": summary.skipped,
        "failed": summary.failed,
        "bytes": summary.bytes,
        "demos": summary.demos,
        "failures": summary.failures,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.cdp_url and args.engine == "auto":
        args.engine = "cdp"
    if args.engine == "cdp" and not args.cdp_url:
        parser.error("--engine cdp 需要同时提供 --cdp-url http://127.0.0.1:9222")

    # Make --cookie-file useful for both page requests and curl downloads.
    if args.cookie_file and not args.cookie:
        try:
            args.cookie = read_netscape_cookies(args.cookie_file)
        except FileNotFoundError as error:
            print(f"错误: {error}", file=sys.stderr)
            return 2

    quiet = bool(args.quiet or args.json)
    out_dir = ensure_dir(Path(args.out).expanduser())

    try:
        jobs = hltv.jobs(
            match_urls=args.match_url,
            event_urls=args.event_url,
            demo_ids=args.demo_id,
            limit=args.limit,
            pages=args.pages,
            stars=args.stars,
            since=args.since,
            use_jina=not args.no_jina,
            cookie=args.cookie,
            cookie_file=args.cookie_file,
            proxy=args.proxy,
            timeout=args.api_timeout,
            quiet=quiet,
            engine=args.engine,
            impersonate=args.impersonate,
            browser_channel=args.browser_channel,
            cdp_url=args.cdp_url,
            state_file=args.state_file,
            headless=args.headless,
            auto_headed=not args.no_headed_fallback,
            challenge_timeout=args.challenge_timeout,
        )
    except (SourceError, StealthError, ValueError, FileNotFoundError) as error:
        print(f"错误: {error}", file=sys.stderr)
        return 2

    if not jobs:
        if args.json:
            print(
                json.dumps(
                    {
                        "planned": 0,
                        "downloaded": 0,
                        "skipped": 0,
                        "failed": 0,
                        "demos": 0,
                        "message": "没有可下载的 demo",
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(
                "没有可下载的 demo（可能都已过期、没有 demo，或过滤条件太严）。",
                file=sys.stderr,
            )
        return 0

    if args.dry_run:
        if args.json:
            print(
                json.dumps(
                    {"planned": len(jobs), "jobs": [_job_dict(job) for job in jobs]},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"计划下载 {len(jobs)} 个 demo -> {out_dir}")
            for index, job in enumerate(jobs, start=1):
                print(
                    f"  {index:3d}. {job.map_name or '?':12s} "
                    f"{job.date or '?':10s} "
                    f"{safe_job_url(job) if job.url else job.local_path}"
                )
        close_jobs(jobs)
        return 0

    manifest = Manifest(out_dir)
    try:
        summary = run_jobs(
            jobs,
            out_dir=out_dir,
            manifest=manifest,
            force=args.force,
            keep_archive=args.keep_archive,
            cookie=args.cookie,
            cookie_file=args.cookie_file,
            proxy=args.proxy,
            timeout=args.timeout,
            delay=args.delay,
            fail_fast=args.fail_fast,
            quiet=quiet,
        )
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    except Exception as error:  # --fail-fast re-raises the first job failure
        print(f"错误: {error}", file=sys.stderr)
        return 1
    finally:
        close_jobs(jobs)

    if args.json:
        print(json.dumps(_summary_dict(summary), ensure_ascii=False, indent=2))
    else:
        print(
            f"\n完成: 新增 {summary.downloaded}，跳过 {summary.skipped}，"
            f"失败 {summary.failed}，合计 {summary.bytes} 字节"
        )
        print(f"清单: {manifest.path}")
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
