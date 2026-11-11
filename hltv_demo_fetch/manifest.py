"""Append-only manifest used to make repeated fetches idempotent."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .util import now_iso


def demo_key(source: str, match_id: str = "", demo_id: str = "") -> str:
    return f"{source}:{match_id or '-'}:{demo_id or '-'}"


@dataclass
class Manifest:
    out_dir: Path | str
    filename: str = "manifest.jsonl"
    failed_filename: str = "failed.jsonl"
    records: list[dict[str, Any]] = field(default_factory=list)
    keys: set[str] = field(default_factory=set)
    hashes: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.out_dir = Path(self.out_dir).expanduser()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.out_dir / self.filename
        self.failed_path = self.out_dir / self.failed_filename
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                self.records.append(record)
                key = record.get("key") or demo_key(
                    str(record.get("source", "")),
                    str(record.get("match_id", "")),
                    str(record.get("demo_id", "")),
                )
                self.keys.add(str(key))
                digest = record.get("sha256")
                if digest:
                    self.hashes.add(str(digest))

    def has_key(self, key: str) -> bool:
        return key in self.keys

    def has_hash(self, digest: str) -> bool:
        return digest in self.hashes

    def add(self, record: dict[str, Any]) -> None:
        record = dict(record)
        record.setdefault(
            "key",
            demo_key(
                str(record.get("source", "")),
                str(record.get("match_id", "")),
                str(record.get("demo_id", "")),
            ),
        )
        record.setdefault("recorded_at", now_iso())
        self.records.append(record)
        self.keys.add(str(record["key"]))
        if record.get("sha256"):
            self.hashes.add(str(record["sha256"]))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def add_failure(self, record: dict[str, Any]) -> None:
        record = dict(record)
        record.setdefault("recorded_at", now_iso())
        with self.failed_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def summary(self) -> dict[str, Any]:
        counts = Counter(
            str(record.get("source", "unknown")) for record in self.records
        )
        maps = Counter(
            str(record.get("map", "")) for record in self.records if record.get("map")
        )
        total_bytes = sum(int(record.get("bytes") or 0) for record in self.records)
        return {
            "demos": len(self.records),
            "bytes": total_bytes,
            "by_source": dict(sorted(counts.items())),
            "by_map": dict(maps.most_common(20)),
            "manifest": str(self.path),
        }

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.records[-limit:]
