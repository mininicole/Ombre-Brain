#!/usr/bin/env python3
"""Verify an isolated Evan Ombre restore without printing data content.

Adapted from Gale's verify_restored_backup.py for create_evan_backup.py output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path


TIMESTAMP_COLUMNS = {
    "updated_at",
    "created_at",
    "timestamp",
    "ts",
    "last_seen_at",
    "completed_at",
    "expires_at",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def verify_manifest(root: Path) -> dict[str, object]:
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "ombre-migration-backup-v2":
        raise RuntimeError("Unexpected backup manifest schema")
    expected_paths: set[str] = {"manifest.json"}
    total_bytes = 0
    for item in manifest.get("entries", []):
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("Unsafe manifest path")
        path = (root / relative).resolve(strict=True)
        if root not in path.parents:
            raise RuntimeError("Manifest path escaped restore root")
        if path.stat().st_size != item["bytes"]:
            raise RuntimeError(f"Size mismatch for {relative.as_posix()}")
        if sha256(path) != item["sha256"]:
            raise RuntimeError(f"Hash mismatch for {relative.as_posix()}")
        expected_paths.add(relative.as_posix())
        total_bytes += path.stat().st_size
    actual_paths = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }
    if actual_paths != expected_paths:
        raise RuntimeError("Restored file set differs from manifest")
    return {
        "schema": manifest["schema"],
        "files": len(actual_paths),
        "payload_bytes": total_bytes,
        "capture_started_at": manifest.get("capture_started_at"),
        "created_at": manifest.get("created_at"),
    }


def sqlite_metadata(data_root: Path) -> list[dict[str, object]]:
    databases: list[dict[str, object]] = []
    for path in sorted(p for p in data_root.rglob("*") if p.is_file() and p.open("rb").read(15) == b"SQLite format 3"):
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            if integrity != [("ok",)]:
                raise RuntimeError(f"SQLite integrity failure: {path.name}")
            table_names = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            tables: list[dict[str, object]] = []
            for table in table_names:
                quoted_table = quote_identifier(table)
                count = connection.execute(f"SELECT COUNT(*) FROM {quoted_table}").fetchone()[0]
                columns = [row[1] for row in connection.execute(f"PRAGMA table_info({quoted_table})")]
                latest: dict[str, object] = {}
                for column in sorted(set(columns) & TIMESTAMP_COLUMNS):
                    value = connection.execute(
                        f"SELECT MAX({quote_identifier(column)}) FROM {quoted_table}"
                    ).fetchone()[0]
                    if value is not None:
                        latest[column] = value
                tables.append({"name": table, "rows": count, "latest": latest})
            databases.append(
                {
                    "path": path.relative_to(data_root).as_posix(),
                    "bytes": path.stat().st_size,
                    "integrity": "ok",
                    "tables": tables,
                }
            )
        finally:
            connection.close()
    return databases


def assert_exclusions(root: Path) -> None:
    forbidden_names = {
        ".guardian_presence.json",
        ".migration-freeze-all",
        ".migration-read-only",
        "_migration_backups",
        "backups",
    }
    data = root / "data"
    if (data / "gale").exists():
        raise RuntimeError("Gale subtree must not be restored with Evan")
    for path in data.rglob("*"):
        relative = path.relative_to(data)
        if relative.parts[0] == ".claude" and relative.parts[:2] != (".claude", "projects") and path.is_file():
            raise RuntimeError(f"Unexpected .claude file: {relative.as_posix()}")
        if any(part in forbidden_names for part in relative.parts):
            raise RuntimeError(f"Excluded path present: {relative.as_posix()}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("restore_root")
    arguments = parser.parse_args()
    root = Path(arguments.restore_root).resolve(strict=True)
    if root == Path("/"):
        raise RuntimeError("Refusing filesystem-root verification")
    assert_exclusions(root)
    required_items = [
        "source/server.py",
        "source/start.sh",
        "source/requirements.txt",
        "source/handoff_store.py",
        "source/presence_bridge.py",
        "runtime_packages/night_fall/launcher.py",
        "data/embeddings.db",
        "data/chatnest/conversations.db",
        "data/letters.json",
    ]
    for required in required_items:
        if not (root / required).is_file():
            raise RuntimeError(f"Required restore item missing: {required}")
    result = {
        "status": "ok",
        "manifest": verify_manifest(root),
        "sqlite": sqlite_metadata(root / "data"),
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
