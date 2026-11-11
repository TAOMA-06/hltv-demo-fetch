"""Small, dependency-free helpers used by hltv-demo-fetch."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_DATE_TEXT = re.compile(
    r"([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})",
    re.IGNORECASE,
)


def ensure_dir(path: Path | str) -> Path:
    """Create *path* (and parents) and return it as a Path."""

    directory = Path(path).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def safe_name(value: object, *, max_len: int = 100, fallback: str = "demo") -> str:
    """Return a filesystem-safe name that still works on macOS/Linux/Windows.

    Unicode (including Chinese) is preserved; only characters that are invalid on
    common filesystems are replaced.  The result never contains a path separator,
    so callers can safely join it to an output directory.
    """

    text = unicodedata.normalize("NFKC", str(value or ""))
    text = _INVALID_FILENAME.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    text = re.sub(r"_+", "_", text)
    if not text:
        text = fallback
    if text.upper() in _RESERVED_WINDOWS_NAMES:
        text = f"_{text}"
    if len(text) > max_len:
        text = text[:max_len].rstrip(" ._")
    return text or fallback


def strip_demo_suffixes(name: str) -> str:
    """``foo.dem.gz`` -> ``foo``; ``foo.rar`` -> ``foo``."""

    lowered = name.lower()
    for suffix in (
        ".dem.gz",
        ".dem.bz2",
        ".dem.zst",
        ".dem.xz",
        ".dem",
        ".gz",
        ".bz2",
        ".zst",
        ".zip",
        ".rar",
        ".7z",
        ".tar.gz",
        ".tgz",
        ".tar",
    ):
        if lowered.endswith(suffix):
            return name[: -len(suffix)]
    return name


def format_date(value: object, *, fmt: str = "%Y-%m-%d") -> str:
    """Best-effort formatting of an ISO date, unix timestamp, or date object."""

    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat() if fmt == "%Y-%m-%d" else value.strftime(fmt)
    if isinstance(value, date):
        return value.isoformat() if fmt == "%Y-%m-%d" else value.strftime(fmt)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).strftime(fmt)
    text = str(value).strip()
    if not text:
        return ""
    if re.fullmatch(r"\d{10,13}", text):
        seconds = int(text) / (1000 if len(text) == 13 else 1)
        return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime(fmt)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime(fmt)
    except ValueError:
        pass
    parsed = parse_human_date(text)
    return parsed.strftime(fmt) if parsed else ""


def parse_human_date(text: str) -> Optional[date]:
    """Parse ``1st of November 2026`` / ``November 1st 2026`` / ``2026-11-01``."""

    if not text:
        return None
    cleaned = text.strip()
    try:
        return date.fromisoformat(cleaned[:10])
    except ValueError:
        pass
    match = _DATE_TEXT.search(cleaned)
    if match:
        month_name, day_text, year_text = match.groups()
        month = _MONTHS.get(month_name.lower())
        if month:
            try:
                return date(int(year_text), month, int(day_text))
            except ValueError:
                return None
    # HLTV sometimes writes "1st of November 2026".
    match = re.search(
        r"(\d{1,2})(?:st|nd|rd|th)?\s+of\s+([A-Za-z]+)\s+(\d{4})",
        cleaned,
        re.IGNORECASE,
    )
    if match:
        day_text, month_name, year_text = match.groups()
        month = _MONTHS.get(month_name.lower())
        if month:
            try:
                return date(int(year_text), month, int(day_text))
            except ValueError:
                return None
    return None


def parse_date_argument(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    parsed = parse_human_date(value)
    if parsed is None:
        raise ValueError(
            f"无法解析日期: {value!r}（示例: 2026-10-01 或 1st of October 2026）"
        )
    return parsed


def now_iso() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def human_bytes(value: int | float) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def sha256_file(path: Path | str, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def read_netscape_cookies(path: Path | str) -> str:
    """Convert a Netscape cookies.txt file into a Cookie header value."""

    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise FileNotFoundError(f"Cookie 文件不存在: {file_path}")
    pairs: list[str] = []
    for line in file_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        line = line.removeprefix("#HttpOnly_")
        fields = line.split("\t")
        if len(fields) >= 7:
            name, value = fields[5], fields[6]
        elif "=" in line:
            name, value = line.split("=", 1)
        else:
            continue
        name = name.strip()
        if name:
            pairs.append(f"{name}={value.strip()}")
    return "; ".join(pairs)


def redact_url(url: str) -> str:
    """Hide access tokens before printing URLs to the terminal / logs."""

    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    sensitive = {
        "token",
        "access_token",
        "accesstoken",
        "signature",
        "sig",
        "key",
        "auth",
    }
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        query.append((key, "***" if key.lower() in sensitive else value))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def display_path(path: Path | str, root: Path | str | None = None) -> str:
    path = Path(path)
    if root is not None:
        try:
            return str(path.resolve().relative_to(Path(root).resolve()))
        except ValueError:
            pass
    return str(path)
