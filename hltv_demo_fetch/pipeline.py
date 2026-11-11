"""Download orchestration: fetch -> extract -> checksum -> manifest."""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlsplit

from .archive import extract_demos
from .manifest import Manifest, demo_key
from .net import DownloadError, download_file
from .util import (
    display_path,
    ensure_dir,
    human_bytes,
    now_iso,
    redact_url,
    safe_name,
)

DEMO_EXTENSIONS = {
    ".dem",
    ".dem.gz",
    ".dem.bz2",
    ".dem.zst",
    ".dem.xz",
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".tar.gz",
    ".tar.xz",
    ".tar.bz2",
    ".tar.zst",
    ".tgz",
}


@dataclass
class Job:
    """One thing to download (or one local file/archive to import)."""

    source: str
    match_id: str = ""
    demo_id: str = ""
    url: str = ""
    local_path: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    map_name: str = ""
    date: str = ""
    account: str = ""
    suggested_name: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    fallback_url_resolver: Optional[Callable[[], str]] = None
    # Sources with a stateful session (HLTV's anti-bot transport) can provide
    # their own downloader instead of the generic curl/urllib path.
    downloader: Optional[Callable[..., Any]] = None
    # Called once after the whole batch finishes (closes browsers/sessions).
    closer: Optional[Callable[[], None]] = None

    @property
    def key(self) -> str:
        return demo_key(self.source, self.match_id, self.demo_id or self.local_path)

    @property
    def filename_base(self) -> str:
        if self.suggested_name:
            return safe_name(self.suggested_name, max_len=110)
        parts = [self.source, self.match_id or self.demo_id, self.map_name, self.date]
        return safe_name("_".join(part for part in parts if part), max_len=110)


@dataclass
class Summary:
    planned: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    bytes: int = 0
    demos: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "planned": self.planned,
            "downloaded": self.downloaded,
            "skipped": self.skipped,
            "failed": self.failed,
            "bytes": self.bytes,
            "bytes_human": human_bytes(self.bytes),
            "demos": self.demos,
            "failures": self.failures,
        }


def _url_suffix(url: str) -> str:
    if not url:
        return ".bin"
    path = urlsplit(url).path.lower()
    for suffix in (
        ".dem.gz",
        ".dem.bz2",
        ".dem.zst",
        ".dem.xz",
        ".tar.gz",
        ".tar.xz",
        ".tar.bz2",
        ".tar.zst",
        ".dem",
        ".zip",
        ".rar",
        ".7z",
        ".gz",
        ".bz2",
        ".zst",
        ".xz",
        ".tar",
        ".tgz",
    ):
        if path.endswith(suffix):
            return "." + suffix.lstrip(".")
    return ".bin"


def _log(quiet: bool, message: str) -> None:
    if not quiet:
        print(message, flush=True)


def close_jobs(jobs: Iterable[Job]) -> None:
    """Close shared source sessions once the batch is done."""

    _close_job_sessions(jobs)


def _close_job_sessions(jobs: Iterable[Job]) -> None:
    """Close shared source sessions once the batch is done."""

    seen: set[int] = set()
    for job in jobs:
        closer = getattr(job, "closer", None)
        if closer is None:
            continue
        key = id(getattr(closer, "__self__", closer))
        if key in seen:
            continue
        seen.add(key)
        try:
            closer()
        except Exception:
            pass


def safe_job_url(job: Job) -> str:
    """Redact signed query tokens before printing a URL."""

    return redact_url(job.url)


def _download_job(
    job: Job,
    *,
    destination: Path,
    cookie: Optional[str],
    cookie_file: Optional[str],
    proxy: Optional[str],
    timeout: float,
    retries: int,
    force: bool,
    quiet: bool,
) -> Any:
    try:
        if job.downloader is not None:
            return job.downloader(
                destination=destination,
                timeout=timeout,
                force=force,
            )
        return download_file(
            job.url,
            destination,
            headers=job.headers,
            cookie=cookie,
            cookie_file=cookie_file,
            proxy=proxy,
            timeout=timeout,
            retries=retries,
            resume=not force,
        )
    except DownloadError as first_error:
        if job.fallback_url_resolver is None:
            raise
        _log(quiet, f"    ! 直连失败，尝试解析真实下载地址: {first_error}")
        fallback_url = job.fallback_url_resolver()
        if not fallback_url or fallback_url == job.url:
            raise
        destination.unlink(missing_ok=True)
        _log(quiet, f"    ~ 改从 CDN 下载: {redact_url(fallback_url)}")
        return download_file(
            fallback_url,
            destination,
            headers=job.headers,
            cookie=cookie,
            cookie_file=cookie_file,
            proxy=proxy,
            timeout=timeout,
            retries=retries,
            resume=False,
        )


def run_jobs(
    jobs: Iterable[Job],
    *,
    out_dir: Path | str,
    manifest: Manifest,
    dry_run: bool = False,
    force: bool = False,
    keep_archive: bool = False,
    cookie: Optional[str] = None,
    cookie_file: Optional[str] = None,
    proxy: Optional[str] = None,
    timeout: float = 180.0,
    retries: int = 3,
    delay: float = 1.0,
    fail_fast: bool = False,
    quiet: bool = False,
) -> Summary:
    output_dir = ensure_dir(out_dir)
    temp_dir = ensure_dir(output_dir / ".tmp")
    summary = Summary()
    jobs = list(jobs)

    for index, job in enumerate(jobs, start=1):
        summary.planned += 1
        key = job.key
        prefix = f"[{index}/{len(jobs)}] {job.source} {job.match_id or job.demo_id}"
        if not force and manifest.has_key(key):
            summary.skipped += 1
            _log(quiet, f"{prefix} -> 已拉取过，跳过")
            continue
        if dry_run:
            _log(
                quiet,
                f"{prefix} -> {safe_job_url(job) or job.local_path}"
                f"  ({job.map_name or '?'} {job.date or ''})".rstrip(),
            )
            continue

        _log(
            quiet,
            f"{prefix} -> {job.map_name or '?'} {job.date or ''} "
            f"{safe_job_url(job) if job.url else job.local_path}".rstrip(),
        )

        local_path = Path(job.local_path).expanduser() if job.local_path else None
        archive_path: Optional[Path] = None
        downloaded = None
        try:
            if local_path is not None:
                if not local_path.exists():
                    raise FileNotFoundError(f"文件不存在: {local_path}")
                archive_path = local_path
            else:
                if not job.url:
                    raise ValueError("Job 没有 url 或 local_path")
                suffix = _url_suffix(job.url)
                archive_path = temp_dir / f"{safe_name(key, max_len=80)}{suffix}"
                if force:
                    archive_path.unlink(missing_ok=True)
                downloaded = _download_job(
                    job,
                    destination=archive_path,
                    cookie=cookie,
                    cookie_file=cookie_file,
                    proxy=proxy,
                    timeout=timeout,
                    retries=retries,
                    force=force,
                    quiet=quiet,
                )
                _log(
                    quiet,
                    f"    + {human_bytes(downloaded.size)}  {display_path(archive_path, output_dir)}",
                )
                if keep_archive:
                    keep_path = output_dir / f"{job.filename_base}{suffix}"
                    if keep_path.exists() and force:
                        keep_path.unlink()
                    if keep_path.resolve() != archive_path.resolve():
                        shutil.move(str(archive_path), str(keep_path))
                    archive_path = keep_path

            extracted = extract_demos(
                archive_path,
                output_dir,
                job.filename_base,
                force=force,
                keep_source=keep_archive or local_path is not None,
            )

            for demo in extracted:
                if not force and manifest.has_hash(demo.sha256):
                    if demo.created:
                        demo.path.unlink(missing_ok=True)
                    summary.skipped += 1
                    _log(
                        quiet,
                        f"    = 内容重复，跳过: {display_path(demo.path, output_dir)}",
                    )
                    continue
                record = {
                    "key": key,
                    "source": job.source,
                    "match_id": job.match_id,
                    "demo_id": job.demo_id,
                    "account": job.account,
                    "map": job.map_name,
                    "date": job.date,
                    "path": display_path(demo.path, output_dir),
                    "bytes": demo.size,
                    "sha256": demo.sha256,
                    "url": redact_url(downloaded.final_url if downloaded else ""),
                    "original_url": safe_job_url(job),
                    "downloaded_at": now_iso(),
                    "meta": job.meta,
                }
                manifest.add(record)
                summary.downloaded += 1
                summary.demos += 1
                summary.bytes += demo.size
                _log(
                    quiet,
                    f"    ✓ {display_path(demo.path, output_dir)}  {human_bytes(demo.size)}",
                )
        except Exception as error:  # noqa: BLE001 - one bad demo must not kill the batch
            message = f"{type(error).__name__}: {error}"
            summary.failed += 1
            failure = {
                "key": key,
                "source": job.source,
                "match_id": job.match_id,
                "demo_id": job.demo_id,
                "url": safe_job_url(job),
                "error": message,
                "recorded_at": now_iso(),
                "meta": job.meta,
            }
            summary.failures.append(failure)
            manifest.add_failure(failure)
            _log(quiet, f"    ✗ {message}")
            if fail_fast:
                _close_job_sessions(jobs)
                raise
        finally:
            if (
                archive_path
                and archive_path.exists()
                and not keep_archive
                and local_path is None
            ):
                archive_path.unlink(missing_ok=True)

        if delay > 0 and index < len(jobs):
            time.sleep(delay)

    _close_job_sessions(jobs)
    return summary


def iter_local_sources(
    paths: Iterable[Path | str], *, recursive: bool = True
) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"路径不存在: {path}")
        candidates: Iterable[Path]
        if path.is_dir():
            candidates = path.rglob("*") if recursive else path.glob("*")
        else:
            candidates = [path]
        for candidate in candidates:
            if not candidate.is_file():
                continue
            lowered = candidate.name.lower()
            if not lowered.endswith(tuple(DEMO_EXTENSIONS)):
                continue
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append(candidate)
    return sorted(found)


def local_jobs(
    paths: Iterable[Path | str], *, recursive: bool = True, source: str = "import"
) -> list[Job]:
    jobs: list[Job] = []
    for path in iter_local_sources(paths, recursive=recursive):
        stem = path.name
        for suffix in (
            ".dem.gz",
            ".dem.bz2",
            ".dem.zst",
            ".dem.xz",
            ".dem",
            ".tar.gz",
            ".tar.xz",
            ".tar.bz2",
            ".tar.zst",
            ".tgz",
            ".zip",
            ".rar",
            ".7z",
            ".tar",
        ):
            if stem.lower().endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        jobs.append(
            Job(
                source=source,
                match_id=safe_name(stem, max_len=80),
                local_path=str(path),
                suggested_name=safe_name(f"{source}_{stem}", max_len=100),
                meta={"imported_from": str(path)},
            )
        )
    return jobs
