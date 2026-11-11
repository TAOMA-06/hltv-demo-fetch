"""Demo archive extraction.

HLTV ships ``.rar``/``.zip`` archives that may contain one demo per map;
Renown ships single ``.dem.gz`` files; 5E can return ``.zip``/``.rar`` or a
plain ``.dem``.  This module normalises all of them into plain ``.dem`` files.
"""

from __future__ import annotations

import bz2
import gzip
import os
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .util import safe_name, sha256_file

DEMO_SUFFIX = ".dem"


class ArchiveError(RuntimeError):
    """The downloaded file could not be turned into a demo."""


@dataclass
class ExtractedDemo:
    path: Path
    original_name: str
    size: int
    sha256: str
    created: bool


def _dest_for(
    out_dir: Path, filename_base: str, *, multiple: bool, entry_name: str = ""
) -> Path:
    base = safe_name(filename_base, fallback="demo")
    if base.lower().endswith(".dem"):
        base = base[:-4]
    if multiple:
        entry_stem = Path(entry_name).with_suffix("").as_posix().replace("/", "_")
        suffix = safe_name(entry_stem or "map", max_len=60, fallback="map")
        return out_dir / f"{base}__{suffix}.dem"
    return out_dir / f"{base}.dem"


def _record(path: Path, original_name: str, created: bool) -> ExtractedDemo:
    return ExtractedDemo(
        path=path,
        original_name=original_name,
        size=path.stat().st_size,
        sha256=sha256_file(path),
        created=created,
    )


def _safe_member_path(root: Path, name: str) -> Path:
    target = (root / name).resolve()
    root_resolved = root.resolve()
    if root_resolved not in target.parents and target != root_resolved:
        raise ArchiveError(f"压缩包包含非法路径: {name}")
    return target


def _move_or_copy(source: Path, dest: Path, *, move: bool) -> bool:
    """Return True when *dest* was created by this call."""

    if dest.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    if move:
        shutil.move(str(source), str(dest))
    else:
        shutil.copy2(source, dest)
    return True


def _decompress_single(source: Path, dest: Path, *, move: bool) -> bool:
    if dest.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    lowered = source.name.lower()
    if lowered.endswith((".gz", ".dem.gz")):
        with gzip.open(source, "rb") as reader, dest.open("wb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
    elif lowered.endswith((".bz2", ".dem.bz2")):
        with bz2.open(source, "rb") as reader, dest.open("wb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
    elif lowered.endswith((".zst", ".dem.zst")):
        zstd = shutil.which("zstd")
        if not zstd:
            raise ArchiveError("需要 zstd 解压 .zst；macOS 可执行 `brew install zstd`")
        completed = subprocess.run(
            [zstd, "-d", "-q", "-f", "-o", str(dest), str(source)],
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            dest.unlink(missing_ok=True)
            raise ArchiveError(
                f"zstd 解压失败: {(completed.stderr or '').strip()[-300:]}"
            )
    elif lowered.endswith((".xz", ".dem.xz")):
        import lzma

        with lzma.open(source, "rb") as reader, dest.open("wb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
    else:
        raise ArchiveError(f"不支持的压缩格式: {source.name}")
    if move:
        source.unlink(missing_ok=True)
    return True


def _extract_zip(source: Path, temp_dir: Path) -> list[Path]:
    found: list[Path] = []
    with zipfile.ZipFile(source) as archive:
        for info in archive.infolist():
            if info.is_dir() or not info.filename.lower().endswith(DEMO_SUFFIX):
                continue
            target = _safe_member_path(temp_dir, info.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as reader, target.open("wb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
            found.append(target)
    return found


def _extract_tar(source: Path, temp_dir: Path) -> list[Path]:
    found: list[Path] = []
    with tarfile.open(source) as archive:
        for member in archive.getmembers():
            if not member.isfile() or not member.name.lower().endswith(DEMO_SUFFIX):
                continue
            target = _safe_member_path(temp_dir, member.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            reader = archive.extractfile(member)
            if reader is None:
                continue
            with reader, target.open("wb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
            found.append(target)
    return found


def _extract_tar_zst(source: Path, temp_dir: Path) -> list[Path]:
    """Decompress ``.tar.zst`` with the external zstd binary, then untar."""
    zstd = shutil.which("zstd")
    if not zstd:
        raise ArchiveError("需要 zstd 解压 .tar.zst；macOS 可执行 `brew install zstd`")
    tar_path = temp_dir / "_source.tar"
    completed = subprocess.run(
        [zstd, "-d", "-q", "-f", "-o", str(tar_path), str(source)],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ArchiveError(f"zstd 解压失败: {(completed.stderr or '').strip()[-300:]}")
    try:
        return _extract_tar(tar_path, temp_dir)
    finally:
        tar_path.unlink(missing_ok=True)


def _extract_with_external(source: Path, temp_dir: Path) -> list[Path]:
    """Extract RAR/7z with the first tool that actually works.

    HLTV ships RAR5 archives.  ``7z`` can list RAR5 but often cannot decompress
    it ("Unsupported Method"), while ``unar`` / ``unrar`` / ``bsdtar`` can.
    Try real RAR tools first, then 7z, and fall back to the next tool if one
    fails part-way through.
    """

    ext = source.suffix.lower()
    candidates: list[tuple[str, list[str]]] = []

    unar = shutil.which("unar")
    if unar:
        candidates.append(
            ("unar", [unar, "-q", "-f", "-o", str(temp_dir), str(source)])
        )
    unrar = shutil.which("unrar")
    if unrar:
        candidates.append(
            (
                "unrar",
                [unrar, "x", "-o+", "-y", str(source), str(temp_dir) + os.sep],
            )
        )
    bsdtar = shutil.which("bsdtar")
    if bsdtar:
        candidates.append(("bsdtar", [bsdtar, "-xf", str(source), "-C", str(temp_dir)]))
    seven_zip = shutil.which("7z") or shutil.which("7zz") or shutil.which("7za")
    if seven_zip:
        candidates.append(("7z", [seven_zip, "x", "-y", f"-o{temp_dir}", str(source)]))

    if not candidates:
        raise ArchiveError(
            f"解压 .{ext or '压缩包'} 需要 unar、unrar、bsdtar 或 7z；"
            "macOS 推荐 `brew install unar`（或 `brew install sevenzip`）"
        )

    errors: list[str] = []
    for label, command in candidates:
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=600
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            errors.append(f"{label}: {error}")
            continue
        found = [path for path in temp_dir.rglob(f"*{DEMO_SUFFIX}") if path.is_file()]
        if found:
            return found
        detail = (completed.stderr or completed.stdout or "").strip()[-300:]
        errors.append(f"{label}: {detail or f'退出码 {completed.returncode}'}")

    raise ArchiveError(
        "RAR/7z 解压失败（已依次尝试可用工具）: " + "; ".join(errors[-4:])
    )


def extract_demos(
    source: Path | str,
    out_dir: Path | str,
    filename_base: str,
    *,
    force: bool = False,
    keep_source: bool = False,
) -> list[ExtractedDemo]:
    """Extract every ``.dem`` from *source* into *out_dir*.

    ``keep_source=False`` removes the downloaded archive after a successful
    extraction.  Existing output files are left untouched unless *force* is set.
    """

    source_path = Path(source).expanduser()
    if not source_path.is_file():
        raise ArchiveError(f"文件不存在: {source_path}")
    output_dir = Path(out_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    lowered = source_path.name.lower()

    if lowered.endswith(DEMO_SUFFIX):
        if source_path.resolve().parent == output_dir.resolve():
            # Importing a demo that is already in the output directory should
            # record it in place instead of copying foo.dem -> import_foo.dem.
            return [_record(source_path, source_path.name, created=False)]
        dest = _dest_for(output_dir, filename_base, multiple=False)
        if dest.exists() and not force:
            return [_record(dest, source_path.name, created=False)]
        if dest.exists() and force:
            dest.unlink()
        created = _move_or_copy(source_path, dest, move=not keep_source)
        return [_record(dest, source_path.name, created=created)]

    if lowered.endswith((".gz", ".bz2", ".zst", ".xz")) and not lowered.endswith(
        (".tar.gz", ".tar.xz", ".tar.bz2", ".tar.zst", ".tgz")
    ):
        dest = _dest_for(output_dir, filename_base, multiple=False)
        if dest.exists() and not force:
            return [_record(dest, source_path.name, created=False)]
        if dest.exists() and force:
            dest.unlink()
        created = _decompress_single(source_path, dest, move=not keep_source)
        return [_record(dest, source_path.name, created=created)]

    found: list[Path] = []
    with tempfile.TemporaryDirectory(
        prefix="demo-fetch-", dir=str(output_dir)
    ) as temp_name:
        temp_dir = Path(temp_name)
        if lowered.endswith(".zip"):
            found = _extract_zip(source_path, temp_dir)
        elif lowered.endswith((".tar", ".tar.gz", ".tar.xz", ".tar.bz2", ".tgz")):
            found = _extract_tar(source_path, temp_dir)
        elif lowered.endswith(".tar.zst"):
            found = _extract_tar_zst(source_path, temp_dir)
        elif lowered.endswith((".rar", ".7z")):
            found = _extract_with_external(source_path, temp_dir)
        else:
            # Unknown extension: sniff it.  This also handles servers that omit
            # the archive suffix from Content-Type/URL.
            if zipfile.is_zipfile(source_path):
                found = _extract_zip(source_path, temp_dir)
            elif tarfile.is_tarfile(source_path):
                found = _extract_tar(source_path, temp_dir)
            else:
                found = _extract_with_external(source_path, temp_dir)

        if not found:
            raise ArchiveError(
                f"压缩包内没有 .dem: {source_path.name}（如果是 HTML/错误页，请检查下载链接是否过期）"
            )

        temp_root = temp_dir.resolve()
        demos: list[ExtractedDemo] = []
        multiple = len(found) > 1
        for demo in sorted(path.resolve() for path in found):
            dest = _dest_for(
                output_dir,
                filename_base,
                multiple=multiple,
                entry_name=demo.relative_to(temp_root).as_posix(),
            )
            if dest.exists() and not force:
                demos.append(_record(dest, demo.name, created=False))
                continue
            if dest.exists() and force:
                dest.unlink()
            created = _move_or_copy(demo, dest, move=True)
            demos.append(_record(dest, demo.name, created=created))

    if not keep_source:
        source_path.unlink(missing_ok=True)
    return demos
